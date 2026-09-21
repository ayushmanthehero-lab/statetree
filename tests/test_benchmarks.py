from pathlib import Path
import tempfile
import unittest
import json

from benchmarks.live import run_case, parse_answer
from benchmarks.replay import compare
from tests.helpers import ScriptedModel
from unittest.mock import patch
import contextlib
import io


class BenchmarkTests(unittest.TestCase):
    def test_live_cli_passes_and_reports_context_and_output_limits(self):
        from benchmarks.live import main
        for extra, expected_context, expected_output, expected_budget in [
            ([], 8192, 512, 6000),
            (['--context-window-limit', '4096', '--max-output-tokens', '64', '--budget', '3000'],
             4096, 64, 3000),
            (['--context-window-limit', '4096', '--max-output-tokens', '64', '--budget', '4032'],
             4096, 64, 4032),
        ]:
            created = []
            def model_factory(endpoint_name, **options):
                model = ScriptedModel()
                model.update_config(**options)
                created.append((endpoint_name, model))
                return model
            def fake_case(policy, task_id, steps, budget, model, output_dir, cache_convention):
                self.assertEqual(model.context_window_limit, expected_context)
                self.assertEqual(model.get_config()['max_tokens'], expected_output)
                self.assertEqual(budget, expected_budget)
                return {'policy': policy, 'success': True, 'usage': {
                    'total_tokens': 12, 'unknown_usage_requests': 0, 'ambiguous_usage_requests': 0}}
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'report.json'
                with patch('statetree.models.sagemaker.SageMakerModel', side_effect=model_factory), \
                     patch('benchmarks.live.run_case', side_effect=fake_case), \
                     patch('boto3.Session', side_effect=AssertionError('Unexpected AWS session')), \
                     contextlib.redirect_stdout(io.StringIO()):
                    main(['--live', '--endpoint-name', 'cpu-test', '--region', 'ap-south-2',
                          '--tasks', '1', '--steps', '1', '--output', str(output), *extra])
                report = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(report['context_window_limit'], expected_context)
                self.assertEqual(report['max_output_tokens'], expected_output)
                self.assertEqual(report['statetree_input_estimate_budget'], expected_budget)
                self.assertEqual(len(created), 3)
                self.assertTrue(all(endpoint == 'cpu-test' for endpoint, _ in created))
                self.assertEqual(set(report['summary']), {'full_history', 'strands_auto', 'statetree'})

    def test_invalid_context_or_budget_reserve_fails_before_client_or_output(self):
        from benchmarks.live import main
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'uncreated' / 'report.json'
            for extra in [
                ['--context-window-limit', '0'], ['--context-window-limit', '-1'],
                ['--context-window-limit', 'invalid'],
                ['--context-window-limit', '4096', '--max-output-tokens', '64', '--budget', '4033'],
                ['--context-window-limit', '4096'],
                ['--context-window-limit', '64', '--max-output-tokens', '64', '--budget', '1'],
            ]:
                with self.subTest(extra=extra), \
                     patch('statetree.models.sagemaker.SageMakerModel', side_effect=AssertionError('Unexpected model')), \
                     patch('boto3.Session', side_effect=AssertionError('Unexpected AWS session')), \
                     contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        main(['--live', '--endpoint-name', 'cpu-test', '--region', 'ap-south-2',
                              '--tasks', '1', '--steps', '1', '--output', str(output), *extra])
                    self.assertEqual(stopped.exception.code, 2)
                self.assertFalse(output.parent.exists())

    def test_sagemaker_cli_requires_explicit_live_endpoint_and_region(self):
        from benchmarks.live import main
        with patch('boto3.Session', side_effect=AssertionError('Unexpected AWS session')):
            for arguments in ([], ['--live'], ['--live', '--endpoint-name', 'demo']):
                with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        main(arguments)
                    self.assertEqual(stopped.exception.code, 2)

    def test_live_policies_execute_same_task_offline_with_actual_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            for policy in ('full_history', 'strands_auto', 'statetree'):
                model = ScriptedModel([
                    {'tool': 'read_document', 'id': 'zero', 'input': {'index': 0}}, 'read zero',
                    {'tool': 'read_document', 'id': 'one', 'input': {'index': 1}}, 'read one',
                    '{"first_marker":"PROJECT-0-OK","total":1}',
                ])
                result = run_case(policy, 0, 2, 6000, model, Path(directory), 'auto')
                self.assertIsNone(result['error'], result['error'])
                self.assertTrue(result['success'])
                self.assertEqual(result['usage']['requests'], 5)
                self.assertEqual(result['usage']['total_tokens'], 550)

    def test_replay_keeps_every_materialized_request_within_budget(self):
        result = compare(steps=12, budget=6000)
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(len(result['calls']), 12)
        self.assertTrue(all(row['statetree_input_estimate'] <= 6000 for row in result['calls']))
        self.assertGreater(result['estimated_input_reduction_percent'], 0)
        self.assertGreater(result['archived_objects'], 0)

    def test_scorer_does_not_treat_unrelated_json_as_answer(self):
        self.assertIsNone(parse_answer('{"unrelated":123}'))
        self.assertEqual(parse_answer('Result: {"total": 3, "first_marker":"A"}'),
                         {'total': 3, 'first_marker': 'A'})
