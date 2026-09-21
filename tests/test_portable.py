import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from strands import Agent

from statetree.core.state import AgentState
from statetree.storage.local import LocalStateStore
from statetree.storage.portable import export_bundle, import_bundle, read_bundle, write_bundle
from statetree.adapters.portable import (MappingStateAdapter, StrandsStateAdapter,
                                        CrewAIFlowStateAdapter, transfer_state)
from tests.helpers import ScriptedModel


class PortableTests(unittest.TestCase):
    def test_state_detaches_inputs_and_rejects_lossy_or_unsupported_data(self):
        facts = {'answer': {'value': 42, 'evidence': ['test'], 'version': 1}}
        state = AgentState(goal='finish', facts=facts)
        facts['answer']['value'] = 0
        self.assertEqual(state.to_dict()['facts']['answer']['value'], 42)
        for data in ({**state.to_dict(), 'schema_version': 2},
                     {**state.to_dict(), 'surprise': True},
                     {**state.to_dict(), 'required_capabilities': ['private-kv-cache']},
                     {**state.to_dict(), 'facts': {1: 'lossy'}},
                     {**state.to_dict(), 'memory': {'x': float('nan')}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                AgentState.from_dict(data)

    def test_bundle_transfers_evidence_and_state_to_fresh_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = LocalStateStore(root / 'source')
            evidence = source.put_archive({'log': 'verified locally'})
            state = AgentState(goal='continue', archive_ids=[evidence],
                               facts={'passed': {'value': True, 'evidence': [evidence]}})
            bundle = root / 'handoff.json'
            write_bundle(bundle, export_bundle(state, source))
            code = """
import sys
from statetree.storage.local import LocalStateStore
from statetree.storage.portable import read_bundle, import_bundle
s = LocalStateStore(sys.argv[2])
state = import_bundle(read_bundle(sys.argv[1]), s)
assert state.goal == 'continue'
assert s.read_archive(state.archive_ids[0]) == {'log': 'verified locally'}
print('portable recovery PASS')
"""
            result = subprocess.run([sys.executable, '-B', '-c', code, str(bundle), str(root/'target')],
                                    capture_output=True, text=True, check=True)
            self.assertIn('portable recovery PASS', result.stdout)

    def test_tampered_or_missing_archive_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = LocalStateStore(root/'source'), LocalStateStore(root/'target')
            evidence = source.put_archive({'result': 7})
            bundle = export_bundle(AgentState(goal='test', archive_ids=[evidence]), source)
            bundle['archives'][evidence]['result'] = 9
            with self.assertRaises(ValueError):
                import_bundle(bundle, target)
            self.assertEqual(list(target.archive_dir.glob('*.json')), [])
            with self.assertRaises(ValueError):
                export_bundle(AgentState(goal='test', archive_ids=['a'*64]), source)

    def test_strands_to_custom_transfers_public_state_without_history(self):
        agent = Agent(model=ScriptedModel(), callback_handler=None)
        agent.messages.append({'role': 'user', 'content': [{'text': 'old noisy transcript'}]})
        source = StrandsStateAdapter(agent)
        state = AgentState(goal='continue', constraints=['keep API'], subgoals=['test'])
        source.import_state(state)
        destination = {}
        transfer_state(source, MappingStateAdapter(destination))
        self.assertEqual(destination['statetree']['goal'], 'continue')
        self.assertNotIn('old noisy transcript', json.dumps(destination))
        self.assertEqual(agent.state.get('statetree_context')['constraints'], ['keep API'])

    def test_prompt_projection_excludes_archived_memory_and_enforces_bound(self):
        state = AgentState(goal='continue', memory={'history': ['noise'*1000]})
        payload = state.prompt_state(max_input_tokens=1000)
        self.assertNotIn('noise', json.dumps(payload))
        with self.assertRaises(ValueError):
            AgentState(goal='x'*1000).prompt_state(max_input_tokens=50)

    def test_crewai_public_structured_state_preserves_other_fields(self):
        from pydantic import BaseModel, Field
        class FlowState(BaseModel):
            id: str = 'destination-flow'
            statetree: dict = Field(default_factory=dict)
        class PublicFlow:
            state = FlowState()
        flow = PublicFlow()
        adapter = CrewAIFlowStateAdapter(flow)
        adapter.import_state(AgentState(goal='continue'))
        self.assertEqual(flow.state.id, 'destination-flow')
        self.assertEqual(adapter.export_state().goal, 'continue')

    def test_bundle_inner_hash_rechecked_even_with_valid_outer_hash(self):
        from statetree.core.commit import digest
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(directory)
            identifier = store.put_archive({'original': True})
            bundle = export_bundle(AgentState(goal='continue', archive_ids=[identifier]), store)
            bundle['archives'][identifier] = {'original': False}
            bundle['digest'] = digest({key: value for key, value in bundle.items() if key != 'digest'})
            with self.assertRaisesRegex(ValueError, 'archive failed'):
                import_bundle(bundle, store)


if __name__ == '__main__':
    unittest.main()
