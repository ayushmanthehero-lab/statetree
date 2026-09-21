"""Pinned local Git snapshots with separate index and working-tree state.

A snapshot owns its captured files and currently tracked files. Unrelated
untracked files created later are preserved. Symlinks and submodules are
rejected; database/process/remote effects are outside this adapter's scope.
"""

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile


_MARKER = 'StateTree workspace v1\n'
_DEFAULT_EXCLUDES = ('.statetree', '.venv', '__pycache__')


class GitWorkspace:
    def __init__(self, path='.', exclude_paths=()):
        self.path = Path(path).resolve()
        root = Path(self._git('rev-parse', '--show-toplevel')).resolve()
        if root != self.path:
            raise ValueError('repo_path must be the Git repository root')
        self.excludes = tuple(sorted(set(_DEFAULT_EXCLUDES + tuple(exclude_paths))))
        for item in self.excludes:
            self._safe_name(item)

    def _git(self, *args, env=None, input=None):
        result = subprocess.run(
            ['git', *args], cwd=self.path, env=env, input=input,
            capture_output=True, check=True,
        )
        return result.stdout.decode('utf-8').rstrip('\n')

    def _safe_name(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError('Unsafe workspace path')
        parts = PurePosixPath(name).parts
        if (PurePosixPath(name).is_absolute() or '..' in parts or
                any(p.casefold() == '.git' for p in parts) or
                '\\' in name or ':' in name or '\x00' in name):
            raise ValueError('Unsafe workspace path')
        return name

    def _excluded(self, name):
        return any(name == item or name.startswith(item + '/') for item in self.excludes)

    def _entries(self, tree):
        raw = self._git('ls-tree', '-rz', tree)
        result = {}
        for record in raw.split('\x00'):
            if not record:
                continue
            metadata, name = record.split('\t', 1)
            mode, kind, sha = metadata.split()
            self._safe_name(name)
            if mode not in ('100644', '100755') or kind != 'blob':
                raise ValueError('Symlinks and submodules are not supported in workspace checkpoints')
            result[name] = (mode, sha)
        return result

    def _commit_tree(self, tree, parents, message):
        args = ['-c', 'user.name=StateTree', '-c', 'user.email=checkpoint@statetree.invalid',
                'commit-tree', tree]
        for parent in parents:
            args.extend(['-p', parent])
        return self._git(*args, input=message.encode('utf-8'))

    def snapshot(self):
        self._check_index_flags()
        head = self._git('rev-parse', '--verify', 'HEAD')
        original_index = Path(self._git('rev-parse', '--git-path', 'index'))
        if not original_index.is_absolute():
            original_index = self.path / original_index
        with tempfile.TemporaryDirectory(prefix='statetree-index-') as directory:
            index_path = Path(directory) / 'index'
            env = dict(os.environ, GIT_INDEX_FILE=str(index_path), GIT_OPTIONAL_LOCKS='0')
            if original_index.exists():
                shutil.copyfile(original_index, index_path)
            else:
                self._git('read-tree', '--empty', env=env)
            index_tree = self._git('write-tree', env=env)
            index_entries = self._entries(index_tree)
            if any(self._excluded(name) for name in index_entries):
                raise ValueError('Excluded StateTree storage/config paths must not be tracked')
            # The copied index's cached stat information is not a content
            # check. Same-size edits with preserved/coarse mtimes can look
            # unchanged, and copying the index changes its racy-Git timestamp.
            # Rebuild only this private temporary index, preserving the saved
            # index_tree while forcing all working files to be read by add.
            index_path.unlink()
            self._git('read-tree', index_tree, env=env)
            # Explicit negative pathspecs can make git add reject an ignored
            # .statetree directory. Enumerate eligible files instead, including
            # tracked deletions, while respecting the user's ignore rules.
            # NUL framing and literal pathspecs protect unusual filenames and
            # avoid OS command-line length limits on large workspaces.
            names = self._git('ls-files', '--cached', '--others', '--exclude-standard', '-z', env=env)
            paths = sorted({name for name in names.split('\x00') if name and not self._excluded(name)})
            if paths:
                pathspec = ''.join(':(literal)' + name + '\x00' for name in paths).encode('utf-8')
                self._git('add', '-A', '--pathspec-from-file=-', '--pathspec-file-nul', env=env, input=pathspec)
            worktree = self._git('write-tree', env=env)
            self._entries(worktree)
        index_commit = self._commit_tree(index_tree, [head], 'StateTree saved index\n')
        metadata = json.dumps({'excludes': self.excludes}, sort_keys=True)
        sha = self._commit_tree(worktree, [head, index_commit], _MARKER + metadata + '\n')
        self._git('update-ref', 'refs/statetree/workspaces/' + sha, sha)
        return sha

    def compose_snapshot(self, working_sha, index_sha):
        """Pin verified working files with an independently preserved Git index.

        This creates immutable objects only. Restore's collision checks still
        apply; callers must also protect ignored files before branch adoption.
        """
        self._checkpoint(working_sha)
        _, _, index_commit = self._checkpoint(index_sha)
        tree = self._git('rev-parse', working_sha + '^{tree}')
        head = self._git('rev-parse', '--verify', 'HEAD')
        metadata = json.dumps({'excludes': self.excludes}, sort_keys=True)
        sha = self._commit_tree(tree, [head, index_commit], _MARKER + metadata + '\n')
        self._git('update-ref', 'refs/statetree/workspaces/' + sha, sha)
        return sha

    def _checkpoint(self, sha):
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
            raise ValueError('Expected an immutable workspace checkpoint SHA')
        try:
            raw = self._git('cat-file', '-p', sha)
            headers, message = raw.split('\n\n', 1)
            parents = [line[7:] for line in headers.splitlines() if line.startswith('parent ')]
            if not message.startswith(_MARKER) or len(parents) != 2:
                raise ValueError('Not a StateTree workspace checkpoint')
            metadata = json.loads(message[len(_MARKER):])
            if tuple(metadata['excludes']) != self.excludes:
                raise ValueError('Workspace checkpoint exclusions do not match this adapter')
            worktree = self._entries(sha)
            index = self._entries(parents[1])
            if any(self._excluded(name) for name in set(worktree) | set(index)):
                raise ValueError('Checkpoint contains excluded paths')
            for _, blob in set(worktree.values()) | set(index.values()):
                self._git('cat-file', '-e', blob)
            return worktree, index, parents[1]
        except (subprocess.CalledProcessError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError('Workspace checkpoint objects are missing or invalid') from error

    def validate(self, sha):
        self._checkpoint(sha)

    def _check_index_flags(self):
        entries = self._git('ls-files', '-v', '-z').split('\x00')
        if any(item and (item[0].islower() or item[0] == 'S') for item in entries):
            raise ValueError('Unsupported Git index flags: clear assume-unchanged/skip-worktree before checkpointing')

    def restore(self, sha):
        target, saved_index, index_commit = self._checkpoint(sha)
        self._check_index_flags()
        current = set(self._git('ls-files', '-z').split('\x00')) - {''}
        if any(self._excluded(name) for name in current):
            raise ValueError('Restore would change excluded tracked paths')
        originally_untracked = set(target) - set(saved_index)
        for name in set(target) | current:
            self._safe_name(name)
            destination = self.path / name
            if destination.is_dir():
                raise ValueError('Untracked directory collision during restore')
            # Never follow filesystem links, even at a parent directory.
            for candidate in [destination, *destination.parents]:
                if candidate == self.path:
                    break
                if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)():
                    raise ValueError('Workspace path collision with a symlink or junction')
            if name in target:
                if destination.exists() and name not in current and name not in originally_untracked:
                    raise ValueError('Untracked file collision during restore')
                for parent in destination.parents:
                    if parent == self.path:
                        break
                    if parent.exists() and not parent.is_dir():
                        raise ValueError('Untracked parent path collision during restore')
        # Git applies its checkout filters and executable modes. Preflight above
        # prevents overwriting unrelated untracked files.
        # An empty target and empty tracked index has no matching pathspec.
        # Preserve unrelated untracked files and still restore the saved index.
        if target or current:
            self._git('restore', '--source', sha, '--worktree', '--', '.')
        self._git('read-tree', index_commit)
