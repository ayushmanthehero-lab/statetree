"""Checkpointed Strands tools in a private, disposable project copy.

The journal records intentions before edits/commands and receipts before events.
Uncertain commands require operator reconciliation; they are never replayed.
"""

import copy
from contextlib import nullcontext
from datetime import datetime, timezone
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from uuid import uuid4

from strands import Agent, tool
from strands.tools.executors import SequentialToolExecutor

from statetree.memory.commits import CommitNote
from statetree.runtime.runtime import StateTreeRuntime
from statetree.runtime.usage import UsageLedger
from statetree.workspace.git import GitWorkspace
from .files import (MAX_FILE, MAX_FILES, MAX_PROJECT, ProjectBusy, SafeFiles, UnsafePath,
                    digest, project_lock, safe_name)
from .journal import Journal, encode


SYSTEM = ('You are a local project coding assistant. Inspect files, make small exact edits and run relevant checks. '
          'Work only through supplied tools. Commands run in a networkless disposable Docker container. '
          'Never claim a check passed without its recorded output. Tool receipts are durable: do not repeat '
          'confirmed effects. Request clarification if blocked. Finish with a concise factual result. '
          'A finished patch is reviewed and applied separately; it has not changed the original project. '
          'Use read_file before edit_file. Keep each tool call short, under 100 output tokens; make small patches. '
          'Avoid secrets. Delegate at most one bounded read-only review.')
ID = re.compile(r'[0-9a-f]{32}\Z')
MAX_MODEL_STEPS = 24
MAX_DIFF = 64000


def now():
    return datetime.now(timezone.utc).isoformat()


def short(value, limit):
    return str(value).encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def git_environment(environment=None):
    source = os.environ if environment is None else environment
    safe = {key: value for key, value in source.items() if not key.startswith('GIT_')}
    safe.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    if environment is not None:
        for key in ('GIT_INDEX_FILE', 'GIT_OPTIONAL_LOCKS'):
            if key in environment:
                safe[key] = environment[key]
    return safe


def git(repo, *arguments):
    result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
                             '-C', str(repo), *arguments], capture_output=True, timeout=30, env=git_environment())
    if result.returncode:
        raise ValueError('Git project operation failed; verify the selected local repository.')
    return result.stdout.decode('utf-8').rstrip('\n')


class TaskGitWorkspace(GitWorkspace):
    """Project attributes cannot activate operator-global filters or hooks."""
    def _git(self, *arguments, env=None, input=None):
        return super()._git('-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
                            *arguments, env=git_environment(env), input=input)


