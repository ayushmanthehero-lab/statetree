"""Opt-in, pinned local GPU assets. Nothing downloads on import or startup.

The executable and CUDA runtime are upstream llama.cpp release artifacts.
The GGUF is Unsloth's quantization of Qwen3.5-4B. See docs/GTX1650_SETUP.md.
"""
from __future__ import annotations

import hashlib
import json
import os
from http.client import IncompleteRead
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import math
import re
import shutil
import stat
import ssl
import tempfile
import time
from urllib.error import ContentTooShortError, HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import ZipFile

RELEASE = 'b11064'
MODEL_REVISION = 'e87f176479d0855a907a41277aca2f8ee7a09523'
ASSETS = [
    {'name': 'llama-b11064-bin-win-cuda-12.4-x64.zip',
     'url': 'https://github.com/ggml-org/llama.cpp/releases/download/b11064/llama-b11064-bin-win-cuda-12.4-x64.zip',
     'sha256': '6996aba065ea701f58533508f8475e02dc376caea330b7a4e009ca0b078bc8f3'},
    {'name': 'cudart-llama-bin-win-cuda-12.4-x64.zip',
     'url': 'https://github.com/ggml-org/llama.cpp/releases/download/b11064/cudart-llama-bin-win-cuda-12.4-x64.zip',
     'sha256': '8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6'},
    {'name': 'Qwen3.5-4B-Q4_K_M.gguf',
     'url': 'https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/e87f176479d0855a907a41277aca2f8ee7a09523/Qwen3.5-4B-Q4_K_M.gguf',
     'sha256': '00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4'},
]


def default_cache() -> Path:
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'StateTree' / 'gtx1650'
    return Path.home() / '.cache' / 'statetree' / 'gtx1650'


def sha256_file(path: Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _no_links(path: Path) -> None:
    for p in (path, *path.parents):
        if p.is_symlink() or getattr(p, 'is_junction', lambda: False)():
            raise ValueError(f'Asset paths must not traverse filesystem links: {p}')


def _receive_download(url: str, partial: Path, offset: int, timeout: float) -> None:
    """One HTTP attempt. Only validated ranges may append to a partial file."""
    headers = {'User-Agent': 'StateTree-GPU-Setup/2', 'Accept-Encoding': 'identity',
               'Cache-Control': 'no-cache'}
    if offset:
        headers['Range'] = f'bytes={offset}-'
    # Open the original pinned URL on every attempt, not an old signed redirect.
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        status = getattr(response, 'status', 200)
        if status not in (200, 206):
            raise RuntimeError(f'Unexpected asset HTTP status {status}')
        length = response.headers.get('Content-Length')
        if length is not None and not re.fullmatch(r'[0-9]+', length):
            raise ValueError('Server returned an invalid Content-Length')
        length = int(length) if length is not None else None
        if status == 206:
            match = re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)',
                                 response.headers.get('Content-Range', ''))
            if not offset or match is None:
                raise ValueError('Server returned an unexpected download range')
            start, end = int(match[1]), int(match[2])
            total = None if match[3] == '*' else int(match[3])
            if (start != offset or end < start or (total is not None and end >= total)
                    or (length is not None and length != end - start + 1)):
                raise ValueError('Server returned an unexpected download range')
            length, mode = end - start + 1, 'ab'
        else:
            if offset:
                print('  Server did not accept resume; restarting this partial file safely.', flush=True)
            mode, offset, total = 'wb', 0, length
        done, reported, report_time = offset, offset, time.monotonic()

        def progress() -> None:
            suffix = f' / {total / 1024**2:.1f} MiB ({100 * done / total:.1f}%)' if total else ' MiB'
            print(f'  {partial.name.removesuffix(".part")}: {done / 1024**2:.1f}{suffix}', flush=True)

        progress()
        with partial.open(mode) as stream:
            while True:
                try:
                    chunk = response.read(1024 * 1024)
                except IncompleteRead as error:
                    # http.client may return final received bytes on the exception.
                    stream.write(error.partial)
                    raise
                if not chunk:
                    break
                stream.write(chunk)
                done += len(chunk)
                if length is not None and done - offset > length:
                    raise ValueError('Server sent more bytes than its declared download range')
                if done - reported >= 16 * 1024 * 1024 or time.monotonic() - report_time >= 5:
                    progress()
                    reported, report_time = done, time.monotonic()
            stream.flush()
            os.fsync(stream.fileno())
        progress()
        if ((length is not None and done - offset < length)
                or (total is not None and done < total)):
            # Preserve a short response; a clean EOF is not necessarily completion.
            raise ContentTooShortError('Connection ended before the complete asset arrived', None)


