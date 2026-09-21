"""Immutable commit notes and deterministic, ancestry-scoped retrieval."""

from dataclasses import dataclass, field, fields
import json
import re

from statetree.core.commit import canonical_json, digest, validate_id


def _text(value):
    if type(value) is not str or not value.strip():
        raise ValueError('Commit note text must be a nonempty string')
    return value


def _versions(value):
    if type(value) is not dict:
        raise ValueError('Resource versions must be a JSON object')
    return {_text(key): _text(version) for key, version in value.items()}


@dataclass(frozen=True)
class CommitNote:
    summary: str
    changes: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    dependencies: dict[str, str] = field(default_factory=dict)
    supersedes: list[str] = field(default_factory=list)
    outcome: str = 'completed'

    def __post_init__(self):
        for key, value in self.to_dict().items():
            object.__setattr__(self, key, value)

    def to_dict(self) -> dict:
        result = {'summary': _text(self.summary), 'outcome': _text(self.outcome)}
        if self.outcome not in ('completed', 'failed', 'partial'):
            raise ValueError('Invalid commit note outcome')
        for key in ('changes', 'decisions', 'pending', 'evidence_ids', 'supersedes'):
            value = getattr(self, key)
            if type(value) is not list:
                raise ValueError(f'{key} must be a JSON list of strings')
            result[key] = [_text(item) for item in value]
        for key in ('evidence_ids', 'supersedes'):
            for item in result[key]:
                validate_id(item)
        result['dependencies'] = _versions(self.dependencies)
        return result

    @classmethod
    def from_dict(cls, value) -> 'CommitNote':
        if (type(value) is not dict or 'summary' not in value
                or set(value) - {item.name for item in fields(cls)}):
            raise ValueError('Invalid commit note schema')
        return cls(**value)


@dataclass(frozen=True)
class NoteSelection:
    notes: list[dict]
    estimated_count: int
    counting_method: str


_STOPWORDS = frozenset('a an and are as at be been but by can could did do does '
                      'for from had has have how i if in into is it its may my '
                      'of on or our should so than that the their them then there '
                      'these they this those to use was we were what when where '
                      'which who will with would you your'.split())


def _terms(text):
    camel = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    acronyms = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', camel)
    return set(re.findall(r'[^\W_]+', f'{text} {camel} {acronyms}'.casefold())) - _STOPWORDS


def _score(note, query):
    weighted = [(note.summary, 4), (' '.join(note.changes), 2),
                (' '.join(note.decisions), 3), (' '.join(note.pending), 2),
                (' '.join(note.dependencies), 1)]
    return sum(len(query & _terms(text)) * weight for text, weight in weighted)


class CommitMemory:
    def __init__(self, store):
        self.store = store
        self.notes_dir = store.root / 'notes'

    def validate(self, note) -> CommitNote:
        """Copy a note and verify every referenced evidence archive."""
        note = CommitNote.from_dict(note.to_dict() if isinstance(note, CommitNote) else note)
        for evidence_id in note.evidence_ids:
            self.store.read_archive(evidence_id)
        return note

    def write(self, commit_id, note) -> None:
        self.store.load_commit(commit_id)
        note = self.validate(note)
        payload = {'schema_version': 1, 'commit_id': commit_id, 'note': note.to_dict()}
        envelope = {**payload, 'content_digest': digest(payload)}
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.store._write_immutable(self.notes_dir / (commit_id + '.json'), envelope)

    def read(self, commit_id) -> CommitNote | None:
        self.store.load_commit(commit_id)
        path = self.notes_dir / (commit_id + '.json')
        try:
            envelope = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise ValueError('Commit note is missing or corrupt') from error
        if (type(envelope) is not dict or set(envelope) != {
                'schema_version', 'commit_id', 'note', 'content_digest'}
                or type(envelope['schema_version']) is not int
                or envelope['schema_version'] != 1 or envelope['commit_id'] != commit_id):
            raise ValueError('Invalid commit note envelope or commit binding')
        expected = envelope.pop('content_digest')
        if digest(envelope) != expected:
            raise ValueError('Commit note failed its content hash check')
        return CommitNote.from_dict(envelope['note'])

    def search(self, query, head_id, *, budget, counter=None, resources=None,
               limit=8) -> NoteSelection:
        """Select whole relevant notes reachable from head within a JSON-list budget.

        Counts use canonical JSON text; the default is UTF-8 bytes, not tokens.
        An empty selection reports its scaffold count even if the budget is zero;
        callers can omit that empty scaffold entirely from the prompt.
        """
        if type(budget) is not int or budget < 0:
            raise ValueError('Commit note budget must be a nonnegative integer')
        if type(limit) is not int or limit <= 0:
            raise ValueError('Commit note limit must be a positive integer')
        if counter is not None and not callable(counter):
            raise ValueError('Commit note counter must be callable')
        if type(query) is not str:
            raise ValueError('Commit note query must be a string')
        resources = {} if resources is None else _versions(resources)
        method = 'custom_counter' if counter is not None else 'utf8_bytes_estimate'

        def count(notes):
            serialized = canonical_json(notes)
            amount = counter(serialized.decode('utf-8')) if counter is not None else len(serialized)
            if type(amount) is not int or amount < 0:
                raise ValueError('Commit note counter must return a nonnegative integer')
            return amount

        selected = []
        selected_count = count(selected)
        terms = _terms(query)
        if not terms or head_id is None or head_id == '' or budget == 0:
            return NoteSelection(selected, selected_count, method)

        candidates, seen, suppressed = [], set(), set()
        current = head_id
        while current is not None:
            if current in seen:
                raise ValueError('Checkpoint ancestry contains a cycle')
            seen.add(current)
            manifest = self.store.load_commit(current)
            note = self.read(current)
            if note is not None and all(resources.get(key) == version
                                        for key, version in note.dependencies.items()):
                if note.supersedes:
                    self.validate(note)
                    suppressed.update(note.supersedes)
                score = _score(note, terms)
                if current not in suppressed and score > 0:
                    item = dict(commit_id=current, workspace_sha=manifest['workspace_sha'],
                                verification_status=manifest['verification_status'], **note.to_dict())
                    candidates.append((score, len(seen), item, note))
            current = manifest['parent']

        candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
        for _, _, item, note in candidates:
            proposed_count = count([*selected, item])
            if proposed_count <= budget:
                self.validate(note)
                selected.append(item)
                selected_count = proposed_count
                if len(selected) == limit:
                    break
        return NoteSelection(selected, selected_count, method)
