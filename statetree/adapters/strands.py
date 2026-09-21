"""Strands 1.56 text/tool agent hooks for context and per-request usage.

Direct structured_output() and model calls outside the agent loop do not emit
these model hooks. Their usage must be recorded separately in UsageLedger.
"""

import copy
import json
from uuid import uuid4

from strands import tool
from strands.hooks import (
    AfterInvocationEvent, AfterModelCallEvent, BeforeInvocationEvent,
    BeforeModelCallEvent, MessageAddedEvent,
)

from statetree.context import ContextBuilder


def archive_reader(store):
    @tool
    def statetree_read_archive(archive_id: str, offset: int = 0, limit: int = 1000) -> dict:
        """Read a page of archived evidence using its StateTree archive ID.

        Args:
            archive_id: Full content hash from an archived-result reference.
            offset: Starting character position, initially zero.
            limit: Characters to retrieve, between 1 and 4096.
        """
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError('Invalid archive page bounds')
        text = json.dumps(store.read_archive(archive_id), ensure_ascii=False, sort_keys=True)
        end = min(len(text), offset + limit)
        return {'archive_id': archive_id, 'offset': offset, 'content': text[offset:end],
                'next_offset': end if end < len(text) else None, 'total_characters': len(text)}
    return statetree_read_archive


class StateTreeHooks:
    def __init__(self, *, store, ledger, goal, run_id=None, branch='main',
                 max_input_tokens=None, recent_turns=4, counter=None,
                 state=None, total_token_limit=None, input_cache_convention='auto',
                 phase='agent', commit_memory=None, commit_head=None, commit_context_budget=0):
        if total_token_limit is not None and (type(total_token_limit) is not int or total_token_limit <= 0):
            raise ValueError('total_token_limit must be a positive integer')
        self.store = store
        self.ledger = ledger
        self.goal = goal
        self.run_id = run_id or uuid4().hex
        self.branch = branch
        self.state = state
        self.phase = phase
        self.input_cache_convention = input_cache_convention
        self.total_token_limit = total_token_limit
        self.builder = None if max_input_tokens is None else ContextBuilder(
            max_input_tokens, recent_turns=recent_turns, counter=counter, archive=store.put_archive,
        )
        self.last_context = None
        self.last_request_archive = None
        self._base_system = None
        self._request_id = None
        self._provider_usage_start = None
        self._agent = None
        self.commit_memory = commit_memory
        self.commit_head = commit_head
        self.commit_context_budget = commit_context_budget
        self.last_recall = None
        self._completed_responses = 0
        self._before_model_event = None
        self._commit_query = None

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeInvocationEvent, self.before_invocation, order=200)
        registry.add_callback(BeforeModelCallEvent, self.before_model, order=200)
        # Priorities stay ascending; only equal-priority registration reverses.
        registry.add_callback(AfterModelCallEvent, self.after_model, order=200)
        registry.add_callback(MessageAddedEvent, self.message_added)
        registry.add_callback(AfterInvocationEvent, self.after_invocation, order=200)

    def before_invocation(self, event):
        if self._agent is not None and self._agent is not event.agent:
            raise ValueError('Use one StateTreeHooks instance per agent')
        self._agent = event.agent
        self._commit_query = None
        self.last_recall = None
        self._before_model_event = None
        if self.builder:
            if event.agent.model.stateful:
                raise ValueError('StateTree context preparation requires a stateless model provider')
            content = event.agent.system_prompt_content or []
            if any(set(block) != {'text'} for block in content):
                raise ValueError('StateTree currently supports plain-text system prompts only')
            self._base_system = event.agent.system_prompt

    def before_model(self, event):
        self._before_model_event = event
        self._request_id = None
        self._provider_usage_start = (
            event.agent.model, getattr(event.agent.model, 'provider_call_sequence', None))
        if self.total_token_limit is not None:
            totals = self.ledger.summary(run_id=self.run_id)
            if totals['unknown_usage_requests'] or totals['ambiguous_usage_requests']:
                raise RuntimeError('Cannot enforce cumulative limit with unknown or ambiguous prior usage')
            if totals['total_tokens'] >= self.total_token_limit:
                raise RuntimeError('Cumulative token limit reached')
        if event.cancel:
            return
        # Keep evidence before mutating the active messages.
        self.last_request_archive = self.store.put_archive(copy.deepcopy(event.agent.messages))
        if self.builder:
            state = self.state() if callable(self.state) else self.state
            if state is None:
                state = event.agent.state.get('statetree_context') or {}
            notes = None
            if self.commit_memory is not None:
                if self._commit_query is None:
                    latest = next((message for message in reversed(event.agent.messages)
                                   if message['role'] == 'user' and not any(
                                       'toolResult' in block for block in message['content'])), None)
                    self._commit_query = ' '.join(block['text'] for block in latest['content']
                                                  if 'text' in block) if latest else ''
                self.last_recall = self.commit_memory.search(
                    self._commit_query, self.commit_head(), budget=self.commit_context_budget,
                    counter=self.builder.counter, resources={
                        key: value for key, value in state.get('resources', {}).items()
                        if isinstance(value, str) and value.strip()})
                notes = self.last_recall.notes
            prior_archive = event.agent.state.get('statetree_prior_history')
            if prior_archive is not None:
                self.store.read_archive(prior_archive)
            self.last_context = self.builder.build(
                event.agent.messages, goal=self.goal, state=state,
                system_prompt=self._base_system or '',
                tool_specs=event.agent.tool_registry.get_all_tool_specs(),
                commit_notes=notes, prior_history_archive=prior_archive,
            )
            event.agent.messages[:] = self.last_context.messages
            event.agent.system_prompt = self.last_context.system_prompt
        self._request_id = uuid4().hex

    def after_model(self, event):
        # The same event object reflects cancellations by later application
        # hooks, which Strands represents as synthetic stop responses.
        if self._before_model_event is not None and self._before_model_event.cancel:
            self._request_id = None
            return
        if event.stop_response:
            self._completed_responses += 1
        if self._request_id is None:
            return  # A preparation/cancellation error did not invoke the model.
        usage = None
        if event.stop_response:
            usage = event.stop_response.message.get('metadata', {}).get('usage')
        model = event.agent.model
        started = self._provider_usage_start
        current_call = (started is not None and started[0] is model and started[1] is not None
                        and started[1] != getattr(model, 'provider_call_sequence', None))
        # Preserve valid provider usage even for rejected response content. The
        # sequence check prevents reusing the previous response when a hook or
        # middleware fails before this stream begins. Strands itself fills absent
        # counts with zeros, so completed responses also need the raw contract.
        if hasattr(model, 'last_provider_usage'):
            if getattr(model, 'provider_call_sequence', None) is not None:
                usage = copy.deepcopy(model.last_provider_usage) if current_call else None
            elif event.stop_response:
                usage = copy.deepcopy(model.last_provider_usage)
        config = event.agent.model.get_config()
        model_id = str(config.get('model_id', '')) if isinstance(config, dict) else type(event.agent.model).__name__
        self.ledger.record(
            self._request_id, run_id=self.run_id, branch=self.branch, phase=self.phase,
            model=model_id, usage=usage,
            status='error' if event.exception else 'retry' if event.retry else 'ok',
            input_cache_convention=self.input_cache_convention,
        )
        self._request_id = None

    def message_added(self, event):
        # Raw observations remain available even after active-history truncation.
        self.store.put_archive(copy.deepcopy(event.message))

    def after_invocation(self, event):
        if self.builder and self._agent is event.agent:
            event.agent.system_prompt = self._base_system