def download_verified(url: str, destination: Path, expected_sha256: str, *,
                      max_attempts: int = 10, timeout: float = 30) -> Path:
    """Retry/resume interrupted HTTPS downloads; publish only after SHA-256 passes.

    Verified cache files are reused. Network failures retain .part files across
    invocations. Certificate, integrity, permission and disk errors are not hidden
    as transient connection failures. Never run two installers for one cache.
    """
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 50:
        raise ValueError('max_attempts must be an integer between 1 and 50')
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('timeout must be a positive finite number')
    destination = Path(destination).absolute()
    _no_links(destination)
    if not url.startswith('https://'):
        raise ValueError('Asset downloads require HTTPS')
    if destination.exists():
        print(f'Checking cached file: {destination.name}', flush=True)
        if sha256_file(destination) == expected_sha256:
            print(f'Already verified: {destination.name}', flush=True)
            return destination
        raise ValueError(f'SHA-256 mismatch in cached {destination}; remove this corrupt file and retry setup')
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.part')
    _no_links(partial)
    if partial.exists() and partial.stat().st_size:
        print(f'Checking saved partial: {partial.name}', flush=True)
        if sha256_file(partial) == expected_sha256:
            os.replace(partial, destination)
            print(f'SHA-256 verified: {destination.name}', flush=True)
            return destination
    for attempt in range(1, max_attempts + 1):
        offset = partial.stat().st_size if partial.exists() else 0
        resume = f'; resume at {offset:,} bytes' if offset else ''
        print(f'Downloading {destination.name} (attempt {attempt}/{max_attempts}{resume})', flush=True)
        print(f'  Connecting; network operation timeout: {timeout:g} seconds.', flush=True)
        try:
            _receive_download(url, partial, offset, timeout)
        except (URLError, ConnectionError, TimeoutError, IncompleteRead, ssl.SSLEOFError) as error:
            if isinstance(error, URLError) and isinstance(error.reason, ssl.SSLCertVerificationError):
                raise
            if isinstance(error, HTTPError):
                if error.code == 416 and offset:
                    error.close()
                    # A prior attempt may have finished writing before losing its
                    # response. Recover that case without redownloading the model.
                    if sha256_file(partial) == expected_sha256:
                        os.replace(partial, destination)
                        print(f'SHA-256 verified: {destination.name}', flush=True)
                        return destination
                    partial.unlink()
                    print('  Saved range is no longer usable; restarting this partial file.', flush=True)
                elif error.code not in (408, 429, 500, 502, 503, 504):
                    raise
                else:
                    error.close()
            kept = partial.stat().st_size if partial.exists() else 0
            print(f'  Download interrupted: {error}. Saved {kept:,} bytes for resume.', flush=True)
            if attempt == max_attempts:
                raise RuntimeError(
                    f'Download interrupted after {max_attempts} attempts for {destination.name}. '
                    f'Rerun setup to resume; partial file: {partial}. '
                    'Already verified assets will not be downloaded again.'
                ) from error
            delay = min(2 ** min(attempt, 5), 30)
            print(f'  Retrying in {delay} seconds...', flush=True)
            time.sleep(delay)
            continue
        print(f'Checking SHA-256: {destination.name}', flush=True)
        if sha256_file(partial) != expected_sha256:
            partial.unlink(missing_ok=True)
            raise ValueError(f'SHA-256 mismatch for {destination.name}; the download was not installed')
        os.replace(partial, destination)
        print(f'SHA-256 verified: {destination.name}', flush=True)
        return destination
    raise AssertionError('Unreachable download retry state')


