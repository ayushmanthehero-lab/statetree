import importlib
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest

from tests.helpers import git, init_repo
from statetree.runtime.usage import UsageLedger


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('statetree.runtime.parallel'),
                             'Speculative coordination is required')
        self.module = importlib.import_module('statetree.runtime.parallel')
        branches = importlib.import_module('statetree.workspace.branches')
        self.temp = tempfile.TemporaryDirectory(prefix='statetree-parallel-')
        self.addCleanup(self.temp.cleanup)
        self.repo = init_repo(Path(self.temp.name) / 'repo')
        self.manager = branches.BranchManager(
            self.repo, root=Path(self.temp.name) / 'managed',
            verifier=lambda path, state, phase: state.get('valid', True),
        )
        self.base = self.manager.initialize({'choice': '', 'valid': True})
        self.ledger = UsageLedger(Path(self.temp.name) / 'usage.sqlite3')

    def test_actual_concurrent_isolation_deterministic_selection_and_loser_usage(self):
        barrier = threading.Barrier(2)

        def worker(branch):
            (branch.path / 'file.txt').write_text(branch.name, encoding='utf-8')
            barrier.wait(timeout=10)
            self.assertEqual((branch.path / 'file.txt').read_text(), branch.name)
            return {'state': {'choice': branch.name, 'valid': True}, 'result': {'score': 5},
                    'usage': [{'request_id': branch.name + '-request',
                               'usage': {'inputTokens': 10, 'outputTokens': 2}}]}

        coordinator = self.module.SpeculativeCoordinator(self.manager, ledger=self.ledger, max_workers=2)
        report = coordinator.run({'beta': worker, 'alpha': worker}, run_id='race',
                                 score=lambda candidate: candidate['result']['score'])
        self.assertEqual(report['winner'], 'alpha')
        self.assertEqual(len(report['attempts']), 2)
        self.assertEqual(self.ledger.summary(run_id='race')['total_tokens'], 24)
        self.assertEqual(self.manager.head()['state']['choice'], 'alpha')
        self.assertEqual((self.repo / 'file.txt').read_text(), 'initial')
        for attempt in report['attempts']:
            self.assertTrue(Path(attempt['path']).is_dir())
            self.assertEqual((Path(attempt['path']) / 'file.txt').read_text(), attempt['name'])

    def test_failed_worker_retains_immediately_recorded_usage_and_all_fail_preserves_head(self):
        def worker(branch):
            branch.record_usage({'inputTokens': 7, 'outputTokens': 3}, request_id='failed-request')
            raise RuntimeError('worker failed')

        coordinator = self.module.SpeculativeCoordinator(self.manager, ledger=self.ledger, max_workers=1)
        report = coordinator.run({'failed': worker}, run_id='failed-race')
        self.assertIsNone(report['winner'])
        self.assertEqual(self.manager.head()['id'], self.base['id'])
        self.assertEqual(self.ledger.summary(run_id='failed-race')['total_tokens'], 10)
        self.assertIn('worker failed', report['attempts'][0]['error'])
        self.assertTrue(Path(report['attempts'][0]['path']).exists())

    def test_independent_verifier_rejects_worker_self_assertion(self):
        def worker(branch):
            return {'state': {'valid': False}, 'result': {'passed': True},
                    'usage': [{'usage': {'inputTokens': 2, 'outputTokens': 1}}]}

        report = self.module.SpeculativeCoordinator(self.manager, ledger=self.ledger).run(
            {'untrusted': worker}, run_id='rejected',
        )
        self.assertIsNone(report['winner'])
        self.assertEqual(self.manager.head()['id'], self.base['id'])
        self.assertEqual(self.ledger.summary(run_id='rejected')['total_tokens'], 3)

    def test_stale_parent_during_workers_cannot_promote(self):
        def worker(branch):
            other = self.manager.fork('external')
            candidate = self.manager.complete(other, state={'choice': 'external', 'valid': True})
            self.manager.integrate(candidate['id'], expected_parent=self.base['id'])
            return {'state': {'choice': 'worker', 'valid': True}}

        report = self.module.SpeculativeCoordinator(self.manager, ledger=self.ledger).run(
            {'late': worker}, run_id='late-race',
        )
        self.assertIsNone(report['winner'])
        self.assertEqual(self.manager.head()['state']['choice'], 'external')
        self.assertIn('StaleParentError', report['attempts'][0]['error'])

    def test_invalid_worker_limit_rejected(self):
        for workers in (0, -1, True, 1.5):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                self.module.SpeculativeCoordinator(self.manager, ledger=self.ledger, max_workers=workers)

    def test_branch_limit_rejects_before_creating_worktrees(self):
        coordinator = self.module.SpeculativeCoordinator(self.manager, ledger=self.ledger, max_branches=1)
        with self.assertRaises(ValueError):
            coordinator.run({'one': lambda b: {}, 'two': lambda b: {}}, run_id='too-many')
        self.assertEqual(len(list(self.manager.worktrees.iterdir())), 1)

    def test_token_threshold_and_unknown_usage_stop_subsequent_worker_calls(self):
        def bounded(branch):
            branch.record_usage({'inputTokens': 7, 'outputTokens': 3}, request_id='bounded-one')
            branch.check_budget()
            self.fail('A second request must not start')

        def unknown(branch):
            branch.record_usage(None, request_id='unknown-one', status='error')
            branch.check_budget()
            self.fail('Unknown usage must not be treated as zero')

        coordinator = self.module.SpeculativeCoordinator(
            self.manager, ledger=self.ledger, max_workers=1, per_branch_token_limit=10,
        )
        report = coordinator.run({'bounded': bounded, 'unknown': unknown}, run_id='limited')
        self.assertIsNone(report['winner'])
        self.assertEqual(report['usage']['total_tokens'], 10)
        self.assertEqual(report['usage']['unknown_usage_requests'], 1)
        self.assertTrue(all(attempt['error'] for attempt in report['attempts']))

    def test_coordinator_supports_repeated_strategy_names_across_runs(self):
        def strategy(branch):
            return {'state': {'choice': 'again', 'valid': True}}

        coordinator = self.module.SpeculativeCoordinator(self.manager, ledger=self.ledger)
        first = coordinator.run({'same': strategy}, run_id='first')
        second = coordinator.run({'same': strategy}, run_id='second')
        self.assertEqual(first['winner'], 'same')
        self.assertEqual(second['winner'], 'same')
        self.assertNotEqual(first['attempts'][0]['path'], second['attempts'][0]['path'])


if __name__ == '__main__':
    unittest.main()
