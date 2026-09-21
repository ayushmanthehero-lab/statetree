"""Immutable files with transactional branch references in SQLite."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from statetree.core.commit import (
    StateTreeCommit, canonical_json, digest, validate_branch, validate_id,
)


class HeadConflictError(RuntimeError):
    pass


class LocalStateStore:
    def __init__(self, root='.statetree'):
        self.root = Path(root).resolve()
        self.commits_dir = self.root / 'commits'
        self.snapshots_dir = self.root / 'snapshots'
        self.archive_dir = self.root / 'archive'
        for directory in (self.commits_dir, self.snapshots_dir, self.archive_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'metadata.sqlite3'
        if (self.root / 'HEAD').exists() and not self.database.exists():
            raise ValueError('Legacy StateTree store: use a new store directory; migration is not automatic')
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS refs (name TEXT PRIMARY KEY, commit_id TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def bind_workspace(self, path):
        identity = str(Path(path).resolve())
        with self.transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE name='workspace'").fetchone()
            if row and row[0] != identity:
                raise ValueError('This store is bound to a different workspace')
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('workspace', ?)", (identity,))

    def _write_immutable(self, path, value):
        raw = canonical_json(value)
        fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise ValueError('Immutable object already exists with different bytes')
        finally:
            Path(name).unlink(missing_ok=True)

    def put_snapshot(self, value):
        blob_id = digest(value)
        self._write_immutable(self.snapshots_dir / (blob_id + '.json'), value)
        return blob_id

    def put_archive(self, value):
        blob_id = digest(value)
        self._write_immutable(self.archive_dir / (blob_id + '.json'), value)
        return blob_id

    def read_archive(self, blob_id):
        return self._read_blob(self.archive_dir, blob_id)

    def read_snapshot(self, blob_id):
        return self._read_blob(self.snapshots_dir, blob_id)

    def _read_blob(self, directory, blob_id):
        validate_id(blob_id)
        try:
            data = json.loads((directory / (blob_id + '.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise ValueError('Checkpoint blob is missing or invalid') from error
        if digest(data) != blob_id:
            raise ValueError('Checkpoint blob failed its content hash check')
        return data

    def save_commit(self, commit):
        commit.validate()
        self._write_immutable(self.commits_dir / (commit.id + '.json'), commit.to_dict())

    def load_commit(self, commit_id):
        validate_id(commit_id)
        try:
            data = json.loads((self.commits_dir / (commit_id + '.json')).read_text(encoding='utf-8'))
            commit = StateTreeCommit(**data)
            commit.validate()
        except (OSError, TypeError, ValueError) as error:
            raise ValueError('Checkpoint manifest is missing, unsupported or corrupt') from error
        if commit.id != commit_id:
            raise ValueError('Checkpoint filename does not match content')
        return data

    @staticmethod
    def _head(db, name):
        row = db.execute('SELECT commit_id FROM refs WHERE name=?', (name,)).fetchone()
        return row[0] if row else None

    def get_head(self, branch='main', *, db=None):
        validate_branch(branch)
        if db is not None:
            return self._head(db, 'branch:' + branch)
        with self.transaction() as connection:
            return self._head(connection, 'branch:' + branch)

    def get_canonical_head(self):
        with self.transaction() as db:
            return self._head(db, 'canonical')

    def move_head(self, db, branch, commit_id, *, expected_parent, promote=False):
        validate_branch(branch)
        data = self.load_commit(commit_id)
        actual = self.get_head(branch, db=db)
        if actual != expected_parent:
            raise HeadConflictError('Branch advanced since this runtime loaded; restore or reopen before committing')
        if promote and (branch != 'main' or data['verification_status'] != 'verified'):
            raise ValueError('Only verified main checkpoints can advance canonical state')
        db.execute('INSERT OR REPLACE INTO refs VALUES (?, ?)', ('branch:' + branch, commit_id))
        if promote:
            db.execute("INSERT OR REPLACE INTO refs VALUES ('canonical', ?)", (commit_id,))

    def fork(self, branch, commit_id):
        validate_branch(branch)
        self.load_commit(commit_id)
        with self.transaction() as db:
            if self._head(db, 'branch:' + branch) is not None:
                raise ValueError('Branch already exists')
            db.execute('INSERT INTO refs VALUES (?, ?)', ('branch:' + branch, commit_id))
