import importlib
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests.helpers import git, init_repo


class BranchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('statetree.workspace.branches'),
                             'Isolated branch implementation is required')
        self.module = importlib.import_module('statetree.workspace.branches')
        self.temp = tempfile.TemporaryDirectory(prefix='statetree-branches-')
        self.addCleanup(self.temp.cleanup)
        self.repo = init_repo(Path(self.temp.name) / 'repo')
        self.root = Path(self.temp.name) / 'managed'
        self.verifications = []

        def verifier(path, state, phase):
            self.verifications.append((Path(path), state, phase))
            return state.get('valid', True) is True

        self.manager = self.module.BranchManager(self.repo, root=self.root, verifier=verifier)
        self.base = self.manager.initialize(
            {'facts': {'left': 0, 'right': 0}, 'valid': True},
            resource_versions={'catalog': 'v1'},
        )

    def candidate(self, name, filename, text, *, state=None, **declarations):
        branch = self.manager.fork(name, **declarations)
        (branch.path / filename).write_text(text, encoding='utf-8')
        return branch, self.manager.complete(
            branch, state=branch.state if state is None else state, result={'name': name},
        )

    def test_real_worktrees_preserve_dirty_original_and_separate_indexes(self):
        (self.repo / 'file.txt').write_text('staged', encoding='utf-8')
        git(self.repo, 'add', 'file.txt')
        (self.repo / 'file.txt').write_text('working', encoding='utf-8')
        (self.repo / 'draft.txt').write_text('untracked', encoding='utf-8')
        original_index = (self.repo / '.git/index').read_bytes()
        original_head = git(self.repo, 'rev-parse', 'HEAD')
        other = self.module.BranchManager(
            self.repo, root=Path(self.temp.name) / 'dirty-manager', verifier=lambda *args: True,
        )
        other.initialize({'goal': 'preserve'})
        left = other.fork('left')
        right = other.fork('right')
        self.assertNotEqual(left.path, right.path)
        self.assertEqual((left.path / 'file.txt').read_text(), 'working')
        self.assertEqual((right.path / 'draft.txt').read_text(), 'untracked')
        (left.path / 'file.txt').write_text('left', encoding='utf-8')
        git(left.path, 'add', 'file.txt')
        self.assertEqual((right.path / 'file.txt').read_text(), 'working')
        self.assertNotEqual(git(left.path, 'rev-parse', '--git-path', 'index'),
                            git(right.path, 'rev-parse', '--git-path', 'index'))
        self.assertEqual((self.repo / '.git/index').read_bytes(), original_index)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), original_head)
        self.assertEqual((self.repo / 'file.txt').read_text(), 'working')

    def test_nonoverlapping_file_and_nested_json_changes_merge_from_same_base(self):
        left, first = self.candidate('left', 'left.txt', 'left',
                                     state={'facts': {'left': 1, 'right': 0}, 'valid': True})
        right, second = self.candidate('right', 'right.txt', 'right',
                                       state={'facts': {'left': 0, 'right': 2}, 'valid': True})
        one = self.manager.integrate(first['id'], expected_parent=self.base['id'])
        two = self.manager.integrate(second['id'], expected_parent=one['id'])
        self.assertEqual(two['state']['facts'], {'left': 1, 'right': 2})
        self.assertEqual(git(self.repo, 'show', two['workspace_sha'] + ':left.txt'), 'left')
        self.assertEqual(git(self.repo, 'show', two['workspace_sha'] + ':right.txt'), 'right')
        self.assertEqual(self.manager.head()['id'], two['id'])
        self.assertEqual([entry[2] for entry in self.verifications],
                         ['candidate', 'integrated', 'candidate', 'integrated'])
        self.assertEqual((left.path / 'left.txt').read_text(), 'left')
        self.assertFalse((self.repo / 'left.txt').exists())
        self.assertEqual((right.path / 'right.txt').read_text(), 'right')

    def test_overlapping_file_changes_cannot_overwrite_canonical(self):
        _, first = self.candidate('left', 'file.txt', 'left')
        _, second = self.candidate('right', 'file.txt', 'right')
        published = self.manager.integrate(first['id'], expected_parent=self.base['id'])
        with self.assertRaisesRegex(self.module.MergeConflictError, 'file.txt'):
            self.manager.integrate(second['id'], expected_parent=published['id'])
        self.assertEqual(self.manager.head()['id'], published['id'])

    def test_conflicting_json_updates_and_delete_modify_are_rejected(self):
        for name, base, current, candidate in (
            ('value', {'a': 0}, {'a': 1}, {'a': 2}),
            ('deleted', {'a': {'nested': 1}}, {}, {'a': {'nested': 2}}),
            ('list', {'a': [0]}, {'a': [1]}, {'a': [2]}),
        ):
            with self.subTest(name=name):
                with self.assertRaises(self.module.MergeConflictError):
                    self.module.merge_json(base, current, candidate)

    def test_declared_read_dependency_and_overlapping_write_versions_are_checked(self):
        _, reader = self.candidate('reader', 'reader.txt', 'read result', reads={'catalog': 'v1'})
        _, writer = self.candidate('writer', 'writer.txt', 'write result', writes={'catalog': 'v1'})
        _, other_writer = self.candidate('writer-two', 'other.txt', 'other', writes={'catalog': 'v1'})
        current = self.manager.integrate(writer['id'], expected_parent=self.base['id'])
        self.assertNotEqual(current['resources']['catalog'], 'v1')
        for candidate in (reader, other_writer):
            with self.assertRaisesRegex(self.module.MergeConflictError, 'catalog'):
                self.manager.integrate(candidate['id'], expected_parent=current['id'])
        self.assertEqual(self.manager.head()['id'], current['id'])

    def test_stale_parent_is_rejected_even_for_nonoverlapping_changes(self):
        _, first = self.candidate('left', 'left.txt', 'left')
        _, second = self.candidate('right', 'right.txt', 'right')
        published = self.manager.integrate(first['id'], expected_parent=self.base['id'])
        with self.assertRaises(self.module.StaleParentError):
            self.manager.integrate(second['id'], expected_parent=self.base['id'])
        self.assertEqual(self.manager.head()['id'], published['id'])

    def test_verifier_failure_and_exception_preserve_canonical(self):
        for name, verifier in (
            ('false', lambda path, state, phase: False),
            ('exception', lambda path, state, phase: 1 / 0),
            ('integrated', lambda path, state, phase: phase == 'candidate'),
        ):
            with self.subTest(name=name):
                manager = self.module.BranchManager(self.repo, root=self.root, verifier=verifier)
                branch = manager.fork(name)
                candidate = manager.complete(branch, state=branch.state, result={'passed': True})
                with self.assertRaises(self.module.VerificationError):
                    manager.integrate(candidate['id'], expected_parent=self.base['id'])
                self.assertEqual(manager.head()['id'], self.base['id'])
        self.assertEqual((self.repo / 'file.txt').read_text(), 'initial')

    def test_reopen_preserves_base_and_candidate_manifests(self):
        branch, candidate = self.candidate('saved', 'saved.txt', 'saved')
        branch.state['facts']['left'] = 'mutated caller'
        reopened = self.module.BranchManager(self.repo, root=self.root, verifier=lambda *args: True)
        result = reopened.integrate(candidate['id'], expected_parent=self.base['id'])
        self.assertEqual(result['state']['facts']['left'], 0)
        self.assertEqual(git(self.repo, 'show', result['workspace_sha'] + ':saved.txt'), 'saved')

    def test_invalid_branch_paths_and_wrong_declared_version_fail(self):
        for name in ('../outside', '/outside', 'a/b', '.git', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.manager.fork(name)
        with self.assertRaises(self.module.MergeConflictError):
            self.manager.fork('bad-read', reads={'catalog': 'wrong'})
        with self.assertRaises(ValueError):
            self.module.BranchManager(self.repo, root=self.repo / 'nested', verifier=lambda *args: True)

    def test_file_read_version_detects_a_changed_dependency(self):
        version = self.manager.resource_version(self.base, 'file:file.txt')
        _, reader = self.candidate('file-reader', 'derived.txt', 'derived',
                                    reads={'file:file.txt': version})
        _, writer = self.candidate('file-writer', 'file.txt', 'changed')
        published = self.manager.integrate(writer['id'], expected_parent=self.base['id'])
        with self.assertRaisesRegex(self.module.MergeConflictError, 'file:file.txt'):
            self.manager.integrate(reader['id'], expected_parent=published['id'])

    def test_file_dependency_aliases_cannot_be_mistaken_for_absent_files(self):
        aliases = ('./file.txt', 'dir/../file.txt', 'dir//file.txt', 'FILE.TXT', 'file.txt.')
        for alias in aliases:
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                self.manager.resource_version(self.base, 'file:' + alias)
        with self.assertRaises(ValueError):
            self.manager.fork('aliased-reader', reads={'file:./file.txt': None})

    def test_simultaneous_publication_only_one_expected_parent_wins(self):
        _, left = self.candidate('race-left', 'left.txt', 'left')
        _, right = self.candidate('race-right', 'right.txt', 'right')
        barrier = threading.Barrier(2)

        def verifier(path, state, phase):
            if phase == 'integrated':
                barrier.wait(timeout=20)
            return True

        self.manager.verifier = verifier

        def integrate(candidate):
            try:
                return self.manager.integrate(candidate['id'], expected_parent=self.base['id'])
            except self.module.StaleParentError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(integrate, [left, right]))
        published = [value for value in results if value is not None]
        self.assertEqual(len(published), 1)
        self.assertEqual(self.manager.head()['id'], published[0]['id'])

    def test_verifier_must_check_immutable_candidate_and_not_mutate_files(self):
        branch, candidate = self.candidate('finished', 'file.txt', 'candidate')
        (branch.path / 'file.txt').write_text('changed after completion', encoding='utf-8')
        observed = []

        def verifier(path, state, phase):
            observed.append((path / 'file.txt').read_text())
            if phase == 'integrated':
                (path / 'file.txt').write_text('verifier changed files', encoding='utf-8')
            return True

        self.manager.verifier = verifier
        with self.assertRaises(self.module.VerificationError):
            self.manager.integrate(candidate['id'], expected_parent=self.base['id'])
        self.assertEqual(observed, ['candidate', 'candidate'])
        self.assertEqual(self.manager.head()['id'], self.base['id'])

    def test_verifier_cannot_validate_a_mutated_state_instead_of_published_state(self):
        branch, candidate = self.candidate('state-mutation', 'file.txt', 'candidate',
                                           state={'valid': False})

        def verifier(path, state, phase):
            state['valid'] = True
            return state['valid']

        self.manager.verifier = verifier
        with self.assertRaises(self.module.VerificationError):
            self.manager.integrate(candidate['id'], expected_parent=self.base['id'])
        self.assertEqual(self.manager.head()['id'], self.base['id'])


if __name__ == '__main__':
    unittest.main()
