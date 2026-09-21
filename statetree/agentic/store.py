"""SQLite intent/receipt journal, independent of rewound Project snapshots."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from statetree.agent.files import reject_link


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_:\-]{1,160}', value):
        raise ValueError('Invalid task/operation identifier')
    return value


class TaskStore:
    def __init__(self, repo):
        self.repo = Path(repo).absolute()
        for p in (self.repo, *self.repo.parents):
            reject_link(p)
        self.root = self.repo / '.statetree' / 'project' / 'agentic'
        for p in (self.repo / '.statetree', self.root.parent, self.root):
            reject_link(p)
            p.mkdir(exist_ok=True)
        self.path = self.root / 'tasks.sqlite3'
        reject_link(self.path)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, prompt_key TEXT NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                task_id TEXT NOT NULL, tool TEXT NOT NULL, arguments TEXT NOT NULL, intent TEXT NOT NULL,
                dispatched INTEGER NOT NULL DEFAULT 0, receipt TEXT, checkpoint_id TEXT, resolution TEXT);
            CREATE INDEX IF NOT EXISTS operations_task ON operations(task_id,seq);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                kind TEXT NOT NULL, value TEXT NOT NULL, created TEXT NOT NULL);
            ''')
            if 'resolution' not in {r[1] for r in db.execute('PRAGMA table_info(operations)')}:
                db.execute('ALTER TABLE operations ADD COLUMN resolution TEXT')

    @contextmanager
    def db(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            if write:
                db.execute('BEGIN IMMEDIATE')
            with db:
                yield db

    def submit(self, prompt, *, new_task=False, allow_execution=False):
        if type(prompt) is not str or not prompt.strip() or len(prompt.encode()) > 12000:
            raise ValueError('Task must contain 1-12000 UTF-8 bytes. Very long goals may exceed the model context.')
        if type(new_task) is not bool or type(allow_execution) is not bool:
            raise ValueError('new_task and allow_execution must be booleans')
        key = hashlib.sha256(' '.join(prompt.split()).encode()).hexdigest()
        with self.db(write=True) as db:
            if not new_task:
                old = db.execute('SELECT value FROM tasks WHERE prompt_key=? ORDER BY rowid DESC LIMIT 1', (key,)).fetchone()
                if old:
                    # A resend cannot silently escalate the existing task's permissions.
                    return json.loads(old[0])
            value = dict(id=uuid4().hex, prompt=prompt.strip(), status='queued', allow_execution=allow_execution,
                         created_at=now(), updated_at=now(), pause_requested=False, steps=0, model_calls=0,
                         checkpoint_id=None, pending=None, final='', error='', note={}, plan={},
                         last_receipt=None, model_request=None, response_record=None, limit=100)
            db.execute('INSERT INTO tasks VALUES (?,?,?)', (value['id'], key, encode(value)))
            return value

    def task(self, task_id):
        identity(task_id)
        with self.db() as db:
            row = db.execute('SELECT value FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise KeyError('Unknown local task')
        return json.loads(row[0])

    def tasks(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Task limit must be 1-200')
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT value FROM tasks ORDER BY rowid DESC LIMIT ?', (limit,))]

    def patch(self, task_id, **fields):
        identity(task_id)
        with self.db(write=True) as db:
            row = db.execute('SELECT value FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None: raise KeyError('Unknown task')
            value = json.loads(row[0])
            if set(fields) - set(value) or {'id','prompt','allow_execution','created_at'} & set(fields):
                raise ValueError('Unsupported task update')
            value.update(fields, updated_at=now())
            db.execute('UPDATE tasks SET value=? WHERE id=?', (encode(value), task_id))
            return value

    @staticmethod
    def _operation(row):
        if row is None: return None
        value = dict(row)
        for field in ('arguments','intent','receipt','resolution'):
            value[field] = json.loads(value[field]) if value[field] is not None else None
        return value

    def operation(self, op_id):
        identity(op_id)
        with self.db() as db:
            return self._operation(db.execute('SELECT * FROM operations WHERE id=?', (op_id,)).fetchone())

    def operations(self, task_id):
        identity(task_id)
        with self.db() as db:
            return [self._operation(row) for row in db.execute('SELECT * FROM operations WHERE task_id=? ORDER BY seq', (task_id,))]

    def prepare(self, task_id, op_id, tool, arguments, intent=None):
        identity(task_id); identity(op_id)
        self.task(task_id)
        with self.db(write=True) as db:
            old = self._operation(db.execute('SELECT * FROM operations WHERE id=?', (op_id,)).fetchone())
            if old:
                if (old['task_id'],old['tool'],old['arguments']) != (task_id,tool,arguments):
                    raise ValueError('Operation identity conflict')
                return old
            db.execute('INSERT INTO operations(id,task_id,tool,arguments,intent) VALUES (?,?,?,?,?)',
                       (op_id,task_id,tool,encode(arguments),encode(intent or {})))
        return self.operation(op_id)

    def unresolved_command(self):
        # Query all operations, not only the visible task-history page.
        with self.db() as db:
            for row in db.execute("SELECT * FROM operations WHERE tool='run_command' AND resolution IS NULL ORDER BY seq"):
                op=self._operation(row)
                if op['dispatched'] and op['receipt'] is None or (op['receipt'] or {}).get('partial_effects_possible'):
                    return op
        return None

    def mark_dispatched(self, op_id):
        with self.db(write=True) as db:
            db.execute('UPDATE operations SET dispatched=1 WHERE id=? AND receipt IS NULL', (identity(op_id),))

    def finish(self, op_id, receipt):
        payload = encode(receipt)
        with self.db(write=True) as db:
            row=db.execute('SELECT receipt FROM operations WHERE id=?',(identity(op_id),)).fetchone()
            if row is None: raise KeyError('Unknown operation')
            if row[0] is not None and row[0] != payload:
                raise ValueError('A completed receipt cannot be overwritten')
            db.execute('UPDATE operations SET receipt=? WHERE id=?',(payload,op_id))
        return receipt

    def checkpointed(self, op_id, checkpoint_id):
        with self.db(write=True) as db:
            db.execute('UPDATE operations SET checkpoint_id=? WHERE id=? AND receipt IS NOT NULL', (checkpoint_id,identity(op_id)))

    def event(self, task_id, kind, value):
        with self.db(write=True) as db:
            db.execute('INSERT INTO events(task_id,kind,value,created) VALUES (?,?,?,?)',
                       (identity(task_id),kind,encode(value),now()))

    def events(self, task_id, *, after=0, limit=100):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=200:
            raise ValueError('Invalid event page')
        with self.db() as db:
            rows=db.execute('SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?',
                            (identity(task_id),after,limit)).fetchall()
        return [dict(seq=r['seq'],kind=r['kind'],value=json.loads(r['value']),created_at=r['created']) for r in rows]

    def resolve(self, op_id, note):
        if type(note) is not str or not 10 <= len(note.strip()) <= 3000:
            raise ValueError('Describe the inspected outcome in 10-3000 characters')
        with self.db(write=True) as db:
            row = db.execute('SELECT resolution FROM operations WHERE id=?', (identity(op_id),)).fetchone()
            if row is None: raise KeyError('Unknown operation')
            if row[0] is not None: raise ValueError('Operation is already reconciled')
            resolution = {'source':'operator','note':note.strip(),'created_at':now()}
            db.execute('UPDATE operations SET resolution=? WHERE id=?', (encode(resolution),op_id))
        return resolution
