"""Quiescent Strands model handoff through compact public application state."""

import copy

from strands.models import Model

from statetree.adapters.portable import StrandsStateAdapter
from statetree.context import ContextBuilder
from statetree.context.builder import _message_groups


def _model_id(model):
    config = model.get_config()
    return str(config.get('model_id', type(model).__name__))


def switch_model(runtime, model, *, max_input_tokens=None, counter=None):
    """Called under Runtime's operation lock; never invokes either provider."""
    agent = runtime.agent
    if not isinstance(model, Model):
        raise ValueError('Bind a configured Strands Model instance, not a model name')
    if model.stateful or agent.model.stateful:
        raise ValueError('Private stateful provider sessions cannot be migrated')
    if runtime.hooks is None or runtime.hooks.builder is None:
        raise ValueError('Model handoff requires bounded StateTree context management')
    if any(set(block) != {'text'} for block in (agent.system_prompt_content or [])):
        raise ValueError('Model handoff supports plain-text system prompts')
    _message_groups(agent.messages)  # Refuse in-flight tool transactions.
    state = StrandsStateAdapter(agent).export_state()
    existing = runtime.hooks.builder
    budget = existing.max_input_tokens if max_input_tokens is None else max_input_tokens
    count = existing.counter if counter is None else counter
    # Validate the complete compact request overhead, including the tools.
    builder = ContextBuilder(budget, recent_turns=existing.recent_turns,
                             counter=count, archive=runtime.store.put_archive)
    prepared = builder.build([], goal=state.goal, state=state.prompt_state(),
                             system_prompt=agent.system_prompt or '',
                             tool_specs=agent.tool_registry.get_all_tool_specs())
    # Resolve all declared evidence before changing the active model.
    for identifier in state.archive_ids:
        runtime.store.read_archive(identifier)
    old_model, old_messages = agent.model, copy.deepcopy(agent.messages)
    old_context = copy.deepcopy(agent.state.get('statetree_context'))
    old_goal, old_hook_goal = runtime.goal, runtime.hooks.goal
    from_model, to_model = _model_id(old_model), _model_id(model)
    old_parent = runtime._parent
    checkpoint = runtime._commit(current_subgoal='before model handoff')
    receipt = {'kind': 'model-handoff', 'checkpoint_id': checkpoint.id,
               'from_model': from_model, 'to_model': to_model,
               'estimated_input_tokens': prepared.estimated_input_tokens,
               'counting_method': prepared.counting_method}
    try:
        agent.model = model
        agent.messages[:] = []
        agent.state.set('statetree_context', state.prompt_state())
        runtime.goal = runtime.hooks.goal = state.goal
        runtime.hooks.builder = builder
        receipt['archive_id'] = runtime.store.put_archive(receipt)
    except Exception:
        agent.model = old_model
        agent.messages[:] = old_messages
        if old_context is None:
            agent.state.delete('statetree_context')
        else:
            agent.state.set('statetree_context', old_context)
        runtime.goal, runtime.hooks.goal = old_goal, old_hook_goal
        runtime.hooks.builder = existing
        # Undo only this operation's reference publication. Never rewind a
        # different writer that advanced after our pre-handoff checkpoint.
        with runtime.store.transaction() as db:
            if runtime.store.get_head(runtime.branch, db=db) != checkpoint.id:
                raise RuntimeError('Handoff failed and another writer advanced; '
                                   f'pre-handoff checkpoint retained: {checkpoint.id}')
            if old_parent is None:
                db.execute('DELETE FROM refs WHERE name=?', ('branch:' + runtime.branch,))
            else:
                runtime.store.move_head(db, runtime.branch, old_parent, expected_parent=checkpoint.id)
        runtime._parent = old_parent
        raise
    return receipt
