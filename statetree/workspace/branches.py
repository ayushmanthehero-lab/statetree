"""Local worktree isolation and conservative, verified canonical integration.

This manager has its own portable-state canonical ref. It never moves the
legacy Strands checkpoint ref or checks files out over the caller's workspace.
Workers are trusted local callables: worktrees are not security sandboxes.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from uuid import uuid4

from statetree.core.commit import canonical_json, digest
from statetree.storage.local import LocalStateStore
from statetree.workspace.git import GitWorkspace


class MergeConflictError(RuntimeError):
    """Concurrent changes or stale declared dependencies need a new decision."""


class StaleParentError(RuntimeError):
    """Canonical state changed after the caller chose its expected parent."""


class VerificationError(RuntimeError):
    """An independent check failed, raised, or changed the checked files."""


_MISSING = object()


def _json_copy(value):
    def check(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError('State JSON keys must be strings')
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError('Expected portable JSON values')
    check(value)
    return json.loads(canonical_json(value))


def _equal(left, right):
    if left is _MISSING or right is _MISSING:
        return left is right
    return canonical_json(left) == canonical_json(right)


def _merge(base, current, candidate, path):
    if _equal(candidate, base):
        return current
    if _equal(current, base) or _equal(current, candidate):
        return candidate
    if all(isinstance(value, dict) for value in (base, current, candidate)):
        merged = {}
        for key in sorted(base.keys() | current.keys() | candidate.keys()):
            value = _merge(base.get(key, _MISSING), current.get(key, _MISSING),
                           candidate.get(key, _MISSING), path + '/' + key)
            if value is not _MISSING:
                merged[key] = value
        return merged
    raise MergeConflictError('Conflicting change at ' + path)


def merge_json(base, current, candidate):
    """Three-way JSON merge; dictionaries recurse, arrays/scalars are atomic.

    Conflicting updates and deletion versus modification are rejected. This
    performs no semantic inference about application facts or source code.
    """
    return _json_copy(_merge(_json_copy(base), _json_copy(current), _json_copy(candidate), '$'))


@dataclass
class Branch:
    name: str
    path: Path
    base_id: str
    state: dict
    reads: dict
    writes: dict
    _key: str | None = field(default=None, repr=False)
    _usage_recorder: object = field(default=None, repr=False)
    _budget_checker: object = field(default=None, repr=False)

    candidate_id: str | None = None

    def record_usage(self, usage=None, *, request_id=None, phase='strategy', model='',
                     status='ok', input_cache_convention='auto'):
        """Persist an observed request immediately, including missing usage.

        Available on coordinator-provided branches. Call after each request,
        including failed requests; ``None`` records unknown usage, never zero.
        """
        if self._usage_recorder is None:
            raise RuntimeError('No usage recorder is attached to this branch')
        return self._usage_recorder(
            usage, request_id=request_id, phase=phase, model=model, status=status,
            input_cache_convention=input_cache_convention,
        )

    def check_budget(self):
        """Check configured persisted thresholds before starting another call."""
        if self._budget_checker is not None:
            self._budget_checker()


class BranchManager:
    """Own isolated local candidates and a separately persisted canonical ref.

    ``verifier(path, state, phase)`` must return the boolean True. It is fixed
    by the application, not supplied by workers, and runs on fresh worktrees
    of immutable snapshots for both ``candidate`` and ``integrated`` phases.
    It must not change the state or nonignored files. Generated ignored test output
    is allowed. Returned canonical revisions expose ``workspace_path`` and
    ``workspace_sha``; adopting them in another runtime is an explicit action.

    Named external resource versions are application declarations. File write
    conflicts are detected automatically, but reads must be declared. Names
    ``file:relative/path`` refer to automatically calculated blob versions.
    Other names refer to ``resource_versions`` supplied at initialization.
    """

    def __init__(self, repo_path, *, root=None, verifier):
        if not callable(verifier):
            raise ValueError('An independent verifier callback is required')
        self.workspace = GitWorkspace(repo_path)
        self.repo = self.workspace.path
        requested = Path(root) if root is not None else self.repo / '.statetree' / 'parallel'
        requested = requested.absolute()
        for path in (requested, *requested.parents):
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('Branch storage cannot traverse filesystem links')
        self.root = requested.resolve()
        if self.root == self.repo or self.repo.is_relative_to(self.root):
            raise ValueError('Branch storage must not contain the source repository')
        if self.root.is_relative_to(self.repo) and not self.root.is_relative_to(self.repo / '.statetree'):
            raise ValueError('In-repository branch storage must be under .statetree')
        self.root.mkdir(parents=True, exist_ok=True)
        self.worktrees = self.root / 'worktrees'
        self.worktrees.mkdir(exist_ok=True)
        self.store = LocalStateStore(self.root / 'store')
        self.store.bind_workspace(self.repo)
        self.verifier = verifier
        with self.store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS parallel_refs (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS parallel_branches (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS parallel_candidates (name TEXT PRIMARY KEY, value TEXT NOT NULL)')

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
            raise ValueError('Branch names require 1-64 letters, digits, dot, dash or underscore')
        return name

    def _save(self, value):
        value = _json_copy(value)
        return {'id': self.store.put_archive(value), **value}

    def _load(self, identifier, kind=None):
        value = self.store.read_archive(identifier)
        if not isinstance(value, dict) or (kind is not None and value.get('kind') != kind):
            raise ValueError('Wrong parallel manifest type')
        return {'id': identifier, **value}

    def _head_id(self, db):
        row = db.execute("SELECT value FROM parallel_refs WHERE name='canonical'").fetchone()
        return row[0] if row else None

    def head(self):
        with self.store.transaction() as db:
            identifier = self._head_id(db)
        return self._load(identifier, 'revision') if identifier else None

    def initialize(self, state, resource_versions=None):
        """Capture the caller's current files once without changing its index."""
        if not isinstance(state, dict):
            raise ValueError('state must be a JSON dictionary')
        state = _json_copy(state)
        resources = self._declarations(resource_versions)
        if any(name.startswith('file:') for name in resources):
            raise ValueError('File resource versions are derived from snapshots')
        with self.store.transaction() as db:
            if self._head_id(db) is not None:
                raise ValueError('This branch manager is already initialized')
            sha = self.workspace.snapshot()
            path = self._worktree(sha, 'canonical')
            revision = self._save({
                'kind': 'revision', 'parent': None, 'state': state, 'resources': resources,
                'workspace_sha': sha, 'workspace_path': str(path), 'source_candidate': None,
            })
            db.execute("INSERT INTO parallel_refs VALUES ('canonical', ?)", (revision['id'],))
        return revision

    def _worktree(self, sha, prefix):
        self.workspace.validate(sha)
        path = self.worktrees / (prefix + '-' + uuid4().hex)
        # Git creates a new empty directory and never checks out over a caller path.
        self.workspace._git('worktree', 'add', '--detach', str(path), sha)
        return path

    def _declarations(self, values):
        if values is None:
            return {}
        if not isinstance(values, dict):
            raise ValueError('Resource declarations must map names to expected versions')
        values = _json_copy(values)
        if any(not key or not (value is None or isinstance(value, str))
               for key, value in values.items()):
            raise ValueError('Resource names must be nonempty and versions strings or None')
        for key in values:
            if key.startswith('file:'):
                self._file_resource_path(key[5:])
        return values

    def _file_resource_path(self, name):
        self.workspace._safe_name(name)
        parts = PurePosixPath(name).parts
        if (str(PurePosixPath(name)) != name or name == '.' or
                any(part.endswith(('.', ' ')) for part in parts)):
            raise ValueError('File resources require canonical relative Git paths')
        return name

    def resource_version(self, revision, name):
        """Get an external version or a file content/mode version for declaring reads."""
        if name.startswith('file:'):
            path = self._file_resource_path(name[5:])
            entries = self.workspace._entries(revision['workspace_sha'])
            if path not in entries and any(key.casefold() == path.casefold() for key in entries):
                raise ValueError('Use the exact Git filename case in file resources')
            entry = entries.get(path)
            return digest(list(entry)) if entry is not None else None
        return revision['resources'].get(name)

    def _check_resources(self, revision, reads, writes):
        for declarations in (reads, writes):
            for name, expected in declarations.items():
                if self.resource_version(revision, name) != expected:
                    raise MergeConflictError('Stale resource dependency: ' + name)

    def fork(self, name, *, base_id=None, reads=None, writes=None, namespace=None):
        name = self._name(name)
        key = name if namespace is None else self._name(namespace) + ':' + name
        reads, writes = self._declarations(reads), self._declarations(writes)
        with self.store.transaction() as db:
            if db.execute('SELECT 1 FROM parallel_branches WHERE name=?', (key,)).fetchone():
                raise ValueError('Branch name already exists')
            identifier = base_id or self._head_id(db)
            if identifier is None:
                raise ValueError('Initialize canonical state before forking')
            base = self._load(identifier, 'revision')
            self._check_resources(base, reads, writes)
            path = self._worktree(base['workspace_sha'], name)
            descriptor = {'name': name, 'path': str(path), 'base_id': identifier,
                          'reads': reads, 'writes': writes}
            db.execute('INSERT INTO parallel_branches VALUES (?, ?)',
                       (key, canonical_json(descriptor).decode('utf-8')))
        return Branch(name, path, identifier, _json_copy(base['state']), reads, writes, _key=key)

    def list_branches(self):
        """List durable branch descriptors and the latest completed candidate."""
        with self.store.transaction() as db:
            rows = db.execute('SELECT b.name, b.value, c.value FROM parallel_branches b '
                              'LEFT JOIN parallel_candidates c ON b.name=c.name ORDER BY b.name').fetchall()
        return [{**json.loads(value), 'key': key, 'candidate_id': candidate}
                for key, value, candidate in rows]

    def open_branch(self, name, *, namespace=None):
        """Reopen a persisted branch without resetting its working files.

        ``state`` comes from its latest completed candidate, or its original
        base. Uncheckpointed edits remain in its worktree for reconciliation.
        """
        name = self._name(name)
        key = name if namespace is None else self._name(namespace) + ':' + name
        records = [row for row in self.list_branches() if row['key'] == key]
        if not records:
            raise ValueError('Unknown branch')
        row = records[0]
        path = Path(row['path'])
        if (path.resolve().parent != self.worktrees or path.is_symlink()
                or getattr(path, 'is_junction', lambda: False)() or not path.is_dir()):
            raise ValueError('Branch worktree is missing or moved outside managed storage')
        GitWorkspace(path)  # Require an actual Git worktree.
        value = self._load(row['candidate_id'], 'candidate') if row['candidate_id'] else self._load(row['base_id'], 'revision')
        return Branch(name, path, row['base_id'], _json_copy(value['state']),
                      row['reads'], row['writes'], _key=key, candidate_id=row['candidate_id'])

    def candidate(self, candidate_id):
        """Read and validate an immutable candidate."""
        return self._load(candidate_id, 'candidate')

    def complete(self, branch, *, state, result=None):
        """Pin a finished candidate; later changes in its folder cannot change it."""
        if not isinstance(state, dict):
            raise ValueError('Candidate state must be a JSON dictionary')
        self._name(branch.name)
        with self.store.transaction() as db:
            row = db.execute('SELECT value FROM parallel_branches WHERE name=?',
                             (branch._key or branch.name,)).fetchone()
        if row is None:
            raise ValueError('Unknown branch')
        descriptor = json.loads(row[0])
        path = Path(descriptor['path'])
        if branch.name != descriptor['name'] or branch.path != path or branch.base_id != descriptor['base_id']:
            raise ValueError('Branch identity does not match its persisted descriptor')
        if path.resolve().parent != self.worktrees or path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Branch path moved outside the managed worktree directory')
        sha = GitWorkspace(path).snapshot()
        self.workspace.validate(sha)
        candidate = self._save({
            'kind': 'candidate', 'branch': branch.name, 'branch_key': branch._key or branch.name,
            'base_id': descriptor['base_id'],
            'state': state, 'result': result, 'reads': descriptor['reads'], 'writes': descriptor['writes'],
            'workspace_sha': sha, 'workspace_path': str(path),
        })

        key = branch._key or branch.name
        with self.store.transaction() as db:
            row = db.execute('SELECT value FROM parallel_candidates WHERE name=?', (key,)).fetchone()
            current = row[0] if row else None
            if current != branch.candidate_id:
                raise StaleParentError('Candidate advanced; reopen the branch before completing it')
            db.execute('INSERT OR REPLACE INTO parallel_candidates VALUES (?, ?)', (key, candidate['id']))
        branch.candidate_id = candidate['id']
        return candidate

    def _verify(self, path, sha, state, phase):
        try:
            checked_state = _json_copy(state)
            passed = self.verifier(path, checked_state, phase)
            if passed is not True:
                raise VerificationError('Independent ' + phase + ' verification did not pass')
            if not _equal(state, checked_state):
                raise VerificationError('Verifier changed the checked application state')
            after = GitWorkspace(path).snapshot()
            if self.workspace._entries(after) != self.workspace._entries(sha):
                raise VerificationError('Verifier changed the checked workspace')
        except VerificationError:
            raise
        except Exception as error:
            raise VerificationError('Independent ' + phase + ' verification raised: ' + str(error)) from error

    def verify_candidate(self, candidate_id):
        """Check immutable candidate files independently; does not publish a ref."""
        candidate = self._load(candidate_id, 'candidate')
        path = self._worktree(candidate['workspace_sha'], 'verify')
        self._verify(path, candidate['workspace_sha'], candidate['state'], 'candidate')
        return candidate

    def _merge_files(self, base, current, candidate):
        entries = [self.workspace._entries(value['workspace_sha']) for value in (base, current, candidate)]
        # Tuples become JSON lists so the same conservative three-way rule applies.
        merged = merge_json(*[{key: list(value) for key, value in tree.items()} for tree in entries])
        folded = {}
        for name in sorted(merged):
            lower = name.casefold()
            if lower in folded and folded[lower] != name:
                raise MergeConflictError('Case-insensitive file collision: ' + name)
            folded[lower] = name
        for name in folded:
            pieces = name.split('/')
            if any('/'.join(pieces[:index]) in folded for index in range(1, len(pieces))):
                raise MergeConflictError('File/directory collision: ' + name)
        with tempfile.TemporaryDirectory(prefix='statetree-merge-index-') as directory:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'))
            self.workspace._git('read-tree', '--empty', env=env)
            raw = b''.join((mode + ' ' + blob + '\t' + name + '\x00').encode('utf-8')
                           for name, (mode, blob) in sorted(merged.items()))
            self.workspace._git('update-index', '-z', '--index-info', env=env, input=raw)
            tree = self.workspace._git('write-tree', env=env)
        # Turn the merged tree into a genuine GitWorkspace snapshot before using it.
        commit = self.workspace._commit_tree(tree, [current['workspace_sha']], 'StateTree integrated files\n')
        path = self.worktrees / ('integrated-' + uuid4().hex)
        self.workspace._git('worktree', 'add', '--detach', str(path), commit)
        sha = GitWorkspace(path).snapshot()
        return path, sha

    def integrate(self, candidate_id, *, expected_parent):
        """Rebase against current files/state, verify, then CAS-publish canonical.

        The candidate base may be older than ``expected_parent``. Disjoint
        changes merge; stale declared reads/writes and overlapping changes
        fail. Verification runs outside the SQLite transaction; a final
        expected-parent check prevents racing publication.
        """
        current = self.head()
        if current is None or current['id'] != expected_parent:
            raise StaleParentError('Canonical parent changed before integration')
        candidate = self._load(candidate_id, 'candidate')
        base = self._load(candidate['base_id'], 'revision')
        self._check_resources(current, candidate['reads'], candidate['writes'])
        state = merge_json(base['state'], current['state'], candidate['state'])
        path, sha = self._merge_files(base, current, candidate)
        self.verify_candidate(candidate_id)
        self._verify(path, sha, state, 'integrated')
        resources = _json_copy(current['resources'])
        for name in candidate['writes']:
            if not name.startswith('file:'):
                resources[name] = digest({'prior': resources.get(name), 'candidate': candidate_id})
        revision = self._save({
            'kind': 'revision', 'parent': current['id'], 'state': state, 'resources': resources,
            'workspace_sha': sha, 'workspace_path': str(path), 'source_candidate': candidate_id,
        })
        with self.store.transaction() as db:
            if self._head_id(db) != expected_parent:
                raise StaleParentError('Canonical parent changed during verification')
            db.execute("UPDATE parallel_refs SET value=? WHERE name='canonical'", (revision['id'],))
        return revision
