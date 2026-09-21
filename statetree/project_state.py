"""Public-state snapshots for local project management, not an inference model.

The SDK is deliberately optional for inspecting, checkpointing and recovering a
project. Real inference is delegated to Strands by project_inference.py.
"""
from dataclasses import dataclass

from statetree.core.state import AgentState, json_copy


class StateValues:
    def __init__(self, values=None):
        self.values = json_copy(values or {})

    def get(self, key, default=None):
        return json_copy(self.values.get(key, default))

    def set(self, key, value):
        self.values[key] = json_copy(value)

    def delete(self, key):
        self.values.pop(key, None)


@dataclass(frozen=True)
class PublicSnapshot:
    data: dict

    def to_dict(self):
        return {'format': 'statetree.public-session', 'schema_version': 1,
                'data': json_copy(self.data)}

    @classmethod
    def from_dict(cls, value):
        value = json_copy(value)
        if (type(value) is not dict or set(value) != {'format', 'schema_version', 'data'}
                or value['format'] != 'statetree.public-session'
                or type(value['schema_version']) is not int or value['schema_version'] != 1):
            raise ValueError('Not a supported StateTree public-session snapshot')
        data = value['data']
        if (type(data) is not dict or set(data) != {'state', 'messages', 'system_prompt'}
                or type(data['state']) is not dict or type(data['messages']) is not list
                or type(data['system_prompt']) is not str):
            raise ValueError('Malformed public-session snapshot')
        AgentState.from_dict(data['state'].get('statetree'))
        # Completed tool exchanges only: never resume half an unrecorded effect.
        from statetree.context.builder import _message_groups
        _message_groups(data['messages'])
        return cls(data)


class PublicStateAgent:
    """Snapshot-capable public data holder. This class never invents an answer."""
    def __init__(self, goal, system_prompt=''):
        self.state = StateValues({'statetree': AgentState(goal=goal).to_dict()})
        self.messages = []
        self.system_prompt = system_prompt

    def take_snapshot(self, **kwargs):
        return PublicSnapshot.from_dict(PublicSnapshot({
            'state': self.state.values, 'messages': self.messages,
            'system_prompt': self.system_prompt}).to_dict())

    def load_snapshot(self, snapshot):
        data = PublicSnapshot.from_dict(snapshot.to_dict()).data
        self.state = StateValues(data['state'])
        self.messages = data['messages']
        self.system_prompt = data['system_prompt']
