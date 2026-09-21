import io
import json
import unittest

from statetree.models.sagemaker import SageMakerModel
from statetree.web.demo import QUESTION, run_demo, compare_results, fixture_history


class FixedClient:
    def __init__(self, usage=None, answer='7 days', fail=False):
        self.usage = usage
        self.answer = answer
        self.fail = fail
        self.requests = []

    def invoke_endpoint(self, **kwargs):
        self.requests.append(json.loads(kwargs['Body']))
        if self.fail:
            raise RuntimeError('private-upstream-token must never be shown')
        result = {'choices': [{'message': {'role': 'assistant', 'content': self.answer},
                               'finish_reason': 'stop'}]}
        if self.usage is not None:
            result['usage'] = self.usage
        return {'Body': io.BytesIO(json.dumps(result).encode())}


class HybridDemoTests(unittest.TestCase):
    def test_real_runtime_selects_committed_note_and_reports_provider_counts(self):
        clients = [FixedClient({'prompt_tokens': 850, 'completion_tokens': 3, 'total_tokens': 853}),
                   FixedClient({'prompt_tokens': 670, 'completion_tokens': 3, 'total_tokens': 673,
                                'prompt_tokens_details': {'cached_tokens': 20}})]
        pending = iter(clients)
        report = run_demo(lambda: SageMakerModel('fixture', client=next(pending),
                                                 temperature=0, max_tokens=64))
        self.assertEqual([len(c.requests) for c in clients], [1, 1])
        for client in clients:
            self.assertEqual(client.requests[0]['messages'][-1]['content'], QUESTION)
            self.assertEqual(client.requests[0]['temperature'], 0)
            self.assertEqual(client.requests[0]['max_tokens'], 64)
        self.assertGreater(len(clients[0].requests[0]['messages']), 4)
        self.assertEqual(len(clients[1].requests[0]['messages']), 2)
        self.assertTrue(report['statetree']['selected_commit_ids'])
        self.assertEqual(report['comparison']['input_tokens_saved'], 180)
        self.assertEqual(report['statetree']['usage']['input_tokens'], 670)
        self.assertEqual(report['statetree']['usage']['cache_read_input_tokens'], 20)
        self.assertTrue(report['comparison']['both_tasks_passed'])
        self.assertIn('caller-authored', report['fixture']['commit_note_origin'])
        self.assertEqual(report['fixture']['id'], 'export-retention-v2')
        self.assertEqual(report['fixture']['history_messages'], 20)
        self.assertEqual(len(fixture_history()), 20)

    def test_missing_usage_is_unknown_and_cannot_be_claimed_as_savings(self):
        report = run_demo(lambda: SageMakerModel('fixture', client=FixedClient(), temperature=0, max_tokens=64))
        self.assertEqual(report['baseline']['usage']['status'], 'unknown')
        self.assertIsNone(report['baseline']['usage']['input_tokens'])
        self.assertIsNone(report['comparison']['input_reduction_percent'])

    def test_error_is_terminal_and_does_not_reveal_exception_or_claim_success(self):
        report = run_demo(lambda: SageMakerModel('fixture', client=FixedClient(fail=True), temperature=0, max_tokens=64))
        self.assertFalse(report['comparison']['both_tasks_passed'])
        self.assertEqual(report['baseline']['status'], 'error')
        self.assertNotIn('private-upstream-token', json.dumps(report))
        self.assertIsNone(report['comparison']['input_tokens_saved'])

    def test_negative_savings_and_task_failure_remain_visible(self):
        baseline = {'passed': True, 'usage': {'status': 'known', 'input_tokens': 100}}
        optimized = {'passed': False, 'usage': {'status': 'known', 'input_tokens': 120}}
        comparison = compare_results(baseline, optimized)
        self.assertEqual(comparison['input_tokens_saved'], -20)
        self.assertEqual(comparison['input_reduction_percent'], -20.0)
        self.assertFalse(comparison['both_tasks_passed'])
        self.assertEqual(comparison['status'], 'task_failed')


if __name__ == '__main__':
    unittest.main()
