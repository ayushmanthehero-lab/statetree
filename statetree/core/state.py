"""Framework-neutral public application state, separate from opaque snapshots."""

from dataclasses import dataclass, field, fields
import json
import math
from typing import Any

from statetree.core.commit import canonical_json, validate_id


CAPABILITIES = frozenset({'json-state-v1', 'archive-v1'})


def json_copy(value):
    """Reject lossy JSON conversions before copying caller-owned values."""
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError('State must contain finite JSON values with string object keys')
    try:
        check(value)
        return json.loads(canonical_json(value))
    except (RecursionError, OverflowError) as error:
        raise ValueError('State must be an acyclic JSON value') from error


@dataclass(frozen=True)
class AgentState:
    goal: str
    constraints: list[str] = field(default_factory=list)
    subgoals: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    pending_actions: list[dict] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    checkpoints: list[str] = field(default_factory=list)
    archive_ids: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)
    required_capabilities: list[str] = field(default_factory=lambda: ['json-state-v1'])
    schema_version: int = 1

    def __post_init__(self):
        data = {item.name: getattr(self, item.name) for item in fields(self)}
        self._validate(data)
        for key, value in json_copy(data).items():
            object.__setattr__(self, key, value)

    @staticmethod
    def _validate(data):
        if type(data['schema_version']) is not int or data['schema_version'] != 1:
            raise ValueError('Unsupported portable state schema')
        if type(data['goal']) is not str or not data['goal'].strip():
            raise ValueError('State requires a nonempty goal')
        for key in ('constraints', 'subgoals', 'checkpoints', 'archive_ids', 'required_capabilities'):
            if type(data[key]) is not list or any(type(x) is not str or not x for x in data[key]):
                raise ValueError(f'{key} must be a list of nonempty strings')
        for key in ('facts', 'resources', 'artifacts', 'memory', 'extensions'):
            if type(data[key]) is not dict:
                raise ValueError(f'{key} must be an object')
        if type(data['pending_actions']) is not list or any(type(x) is not dict for x in data['pending_actions']):
            raise ValueError('pending_actions must be a list of action objects')
        if set(data['required_capabilities']) - CAPABILITIES:
            raise ValueError('Unsupported required state capabilities')
        if len(set(data['archive_ids'])) != len(data['archive_ids']):
            raise ValueError('Duplicate archive references')
        for identifier in data['archive_ids']:
            validate_id(identifier)
        json_copy(data)

    def to_dict(self):
        data = {item.name: getattr(self, item.name) for item in fields(self)}
        self._validate(data)
        return json_copy(data)

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict or 'goal' not in data or 'schema_version' not in data:
            raise ValueError('Portable state requires goal and schema_version')
        if set(data) - {item.name for item in fields(cls)}:
            raise ValueError('Unknown portable state fields; use extensions explicitly')
        try:
            return cls(**json_copy(data))
        except TypeError as error:
            raise ValueError('Malformed portable state') from error

    def prompt_state(self, *, max_input_tokens=None, counter=None):
        """Project active public state; budget is an estimate (UTF-8 bytes by default)."""
        data = self.to_dict()
        result = {key: data[key] for key in (
            'goal', 'constraints', 'subgoals', 'facts', 'pending_actions',
            'resources', 'artifacts', 'archive_ids')}
        result['extensions'] = {key: value for key, value in data['extensions'].items()
                                if not key.startswith('statetree.')}
        if max_input_tokens is not None:
            if type(max_input_tokens) is not int or max_input_tokens <= 0:
                raise ValueError('max_input_tokens must be a positive integer')
            serialized = canonical_json(result).decode('utf-8')
            estimate = counter(serialized) if counter else len(serialized.encode('utf-8'))
            if type(estimate) is not int or estimate < 0:
                raise ValueError('Counter must return a nonnegative integer')
            if estimate > max_input_tokens:
                raise ValueError('Required portable state exceeds the input estimate budget')
        return result
