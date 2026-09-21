import tempfile
import threading
import unittest
import subprocess
import sys
from pathlib import Path

from strands import Agent

from statetree.core.state import AgentState
from statetree.runtime.runtime import StateTreeRuntime
from tests.helpers import ScriptedModel, init_repo


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = init_repo(self.temp.name)
        self.first = ScriptedModel(['before', 'after return'])
        self.first.config['model_id'] = 'offline-A'
        self.agent = Agent(model=self.first, callback_handler=None, context_manager=False)
        self.runtime = StateTreeRuntime(self.agent, goal='fix task', repo_path=self.repo,
                                        max_input_tokens=6000, run_id='switch-demo')

    def test_model_switch_uses_compact_state_and_keeps_cumulative_usage(self):
        self.runtime.set_state(AgentState(goal='fix task', constraints=['keep API'],
                                         facts={'answer': {'value': 42}}))
        self.runtime.run('noisy original conversation')
        second = ScriptedModel(['next'])
        second.config['model_id'] = 'offline-B'
        receipt = self.runtime.switch_model(second)
        self.assertEqual(self.agent.messages, [])
        self.assertEqual(receipt['from_model'], 'offline-A')
        self.assertEqual(receipt['to_model'], 'offline-B')
        self.runtime.run('continue from state')
        self.assertNotIn('noisy original', str(second.requests))
        self.assertIn('keep API', second.requests[0]['system_prompt'])
        self.assertIn('42', second.requests[0]['system_prompt'])
        self.runtime.switch_model(self.first)
        self.runtime.run('finish')
        totals = self.runtime.usage.summary(run_id='switch-demo')
        self.assertEqual(totals['total_tokens'], 330)
        self.assertIs(self.agent.model, self.first)
        self.assertEqual(self.runtime.get_state().goal, 'fix task')

    def test_failed_preflight_keeps_original_model_messages_and_head(self):
        self.runtime.set_state(AgentState(goal='fix task', facts={'huge': 'x'*10000}))
        self.agent.messages[:] = [{'role': 'user', 'content': [{'text': 'original'}]}]
        original = list(self.agent.messages)
        with self.assertRaises(ValueError):
            self.runtime.switch_model(ScriptedModel(), max_input_tokens=100)
        self.assertIs(self.agent.model, self.first)
        self.assertEqual(self.agent.messages, original)
        self.assertIsNone(self.runtime.store.get_head())

    def test_cannot_switch_with_unfinished_tool_call(self):
        self.runtime.set_state(AgentState(goal='fix task'))
        self.agent.messages[:] = [{'role': 'assistant', 'content': [
            {'toolUse': {'toolUseId': 'pending', 'name': 'write', 'input': {}}}]}]
        with self.assertRaises(ValueError):
            self.runtime.switch_model(ScriptedModel())
        self.assertIs(self.agent.model, self.first)

    def test_running_call_rejects_checkpoint_restore_and_switch(self):
        started, release = threading.Event(), threading.Event()
        errors = []
        class BlockingModel(ScriptedModel):
            async def stream(self, *args, **kwargs):
                started.set()
                release.wait(10)
                async for event in super().stream(*args, **kwargs):
                    yield event
        self.agent.model = BlockingModel()
        def run():
            try:
                self.runtime.run('work')
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            for action in (lambda: self.runtime.commit(),
                           lambda: self.runtime.switch_model(ScriptedModel()),
                           lambda: self.runtime.restore('a'*64)):
                with self.assertRaises(RuntimeError):
                    action()
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_memory_update_removes_stale_facts_from_actual_request(self):
        from statetree.memory.facts import FactMemory
        memory = FactMemory()
        memory.observe('database', 'old-db', evidence=['inspection-1'])
        memory.observe('query', 'old-query', evidence=['test-1'], dependencies={'database': 1})
        memory.observe('database', 'new-db', evidence=['inspection-2'])
        self.runtime.set_state(AgentState(goal='fix task'))
        self.runtime.set_memory(memory)
        self.runtime.run('continue')
        request = str(self.first.requests[-1])
        self.assertIn('new-db', request)
        self.assertNotIn('old-db', request)
        self.assertNotIn('old-query', request)
        checkpoint = self.runtime.commit()
        self.runtime.set_state(AgentState(goal='other'))
        self.runtime.restore(checkpoint.id)
        restored = FactMemory.from_dict(self.runtime.get_state().memory)
        self.assertEqual(restored.active_facts()['database']['value'], 'new-db')

    def test_public_adapter_transfer_updates_attached_runtime_goal(self):
        from statetree.adapters.portable import MappingStateAdapter, StrandsStateAdapter, transfer_state
        source = {'statetree': AgentState(goal='new goal').to_dict()}
        transfer_state(MappingStateAdapter(source), StrandsStateAdapter(self.agent))
        self.runtime.run('continue')
        self.assertEqual(self.runtime.goal, 'new goal')
        self.assertNotIn('fix task', str(self.first.requests[-1]))

    def test_handoff_preserves_application_extensions_in_request(self):
        self.runtime.set_state(AgentState(goal='fix task', extensions={
            'ticket': 'APP-MARKER', 'statetree.workflow': {'history': 'INTERNAL-MARKER'}}))
        second = ScriptedModel()
        self.runtime.switch_model(second)
        self.runtime.run('continue')
        request = str(second.requests[-1])
        self.assertIn('APP-MARKER', request)
        self.assertNotIn('INTERNAL-MARKER', request)

    def test_receipt_failure_rolls_back_head_and_binding(self):
        from unittest.mock import patch
        self.runtime.set_state(AgentState(goal='fix task'))
        self.agent.messages[:] = [{'role': 'user', 'content': [{'text': 'original'}]}]
        original = list(self.agent.messages)
        with patch.object(self.runtime.store, 'put_archive', side_effect=OSError('receipt failure')):
            with self.assertRaises(OSError):
                self.runtime.switch_model(ScriptedModel())
        self.assertIsNone(self.runtime.store.get_head())
        self.assertIsNone(self.runtime._parent)
        self.assertIs(self.agent.model, self.first)
        self.assertEqual(self.agent.messages, original)

    def test_invalid_model_configuration_fails_before_checkpoint_publication(self):
        class BrokenConfig(ScriptedModel):
            def get_config(self):
                raise OSError('configuration unavailable')
        self.runtime.set_state(AgentState(goal='fix task'))
        with self.assertRaises(OSError):
            self.runtime.switch_model(BrokenConfig())
        self.assertIsNone(self.runtime.store.get_head())
        self.assertIsNone(self.runtime._parent)

    def test_adapter_refuses_orphaned_runtime_hooks(self):
        import gc
        import weakref
        from statetree.adapters.portable import StrandsStateAdapter
        owner = weakref.ref(self.runtime)
        del self.runtime
        gc.collect()
        self.assertIsNone(owner())
        with self.assertRaises(RuntimeError):
            StrandsStateAdapter(self.agent).import_state(AgentState(goal='new goal'))

    def test_fresh_process_binds_new_model_from_bundle_and_keeps_usage(self):
        from statetree.storage.portable import write_bundle
        self.runtime.set_state(AgentState(goal='fix task', facts={'answer': {'value': 42}}))
        self.runtime.run('OLD-HISTORY-MARKER')
        bundle_path = self.repo / '.statetree' / 'handoff.json'
        write_bundle(bundle_path, self.runtime.export_state())
        code = """
import sys
from strands import Agent
from statetree.runtime.runtime import StateTreeRuntime
from statetree.storage.portable import read_bundle
from tests.helpers import ScriptedModel
model = ScriptedModel()
model.config['model_id'] = 'offline-B'
agent = Agent(model=model, callback_handler=None, context_manager=False)
runtime = StateTreeRuntime(agent, goal='bind destination', repo_path=sys.argv[1],
                           max_input_tokens=6000, run_id='switch-demo')
runtime.import_state(read_bundle(sys.argv[2]))
runtime.run('continue')
assert '42' in model.requests[-1]['system_prompt']
assert 'OLD-HISTORY-MARKER' not in str(model.requests[-1])
assert runtime.usage.summary(run_id='switch-demo')['total_tokens'] == 220
print('fresh-process model handoff PASS')
"""
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.repo), str(bundle_path)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, check=True)
        self.assertIn('fresh-process model handoff PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()
