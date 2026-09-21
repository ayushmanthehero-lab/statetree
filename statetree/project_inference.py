"""Explicit real-Strands local inference; never a synthetic model fallback.

The project checkpoint holds only public JSON. The SDK owns the live agent
loop; StateTree prepares its context and records usage through the existing
real SDK hooks. Network access is restricted to an operator-bound loopback URL.
"""
from dataclasses import asdict
import os

from statetree.adapters.portable import _apply_strands_state
from statetree.core.state import json_copy


def run_turn(project, prompt, *, new_task=False):
    if type(prompt) is not str or not prompt.strip() or len(prompt.encode('utf-8')) > 262144:
        raise ValueError('Supply a nonempty prompt of at most 256 KiB')
    if type(new_task) is not bool:
        raise ValueError('new_task must be boolean')
    try:
        from strands import Agent
        from strands.hooks import BeforeModelCallEvent
        from statetree.adapters.strands import StateTreeHooks, archive_reader
        from statetree.models.sagemaker import SageMakerModel
    except ImportError as error:
        raise RuntimeError('Real inference requires Strands: install this project with python -m pip install -e .; no model request was made') from error
    from statetree.web.app import LocalClient

    config = project.model_config
    model = SageMakerModel(
        'statetree-local', model_id=config['model_id'], max_tokens=config['max_tokens'],
        context_window_limit=config['context_window_limit'], temperature=0,
        client=LocalClient(config['url'], os.environ.get('STATETREE_LOCAL_API_KEY', ''),
                           timeout=float(os.environ.get('STATETREE_LOCAL_TIMEOUT', '55'))),
    )
    prior = project.agent.state.get('statetree_prior_history')
    if new_task and project.agent.messages:
        prior = project.store.put_archive({'kind': 'previous-task-history',
                                          'messages': project.agent.messages,
                                          'prior_history_archive': prior})
    history = [] if new_task else json_copy(project.agent.messages)
    hooks = StateTreeHooks(
        store=project.store, ledger=project.ledger, goal=project.state.goal,
        run_id=project.config['run_id'], branch='main', phase='project-chat',
        # A conservative byte estimate, not a model tokenizer. Reserve output
        # space and provider framing even when the configured budget is larger.
        max_input_tokens=min(project.config['context_budget'],
                             config['context_window_limit'] - config['max_tokens']),
        recent_turns=project.config['recent_turns'], state=project._prompt_state,
        total_token_limit=project.config['total_token_limit'],
        input_cache_convention=project.config['input_cache_convention'],
        commit_memory=project.runtime.commit_memory, commit_head=lambda: project.runtime._parent,
        commit_context_budget=project.config['note_budget'],
    )

    class LoopBound:
        def __init__(self):
            self.calls = 0

        def register_hooks(self, registry, **kwargs):
            registry.add_callback(BeforeModelCallEvent, self.before_model, order=100)

        def before_model(self, event):
            if self.calls >= 24:
                raise RuntimeError('Local turn stopped at the 24-model-call safety threshold')
            self.calls += 1

    agent = Agent(model=model, messages=history, system_prompt=project.agent.system_prompt,
                  tools=[archive_reader(project.store)], hooks=[LoopBound(), hooks],
                  context_manager=False, callback_handler=None)
    _apply_strands_state(agent, project.state)
    if prior is not None:
        agent.state.set('statetree_prior_history', prior)
    requests_before = project.usage()['requests']
    before = project.agent.take_snapshot()
    try:
        result = agent(prompt)
        answer = str(result)
        project.agent.messages = json_copy(agent.messages)
        if prior is not None:
            project.agent.state.set('statetree_prior_history', prior)
        project._retain_archives(prior)
        # Validates complete tool exchanges before publishing any checkpoint.
        project.agent.take_snapshot()
        commit = project.runtime.commit(note={
            'summary': 'Conversation: ' + prompt[:1200] + '\nResponse: ' + answer[:2400],
            'outcome': 'completed',
        })
    except Exception as error:
        # The public checkpoint remains a resumable complete conversation. Raw
        # failed-attempt observations and observed usage are not erased.
        project.agent.load_snapshot(before)
        archive = project.store.put_archive({'kind': 'failed-model-turn', 'prompt': prompt,
                                             'messages': json_copy(agent.messages),
                                             'prior_history_archive': prior,
                                             'error_type': type(error).__name__})
        project._retain_archives(archive, prior)
        project.runtime.commit(note={'summary': 'Model turn failed; no completed reply checkpointed',
                                     'evidence_ids': [archive], 'outcome': 'failed'})
        raise RuntimeError(f'Model turn failed ({type(error).__name__}); evidence archive: {archive}. '
                           'Recorded usage is retained; check the endpoint and token threshold.') from error
    usage = project.usage()
    return {'answer': answer, 'checkpoint_id': commit.id,
            'provider_requests_this_turn': usage['requests'] - requests_before,
            'usage': usage, 'usage_scope': 'entire project; never rolled back',
            'context': asdict(hooks.last_context) if hooks.last_context else None,
            'recall': asdict(hooks.last_recall) if hooks.last_recall else None,
            'verification': 'unverified model reply; not a correctness guarantee'}
