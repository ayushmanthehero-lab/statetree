import importlib
from pathlib import Path
import tempfile
import unittest

from strands import Agent, tool
from strands.hooks import AfterModelCallEvent

from statetree.context import ContextBudgetError
from statetree.runtime.usage import UsageLedger
from statetree.storage.local import LocalStateStore
from tests.helpers import ScriptedModel, init_repo


class StrandsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStateStore(Path(self.temp.name) / 'state')
        self.ledger = UsageLedger(Path(self.temp.name) / 'usage.sqlite3')

    def adapter(self, **kwargs):
        module = importlib.import_module('statetree.adapters.strands')
        return module.StateTreeHooks(
            store=self.store, ledger=self.ledger, goal='Repair the application',
            run_id='test-run', branch='main', **kwargs,
        )

    def test_actual_sdk_loop_records_each_call_without_duplicate_prefix(self):
        hook = self.adapter(max_input_tokens=4000, recent_turns=1)
        model = ScriptedModel(['one', 'two'])
        agent = Agent(model=model, hooks=[hook], system_prompt='Be careful',
                      context_manager=False, callback_handler=None)
        agent('first')
        agent('second')
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(self.ledger.summary()['total_tokens'], 220)
        self.assertEqual(agent.system_prompt, 'Be careful')
        self.assertEqual(model.requests[1]['system_prompt'].count('StateTree context:'), 1)
        self.assertGreater(len(list(self.store.archive_dir.glob('*.json'))), 0)

    def test_records_all_calls_in_single_tool_loop(self):
        @tool
        def noisy_log() -> str:
            """Read diagnostic output."""
            return 'diagnostic ' * 50

        model = ScriptedModel([
            {'tool': 'noisy_log', 'id': 'one'}, {'tool': 'noisy_log', 'id': 'two'}, 'fixed'
        ])
        hook = self.adapter(max_input_tokens=3500, recent_turns=1)
        agent = Agent(model=model, tools=[noisy_log], hooks=[hook],
                      context_manager=False, callback_handler=None)
        agent('fix the application')
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(self.ledger.summary()['total_tokens'], 330)
        self.assertTrue(any('archived tool result' in str(x) for x in model.requests[-1]['messages']))

    def test_budget_rejection_prevents_model_request(self):
        model = ScriptedModel()
        hook = self.adapter(max_input_tokens=100)
        agent = Agent(model=model, hooks=[hook], context_manager=False, callback_handler=None)
        with self.assertRaises(ContextBudgetError):
            agent('request ' * 100)
        self.assertEqual(model.requests, [])
        self.assertEqual(self.ledger.summary()['requests'], 0)

    def test_model_error_is_visible_as_unknown_usage(self):
        model = ScriptedModel([RuntimeError('network failure')])
        hook = self.adapter()
        agent = Agent(model=model, hooks=[hook], callback_handler=None)
        with self.assertRaises(RuntimeError):
            agent('hello')
        self.assertEqual(self.ledger.summary()['unknown_usage_requests'], 1)

    def test_hook_retry_counts_discarded_request(self):
        hook = self.adapter()
        model = ScriptedModel(['discard', 'keep'])
        agent = Agent(model=model, hooks=[hook], callback_handler=None)
        seen = []

        def retry_once(event):
            if event.stop_response and not seen:
                seen.append(True)
                event.retry = True

        agent.hooks.add_callback(AfterModelCallEvent, retry_once)
        agent('hello')
        self.assertEqual(self.ledger.summary()['total_tokens'], 220)
        self.assertEqual(self.ledger.records()[0]['status'], 'retry')

    def test_cumulative_limit_survives_new_hook_instance(self):
        first = self.adapter(total_token_limit=110)
        Agent(model=ScriptedModel(), hooks=[first], callback_handler=None)('hello')
        model = ScriptedModel()
        second = self.adapter(total_token_limit=110)
        with self.assertRaises(RuntimeError):
            Agent(model=model, hooks=[second], callback_handler=None)('next')
        self.assertEqual(model.requests, [])

    def test_runtime_connects_context_archive_tool_and_usage(self):
        from statetree.runtime.runtime import StateTreeRuntime
        repo = init_repo(Path(self.temp.name) / 'repo')
        model = ScriptedModel()
        agent = Agent(model=model, context_manager=False, callback_handler=None)
        runtime = StateTreeRuntime(agent, goal='Repair', repo_path=repo,
                                   max_input_tokens=5000, run_id='runtime-test')
        runtime.run('start')
        checkpoint = runtime.commit()
        runtime.restore(checkpoint.id)
        self.assertEqual(runtime.usage.summary()['total_tokens'], 110)
        self.assertIn('StateTree context:', model.requests[0]['system_prompt'])
        names = [spec['name'] for spec in agent.tool_registry.get_all_tool_specs()]
        self.assertIn('statetree_read_archive', names)


if __name__ == '__main__':
    unittest.main()