def safe_extract(archive: Path, destination: Path) -> None:
    """Validate every entry before any extraction (including Windows paths)."""
    destination = Path(destination).absolute()
    _no_links(destination)
    with ZipFile(archive) as source:
        entries, names = [], set()
        if sum(i.file_size for i in source.infolist()) > 12 * 1024**3:
            raise ValueError('Archive exceeds the extraction limit')
        for item in source.infolist():
            name = item.filename.replace('\\', '/')
            path = PurePosixPath(name)
            winpath = PureWindowsPath(name)
            key = name.rstrip('/').casefold()
            if (not name or path.is_absolute() or winpath.drive or '..' in path.parts
                    or any(':' in part or part.endswith((' ', '.')) for part in path.parts)
                    or stat.S_ISLNK(item.external_attr >> 16) or key in names):
                raise ValueError(f'Unsafe archive entry: {item.filename}')
            target = destination.joinpath(*path.parts)
            _no_links(target)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('Archive path escapes the destination')
            names.add(key)
            entries.append((item, target))
        for item, target in entries:
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)


def _publish_manifest(cache: Path, server: Path, model: Path, *, custom_server=False, custom_model=False) -> dict:
    manifest = {'schema_version': 1, 'server': str(server.resolve()), 'model': str(model.resolve()),
                'binary_release': 'custom' if custom_server else RELEASE,
                'model_revision': 'custom' if custom_model else MODEL_REVISION}
    cache.mkdir(parents=True, exist_ok=True)
    temp = cache / '.installation.json.pending'
    _no_links(temp)
    temp.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, cache / 'installation.json')
    return manifest


def install(cache: Path | None = None, *, server: Path | None = None, model: Path | None = None) -> dict:
    """Install Windows CUDA binaries and/or weights, only on explicit setup."""
    from statetree.local_gpu import validate_model_file
    custom_server, custom_model = server is not None, model is not None
    cache = Path(cache or default_cache()).absolute()
    _no_links(cache)
    if server is None and (os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64')):
        raise RuntimeError('Automatic CUDA binaries are for Windows x64; supply --server for your platform')
    if server is not None:
        server = Path(server).resolve(strict=True)
        if not server.is_file():
            raise ValueError('--server must be the llama-server executable')
    if model is not None:
        model = validate_model_file(model)
    cache.mkdir(parents=True, exist_ok=True)
    if server is None:
        archives = [download_verified(a['url'], cache / 'downloads' / a['name'], a['sha256']) for a in ASSETS[:2]]
        target = cache / RELEASE
        _no_links(target)
        with tempfile.TemporaryDirectory(prefix='extract-', dir=cache) as tmp:
            stage = Path(tmp)
            for i, archive in enumerate(archives):
                safe_extract(archive, stage / str(i))
            found = list((stage / '0').rglob('llama-server.exe'))
            if len(found) != 1:
                raise ValueError('Expected exactly one llama-server.exe in the pinned release')
            exe = found[0]
            # Co-locate DLLs even when the CUDA runtime ZIP has another root.
            for dll in stage.rglob('*.dll'):
                dst = exe.parent / dll.name
                if dst == dll:
                    continue
                if dst.exists() and sha256_file(dst) != sha256_file(dll):
                    raise ValueError(f'Conflicting runtime DLL: {dll.name}')
                if not dst.exists():
                    shutil.copy2(dll, dst)
            if not (exe.parent / 'ggml-cuda.dll').exists():
                raise ValueError('The installed release is missing ggml-cuda.dll')
            if target.exists():
                shutil.rmtree(target)  # Only this versioned installer-owned binary directory.
            shutil.copytree(exe.parent, target)
        server = target / 'llama-server.exe'
    if model is None:
        a = ASSETS[-1]
        model = download_verified(a['url'], cache / 'models' / a['name'], a['sha256'])
        validate_model_file(model)
    return _publish_manifest(cache, server, model, custom_server=custom_server, custom_model=custom_model)
