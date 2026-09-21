"""JSON-first local CLI. Nothing connects to a model unless ``ask`` is called."""
import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from statetree.project import Project, _safe_root
from statetree.storage.portable import read_bundle, write_bundle


def read_json(path, *, limit=32 * 1024 * 1024):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('JSON file exceeds the size limit')
    return json.loads(raw)


def initialize_git(path):
    """Explicit --git bootstrap; never stage the caller's source or credentials."""
    path, _ = _safe_root(path)
    path.mkdir(parents=True, exist_ok=True)
    if not (path / '.git').exists():
        subprocess.run(['git', 'init', '-q', str(path)], check=True, capture_output=True)
    root = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=path,
                          check=True, capture_output=True, text=True).stdout.strip()
    if Path(root).resolve() != path:
        raise ValueError('Select the repository root, not a child directory')
    ignored = path / '.gitignore'
    if ignored.is_symlink():
        raise ValueError('.gitignore cannot be a filesystem link')
    text = ignored.read_text(encoding='utf-8') if ignored.exists() else ''
    patterns = ['.statetree/', '.venv/', '__pycache__/', '.env', '.env.*', '!.env.example']
    missing = [item for item in patterns if item not in text.splitlines()]
    if missing:
        ignored.write_text(text + ('\n' if text and not text.endswith('\n') else '') + '\n'.join(missing) + '\n', encoding='utf-8')
    has_head = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=path, capture_output=True).returncode == 0
    if not has_head:
        # Empty tree, even if a caller had already staged files. --only prevents
        # an accidental initial commit of the caller's staged source.
        subprocess.run(['git', '-c', 'user.name=StateTree', '-c', 'user.email=local@statetree.invalid',
                        '-c', 'core.hooksPath=/dev/null' if os.name != 'nt' else 'core.hooksPath=NUL',
                        '-c', 'commit.gpgSign=false', 'commit', '--allow-empty', '--only',
                        '-m', 'Initialize local StateTree workspace'], cwd=path, check=True, capture_output=True)


def parser():
    result = argparse.ArgumentParser(prog='statetree', description=__doc__)
    result.add_argument('--repo', default='.', help='Git repository root (before the command)')
    sub = result.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='Initialize checkpointed public project state')
    init.add_argument('--goal', required=True)
    init.add_argument('--git', action='store_true', help='Initialize Git with an empty baseline if needed')
    init.add_argument('--config', help='Operator configuration JSON; trusted argv checks only')
    for name in ('status', 'facts', 'compact', 'doctor'):
        sub.add_parser(name)
    history = sub.add_parser('history')
    history.add_argument('--limit', type=int, default=50)
    history.add_argument('--cursor')
    checkpoint = sub.add_parser('checkpoint')
    checkpoint.add_argument('message')
    checkpoint.add_argument('--verify', action='store_true')
    restore = sub.add_parser('restore')
    restore.add_argument('checkpoint_id')
    restore.add_argument('--yes', action='store_true')
    remember = sub.add_parser('remember')
    remember.add_argument('key')
    remember.add_argument('value', help='JSON value, e.g. 7, true, or a quoted JSON string')
    remember.add_argument('--evidence', action='append', required=True)
    remember.add_argument('--dependencies', default='{}', help='JSON map of fact keys to record IDs')
    forget = sub.add_parser('forget')
    forget.add_argument('key')
    forget.add_argument('--action', choices=['archive', 'invalidate', 'delete'], default='archive')
    recall = sub.add_parser('recall')
    recall.add_argument('query')
    recall.add_argument('--budget', type=int)
    context = sub.add_parser('context')
    context.add_argument('query')
    context.add_argument('--new-task', action='store_true')
    archive = sub.add_parser('archive')
    archive.add_argument('archive_id')
    archive.add_argument('--offset', type=int, default=0)
    archive.add_argument('--limit', type=int, default=1000)
    for name in ('export', 'import'):
        sub.add_parser(name).add_argument('path')
    model = sub.add_parser('model')
    model.add_argument('model_id')
    model.add_argument('--url', required=True, help='Loopback HTTP /v1/chat/completions URL')
    model.add_argument('--max-tokens', type=int)
    model.add_argument('--context-window-limit', type=int)
    usage = sub.add_parser('usage')
    usage.add_argument('--run-id')
    usage.add_argument('--branch')
    usage.add_argument('--phase')
    ask = sub.add_parser('ask')
    ask.add_argument('prompt')
    ask.add_argument('--new-task', action='store_true')
    branch = sub.add_parser('branch').add_subparsers(dest='branch_command', required=True)
    branch.add_parser('list')
    fork = branch.add_parser('fork')
    fork.add_argument('name')
    fork.add_argument('--reads', help='JSON resource key/version mapping')
    fork.add_argument('--writes', help='JSON resource keys list')
    complete = branch.add_parser('complete')
    complete.add_argument('name')
    complete.add_argument('--state', help='Complete ASP state JSON file; defaults to persisted branch state')
    complete.add_argument('--result', help='Optional JSON result')
    merge = branch.add_parser('merge')
    merge.add_argument('candidate_id')
    merge.add_argument('--expected-parent')
    adopt = branch.add_parser('adopt')
    adopt.add_argument('revision_id')
    adopt.add_argument('--yes', action='store_true')
    demo = sub.add_parser('demo')
    demo.add_argument('--directory', required=True, help='New disposable folder (must not exist)')
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int, default=8765)
    return result


