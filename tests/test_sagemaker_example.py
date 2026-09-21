import contextlib
import io
import json
import unittest
from unittest.mock import patch

from examples import sagemaker_agent
from examples.sagemaker_agent import main, run_demo
from statetree.models import SageMakerModel
from tests.helpers import ScriptedModel
from tests.test_sagemaker import Transport, completion


class SageMakerExampleTests(unittest.TestCase):
    def test_text_probe_uses_one_real_agent_call_without_git_or_tools(self):
        transport = Transport(completion('4', usage={
            'prompt_tokens': 22, 'completion_tokens': 1, 'total_tokens': 23}))
        model = SageMakerModel('cpu-test', client=transport, model_id='qwen3.5-4b-q4_k_m',
                               max_tokens=64, context_window_limit=4096)
        with patch('examples.sagemaker_agent.git', side_effect=AssertionError('Unexpected Git call')):
            result = sagemaker_agent.run_text_probe(model)
        self.assertEqual(result['mode'], 'text')
        self.assertEqual(result['text'], '4')
        self.assertEqual(result['raw_usage'], {'inputTokens': 22, 'outputTokens': 1, 'totalTokens': 23})
        self.assertGreaterEqual(result['elapsed_seconds'], 0)
        self.assertEqual(len(transport.requests), 1)
        payload = json.loads(transport.requests[0]['Body'])
        self.assertNotIn('tools', payload)
        self.assertEqual(payload['max_tokens'], 64)
        self.assertEqual(payload['messages'][-1]['content'], 'What is 2+2? Reply only 4.')
        self.assertFalse(payload['chat_template_kwargs']['enable_thinking'])

    def test_text_probe_validates_exact_answer_and_preserves_missing_usage(self):
        with self.assertRaisesRegex(RuntimeError, 'Text smoke check failed'):
            sagemaker_agent.run_text_probe(ScriptedModel(['14']))
        model = SageMakerModel('cpu-test', client=Transport(completion('4')))
        result = sagemaker_agent.run_text_probe(model)
        self.assertIsNone(result['raw_usage'])

    def test_cli_text_mode_routes_to_probe_and_applies_cpu_limits(self):
        transport = Transport(completion('4'))
        def make_model(endpoint_name, **kwargs):
            return SageMakerModel(endpoint_name, client=transport, **kwargs)
        with patch('statetree.models.sagemaker.SageMakerModel', side_effect=make_model), \
             patch('examples.sagemaker_agent.git', side_effect=AssertionError('Unexpected Git call')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            main(['--live', '--endpoint-name', 'cpu-test', '--region', 'ap-south-2',
                  '--mode', 'text', '--model-id', 'qwen3.5-4b-q4_k_m',
                  '--max-output-tokens', '64', '--context-window-limit', '4096'])
        self.assertEqual(json.loads(output.getvalue())['text'], '4')
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(json.loads(transport.requests[0]['Body'])['model'], 'qwen3.5-4b-q4_k_m')
        self.assertEqual(json.loads(transport.requests[0]['Body'])['max_tokens'], 64)

    def test_cli_rejects_invalid_numeric_limits_before_model_creation(self):
        with patch('statetree.models.sagemaker.SageMakerModel', side_effect=AssertionError('Unexpected model')):
            for extra in [['--max-output-tokens', '0'], ['--max-output-tokens', '-1'],
                          ['--context-window-limit', '0'], ['--context-window-limit', 'abc'],
                          ['--max-output-tokens', '64', '--context-window-limit', '64'],
                          ['--mode', 'text', '--large-endpoint-name', 'unused']]:
                with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        main(['--live', '--endpoint-name', 'cpu-test', '--region', 'ap-south-2', *extra])
                    self.assertEqual(stopped.exception.code, 2)

    def test_demo_checks_exact_recalled_value(self):
        model = ScriptedModel([
            {'tool': 'read_demo_value', 'id': 'read', 'input': {}}, 'Recorded 42.', '142',
        ])
        with self.assertRaisesRegex(RuntimeError, 'Recall smoke check failed'):
            run_demo(model)

    def test_agent_demo_reserves_output_within_configured_context_window(self):
        model = ScriptedModel([
            {'tool': 'read_demo_value', 'id': 'read', 'input': {}}, 'Recorded 42.', '42',
        ])
        model.update_config(context_window_limit=4096, max_tokens=64)
        with patch('examples.sagemaker_agent.StateTreeRuntime',
                   wraps=sagemaker_agent.StateTreeRuntime) as runtime:
            result = run_demo(model)
        self.assertEqual(result['recall_response'].strip(), '42')
        self.assertEqual(runtime.call_args.kwargs['max_input_tokens'], 4032)

    def test_demo_exercises_tool_commit_context_and_model_handoff(self):
        small = ScriptedModel([
            {'tool': 'read_demo_value', 'id': 'read', 'input': {}}, 'Recorded 42.',
        ])
        large = ScriptedModel(['42'])
        result = run_demo(small, large)
        self.assertEqual(result['recall_response'].strip(), '42')
        self.assertTrue(result['handoff'])
        self.assertIn(result['checkpoint_id'], result['selected_commit_ids'])
        self.assertEqual(len(small.requests), 2)
        self.assertEqual(len(large.requests), 1)

    def test_cli_rejects_missing_live_endpoint_or_region_without_aws(self):
        with patch('boto3.Session', side_effect=AssertionError('Unexpected AWS session')):
            for arguments in ([], ['--live'], ['--live', '--endpoint-name', 'demo']):
                with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        main(arguments)
                    self.assertEqual(stopped.exception.code, 2)
