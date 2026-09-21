import copy
import json
from pathlib import Path
import tempfile
import unittest

from strands import Agent, tool
from strands.hooks import BeforeInvocationEvent, BeforeModelCallEvent
from strands.types.exceptions import EventLoopException

from statetree.context import ContextBuilder, ContextBudgetError
from statetree.runtime.runtime import StateTreeRuntime
from tests.helpers import ScriptedModel, init_repo


def message(role, text):
    return {'role': role, 'content': [{'text': text}]}


class CommitContextBuilderTests(unittest.TestCase):
    def test_notes_fit_complete_request_and_yield_to_current_requirements(self):
        notes = [{'commit_id': 'a' * 64, 'summary': 'password reset uses email'}]
        messages = [message('user', 'Add password reset')]
        state = {'constraints': ['Never send email without approval']}
        baseline = ContextBuilder(10000).build(messages, goal='Repair', state=state)
        result = ContextBuilder(baseline.estimated_input_tokens).build(
            messages, goal='Repair', state=state, commit_notes=notes)
        self.assertEqual(result.messages, messages)
        self.assertIn('Never send email without approval', result.system_prompt)
        self.assertEqual(result.selected_commit_ids, [])
        self.assertEqual(result.estimated_input_tokens, baseline.estimated_input_tokens)
        roomy = ContextBuilder(10000).build(messages, goal='Repair', state=state,
                                           commit_notes=notes)
        self.assertIn('password reset uses email', roomy.system_prompt)
        self.assertEqual(roomy.selected_commit_ids, ['a' * 64])
        serialized = json.dumps({'system_prompt': roomy.system_prompt, 'messages': roomy.messages,
                                 'tool_specs': None}, ensure_ascii=False, sort_keys=True,
                                separators=(',', ':'))
        self.assertEqual(roomy.estimated_input_tokens, len(serialized.encode('utf-8')))
        self.assertEqual(notes[0]['summary'], 'password reset uses email')

    def test_skips_large_note_but_can_keep_smaller_ranked_note(self):
        notes = [{'commit_id': 'a' * 64, 'summary': 'x' * 4000},
                 {'commit_id': 'b' * 64, 'summary': 'Small useful password note'}]
        result = ContextBuilder(1200).build([message('user', 'password')], goal='Repair',
                                            commit_notes=notes)
        self.assertEqual(result.selected_commit_ids, ['b' * 64])
        self.assertLessEqual(result.estimated_input_tokens, 1200)


class CommitContextRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = init_repo(Path(self.temp.name) / 'repo')
        self.model = ScriptedModel()
        self.agent = Agent(model=self.model, system_prompt='Preserve requirements',
                           context_manager=False, callback_handler=None)
        self.runtime = StateTreeRuntime(self.agent, goal='Build accounts', repo_path=self.repo,
                                        max_input_tokens=12000, commit_context_budget=3000,
                                        recent_turns=1)
        self.agent.state.set('statetree_context', {'constraints': ['Use local storage']})

    def test_notes_are_persisted_with_evidence_and_recalled_for_new_query(self):
        self.agent.messages[:] = [message('user', 'Build login'), message('assistant', 'Login complete')]
        auth = self.runtime.commit(note={'summary': 'Password login ready',
                                         'changes': ['auth.py'], 'pending': ['password reset']},
                                   verified=True, verification={'passed': True, 'checks': ['login tests']})
        self.runtime.commit(message='Changed dashboard colors')
        selected = self.runtime.recall('Implement password reset')
        self.assertEqual([note['commit_id'] for note in selected.notes], [auth.id])
        self.assertEqual(selected.notes[0]['verification_status'], 'verified')
        evidence = self.runtime.store.read_archive(selected.notes[0]['evidence_ids'][-1])
        self.assertEqual(evidence['messages'][1]['content'][0]['text'], 'Login complete')
        self.assertEqual(evidence['verification']['checks'], ['login tests'])
        self.runtime.run('Implement password reset')
        self.assertIn('Password login ready', self.model.requests[-1]['system_prompt'])
        self.assertNotIn('Changed dashboard colors', self.model.requests[-1]['system_prompt'])
        self.runtime.run('Adjust dashboard colors')
        self.assertIn('Changed dashboard colors', self.model.requests[-1]['system_prompt'])
        self.assertNotIn('Password login ready', self.model.requests[-1]['system_prompt'])
        self.assertEqual(self.agent.system_prompt, 'Preserve requirements')

    def test_new_task_archives_old_history_and_preserves_pinned_state(self):
        history = [message('user', 'Old login request'), message('assistant', 'old log ' * 1800)]
        self.agent.messages[:] = copy.deepcopy(history)
        self.runtime.commit(message='Password login ready')
        self.runtime.run('Add password reset', new_task=True)
        request = self.model.requests[-1]
        self.assertEqual(request['messages'], [message('user', 'Add password reset')])
        self.assertIn('Use local storage', request['system_prompt'])
        self.assertIn('Password login ready', request['system_prompt'])
        archive_id = self.agent.state.get('statetree_prior_history')
        self.assertEqual(self.runtime.store.read_archive(archive_id), history)
        self.assertIn(archive_id, request['system_prompt'])
        self.assertLess(len(json.dumps(request)), len(json.dumps(history)))
        self.runtime.run('Continue with validation')
        self.assertIn(archive_id, self.model.requests[-1]['system_prompt'])

    def test_new_task_preparation_failure_restores_history_and_archive_pointer(self):
        history = [message('user', 'Previous request'), message('assistant', 'Previous result')]
        self.agent.messages[:] = copy.deepcopy(history)
        with self.assertRaises(ContextBudgetError):
            self.runtime.run('x' * 16000, new_task=True)
        self.assertEqual(self.agent.messages, history)
        self.assertIsNone(self.agent.state.get('statetree_prior_history'))
        self.assertEqual(self.model.requests, [])

    def test_invalid_note_never_publishes_checkpoint(self):
        with self.assertRaises(ValueError):
            self.runtime.commit(note={'summary': 'Password', 'evidence_ids': ['a' * 64]})
        self.assertIsNone(self.runtime.store.get_head())
        with self.assertRaises(ValueError):
            self.runtime.commit(message='Password', note={'summary': 'Other'})
        self.assertIsNone(self.runtime.store.get_head())

    def test_restore_and_new_instance_retrieve_only_reachable_notes(self):
        first = self.runtime.commit(message='Password uses session tokens')
        second = self.runtime.commit(message='Password uses cookies instead')
        self.runtime.restore(first.id)
        self.runtime.run('password')
        self.assertIn(first.id, self.model.requests[-1]['system_prompt'])
        self.assertNotIn(second.id, self.model.requests[-1]['system_prompt'])
        fresh = StateTreeRuntime(Agent(model=ScriptedModel(), callback_handler=None),
                                 goal='Build accounts', repo_path=self.repo,
                                 max_input_tokens=12000, commit_context_budget=3000)
        self.assertEqual([n['commit_id'] for n in fresh.recall('password').notes], [first.id])

    def test_model_can_read_note_evidence_through_registered_tool(self):
        self.agent.messages[:] = [message('user', 'Password diagnostic detail')]
        self.runtime.commit(message='Password diagnostic recorded')
        evidence_id = self.runtime.recall('Password').notes[0]['evidence_ids'][-1]
        self.model.responses = [{'tool': 'statetree_read_archive', 'id': 'evidence',
                                 'input': {'archive_id': evidence_id}}, 'done']
        self.runtime.run('Investigate password', new_task=True)
        results = [block['toolResult'] for msg in self.model.requests[-1]['messages']
                   for block in msg['content'] if 'toolResult' in block]
        self.assertIn('Password diagnostic detail', json.dumps(results))

    def test_repeated_new_tasks_keep_older_archive_discoverable(self):
        first_history = [message('user', 'Original diagnostic'), message('assistant', 'Original detail')]
        self.agent.messages[:] = copy.deepcopy(first_history)
        self.runtime.run('Password task one', new_task=True)
        first_archive = self.agent.state.get('statetree_prior_history')
        self.runtime.run('Password task two', new_task=True)
        second_archive = self.agent.state.get('statetree_prior_history')
        archived = self.runtime.store.read_archive(second_archive)
        self.assertEqual(archived['prior_history_archive'], first_archive)
        self.assertEqual(self.runtime.store.read_archive(first_archive), first_history)
        self.assertIn('Password task one', json.dumps(archived['messages']))

    def test_resource_metadata_does_not_break_version_matching(self):
        self.agent.state.set('statetree_context', {'resources': {
            'auth.py': 'v1', 'database': {'path': 'db.sqlite'}}})
        checkpoint = self.runtime.commit(note={'summary': 'Password schema',
                                               'dependencies': {'auth.py': 'v1'}})
        self.assertEqual(self.runtime.recall('Password').notes[0]['commit_id'], checkpoint.id)
        self.runtime.run('Password')
        self.assertIn(checkpoint.id, self.model.requests[-1]['system_prompt'])

    def test_new_task_rejects_unfinished_tools_without_mutating_history(self):
        self.agent.messages[:] = [message('user', 'Inspect password'),
                                  {'role': 'assistant', 'content': [{'toolUse': {
                                      'name': 'read', 'toolUseId': 'pending', 'input': {}}}]}]
        before = copy.deepcopy(self.agent.messages)
        with self.assertRaises(ContextBudgetError):
            self.runtime.run('Different task', new_task=True)
        self.assertEqual(self.agent.messages, before)
        self.assertEqual(self.model.requests, [])

    def test_new_task_cancellation_restores_previous_conversation(self):
        history = [message('user', 'Earlier password task'), message('assistant', 'Earlier answer')]
        self.agent.messages[:] = copy.deepcopy(history)

        def cancel(event):
            event.cancel = True

        self.agent.hooks.add_callback(BeforeInvocationEvent, cancel, order=300)
        self.runtime.run('Password next task', new_task=True)
        self.assertEqual(self.agent.messages, history)
        self.assertIsNone(self.agent.state.get('statetree_prior_history'))
        self.assertEqual(self.model.requests, [])

    def test_new_task_late_preparation_failure_restores_previous_conversation(self):
        history = [message('user', 'Earlier password task'), message('assistant', 'Earlier answer')]
        self.agent.messages[:] = copy.deepcopy(history)

        def fail(event):
            raise RuntimeError('Application preparation failed')

        self.agent.hooks.add_callback(BeforeModelCallEvent, fail, order=300)
        with self.assertRaisesRegex(RuntimeError, 'Application preparation failed'):
            self.runtime.run('Password next task', new_task=True)
        self.assertEqual(self.agent.messages, history)
        self.assertIsNone(self.agent.state.get('statetree_prior_history'))
        self.assertEqual(self.model.requests, [])

    def test_model_hook_cancellation_is_not_counted_as_a_response(self):
        history = [message('user', 'Earlier password task'), message('assistant', 'Earlier answer')]
        self.agent.messages[:] = copy.deepcopy(history)

        def cancel(event):
            event.cancel = True

        self.agent.hooks.add_callback(BeforeModelCallEvent, cancel, order=300)
        self.runtime.run('Password next task', new_task=True)
        self.assertEqual(self.agent.messages, history)
        self.assertEqual(self.model.requests, [])
        self.assertEqual(self.runtime.usage.summary()['requests'], 0)

    def test_new_task_preserves_completed_tool_progress_if_later_model_call_fails(self):
        @tool
        def inspect_password() -> str:
            """Read local password diagnostics."""
            return 'password diagnostic result'

        self.agent.tool_registry.process_tools([inspect_password])
        old = [message('user', 'Previous task'), message('assistant', 'Previous answer')]
        self.agent.messages[:] = copy.deepcopy(old)
        self.model.responses = [{'tool': 'inspect_password', 'id': 'inspect', 'input': {}},
                                RuntimeError('Provider stopped')]
        with self.assertLogs('strands.event_loop.event_loop', level='ERROR'):
            with self.assertRaisesRegex(EventLoopException, 'Provider stopped') as caught:
                self.runtime.run('Inspect password', new_task=True)
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertIn('password diagnostic result', json.dumps(self.agent.messages))
        self.assertNotIn('Previous answer', json.dumps(self.agent.messages))
        self.assertEqual(self.runtime.store.read_archive(
            self.agent.state.get('statetree_prior_history')), old)
        self.assertEqual(self.runtime.usage.summary()['requests'], 2)


if __name__ == '__main__':
    unittest.main()
