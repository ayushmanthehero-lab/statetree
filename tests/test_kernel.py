import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from statetree.core.commit import StateTreeCommit
from statetree.runtime.runtime import StateTreeRuntime
from statetree.storage.local import LocalStateStore
from tests.helpers import OfflineAgent, init_repo


class CommitTests(unittest.TestCase):
    def test_commit_creation_and_content_integrity(self):
        commit = StateTreeCommit.create(
            parent=None, branch='main', goal='finish', current_subgoal=None,
            strands_snapshot_path='snapshots/test.json', workspace_sha='abc',
        )
        self.assertEqual(len(commit.id), 64)
        commit.validate()
        with self.assertRaises(ValueError):
            dataclasses.replace(commit, goal='tampered').validate()


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = init_repo(Path(self.temp.name) / 'repo')
        self.agent = OfflineAgent()
        self.runtime = StateTreeRuntime(self.agent, goal='finish', repo_path=self.repo)

    def verified_commit(self):
        return self.runtime.commit(
            verified=True, verification={'passed': True, 'checks': ['offline assertion']}
        )

    def test_commit_restore_in_fresh_process(self):
        checkpoint = self.verified_commit()
        (self.repo / 'file.txt').write_text('broken', encoding='utf-8')
        script = (
            'from tests.helpers import OfflineAgent; '
            'from statetree.runtime.runtime import StateTreeRuntime; '
            'import sys; a=OfflineAgent(); a.values={"task":"broken"}; '
            'r=StateTreeRuntime(a,goal="finish",repo_path=sys.argv[1]); '
            'r.restore(sys.argv[2]); assert a.values["task"]=="original"'
        )
        result = subprocess.run(
            [sys.executable, '-B', '-c', script, str(self.repo), checkpoint.id],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / 'file.txt').read_text(), 'initial')

    def test_snapshot_tampering_rejected_before_workspace_changes(self):
        checkpoint = self.verified_commit()
        path = self.runtime.store.root / checkpoint.strands_snapshot_path
        path.write_text('{}', encoding='utf-8')
        (self.repo / 'file.txt').write_text('keep current', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.runtime.restore(checkpoint.id)
        self.assertEqual((self.repo / 'file.txt').read_text(), 'keep current')

    def test_unverified_progress_advances_branch_not_canonical(self):
        first = self.runtime.commit()
        second = self.runtime.commit()
        self.assertEqual(second.parent, first.id)
        self.assertEqual(self.runtime.store.get_head(), second.id)
        self.assertIsNone(self.runtime.store.get_canonical_head())

    def test_verified_promotion_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.runtime.commit(verified=True)
        self.assertIsNone(self.runtime.store.get_head())

    def test_stale_runtime_cannot_overwrite_newer_head(self):
        stale = StateTreeRuntime(OfflineAgent(), goal='finish', repo_path=self.repo)
        first = self.runtime.commit()
        with self.assertRaises(RuntimeError):
            stale.commit()
        self.assertEqual(self.runtime.store.get_head(), first.id)

    def test_branch_heads_are_independent(self):
        first = self.verified_commit()
        self.runtime.fork('experiment')
        branch = StateTreeRuntime(
            OfflineAgent(), goal='finish', repo_path=self.repo, branch='experiment'
        )
        candidate = branch.commit()
        self.assertEqual(candidate.parent, first.id)
        self.assertEqual(branch.store.get_head('experiment'), candidate.id)
        self.assertEqual(branch.store.get_head('main'), first.id)
        self.assertEqual(branch.store.get_canonical_head(), first.id)

    def test_restore_rewinds_branch_parent(self):
        first = self.runtime.commit()
        self.runtime.commit()
        self.runtime.restore(first.id)
        self.assertEqual(self.runtime.commit().parent, first.id)

    def test_bad_ids_cannot_escape_store(self):
        with self.assertRaises(ValueError):
            self.runtime.store.load_commit('../outside')

    def test_saved_manifest_is_verified_on_read(self):
        checkpoint = self.runtime.commit()
        path = self.runtime.store.commits_dir / (checkpoint.id + '.json')
        data = json.loads(path.read_text())
        data['goal'] = 'corrupted'
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.runtime.store.load_commit(checkpoint.id)

    def test_process_death_before_publication_commit_does_not_advance_refs(self):
        script = '''
import os, sys
from tests.helpers import OfflineAgent
from statetree.runtime.runtime import StateTreeRuntime
r = StateTreeRuntime(OfflineAgent(), goal='finish', repo_path=sys.argv[1])
move = r.store.move_head
def die_before_transaction_commit(*args, **kwargs):
    move(*args, **kwargs)
    os._exit(17)
r.store.move_head = die_before_transaction_commit
r.commit(verified=True, verification={'passed': True})
'''
        result = subprocess.run(
            [sys.executable, '-B', '-c', script, str(self.repo)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        reopened = LocalStateStore(self.runtime.store.root)
        self.assertIsNone(reopened.get_head())
        self.assertIsNone(reopened.get_canonical_head())
        self.assertGreater(len(list(reopened.commits_dir.glob('*.json'))), 0)


if __name__ == '__main__':
    unittest.main()