def execute(args):
    if args.command == 'doctor':
        installed = importlib.util.find_spec('strands') is not None
        try:
            version = importlib.metadata.version('strands-agents')
        except importlib.metadata.PackageNotFoundError:
            version = None
        return {'python': sys.version.split()[0], 'git_available': shutil.which('git') is not None,
                'strands_available': installed, 'strands_version': version,
                'initialized': (Path(args.repo) / '.statetree/project/project.json').is_file(),
                'provider_called': False}
    if args.command == 'demo':
        from statetree.demo import run_demo
        return run_demo(args.directory)
    if args.command == 'init':
        if args.git:
            initialize_git(args.repo)
        return Project.init(args.repo, goal=args.goal, config=read_json(args.config, limit=65536) if args.config else None).status()
    project = Project(args.repo)
    command = args.command
    if command == 'status':
        return project.status()
    if command == 'history':
        return project.history(limit=args.limit, cursor=args.cursor)
    if command == 'checkpoint':
        return project.checkpoint(args.message, verify=args.verify)
    if command == 'restore':
        if not args.yes:
            raise ValueError('Restore changes project state and captured files; pass --yes to confirm')
        return project.restore(args.checkpoint_id)
    if command == 'remember':
        return project.remember(args.key, json.loads(args.value), evidence=args.evidence, dependencies=json.loads(args.dependencies))
    if command == 'facts':
        return project.facts()
    if command == 'forget':
        return project.forget(args.key, action=args.action)
    if command == 'recall':
        return project.recall(args.query, budget=args.budget)
    if command == 'context':
        return project.context(args.query, new_task=args.new_task)
    if command == 'compact':
        return project.compact()
    if command == 'archive':
        return project.read_archive(args.archive_id, offset=args.offset, limit=args.limit)
    if command == 'export':
        bundle = project.export_state()
        # Validate the bundle before atomically replacing the selected file.
        write_bundle(args.path, bundle)
        return {'path': str(Path(args.path).resolve()), 'digest': bundle['digest']}
    if command == 'import':
        return project.import_state(read_bundle(args.path))
    if command == 'model':
        return project.switch_model(args.model_id, args.url, max_tokens=args.max_tokens, context_window_limit=args.context_window_limit)
    if command == 'usage':
        return project.usage(**{key: getattr(args, key) for key in ('run_id', 'branch', 'phase') if getattr(args, key) is not None})
    if command == 'ask':
        return project.ask(args.prompt, new_task=args.new_task)
    if command == 'branch':
        if args.branch_command == 'list':
            return project.branches()
        if args.branch_command == 'fork':
            return project.fork(args.name, reads=json.loads(args.reads) if args.reads else None,
                                writes=json.loads(args.writes) if args.writes else None)
        if args.branch_command == 'complete':
            return project.complete_branch(args.name, state=read_json(args.state) if args.state else None,
                                           result=json.loads(args.result) if args.result else None)
        if args.branch_command == 'merge':
            return project.merge(args.candidate_id, expected_parent=args.expected_parent)
        if args.branch_command == 'adopt':
            if not args.yes:
                raise ValueError('Adoption changes captured project files; pass --yes to confirm')
            return project.adopt(args.revision_id)
    if command == 'serve':
        from statetree.web.workbench import serve
        serve(project.repo, port=args.port)
        return {'stopped': True}
    raise ValueError('Unknown command')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        output = execute(args)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({'error': 'Interrupted; inspect the last durable checkpoint before resuming'}), file=sys.stderr)
        return 130
    except Exception as error:
        # Avoid stack dumps containing prompts, credentials or provider payloads.
        print(json.dumps({'error': str(error), 'type': type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
