"""Actual Git/SQLite project tests; no SDK, mock model or network is needed."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.PIPE, text=True).strip()


def repo_at(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, 'init', '-q')
    git(path, 'config', 'user.name', 'StateTree test')
    git(path, 'config', 'user.email', 'test@statetree.invalid')
    (path / 'example.txt').write_text('initial', encoding='utf-8')
    git(path, 'add', '.')
    git(path, 'commit', '-qm', 'initial')
    return path


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('statetree.project'), 'The integrated Project facade is missing')
        self.Project = importlib.import_module('statetree.project').Project
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = repo_at(Path(self.temp.name) / 'repo')
        self.project = self.Project.init(self.repo, goal='Build an export service')

class ProjectTests(ProjectFixture):
    def test_reopen_preserves_goal_note_state_and_workspace(self):
        self.project.remember('retention', 7, evidence=['caller:decision'])
        commit = self.project.checkpoint('Export retention is 7 days')
        reopened = self.Project(self.repo)
        self.assertEqual(reopened.status()['head'], commit['id'])
        self.assertEqual(reopened.state.goal, 'Build an export service')
        self.assertEqual(reopened.state.facts['retention']['value'], 7)
        self.assertEqual(reopened.history()['items'][0]['note']['summary'], 'Export retention is 7 days')
        self.assertIsNone(reopened.status()['canonical_head'])

    def test_restore_files_state_but_not_observed_usage(self):
        saved = self.project.checkpoint('Before change')
        (self.repo / 'example.txt').write_text('changed', encoding='utf-8')
        self.project.remember('retention', 30, evidence=['caller:changed'])
        self.project.ledger.record('request-1', run_id='r', branch='main', usage={'inputTokens': 100, 'outputTokens': 20})
        self.project.restore(saved['id'])
        self.assertEqual((self.repo / 'example.txt').read_text(), 'initial')
        self.assertNotIn('retention', self.Project(self.repo).state.facts)
        self.assertEqual(self.project.usage()['total_tokens'], 120)

    def test_fact_supersession_invalidates_dependents(self):
        self.project.remember('port', 8000, evidence=['caller:port'])
        self.project.remember('url', 'localhost:8000', evidence=['caller:url'], dependencies={'port': 1})
        self.project.remember('port', 9000, evidence=['caller:port2'])
        self.assertNotIn('url', self.Project(self.repo).state.facts)
        self.assertEqual(self.Project(self.repo).state.facts['port']['value'], 9000)

    def test_archive_and_recall_are_real_and_budgeted(self):
        self.project.checkpoint('Export retention is 7 days')
        chosen = self.project.recall('What is the export retention period?')
        self.assertTrue(chosen['notes'])
        identifier = chosen['notes'][0]['evidence_ids'][0]
        page = self.project.read_archive(identifier, limit=10)
        self.assertEqual(len(page['content']), 10)
        self.assertEqual(page['next_offset'], 10)
        preview = self.project.context('What is the export retention period?', new_task=True)
        self.assertIn('7 days', preview['system_prompt'])
        self.assertEqual(preview['counting_method'], 'utf8_bytes_estimate')
        self.assertLessEqual(preview['estimated_input_tokens'], self.project.config['context_budget'])
        self.assertEqual(self.project.usage()['requests'], 0)

    def test_portable_bundle_validates_before_changing_project(self):
        self.project.remember('retention', 7, evidence=['caller:decision'])
        bundle = self.project.export_state()
        other = self.Project.init(repo_at(Path(self.temp.name) / 'other'), goal='Other goal')
        other.import_state(bundle)
        self.assertEqual(other.state.facts['retention']['value'], 7)
        before = other.status()['head']
        bundle['state']['goal'] = 'tampered'
        with self.assertRaises(ValueError):
            other.import_state(bundle)
        self.assertEqual(other.status()['head'], before)

    def test_stale_writer_is_rejected_without_losing_memory(self):
        stale = self.Project(self.repo)
        self.project.remember('port', 8000, evidence=['caller:port'])
        with self.assertRaises(RuntimeError):
            stale.remember('port', 9000, evidence=['caller:stale'])
        self.assertEqual(self.Project(self.repo).state.facts['port']['value'], 8000)

    def test_history_paginates_without_duplicates(self):
        for i in range(4):
            self.project.checkpoint(f'Decision {i}')
        first = self.project.history(limit=2)
        second = self.project.history(limit=2, cursor=first['next_cursor'])
        self.assertEqual(len(first['items']), 2)
        self.assertEqual(len(second['items']), 2)
        self.assertFalse({v['id'] for v in first['items']} & {v['id'] for v in second['items']})

    def test_verified_promotion_requires_configured_checks(self):
        with self.assertRaises(ValueError):
            self.project.checkpoint('not actually verified', verify=True)
        self.assertIsNone(self.project.status()['canonical_head'])

    def test_model_binding_survives_reopen_without_inference(self):
        old_head = self.project.status()['head']
        receipt = self.project.switch_model('second-local', 'http://127.0.0.1:8081/v1/chat/completions')
        reopened = self.Project(self.repo)
        self.assertEqual(reopened.model_config['model_id'], 'second-local')
        self.assertEqual(receipt['previous_checkpoint'], old_head)
        self.assertEqual(reopened.usage()['requests'], 0)
        with self.assertRaises(ValueError):
            self.project.switch_model('bad', 'https://example.com/v1/chat/completions')

    def test_fresh_process_reopens_same_checkpoint(self):
        saved = self.project.checkpoint('Saved across processes')
        code = 'from statetree.project import Project; import sys; print(Project(sys.argv[1]).status()["head"])'
        result = subprocess.run([sys.executable, '-c', code, str(self.repo)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), saved['id'])

class VerifiedProjectTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        # A separate project with an operator-supplied test command.
        self.checked_repo = repo_at(Path(self.temp.name) / 'checked')
        self.checked = self.Project.init(self.checked_repo, goal='Checked project', config={
            'verification_commands': [[sys.executable, '-c', "from pathlib import Path; assert Path('example.txt').read_text() == 'initial'"]]})

    def test_verified_checkpoint_has_command_evidence(self):
        commit = self.checked.checkpoint('Tests passed', verify=True)
        self.assertEqual(self.checked.status()['canonical_head'], commit['id'])
        self.assertTrue(commit['verification']['checks'])
        evidence = self.checked.store.read_archive(commit['verification']['checks'][0]['archive_id'])
        self.assertEqual(evidence['returncode'], 0)
        self.assertEqual(evidence['phase'], 'checkpoint')

    def test_failed_check_does_not_promote_or_change_source(self):
        (self.checked_repo / 'example.txt').write_text('bad', encoding='utf-8')
        before = self.checked.status()['head']
        with self.assertRaises(RuntimeError):
            self.checked.checkpoint('must fail', verify=True)
        self.assertEqual(self.checked.status()['head'], before)
        self.assertIsNone(self.checked.status()['canonical_head'])
        self.assertEqual((self.checked_repo / 'example.txt').read_text(), 'bad')

    def test_verified_canonical_head_survives_restore(self):
        old = self.checked.status()['head']
        verified = self.checked.checkpoint('checked', verify=True)
        self.checked.restore(old)
        self.assertEqual(self.checked.status()['canonical_head'], verified['id'])


class BranchRecoveryTests(unittest.TestCase):
    def setUp(self):
        from statetree.workspace.branches import BranchManager
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = repo_at(Path(self.temp.name) / 'repo')
        self.root = Path(self.temp.name) / 'branches'
        self.manager = BranchManager(self.repo, root=self.root, verifier=lambda *args: True)
        self.manager.initialize({'feature': None})

    def test_named_branch_reopens_after_restart_with_candidate_state(self):
        branch = self.manager.fork('feature')
        (branch.path / 'new.txt').write_text('candidate', encoding='utf-8')
        candidate = self.manager.complete(branch, state={'feature': True})
        from statetree.workspace.branches import BranchManager
        fresh = BranchManager(self.repo, root=self.root, verifier=lambda *args: True)
        self.assertTrue(hasattr(fresh, 'open_branch'), 'Persisted branches need a public reopen operation')
        reopened = fresh.open_branch('feature')
        self.assertEqual(reopened.state, {'feature': True})
        self.assertEqual(reopened.candidate_id, candidate['id'])
        self.assertEqual(fresh.list_branches()[0]['candidate_id'], candidate['id'])
        revision = fresh.integrate(candidate['id'], expected_parent=fresh.head()['id'])
        self.assertEqual(Path(revision['workspace_path'], 'new.txt').read_text(), 'candidate')
        self.assertFalse((self.repo / 'new.txt').exists())

    def test_stale_candidate_writer_is_rejected(self):
        branch = self.manager.fork('feature')
        self.assertTrue(hasattr(self.manager, 'open_branch'), 'Branch reopening is required')
        stale = self.manager.open_branch('feature')
        self.manager.complete(branch, state={'feature': 'fresh'})
        with self.assertRaises(RuntimeError):
            self.manager.complete(stale, state={'feature': 'stale'})
        self.assertEqual(self.manager.open_branch('feature').state, {'feature': 'fresh'})

class ProjectWorkflowTests(ProjectFixture):
    def test_durable_steps_replay_without_repeating_effects(self):
        from statetree.runtime.durable import Step
        effects = []
        def write(arguments, key):
            effects.append(key)
            (self.repo / 'effect.txt').write_text(arguments['text'], encoding='utf-8')
            return {'written': arguments['text']}
        steps = [Step('write', 'write', {'text': 'done'}, mode='idempotent')]
        result = self.project.run_steps(steps, {'write': write}, run_id='flow-1')
        fresh = self.Project(self.repo)
        self.assertEqual(fresh.run_steps(steps, {'write': write}, run_id='flow-1'), result)
        self.assertEqual(len(effects), 1)
        self.assertEqual((self.repo / 'effect.txt').read_text(), 'done')

    def test_checkpoint_publication_gap_recovers_on_fresh_project(self):
        from unittest.mock import patch
        from statetree.runtime.durable import Step
        from statetree.runtime.workflow import _WorkflowStore
        calls = []
        def action(arguments, key):
            calls.append(key)
            return {'value': 42}
        steps = [Step('effect', 'action', {}, mode='idempotent')]
        with patch.object(_WorkflowStore, 'append', side_effect=RuntimeError('crash after checkpoint')):
            with self.assertRaises(RuntimeError):
                self.project.run_steps(steps, {'action': action}, run_id='recover-1')
        fresh = self.Project(self.repo)
        self.assertEqual(fresh.run_steps(steps, {'action': action}, run_id='recover-1'), [{'value': 42}])
        self.assertEqual(len(calls), 1)

    def test_branch_facade_merges_and_explicitly_adopts(self):
        other_repo = repo_at(Path(self.temp.name) / 'branched')
        project = self.Project.init(other_repo, goal='Branch work', config={
            'verification_commands': [[sys.executable, '-c', "from pathlib import Path; assert Path('example.txt').exists()"]]})
        self.assertTrue(hasattr(project, 'fork'), 'Project must expose isolated branches')
        branch = project.fork('experiment')
        Path(branch['path'], 'feature.txt').write_text('new', encoding='utf-8')
        candidate = project.complete_branch('experiment')
        revision = project.merge(candidate['id'])
        self.assertFalse((other_repo / 'feature.txt').exists())
        project.adopt(revision['id'])
        self.assertEqual((other_repo / 'feature.txt').read_text(), 'new')
        self.assertIsNotNone(project.status()['canonical_head'])

    def test_adoption_refuses_changed_source_since_branch_seed(self):
        other_repo = repo_at(Path(self.temp.name) / 'branched')
        project = self.Project.init(other_repo, goal='Branch work', config={
            'verification_commands': [[sys.executable, '-c', 'pass']]})
        self.assertTrue(hasattr(project, 'fork'), 'Project must expose isolated branches')
        branch = project.fork('experiment')
        Path(branch['path'], 'feature.txt').write_text('new', encoding='utf-8')
        candidate = project.complete_branch('experiment')
        revision = project.merge(candidate['id'])
        (other_repo / 'example.txt').write_text('new main work', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            project.adopt(revision['id'])
        self.assertEqual((other_repo / 'example.txt').read_text(), 'new main work')

class AdoptionSafetyTests(ProjectFixture):
    def configured(self):
        other = repo_at(Path(self.temp.name) / 'untracked')
        (other / 'draft.txt').write_text('old draft', encoding='utf-8')
        (other / '.gitignore').write_text('private.txt\n', encoding='utf-8')
        project = self.Project.init(other, goal='Adopt without staging source', config={
            'verification_commands': [[sys.executable, '-c', 'pass']]})
        return other, project

    def test_adopt_updates_checkpointed_untracked_files_and_preserves_index(self):
        repo, project = self.configured()
        before = git(repo, 'write-tree')
        branch = project.fork('experiment')
        Path(branch['path'], 'draft.txt').write_text('new draft', encoding='utf-8')
        candidate = project.complete_branch('experiment')
        revision = project.merge(candidate['id'])
        project.adopt(revision['id'])
        self.assertEqual((repo / 'draft.txt').read_text(), 'new draft')
        self.assertEqual(git(repo, 'write-tree'), before)

    def test_adopt_refuses_collision_with_ignored_source_file(self):
        repo, project = self.configured()
        (repo / 'private.txt').write_text('keep private source', encoding='utf-8')
        branch = project.fork('experiment')
        Path(branch['path'], '.gitignore').write_text('', encoding='utf-8')
        Path(branch['path'], 'private.txt').write_text('branch content', encoding='utf-8')
        candidate = project.complete_branch('experiment')
        revision = project.merge(candidate['id'])
        with self.assertRaises(ValueError):
            project.adopt(revision['id'])
        self.assertEqual((repo / 'private.txt').read_text(), 'keep private source')