class LocalTaskRunner:
    def __init__(self, project_path, state_root, model_factory, event_sink=None, cancel_requested=None,
                 command_image='python:3.13-slim'):
        self.project = Path(project_path).absolute()
        self.state_root = Path(state_root).absolute()
        self.model_factory = model_factory
        self.event_sink = event_sink or (lambda event: None)
        self.cancel_requested = cancel_requested or (lambda: False)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/@:-]{0,199}', command_image):
            raise ValueError('Invalid locally selected command image.')
        self.command_image = command_image
        self.docker = shutil.which('docker') or next((str(path) for path in (
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/DockerDesktop/resources/bin/docker.exe',
            Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Docker/Docker/resources/bin/docker.exe'
        ) if path.is_file()), 'docker')
        self.project_key = digest(os.path.normcase(str(self.project)).encode())[:32]
        self._mutex = threading.RLock()
        self._blocked = False
        self._runtime = None
        self._journal = None

    def _fence(self):
        return Journal(self.state_root / ('project-' + self.project_key + '.sqlite3'))

    def _cancelled(self):
        try:
            return bool(self.cancel_requested())
        except Exception:
            return True

    def _emit(self, kind, text, details=None):
        event = {'id': uuid4().hex, 'type': kind, 'text': str(text)[:4000],
                 'details': details or {}, 'created_at': now()}
        self._journal.event(event)
        try:
            self.event_sink(copy.deepcopy(event))
        except Exception:
            pass  # Durable event remains available for idempotent redelivery.

    def _paths(self, task_id):
        if not isinstance(task_id, str) or not ID.fullmatch(task_id):
            raise ValueError('Invalid task identifier.')
        self.task_id = task_id
        self.task_root = self.state_root / 'tasks' / task_id
        self.repo = self.task_root / 'repo'
        self.files_root = self.repo / 'files'

    def _snapshot_project(self, task):
        source = SafeFiles(self.project)
        if Path(git(self.project, 'rev-parse', '--show-toplevel')).resolve() != self.project.resolve():
            raise ValueError('Select the root of a Git project.')
        paths = sorted(set(git(self.project, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\x00')) - {''})
        inherited_base = None
        parent_id = task.get('parent_task_id')
        if parent_id:
            if not isinstance(parent_id, str) or not ID.fullmatch(parent_id) or parent_id == task['id']:
                raise ValueError('Invalid parent task identifier.')
            parent_root = self.state_root / 'tasks' / parent_id
            if not (parent_root / 'journal.sqlite3').is_file():
                raise ValueError('The parent task is unavailable on this PC.')
            parent = Journal(parent_root / 'journal.sqlite3')
            parent_task, parent_result = parent.get('task', {}), parent.get('result', {})
            if parent_task.get('project') != str(self.project):
                raise ValueError('The parent task belongs to another project.')
            if parent_result.get('status') not in ('completed', 'applied', 'conflict'):
                raise ValueError('The parent must finish before starting a follow-up task.')
            if any(step['receipt'] is None for step in parent.steps()):
                raise ValueError('The parent has uncertain effects; reconcile them before copying its files.')
            if parent_result['status'] != 'applied':
                source = SafeFiles(parent_root / 'repo' / 'files')
                paths = source.names()
                inherited_base = SafeFiles(parent_root / 'base')
            self._journal.set('parent_evidence', {'parent_task_id': parent_id,
                'prompt': parent_task.get('prompt'), 'result': parent_result.get('result'),
                'checkpoint': parent_result.get('checkpoint'), 'receipts': parent.steps()})
        accepted = []
        for name in paths:
            try:
                safe_name(name)
            except UnsafePath:
                continue
            candidate = self.project / name
            if candidate == self.state_root or self.state_root in candidate.parents:
                continue
            accepted.append(name)
        if len(accepted) > MAX_FILES:
            raise ValueError('Project exceeds the 2000-file limit.')
        self.files_root.mkdir(parents=True, exist_ok=True)
        base_root = self.task_root / 'base'
        base_root.mkdir(parents=True, exist_ok=True)
        files, base = SafeFiles(self.files_root), SafeFiles(base_root)
        manifest, size = {}, 0
        for name in accepted:
            data = source.maybe_read(name)
            if data is None:  # Respect deleted files in a dirty checkout.
                continue
            size += len(data)
            if size > MAX_PROJECT:
                raise ValueError('Project exceeds the 32 MiB snapshot limit.')
            files.write(name, data, None)
            if inherited_base is None:
                base.write(name, data, None)
                manifest[name] = digest(data)
        if inherited_base is not None:
            for name in inherited_base.names():
                data = inherited_base.read(name)
                base.write(name, data, None)
                manifest[name] = digest(data)
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.name', 'StateTree local worker')
        git(self.repo, 'config', 'user.email', 'worker@statetree.invalid')
        git(self.repo, 'config', 'core.autocrlf', 'false')
        git(self.repo, 'add', '-f', '-A', '--', 'files')
        git(self.repo, 'commit', '--allow-empty', '-qm', 'Private task starting state')
        self._journal.set('base', manifest)
        self._journal.set('task', {'id': task['id'], 'prompt': task['prompt'],
                                   'project': str(self.project), 'parent_task_id': parent_id, 'created_at': now()})

    def _open_task(self, task):
        self._paths(task['id'])
        self._journal = Journal(self.task_root / 'journal.sqlite3')
        old = self._journal.get('task')
        if old:
            if (old['project'] != str(self.project)
                    or ' '.join(old['prompt'].split()) != ' '.join(task['prompt'].split())):
                self._journal = None  # Do not overwrite the rightful task's result.
                raise ValueError('Task identity conflict: this ID belongs to a different project or prompt.')
        else:
            # A crash before the starting snapshot commits must not reuse a partial copy.
            if self.files_root.exists():
                raise ValueError('Incomplete initial snapshot. Start a fresh task; the partial copy was retained.')
            self._snapshot_project(task)
        self.task = self._journal.get('task')
        self.files = SafeFiles(self.files_root)
        for event in self._journal.events():
            try:
                self.event_sink(event)
            except Exception:
                break

    def _reconcile(self):
        for step in self._journal.steps():
            if step['receipt'] is not None:
                continue
            intent = step['intent']
            if step['kind'] == 'command':
                # A dead worker can leave a live Docker container; stop it before
                # reading task files. Its outcome remains uncertain even if stopped.
                try:
                    stopped = subprocess.run([self.docker, 'rm', '-f', intent['container']],
                                             capture_output=True, timeout=15)
                    if stopped.returncode and b'No such container' not in stopped.stderr:
                        raise OSError('Container stop was not confirmed')
                except Exception:
                    self._blocked = True
                    self._fence().set('blocked_task', self.task_id)
                    return 'An interrupted command may still be running. Restore Docker and reconcile its container before resuming.'
                self._fence().set('blocked_task', None)
                self._blocked = True
                return 'An interrupted command has an uncertain outcome. Its container is stopped; inspect the retained task workspace before starting a fresh task. It was not replayed.'
            if step['kind'] == 'inference':
                ledger = UsageLedger(self.task_root / 'memory' / 'usage.sqlite3')
                if ledger.summary()['requests'] <= intent['requests_before']:
                    ledger.record('interrupted-' + step['id'], run_id=self.task_id, branch='main',
                                  phase='interrupted', usage=None, status='interrupted')
                self._journal.finish(step['id'], {'status': 'interrupted',
                    'text': 'A model request was interrupted; unavailable usage remains unknown.'})
            elif step['kind'] in ('edit', 'write'):
                current = self.files.maybe_read(intent['path'])
                current_hash = digest(current) if current is not None else None
                if current_hash == intent['after']:
                    receipt = {'status': 'ok', 'path': intent['path'], 'sha256': current_hash,
                               'reconciled': True, 'text': 'Confirmed completed file edit from its content hash.'}
                elif current_hash == intent['before']:
                    receipt = {'status': 'error', 'not_dispatched': True,
                               'text': 'Interrupted before the edit completed; original content remains. Read and issue a new edit.'}
                else:
                    self._blocked = True
                    return 'An interrupted file edit needs reconciliation; its content matches neither the recorded before nor after hash.'
                self._journal.finish(step['id'], receipt)
            else:
                self._journal.finish(step['id'], {'status': 'error', 'text': 'The earlier read/review was interrupted; request new evidence if needed.'})
        return None

    def _usage(self):
        if self._runtime is not None:
            value = self._runtime.usage.summary()
        else:
            value = self._journal.get('usage', {'requests': 0, 'known_usage_requests': 0,
                     'unknown_usage_requests': 0, 'ambiguous_usage_requests': 0}) if self._journal else {'requests': 0}
        value['status'] = ('known' if value.get('requests') and value.get('known_usage_requests') == value['requests']
                           else 'unknown')
        if value['status'] != 'known':
            value['note'] = 'Token totals are known subtotals; unavailable requests are not counted as zero.'
        return value

    def _diff(self):
        base = SafeFiles(self.task_root / 'base')
        old_names = set(self._journal.get('base', {}))
        names = sorted(old_names | set(self.files.names()))
        pieces = []
        for name in names:
            before = base.maybe_read(name) if name in old_names else None
            after = self.files.maybe_read(name)
            if before == after:
                continue
            try:
                left = (before or b'').decode('utf-8').splitlines(keepends=True)
                right = (after or b'').decode('utf-8').splitlines(keepends=True)
                pieces.append(''.join(difflib.unified_diff(left, right, fromfile='a/' + name, tofile='b/' + name)))
            except UnicodeDecodeError:
                pieces.append('Binary file changed: ' + name + '\n')
        text = '\n'.join(pieces)
        return text if len(text) <= MAX_DIFF else text[:MAX_DIFF] + '\n[Diff truncated; full task copy retained locally.]'

    def _result(self, status, result):
        checkpoint = self._journal.get('checkpoint', {}) if self._journal else {}
        try:
            diff = self._diff() if self._journal and not self._blocked else self._journal.get('last_diff', '') if self._journal else ''
        except Exception:
            diff = '[Diff unavailable: unsafe or interrupted workspace; inspect it locally.]'
        value = {'status': status, 'result': result, 'checkpoint': checkpoint, 'usage': self._usage(), 'diff': diff}
        if self._journal:
            self._journal.set('result', value)
            self._journal.set('usage', value['usage'])
            self._journal.set('last_diff', diff)
            self._emit('status', result, {'status': status})
        return value

    def _context(self):
        confirmed = []
        steps = [s for s in self._journal.steps() if s['kind'] != 'inference'][-5:]
        for index, step in enumerate(steps):
            receipt = step['receipt']
            if receipt:
                confirmed.append({'step': step['id'][:16], 'tool': step['intent'].get('tool', step['kind']),
                                  'receipt': {k: short(v, 1600 if index == len(steps) - 1 and k == 'text' else 200)
                                              for k, v in receipt.items() if k in ('status', 'text', 'path', 'sha256', 'exit_code', 'next_offset', 'files', 'matches')}})
        selected = self._runtime.recall(self.task['prompt'], budget=1200, limit=2).notes if self._runtime else []
        recalled = [{'commit_id': note['commit_id'], 'summary': short(note['summary'], 300),
                     'evidence_ids': note.get('evidence_ids', [])[:1]} for note in selected]
        self._journal.set('selected_commit_ids', [note['commit_id'] for note in selected])
        checkpoint = self._journal.get('checkpoint', {})
        prefix = ('Current task focus: ' + short(self.task['prompt'], 240)
                  + '\nRelevant StateTree notes: ' + encode(recalled)
                  + '\nConfirmed durable tool receipts (do not repeat completed effects):\n')
        suffix = '\nCheckpoint evidence: ' + encode({k: checkpoint[k] for k in ('id', 'evidence_id') if k in checkpoint})
        while len((prefix + encode(confirmed) + suffix).encode('utf-8')) > 3000 and len(confirmed) > 1:
            confirmed.pop(0)
        return short(prefix + encode(confirmed) + suffix, 3000)

    def _checkpoint(self, runtime, position, *, result=None):
        if self._blocked:
            raise ValueError('Cannot checkpoint while a command stop is unconfirmed.')
        receipts = self._journal.steps()
        evidence = runtime.store.put_archive({'task_id': self.task_id, 'receipts': receipts})
        changed = [s['intent'].get('path', '') for s in receipts if s['kind'] in ('edit', 'write') and s['receipt'] and s['receipt'].get('status') == 'ok']
        note = CommitNote(summary=('Task: ' + self.task['prompt'][:300] + '. '
                                 + (('Assistant result: ' + result[:500]) if result else 'Confirmed tool boundary; continue from journal receipts.')),
                          changes=list(dict.fromkeys(changed)), evidence_ids=[evidence],
                          pending=[] if result else ['Continue the task from confirmed receipts; do not replay effects.'],
                          outcome='completed' if result else 'partial')
        self.files.names()  # Reject command-created links before Git captures files.
        commit = runtime.commit(note=note)
        metadata = {'id': commit.id, 'position': position, 'evidence_id': evidence,
                    'confirmed_steps': sum(s['receipt'] is not None for s in receipts if s['kind'] != 'inference'),
                    'summary': short(note.summary, 700), 'pending': [short(p, 200) for p in note.pending[:3]],
                    'changes': [short(path, 180) for path in note.changes[:10]],
                    'selected_commit_ids': self._journal.get('selected_commit_ids', []), 'created_at': now()}
        self._journal.set('checkpoint', metadata)
        self._journal.set('usage', self._usage())
        self._emit('checkpoint', 'Saved task checkpoint.', metadata)

    def _effect(self, kind, tool_name, arguments, action, *, metadata=None, deduplicate=False):
        # A review invokes another Strands loop whose read tools run on their
        # own threads; those reads each acquire the file mutex themselves.
        with (nullcontext() if kind == 'review' else self._mutex):
            if self._cancelled():
                return {'status': 'error', 'text': 'Cancelled before the tool started.'}
            if self._blocked:
                return {'status': 'error', 'text': 'Tools are blocked until the uncertain prior command is reconciled.'}
            identity = {'tool': tool_name, 'arguments': arguments}
            if kind == 'command':
                identity['workspace'] = {name: digest(self.files.read(name)) for name in self.files.names()}
            identifier = digest(encode(identity).encode()) if deduplicate else uuid4().hex
            old = self._journal.step(identifier)
            if old:
                if old['receipt'] is None:
                    self._blocked = True
                    return {'status': 'error', 'text': 'The earlier effect has no confirmed receipt. It was not repeated.'}
                if old['receipt'].get('not_dispatched'):
                    self._journal.retry_unperformed(identifier)
                else:
                    self._emit('tool_result', tool_name + ': confirmed prior receipt.',
                               {'tool': tool_name, 'step_id': identifier, 'replayed': True})
                    return copy.deepcopy(old['receipt'])
            intent = {**identity, **(metadata or {})}
            if kind == 'command':
                intent['container'] = 'statetree-' + self.task_id[:12] + '-' + identifier[:12]
            if not old:
                self._journal.begin(identifier, kind, intent)
            self._emit('tool_start', tool_name, {'tool': tool_name, 'step_id': identifier,
                                              'arguments': arguments})
            try:
                receipt = ({'status': 'cancelled', 'not_dispatched': True, 'text': 'Cancelled before dispatch; no effect was performed.'}
                           if self._cancelled() else action(intent))
            except Exception as error:
                receipt = {'status': 'error', 'text': str(error)[:1500] if isinstance(error, (ValueError, UnsafePath, FileNotFoundError))
                           else 'Tool failed; inspect local task state and dependencies.'}
            if not self._blocked:
                self._journal.finish(identifier, receipt)
            self._emit('tool_result', tool_name + ': ' + str(receipt.get('text', receipt.get('status', 'finished')))[:1800],
                       {'tool': tool_name, 'step_id': identifier, 'receipt': receipt})
            return receipt

    def _tools(self, *, readonly=False):
        @tool
        def list_files() -> dict:
            """List up to 100 safe project file paths."""
            return self._effect('read', 'list_files', {}, lambda _: {'files': self.files.names()[:100], 'status': 'ok'})

        @tool
        def read_file(path: str, offset: int = 0) -> dict:
            """Read up to 1500 UTF-8 bytes. Offset is in characters; page with next_offset."""
            def read(_):
                if type(offset) is not int or offset < 0:
                    raise ValueError('Invalid file offset.')
                raw = self.files.read(path)
                content = raw.decode('utf-8')
                page = short(content[offset:], 1500)
                return {'status': 'ok', 'path': path, 'sha256': digest(raw), 'text': page,
                        'next_offset': offset + len(page) if len(content) > offset + len(page) else None}
            return self._effect('read', 'read_file', {'path': path, 'offset': offset}, read)

        @tool
        def search_files(text: str) -> dict:
            """Find literal text in safe project files, returning up to 20 matching lines."""
            def search(_):
                if not text or len(text) > 200:
                    raise ValueError('Use a search string of 1 to 200 characters.')
                matches = []
                for name in self.files.names():
                    try:
                        lines = self.files.read(name).decode('utf-8').splitlines()
                    except UnicodeDecodeError:
                        continue
                    for index, line in enumerate(lines):
                        if text in line:
                            matches.append({'path': name, 'line': index + 1, 'text': line[:250]})
                            if len(matches) == 20:
                                return {'status': 'ok', 'matches': matches, 'truncated': True}
                return {'status': 'ok', 'matches': matches}
            return self._effect('read', 'search_files', {'text': text}, search)

        if readonly:
            return [list_files, read_file, search_files]

        @tool
        def edit_file(path: str, old: str, new: str) -> dict:
            """Replace one exact nonempty occurrence after reading the file."""
            with self._mutex:
                if self._blocked or self._cancelled():
                    return {'status': 'error', 'text': 'Editing is blocked or cancelled.'}
                arguments = {'path': path, 'old': old, 'new': new}
                identifier = digest(encode({'tool': 'edit_file', 'arguments': arguments}).encode())
                previous = self._journal.step(identifier)
                if previous and not (previous['receipt'] or {}).get('not_dispatched'):
                    return self._effect('edit', 'edit_file', arguments, lambda _: {}, deduplicate=True)
                raw = self.files.read(path)
                text = raw.decode('utf-8')
                if not old or text.count(old) != 1:
                    raise ValueError('Expected text must match exactly once. Read the file before editing.')
                changed = text.replace(old, new, 1).encode('utf-8')
                def write(_):
                    self.files.write(path, changed, digest(raw))
                    return {'status': 'ok', 'path': path, 'sha256': digest(changed), 'text': 'Confirmed file edit.'}
                return self._effect('edit', 'edit_file', arguments, write, deduplicate=True,
                                    metadata={'path': path, 'before': digest(raw), 'after': digest(changed)})

        @tool
        def write_file(path: str, content: str, expected_sha256: str = 'missing') -> dict:
            """Create a file, or replace it using the hash returned by read_file."""
            with self._mutex:
                safe_name(path)
                if expected_sha256 != 'missing' and not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
                    raise ValueError('Supply missing or the exact current SHA256.')
                raw = content.encode('utf-8')
                before = None if expected_sha256 == 'missing' else expected_sha256
                def write(_):
                    self.files.write(path, raw, before)
                    return {'status': 'ok', 'path': path, 'sha256': digest(raw), 'text': 'Confirmed file write.'}
                return self._effect('write', 'write_file', {'path': path, 'content': content, 'expected_sha256': expected_sha256},
                                    write, deduplicate=True, metadata={'path': path, 'before': before, 'after': digest(raw)})

        @tool
        def git_diff() -> dict:
            """Read a bounded diff of the private task copy against its starting files."""
            return self._effect('read', 'git_diff', {}, lambda _: {'status': 'ok', 'text': self._diff()[:6000]})

        @tool
        def run_command(command: str) -> dict:
            """Run a check in the preinstalled networkless Docker image; 30 second limit."""
            if not isinstance(command, str) or not command.strip() or len(command) > 2000:
                raise ValueError('Command must contain 1 to 2000 characters.')
            return self._effect('command', 'run_command', {'command': command},
                                lambda intent: self._command(command, intent['container']), deduplicate=True)

        @tool
        def checkpoint(note: str) -> dict:
            """Record a concise model-authored progress note; durable checkpoint follows this tool."""
            return self._effect('note', 'checkpoint', {'note': note[:1000]},
                                lambda _: {'status': 'ok', 'text': 'Model-authored note: ' + note[:1000]})

        @tool
        def delegate_review(question: str) -> dict:
            """Ask one separate read-only agent to inspect the task files; at most four model calls."""
            if not question or len(question) > 1000:
                raise ValueError('Review question must contain 1 to 1000 characters.')
            if any(step['kind'] == 'review' for step in self._journal.steps()):
                raise ValueError('Only one review subtask is allowed per task.')
            return self._effect('review', 'delegate_review', {'question': question}, lambda _: self._review(question))

        return [list_files, read_file, search_files, edit_file, write_file, git_diff, run_command, checkpoint, delegate_review]

    def _command(self, command, container):
        # This fence must survive a process kill before any finally block runs.
        self._fence().set('blocked_task', self.task_id)
        try:
            return self._command_container(command, container)
        finally:
            if not self._blocked:
                self._fence().set('blocked_task', None)

    def _command_container(self, command, container):
        # The caller holds the same mutex used for every host file operation.
        inspected = subprocess.run([self.docker, 'image', 'inspect', self.command_image], capture_output=True, timeout=10)
        if inspected.returncode:
            return {'status': 'error', 'not_dispatched': True, 'text': 'The selected Docker image is unavailable locally. The operator must prepare it; no image was pulled.'}
        create = [self.docker, 'create', '--name', container, '--network', 'none', '--read-only',
                  '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '128',
                  '--memory', '512m', '--cpus', '1', '--user', '65534:65534', '--tmpfs', '/tmp:rw,nosuid,size=64m',
                  '--mount', 'type=bind,source=' + str(self.files_root) + ',target=/workspace',
                  '--workdir', '/workspace', self.command_image, 'sh', '-lc', command]
        made = subprocess.run(create, capture_output=True, timeout=15)
        if made.returncode:
            return {'status': 'error', 'not_dispatched': True, 'text': 'Docker could not create the isolated command container.'}
        process = None
        output, total = [], [0]
        try:
            process = subprocess.Popen([self.docker, 'start', '-a', container], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            def consume():
                while True:
                    chunk = process.stdout.read(1024)
                    if not chunk:
                        return
                    room = max(0, 12000 - total[0])
                    if room:
                        output.append(chunk[:room])
                    total[0] += len(chunk)
            reader = threading.Thread(target=consume, daemon=True)
            reader.start()
            deadline = time.monotonic() + 30
            cancelled = timed_out = False
            while process.poll() is None:
                cancelled = self._cancelled()
                timed_out = time.monotonic() >= deadline
                if cancelled or timed_out:
                    break
                time.sleep(0.1)
            if cancelled or timed_out:
                subprocess.run([self.docker, 'kill', container], capture_output=True, timeout=10)
            process.wait(timeout=10)
            reader.join(timeout=2)
            inspected = subprocess.run([self.docker, 'inspect', '--format', '{{json .State}}', container], capture_output=True, timeout=10)
            state = json.loads(inspected.stdout) if inspected.returncode == 0 else {}
            if state.get('Running') is not False:
                raise RuntimeError('Container stop was not confirmed')
            text = b''.join(output).decode('utf-8', errors='replace')
            if total[0] > 12000:
                text += '\n[Output truncated.]'
            return {'status': 'cancelled' if cancelled else 'timeout' if timed_out else 'ok' if state.get('ExitCode') == 0 else 'error',
                    'exit_code': state.get('ExitCode'), 'text': text, 'container_stopped': True}
        finally:
            try:
                removed = subprocess.run([self.docker, 'rm', '-f', container], capture_output=True, timeout=15)
                if removed.returncode:
                    raise OSError('Cannot confirm container removal')
                if process and process.poll() is None:
                    process.wait(timeout=5)
            except Exception:
                self._blocked = True
                self._fence().set('blocked_task', self.task_id)
            if process and process.stdout:
                process.stdout.close()

    def _review(self, question):
        self._emit('agent', 'Starting a separate read-only review.', {'question': question})
        child = Agent(model=self.model_factory(), tools=self._tools(readonly=True),
                      system_prompt='Inspect the requested project files using read-only tools. Return factual findings. No edits or commands.',
                      callback_handler=None, context_manager=False, tool_executor=SequentialToolExecutor(), checkpointing=True)
        runtime = StateTreeRuntime(child, goal=question, repo_path=self.repo, store_path=self.task_root / 'review-memory',
                                   max_input_tokens=12000, recent_turns=2, input_cache_convention='included')
        runtime.hooks.ledger = self._runtime.usage
        runtime.hooks.phase = 'review'
        prompt, requests = question, 0
        try:
            for _ in range(12):
                if self._cancelled():
                    return {'status': 'error', 'text': 'Review cancelled.'}
                if isinstance(prompt, str):
                    requests += 1
                elif prompt['checkpointResume']['checkpoint']['position'] == 'after_tools':
                    requests += 1
                if requests > 4:
                    return {'status': 'error', 'text': 'Review reached its four-model-call limit.'}
                result = self._invoke(runtime, prompt)
                if result.stop_reason == 'checkpoint':
                    prompt = {'checkpointResume': {'checkpoint': result.checkpoint.to_dict()}}
                else:
                    text = ''.join(block.get('text', '') for block in result.message.get('content', []))
                    self._emit('agent', 'Read-only review finished.', {'result': text[:3000]})
                    return {'status': 'ok', 'text': text[:3000]}
        except Exception:
            return {'status': 'error', 'text': 'The read-only review did not complete; its usage remains included.'}
        return {'status': 'error', 'text': 'Review reached its execution limit.'}

    def _invoke(self, runtime, prompt, *, new_task=False):
        """Journal dispatch before a model request, including child inference."""
        makes_request = (isinstance(prompt, str)
                         or prompt.get('checkpointResume', {}).get('checkpoint', {}).get('position') == 'after_tools')
        identifier = uuid4().hex if makes_request else None
        if identifier:
            self._journal.begin(identifier, 'inference', {'requests_before': self._runtime.usage.summary()['requests']})
        try:
            result = runtime.run(prompt, new_task=new_task, limits={'turns': 1})
        except Exception:
            if identifier:
                self._journal.finish(identifier, {'status': 'error', 'text': 'Model invocation failed; inspect its usage record.'})
            raise
        if identifier:
            self._journal.finish(identifier, {'status': 'ok', 'text': 'Model response received.'})
        return result

    def _capture_archive_results(self, agent):
        """The runtime's archive tool also needs a receipt before compaction."""
        calls = {}
        for message in agent.messages:
            for block in message.get('content', []):
                if 'toolUse' in block:
                    use = block['toolUse']
                    calls[use['toolUseId']] = use
        if not agent.messages:
            return
        for block in agent.messages[-1].get('content', []):
            result = block.get('toolResult')
            use = calls.get(result.get('toolUseId')) if result else None
            if use and use['name'] == 'statetree_read_archive':
                identifier = uuid4().hex
                receipt = {'status': result.get('status', 'success'),
                           'text': encode(result.get('content', [])),
                           'note': 'Long archive pages may be clipped in context; request at most 1000 characters per page.'}
                self._journal.begin(identifier, 'read', {'tool': 'statetree_read_archive', 'arguments': use.get('input', {})})
                self._journal.finish(identifier, receipt)
                self._emit('tool_result', 'Retrieved archived StateTree evidence.', {'step_id': identifier,
                           'tool': 'statetree_read_archive', 'receipt': receipt})

    def run(self, task):
        self._runtime, self._journal, self._blocked = None, None, False
        try:
            if not isinstance(task, dict) or not isinstance(task.get('prompt'), str) or not task['prompt'].strip():
                raise ValueError('A nonempty task prompt is required.')
            if len(task['prompt'].encode('utf-8')) > 2000:
                raise ValueError('Task prompt exceeds the local 2000 UTF-8-byte limit; split it into focused tasks.')
            with project_lock(self.state_root / ('project-' + self.project_key + '.lock')):
                # Strands runs synchronous tools in executor threads. The
                # process lock fences the task; each tool acquires its own mutex.
                with nullcontext():
                    blocked_task = self._fence().get('blocked_task')
                    if blocked_task and blocked_task != task.get('id'):
                        return {'status': 'waiting_for_input', 'result': 'A previous command may still be running. Resume task '
                                + blocked_task + ' to stop and reconcile it before starting new work.',
                                'checkpoint': {}, 'usage': {'status': 'unknown', 'requests': 0}, 'diff': ''}
                    self._open_task(task)
                    prior = self._journal.get('result')
                    if prior and prior['status'] in ('completed', 'applied'):
                        return prior
                    problem = self._reconcile()
                    if problem:
                        return self._result('waiting_for_input', problem)
                    if self._cancelled():
                        return self._result('cancelled', 'Task cancelled; confirmed work is retained.')
                    agent = Agent(model=self.model_factory(), tools=self._tools(), system_prompt=SYSTEM,
                                  callback_handler=None, context_manager=False,
                                  tool_executor=SequentialToolExecutor(), checkpointing=True)
                    self._runtime = StateTreeRuntime(agent, goal=self.task['prompt'], repo_path=self.repo,
                        store_path=self.task_root / 'memory', max_input_tokens=12000, recent_turns=1,
                        commit_context_budget=1600, input_cache_convention='included')
                    self._runtime.workspace = TaskGitWorkspace(self.repo)
                    parent = self._journal.get('parent_evidence')
                    if parent and not self._journal.get('parent_imported'):
                        archive = self._runtime.store.put_archive(parent)
                        self._runtime.commit(note=CommitNote(
                            summary=('Prior task: ' + str(parent['prompt'])[:300]
                                     + '. Assistant result: ' + str(parent['result'])[:600]),
                            evidence_ids=[archive], outcome='completed'))
                        self._journal.set('parent_imported', True)
                    self._emit('status', 'Running from confirmed local task state.', {'status': 'running'})
                    prompt = self._context()
                    for _ in range(MAX_MODEL_STEPS * 2):
                        if self._cancelled():
                            return self._result('cancelled', 'Task cancelled; confirmed work and usage are retained.')
                        if self._blocked:
                            return self._result('waiting_for_input', 'A command outcome is uncertain; tools are blocked until reconciliation.')
                        before = self._runtime.usage.summary()['requests']
                        result = self._invoke(self._runtime, prompt, new_task=isinstance(prompt, str))
                        if self._blocked:
                            return self._result('waiting_for_input', 'A command stop or outcome is uncertain. Reconcile it before resuming; no further files were read.')
                        if self._runtime.usage.summary()['requests'] > before:
                            self._emit('usage', 'Recorded model usage.', self._usage())
                        if result.stop_reason == 'checkpoint':
                            position = result.checkpoint.position
                            if position == 'after_tools':
                                self._capture_archive_results(agent)
                            self._checkpoint(self._runtime, position)
                            prompt = ({'checkpointResume': {'checkpoint': result.checkpoint.to_dict()}}
                                      if position == 'after_model' else self._context())
                            continue
                        if result.stop_reason == 'cancelled':
                            return self._result('cancelled', 'Task cancelled; confirmed work is retained.')
                        text = ''.join(block.get('text', '') for block in result.message.get('content', []))
                        if not text:
                            return self._result('failed', 'The model returned no final result.')
                        self._checkpoint(self._runtime, 'completed', result=text)
                        self._emit('message', text)
                        return self._result('completed', text)
                    return self._result('interrupted', 'Task reached its bounded step limit. Resume explicitly to continue from its checkpoint.')
        except ProjectBusy as error:
            return {'status': 'interrupted', 'result': str(error), 'checkpoint': {}, 'usage': {'status': 'unknown', 'requests': 0}, 'diff': ''}
        except Exception as error:
            message = (str(error)[:1000] if isinstance(error, (ValueError, UnsafePath)) else
                       'The local task could not finish. Model, Docker or checkpoint processing failed; confirmed work is retained for resume.')
            try:
                with project_lock(self.state_root / ('project-' + self.project_key + '.lock')):
                    return self._result('failed', message)
            except ProjectBusy:
                return {'status': 'interrupted', 'result': message, 'checkpoint': {},
                        'usage': {'status': 'unknown', 'requests': 0}, 'diff': ''}

    def apply(self, task_id):
        self._runtime, self._blocked = None, False
        try:
            with project_lock(self.state_root / ('project-' + self.project_key + '.lock')):
                with self._mutex:
                    blocked_task = self._fence().get('blocked_task')
                    if blocked_task:
                        raise ValueError('A command may still be running. Resume task ' + blocked_task + ' to reconcile it before applying files.')
                    self._paths(task_id)
                    if not (self.task_root / 'journal.sqlite3').exists():
                        raise ValueError('Unknown local task.')
                    self._journal = Journal(self.task_root / 'journal.sqlite3')
                    task = self._journal.get('task')
                    if not task or task['project'] != str(self.project):
                        raise ValueError('Task does not belong to this project.')
                    prior = self._journal.get('result', {})
                    if prior.get('status') == 'applied':
                        return prior
                    if prior.get('status') not in ('completed', 'conflict'):
                        raise ValueError('Only a completed, reviewed task can be applied.')
                    self.files = SafeFiles(self.files_root)
                    self.task = task
                    if self._reconcile():
                        raise ValueError('Reconcile unfinished effects before applying.')
                    source = SafeFiles(self.project)
                    base = self._journal.get('base', {})
                    changes, conflicts = [], []
                    for name in sorted(set(base) | set(self.files.names())):
                        data = self.files.maybe_read(name)
                        wanted = digest(data) if data is not None else None
                        if wanted == base.get(name):
                            continue
                        current = source.maybe_read(name)
                        actual = digest(current) if current is not None else None
                        if actual not in (base.get(name), wanted):
                            conflicts.append(name)
                        elif actual != wanted:
                            changes.append((name, data, actual))
                    if conflicts:
                        return self._result('conflict', 'Original files changed; nothing was applied: ' + ', '.join(conflicts))
                    for name, data, actual in changes:
                        if self._cancelled():
                            self._journal.set('apply_cancelled', True)
                            return self._result('conflict', 'Apply stopped; any already applied files are retained. Verify the files, then explicitly retry apply to reconcile the remaining changes.')
                        self._journal.set('apply_intent', {'path': name, 'before': actual, 'after': digest(data) if data is not None else None})
                        if data is None:
                            source.delete(name, actual)
                        else:
                            source.write(name, data, actual)
                        self._journal.set('apply_intent', None)
                    return self._result('applied', f'Applied {len(changes)} changed files. Original Git staging was preserved.')
        except (ValueError, OSError, ProjectBusy) as error:
            return {'status': 'conflict', 'result': str(error)[:1000], 'checkpoint': {}, 'usage': {}, 'diff': ''}
