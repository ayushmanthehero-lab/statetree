"""Local Qwen3.5-4B GPU launcher. Owns its child process, never remote GPUs.

Run ``python -m statetree.local_gpu --help``. Setup is explicit; start uses
already-installed assets. No fallback to paid APIs or silently CPU-only inference.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from statetree.gpu_download import default_cache

MODEL_ID = 'Qwen/Qwen3.5-4B'


def _integer(name, value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer between {low} and {high}')


@dataclass(frozen=True)
class GPUProfile:
    context_size: int = 4096
    max_tokens: int = 256
    gpu_layers: int | str = 'auto'
    reserve_mib: int = 768
    port: int = 8080
    web_port: int = 8765
    threads: int = 4
    timeout: int = 300
    device: str | None = None

    def __post_init__(self):
        for name, low, high in (('context_size', 2048, 32768), ('max_tokens', 32, 8192),
                               ('reserve_mib', 256, 8192), ('port', 1, 65535), ('web_port', 1, 65535),
                               ('threads', 1, 128), ('timeout', 1, 3600)):
            _integer(name, getattr(self, name), low, high)
        if self.port == self.web_port:
            raise ValueError('Model and workbench ports must be different')
        if self.max_tokens > self.context_size // 2:
            raise ValueError('max_tokens must leave at least half the context for input')
        if self.gpu_layers != 'auto':
            _integer('gpu_layers', self.gpu_layers, 1, 999)
        if self.device is not None and not re.fullmatch(r'CUDA\d+', self.device):
            raise ValueError('device must be one CUDA device, for example CUDA0')


def server_command(server: Path, model: Path, profile: GPUProfile) -> list[str]:
    p = profile
    command = [str(server), '--model', str(model), '--alias', MODEL_ID,
               '--host', '127.0.0.1', '--port', str(p.port), '--ctx-size', str(p.context_size),
               '--parallel', '1', '--gpu-layers', str(p.gpu_layers), '--fit', 'on',
               '--fit-target', str(p.reserve_mib), '--device', p.device or 'CUDA0',
               '--batch-size', '256', '--ubatch-size', '64', '--threads', str(p.threads),
               '--flash-attn', 'off', '--cache-type-k', 'f16', '--cache-type-v', 'f16',
               '--jinja', '--chat-template-kwargs', '{"enable_thinking":false}', '--no-mmproj', '--offline',
               # b11064 filters model-loader INFO at trace level (4), above its
               # default (3). wait_ready requires the actual offload report.
               '--log-verbosity', '4', '--log-colors', 'off']
    return command


def choose_cuda_device(output: str, requested: str | None = None) -> str:
    devices = dict(re.findall(r'^\s*(CUDA\d+):\s*(.+)$', output, re.MULTILINE))
    if requested:
        if requested not in devices:
            raise RuntimeError(f'Requested CUDA device {requested} was not enumerated by llama-server')
        return requested
    if not devices:
        raise RuntimeError('No CUDA device was enumerated. Use the CUDA build, matching DLLs and an NVIDIA driver.')
    return next((d for d, name in devices.items() if re.search(r'GTX\s*1650\b', name, re.I)), next(iter(devices)))


def offload_evidence(output: str) -> dict:
    matches = re.findall(r'offloaded\s+(\d+)\s*/\s*(\d+)\s+layers?\s+to\s+GPU', output, re.I)
    if not matches or int(matches[-1][0]) < 1:
        raise RuntimeError('No positive GPU layer-offload report. Refusing CPU-only or unverified GPU startup; inspect the server log.')
    n, total = map(int, matches[-1])
    return {'offloaded_layers': n, 'total_layers': total, 'gpu_used': True,
            'all_layers_offloaded': n == total}


def server_environment(secret: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('LLAMA_ARG_') and k.upper() != 'LLAMA_API_KEY'}
    env['LLAMA_API_KEY'] = secret
    return env


def validate_model_file(path: Path) -> Path:
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('Model must be a GGUF file')
    with path.open('rb') as stream:
        header = stream.read(24)
    if len(header) < 24 or header[:4] != b'GGUF' or int.from_bytes(header[4:8], 'little') not in (2, 3):
        raise ValueError('Not a valid GGUF header. A Git LFS pointer, HTML download or empty file is not a model.')
    return path


def require_free_port(port: int) -> None:
    _integer('port', port, 1, 65535)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            if os.name == 'nt':
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(('127.0.0.1', port))
        except OSError as error:
            raise RuntimeError(f'Local port {port} is busy or unavailable; stop its owner or select a different port. Nothing was killed.') from error


def configure_project(repo: Path, profile: GPUProfile):
    from statetree.cli import initialize_git
    from statetree.project import Project, atomic_json, validate_config
    model = {'model_id': MODEL_ID, 'url': f'http://127.0.0.1:{profile.port}/v1/chat/completions',
             'max_tokens': profile.max_tokens, 'context_window_limit': profile.context_size}
    budget = profile.context_size - profile.max_tokens - 256
    compact = {'context_budget': budget, 'note_budget': min(700, budget // 5),
               'fact_budget': min(600, budget // 5), 'recent_turns': 1, 'model': model}
    repo = Path(repo).absolute()
    if not (repo / '.statetree' / 'project' / 'project.json').exists():
        initialize_git(repo)
        return Project.init(repo, goal='Build and maintain StateTree', config=compact)
    project = Project(repo)
    if project.model_config != model:
        # Existing public handoff archives conversation instead of deleting evidence.
        project.switch_model(**model)
    with project._mutation():
        values = dict(project.config)
        for name, target in compact.items():
            # Keep an existing stricter operator limit instead of raising it.
            values[name] = target if name == 'model' else min(values[name], target)
        values = validate_config(values)
        if values != project.config:
            atomic_json(project.root / 'project.json', values)
        project.config = values
    return project


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Local model endpoints must not redirect')


def _get_json(port: int, route: str, key: str) -> dict:
    request = Request(f'http://127.0.0.1:{port}{route}', headers={'Authorization': 'Bearer ' + key})
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=2) as response:
        raw = response.read(1048577)
    if len(raw) > 1048576:
        raise ValueError('Model readiness response is too large')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('Invalid readiness response')
    return result


def wait_ready(process, port: int, key: str, log: Path, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'llama-server exited with code {process.returncode}; inspect {log}')
        try:
            health = _get_json(port, '/health', key)
            if health.get('status') == 'ok':
                models = _get_json(port, '/v1/models', key)
                if not any(m.get('id') == MODEL_ID for m in models.get('data', []) if isinstance(m, dict)):
                    raise RuntimeError(f'The local server is not advertising {MODEL_ID}; inspect {log}')
                # Log evidence is required even when HTTP is healthy.
                evidence = offload_evidence(log.read_text(encoding='utf-8', errors='replace'))
                # llama.cpp may leave /health public; /v1/models must require a key.
                try:
                    _get_json(port, '/v1/models', '')
                except HTTPError as error:
                    if error.code not in (401, 403):
                        raise
                else:
                    raise RuntimeError('The model server did not enforce its API key; refusing startup')
                return evidence
        except HTTPError as error:
            if error.code not in (503,):
                raise RuntimeError(f'Model readiness HTTP error {error.code}; inspect {log}') from error
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f'Model did not become ready within {timeout}s. Inspect {log}; reduce GPU layers or free VRAM.')


def stop_child(process) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def resolve_assets(cache: Path, server=None, model=None) -> tuple[Path, Path]:
    manifest = {}
    path = cache / 'installation.json'
    if path.exists():
        if path.stat().st_size > 16384:
            raise ValueError('GPU installation manifest is too large')
        manifest = json.loads(path.read_text(encoding='utf-8'))
    server = server or manifest.get('server')
    model = model or manifest.get('model')
    if not server or not model:
        raise RuntimeError('GPU assets are not configured. Run scripts/Setup-GTX1650.ps1, or supply --server and --model.')
    server = Path(server).expanduser().resolve(strict=True)
    if not server.is_file():
        raise ValueError('llama-server executable does not exist')
    return server, validate_model_file(Path(model))


def inspect_backend(server: Path, requested: str | None = None) -> str:
    try:
        result = subprocess.run([str(server), '--list-devices'], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=30, env=server_environment(''), cwd=server.parent)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('Cannot run llama-server. Check CUDA DLLs, NVIDIA driver and the Windows x64 Visual C++ runtime.') from error
    if result.returncode != 0:
        raise RuntimeError(f'llama-server --list-devices failed ({result.returncode}): {(result.stdout + result.stderr)[-3000:]}')
    return choose_cuda_device(result.stdout + '\n' + result.stderr, requested)


def launch(repo: Path, cache: Path, profile: GPUProfile, *, server=None, model=None) -> None:
    if importlib.util.find_spec('strands') is None:
        raise RuntimeError('Strands is missing: use the project .venv after Setup-GTX1650.ps1, or run python -m pip install -e .')
    server, model = resolve_assets(cache, server, model)
    require_free_port(profile.port)
    require_free_port(profile.web_port)
    profile = replace(profile, device=inspect_backend(server, profile.device))
    logdir = cache / 'logs'
    logdir.mkdir(parents=True, exist_ok=True)
    log = logdir / f'llama-{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(3)}.log'
    key = secrets.token_urlsafe(32)
    env = server_environment(key)
    command = server_command(server, model, profile)
    process = None
    before = {name: os.environ.get(name) for name in ('STATETREE_LOCAL_API_KEY', 'STATETREE_LOCAL_TIMEOUT')}
    try:
        with log.open('wb') as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, cwd=server.parent, env=env)
            print(f'Loading Qwen on {profile.device}. Server log: {log}', flush=True)
            evidence = wait_ready(process, profile.port, key, log, profile.timeout)
            print(f'GPU offload confirmed: {evidence["offloaded_layers"]}/{evidence["total_layers"]} layers. '
                  'Other buffers/layers may use system RAM.', flush=True)
            project = configure_project(repo, profile)
            os.environ['STATETREE_LOCAL_API_KEY'] = key
            os.environ['STATETREE_LOCAL_TIMEOUT'] = str(profile.timeout)
            print(f'Project: {project.repo}\nContext: {profile.context_size}; output limit: {profile.max_tokens}. '
                  'Press Ctrl+C here to stop BOTH services.', flush=True)
            from statetree.web.workbench import serve
            serve(project.repo, port=profile.web_port)
    finally:
        stop_child(process)
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('setup', 'doctor', 'start'):
        p = sub.add_parser(name)
        p.add_argument('--cache', type=Path, default=default_cache())
        p.add_argument('--server', type=Path)
        p.add_argument('--model', type=Path)
        if name in ('doctor', 'start'):
            p.add_argument('--device')
        if name == 'start':
            p.add_argument('--repo', type=Path, default=Path.cwd())
            p.add_argument('--context-size', type=int, default=4096)
            p.add_argument('--max-tokens', type=int, default=256)
            p.add_argument('--gpu-layers', default='auto')
            p.add_argument('--reserve-mib', type=int, default=768)
            p.add_argument('--port', type=int, default=8080)
            p.add_argument('--web-port', type=int, default=8765)
            p.add_argument('--threads', type=int, default=4)
            p.add_argument('--timeout', type=int, default=300)
            p.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'setup':
            from statetree.gpu_download import install
            result = install(args.cache, server=args.server, model=args.model)
            print(json.dumps(result, indent=2))
            print('Setup saved. Start performs the CUDA device and actual offload checks; setup does not prove inference.')
        elif args.command == 'doctor':
            server, model = resolve_assets(args.cache, args.server, args.model)
            device = inspect_backend(server, args.device)
            print(json.dumps({'server': str(server), 'model': str(model), 'cuda_device': device,
                              'strands_installed': importlib.util.find_spec('strands') is not None,
                              'model_loaded': False, 'inference_tested': False}, indent=2))
        else:
            layers = int(args.gpu_layers) if args.gpu_layers.isdecimal() else args.gpu_layers
            profile = GPUProfile(**{key: getattr(args, key) for key in ('context_size', 'max_tokens', 'reserve_mib',
                                    'port', 'web_port', 'threads', 'timeout', 'device')}, gpu_layers=layers)
            if args.dry_run:
                print(json.dumps({'command': server_command(args.server or Path('llama-server.exe'),
                                args.model or Path('Qwen3.5-4B-Q4_K_M.gguf'), profile),
                                'gpu_detected': False, 'dry_run': True, 'downloads': False}, indent=2))
            else:
                launch(args.repo, args.cache, profile, server=args.server, model=args.model)
        return 0
    except KeyboardInterrupt:
        print('Stopped; owned model process was cleaned up.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f'GPU setup/start error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
