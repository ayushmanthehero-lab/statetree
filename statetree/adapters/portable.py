"""Public-state adapters. Applications explicitly consume the statetree field.

These adapters do not serialize framework schedulers, credentials, tools, or
private model sessions. Call only at a quiescent application step boundary.
"""

from collections.abc import MutableMapping
from typing import Protocol

from statetree.core.state import AgentState
from statetree.storage.portable import export_bundle, import_bundle


class StateAdapter(Protocol):
    def export_state(self) -> AgentState: ...
    def import_state(self, state: AgentState) -> None: ...


class MappingStateAdapter:
    """Adapter for a custom agent loop's public mutable state mapping."""
    def __init__(self, mapping, *, key='statetree'):
        if not isinstance(mapping, MutableMapping):
            raise ValueError('Expected a mutable application state mapping')
        self.mapping, self.key = mapping, key

    def export_state(self):
        if self.key not in self.mapping:
            raise ValueError('Initialize portable state before exporting it')
        return AgentState.from_dict(self.mapping[self.key])

    def import_state(self, state):
        self.mapping[self.key] = state.to_dict()


class StrandsStateAdapter:
    def __init__(self, agent):
        self.agent = agent

    def export_state(self):
        data = self.agent.state.get('statetree')
        if data is None:
            raise ValueError('Initialize portable state before exporting it')
        return AgentState.from_dict(data)

    def import_state(self, state):
        owner_ref = getattr(self.agent, '_statetree_runtime_ref', None)
        owner = owner_ref() if owner_ref is not None else None
        if owner is not None:
            return owner.set_state(state)
        if getattr(self.agent, '_statetree_runtime_attached', False):
            raise RuntimeError('The attached runtime no longer exists; bind a fresh agent and runtime')
        _apply_strands_state(self.agent, state)


def _apply_strands_state(agent, state):
    """Internal binding; an attached runtime holds its operation lock."""
    data = state.to_dict()
    context = state.prompt_state()
    old_state = agent.state.get('statetree')
    old_context = agent.state.get('statetree_context')
    try:
        agent.state.set('statetree', data)
        agent.state.set('statetree_context', context)
    except Exception:
        for key, old in (('statetree', old_state), ('statetree_context', old_context)):
            if old is None:
                agent.state.delete(key)
            else:
                agent.state.set(key, old)
        raise


class LangGraphStateAdapter:
    """A checkpointer-backed graph must declare a replace-on-write state key.

    The caller controls the graph's continuation node/config. Scheduler state
    is deliberately not transplanted between unrelated graph definitions.
    """
    def __init__(self, graph, config, *, key='statetree', as_node=None):
        self.graph, self.config, self.key, self.as_node = graph, config, key, as_node

    def export_state(self):
        snapshot = self.graph.get_state(self.config)
        if self.key not in snapshot.values:
            raise ValueError('Graph has no initialized portable state')
        return AgentState.from_dict(snapshot.values[self.key])

    def import_state(self, state):
        kwargs = {} if self.as_node is None else {'as_node': self.as_node}
        updated = self.graph.update_state(self.config, {self.key: state.to_dict()}, **kwargs)
        # update_state returns a config pinned to the imported checkpoint.
        # Follow the thread after continuation rather than re-exporting that
        # stale checkpoint forever.
        self.config = {**updated, 'configurable': {
            key: value for key, value in updated.get('configurable', {}).items()
            if key != 'checkpoint_id'}}


class CrewAIFlowStateAdapter:
    """Adapt a Flow's dictionary state or a declared Pydantic state field."""
    def __init__(self, flow, *, key='statetree'):
        self.flow, self.key = flow, key

    def export_state(self):
        values = self.flow.state
        value = values.get(self.key) if isinstance(values, MutableMapping) else getattr(values, self.key, None)
        if value is None:
            raise ValueError('Flow has no initialized portable state')
        return AgentState.from_dict(value)

    def import_state(self, state):
        data = state.to_dict()
        values = self.flow.state
        if isinstance(values, MutableMapping):
            values[self.key] = data
        else:
            if self.key not in getattr(type(values), 'model_fields', {}):
                raise ValueError('Structured Flow state must declare the portable state field')
            setattr(values, self.key, data)


def transfer_state(source, destination, *, source_store=None, destination_store=None):
    """Transfer only the defined public schema and explicitly referenced blobs."""
    state = import_bundle(export_bundle(source.export_state(), source_store), destination_store)
    destination.import_state(state)
    return state
