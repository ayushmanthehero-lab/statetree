"""Small SQLite intent/receipt journal; commit before any externally visible effect."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);'
                             'CREATE TABLE IF NOT EXISTS steps (id TEXT PRIMARY KEY, kind TEXT NOT NULL, '
                             'intent TEXT NOT NULL, receipt TEXT);'
                             'CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, value TEXT NOT NULL);')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def get(self, key, default=None):
        with closing(self.connect()) as db:
            value = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            return json.loads(value[0]) if value else default

    def set(self, key, value):
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, encode(value)))

    def step(self, identifier):
        with closing(self.connect()) as db:
            value = db.execute('SELECT kind,intent,receipt FROM steps WHERE id=?', (identifier,)).fetchone()
            return ({'id': identifier, 'kind': value[0], 'intent': json.loads(value[1]),
                     'receipt': json.loads(value[2]) if value[2] else None} if value else None)

    def begin(self, identifier, kind, intent):
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO steps VALUES (?,?,?,NULL)', (identifier, kind, encode(intent)))

    def finish(self, identifier, receipt):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE steps SET receipt=? WHERE id=?', (encode(receipt), identifier))

    def retry_unperformed(self, identifier):
        with closing(self.connect()) as db, db:
            current = db.execute('SELECT receipt FROM steps WHERE id=?', (identifier,)).fetchone()
            if not current or not current[0] or not json.loads(current[0]).get('not_dispatched'):
                raise ValueError('Only an explicitly unperformed effect can be retried.')
            db.execute('UPDATE steps SET receipt=NULL WHERE id=?', (identifier,))

    def steps(self):
        with closing(self.connect()) as db:
            return [{'id': row[0], 'kind': row[1], 'intent': json.loads(row[2]),
                     'receipt': json.loads(row[3]) if row[3] else None}
                    for row in db.execute('SELECT id,kind,intent,receipt FROM steps ORDER BY rowid')]

    def event(self, event):
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO events VALUES (?,?)', (event['id'], encode(event)))

    def events(self):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT value FROM events ORDER BY rowid')]
