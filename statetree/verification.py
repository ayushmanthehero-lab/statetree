"""Operator-configured verification commands with retained bounded evidence.

Commands are trusted local programs, not a security sandbox. They run without a
shell, in isolated worktrees. No browser-supplied command is accepted here.
"""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from statetree.core.commit import digest
from statetree.workspace.git import GitWorkspace


class CommandVerifier:
    def __init__(self, commands, timeout, store):
        if not commands:
            raise ValueError('Configure verification_commands before verified promotion')
        if (type(commands) is not list or any(type(c) is not list or not c or
                any(type(a) is not str or not a or '\x00' in a for a in c) for c in commands)):
            raise ValueError('Verification requires argv lists, never shell strings')
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError('Verification timeout must be 1-3600 seconds')
        self.commands = [list(command) for command in commands]
        self.timeout, self.store = timeout, store
        self.results = []

    def __call__(self, path, state, phase):
        self.results = []
        for command in self.commands:
            started = time.monotonic()
            timed_out = False
            with tempfile.TemporaryFile() as output:
                options = {'start_new_session': True} if os.name != 'nt' else {
                    'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                # GIT_DIR/GIT_INDEX_FILE inherited from another checkout must
                # not redirect a verification command back into the source.
                env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
                try:
                    process = subprocess.Popen(command, cwd=path, stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=subprocess.STDOUT, env=env, **options)
                    try:
                        code = process.wait(timeout=self.timeout)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        if os.name == 'nt':
                            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                        else:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        process.kill()
                        code = process.wait()
                except OSError as error:
                    code = None
                    output.write(str(error).encode('utf-8'))
                output.seek(0, os.SEEK_END)
                size = output.tell()
                output.seek(max(0, size - 16384))
                text = output.read(16384).decode('utf-8', errors='replace')
            record = {'kind': 'command-verification', 'command': command, 'phase': phase,
                      'returncode': code, 'timed_out': timed_out, 'output': text,
                      'output_bytes': size, 'output_truncated': size > 16384,
                      'elapsed_seconds': round(time.monotonic() - started, 6),
                      'state_digest': digest(state)}
            archive = self.store.put_archive(record)
            self.results.append({'archive_id': archive, 'command': command,
                                 'returncode': code, 'timed_out': timed_out})
            if code != 0 or timed_out:
                raise RuntimeError(f'Verification failed; evidence archive {archive}')
        return True

    def check_workspace(self, workspace, state):
        sha = workspace.snapshot()
        entries = workspace._entries(sha)
        with tempfile.TemporaryDirectory(prefix='statetree-check-') as directory:
            path = Path(directory) / 'worktree'
            workspace._git('worktree', 'add', '--detach', str(path), sha)
            try:
                self(path, state, 'checkpoint')
                checked = GitWorkspace(path)
                if checked._entries(checked.snapshot()) != entries:
                    raise RuntimeError('Verification changed nonignored checked files')
                if workspace._entries(workspace.snapshot()) != entries:
                    raise RuntimeError('Source files changed during verification; check again')
            finally:
                workspace._git('worktree', 'remove', '--force', str(path))
        return {'passed': True, 'checks': list(self.results), 'workspace_sha': sha,
                'state_digest': digest(state), 'verifier': 'operator-command-argv'}
