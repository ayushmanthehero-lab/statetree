"""Recovery, archive-chain and configuration regressions from final review."""
from pathlib import Path
from unittest.mock import patch

from statetree.core.commit import digest
from statetree.project import DEFAULTS, Project, atomic_json
from tests.test_project import ProjectFixture, repo_at


class ProjectHardeningTests(ProjectFixture):
    def test_interrupted_initialization_can_resume_without_a_duplicate_checkpoint(self):
        repo = repo_at(Path(self.temp.name) / 'interrupted')
        with patch('statetree.project.atomic_json', side_effect=OSError('interrupted config publication')):
            with self.assertRaises(OSError):
                Project.init(repo, goal='Recover initialization')
        from statetree.storage.local import LocalStateStore
        head = LocalStateStore(repo / '.statetree/project').get_head()
        recovered = Project.init(repo, goal='Recover initialization')
        self.assertEqual(recovered.status()['head'], head)
        self.assertEqual(len(recovered.history()['items']), 1)

    def test_failed_initialization_cannot_be_rebound_to_a_different_goal(self):
        repo = repo_at(Path(self.temp.name) / 'interrupted')
        with patch('statetree.project.atomic_json', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                Project.init(repo, goal='Original goal')
        with self.assertRaises(ValueError):
            Project.init(repo, goal='Different goal')

    def test_configuration_reloads_before_a_mutating_operation(self):
        config = {**self.project.config, 'total_token_limit': 123}
        atomic_json(self.project.root / 'project.json', config)
        self.project.checkpoint('Reload policy at the boundary')
        self.assertEqual(self.project.config['total_token_limit'], 123)

    def test_verification_timeout_matches_the_verifier_contract(self):
        repo = repo_at(Path(self.temp.name) / 'invalid-timeout')
        with self.assertRaises(ValueError):
            Project.init(repo, goal='Reject bad timeout', config={'verification_timeout': 3601})

    def test_compaction_and_model_binding_retain_prior_archive_chain(self):
        old = self.project.store.put_archive({'older_evidence': 'keep'})
        self.project.agent.state.set('statetree_prior_history', old)
        compact = self.project.compact()
        self.assertEqual(self.project.store.read_archive(compact['archive_id'])['prior_history_archive'], old)
        binding = self.project.switch_model('next', 'http://127.0.0.1:8082/v1/chat/completions')
        self.assertEqual(self.project.store.read_archive(binding['archive_id'])['prior_history_archive'], compact['archive_id'])

    def test_large_archive_index_is_bounded_but_fully_recoverable(self):
        from statetree.core.state import AgentState
        data = self.project.state.to_dict()
        data['archive_ids'] = [self.project.store.put_archive({'observation': i}) for i in range(40)]
        self.project.runtime.set_state(AgentState.from_dict(data))
        context = self.project._prompt_state()
        self.assertLessEqual(len(context['archive_ids']), 8)
        index = self.project.store.read_archive(context['archive_ids'][0])
        self.assertEqual(index['archive_ids'], data['archive_ids'])
        self.assertEqual(self.project.state.archive_ids, data['archive_ids'])

    def test_import_rejects_invalid_interpreted_model_extension_before_publication(self):
        bundle = self.project.export_state()
        bundle['state']['extensions']['statetree.model'] = {**DEFAULTS['model'], 'url': 'https://example.com/v1/chat/completions'}
        bundle['digest'] = digest({key: value for key, value in bundle.items() if key != 'digest'})
        before = self.project.status()['head']
        with self.assertRaises(ValueError):
            self.project.import_state(bundle)
        self.assertEqual(self.project.status()['head'], before)

    def test_portable_imported_branch_marker_does_not_bind_to_foreign_branch_store(self):
        import sys
        original = self.project.state.to_dict()
        original['extensions']['statetree.adopted_revision'] = 'a' * 64
        from statetree.core.state import AgentState
        self.project.runtime.set_state(AgentState.from_dict(original))
        destination_repo = repo_at(Path(self.temp.name) / 'destination')
        destination = Project.init(destination_repo, goal='Import destination', config={
            'verification_commands': [[sys.executable, '-c', 'pass']]})
        destination.import_state(self.project.export_state())
        branch = destination.fork('local-work')
        Path(branch['path'], 'local.txt').write_text('local change', encoding='utf-8')
        candidate = destination.complete_branch('local-work')
        revision = destination.merge(candidate['id'])
        destination.adopt(revision['id'])
        self.assertEqual((destination_repo / 'local.txt').read_text(), 'local change')

    def test_compaction_bundle_contains_original_and_prior_evidence(self):
        old = self.project.store.put_archive({'older': 'evidence'})
        self.project.agent.state.set('statetree_prior_history', old)
        compact = self.project.compact()
        bundle = self.project.export_state()
        self.assertIn(old, bundle['archives'])
        self.assertIn(compact['archive_id'], bundle['archives'])
