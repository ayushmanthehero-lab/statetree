"""Real optional framework integrations; no model or network calls.

Run in an environment containing langgraph and crewai. Their import-time
telemetry is disabled before importing them.
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from typing import TypedDict

os.environ['OTEL_SDK_DISABLED'] = 'true'
os.environ['CREWAI_TELEMETRY_ENABLED'] = 'false'
os.environ['CREWAI_TRACING_ENABLED'] = 'false'
os.environ['CREWAI_TESTING'] = 'true'

from statetree.adapters.portable import (CrewAIFlowStateAdapter, LangGraphStateAdapter,
                                        MappingStateAdapter, transfer_state)
from statetree.core.state import AgentState


class OptionalAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.storage = tempfile.TemporaryDirectory(prefix='statetree-frameworks-')
        cls.addClassCleanup(cls.storage.cleanup)
        cls.previous_storage = os.environ.get('CREWAI_STORAGE_DIR')
        os.environ['CREWAI_STORAGE_DIR'] = cls.storage.name
        def restore():
            if cls.previous_storage is None:
                os.environ.pop('CREWAI_STORAGE_DIR', None)
            else:
                os.environ['CREWAI_STORAGE_DIR'] = cls.previous_storage
        cls.addClassCleanup(restore)

    @unittest.skipUnless(importlib.util.find_spec('langgraph'), 'optional langgraph is not installed')
    def test_real_langgraph_consumes_handoff_and_exports_completed_state(self):
        from langgraph.graph import StateGraph, START, END
        from langgraph.checkpoint.memory import InMemorySaver
        class GraphState(TypedDict):
            statetree: dict
        def finish(values):
            state = AgentState.from_dict(values['statetree']).to_dict()
            state['subgoals'] = ['completed']
            return {'statetree': state}
        graph = StateGraph(GraphState)
        graph.add_node('finish', finish)
        graph.add_edge(START, 'finish')
        graph.add_edge('finish', END)
        compiled = graph.compile(checkpointer=InMemorySaver())
        config = {'configurable': {'thread_id': 'portable-test'}}
        adapter = LangGraphStateAdapter(compiled, config, as_node='__start__')
        source = {'statetree': AgentState(goal='continue').to_dict()}
        transfer_state(MappingStateAdapter(source), adapter)
        result = compiled.invoke(None, adapter.config)
        self.assertEqual(result['statetree']['subgoals'], ['completed'])
        self.assertEqual(adapter.export_state().subgoals, ['completed'])

    @unittest.skipUnless(importlib.util.find_spec('crewai'), 'optional crewai is not installed')
    def test_real_crewai_flow_continues_from_public_state(self):
        # The SDK reads/creates its CLI credential store at import time even
        # with tracing disabled. Redirect just that external boundary; the
        # actual Flow state and execution APIs remain unmodified.
        from unittest.mock import patch
        from crewai_core.token_manager import TokenManager
        patcher = patch.object(TokenManager, '_get_secure_storage_path',
                               return_value=Path(self.storage.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        from crewai.flow.flow import Flow, start
        from pydantic import BaseModel, Field
        class FlowState(BaseModel):
            statetree: dict = Field(default_factory=dict)
        class ContinueFlow(Flow[FlowState]):
            @start()
            def finish(self):
                state = AgentState.from_dict(self.state.statetree).to_dict()
                state['subgoals'] = ['completed']
                self.state.statetree = state
                return state['goal']
        flow = ContinueFlow()
        adapter = CrewAIFlowStateAdapter(flow)
        adapter.import_state(AgentState(goal='continue'))
        self.assertEqual(flow.kickoff(), 'continue')
        self.assertEqual(adapter.export_state().subgoals, ['completed'])


if __name__ == '__main__':
    unittest.main()
