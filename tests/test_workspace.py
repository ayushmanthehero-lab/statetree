import os
import stat
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from statetree.workspace.git import GitWorkspace


class GitWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="statetree-test-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "StateTree tests")
        self.git("config", "user.email", "statetree@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.write("tracked.txt", "base\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "initial")
        self.head = self.git("rev-parse", "HEAD")
        self.workspace = GitWorkspace(self.repo)

    def git(self, *args, input=None):
        return subprocess.run(
            ["git", *args], cwd=self.repo, input=input, text=True,
            capture_output=True, check=True,
        ).stdout.strip()

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_restore_keeps_staged_and_worktree_versions_separate(self):
        self.write("tracked.txt", "staged\n")
        self.git("add", "tracked.txt")
        self.write("tracked.txt", "working\n")
        index_before = (self.repo / ".git/index").read_bytes()
        sha = self.workspace.snapshot()
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)
        self.write("tracked.txt", "later\n")
        self.git("add", "tracked.txt")

        self.workspace.restore(sha)

        self.assertEqual(self.git("show", ":tracked.txt"), "staged")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "working\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_captures_untracked_and_preserves_unrelated_later_files(self):
        self.write("draft/new.txt", "checkpoint\n")
        sha = self.workspace.snapshot()
        self.write("draft/new.txt", "changed\n")
        self.write("later.txt", "keep me\n")
        self.write("tracked-extra.txt", "remove me\n")
        self.git("add", "tracked-extra.txt")

        self.workspace.restore(sha)

        self.assertEqual((self.repo / "draft/new.txt").read_text(), "checkpoint\n")
        self.assertEqual((self.repo / "later.txt").read_text(), "keep me\n")
        self.assertFalse((self.repo / "tracked-extra.txt").exists())
        self.assertEqual(self.git("ls-files"), "tracked.txt")

    def test_excludes_ignored_defaults_and_explicit_paths(self):
        self.write(".gitignore", "ignored/\n")
        for name in ("ignored/file", ".statetree/state", ".venv/bin/python", "private/key"):
            self.write(name, "private\n")
        self.write("included.txt", "included\n")
        workspace = GitWorkspace(self.repo, exclude_paths=("private",))

        sha = workspace.snapshot()

        self.assertEqual(
            self.git("ls-tree", "-r", "--name-only", sha).splitlines(),
            [".gitignore", "included.txt", "tracked.txt"],
        )
        self.write("private/key", "new private\n")
        workspace.restore(sha)
        self.assertEqual((self.repo / "private/key").read_text(), "new private\n")

    def test_ignored_internal_storage_is_not_an_explicit_git_add_error(self):
        self.write('.gitignore', '.statetree/\n.venv/\n.env\n')
        self.write('.statetree/project/state.json', 'internal')
        self.write('.env', 'SECRET=not-for-snapshots')
        self.write('new [file].txt', 'literal name')
        (self.repo / 'tracked.txt').unlink()
        sha = self.workspace.snapshot()
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', sha).splitlines(),
                         ['.gitignore', 'new [file].txt'])

    def test_clean_snapshot_is_marked_pinned_and_survives_pruning(self):
        sha = self.workspace.snapshot()
        self.assertNotEqual(sha, self.head)
        self.assertEqual(self.git("rev-parse", "refs/statetree/workspaces/" + sha), sha)
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now")
        self.workspace.validate(sha)
        self.write("tracked.txt", "later\n")
        self.workspace.restore(sha)
        self.assertEqual((self.repo / "tracked.txt").read_text(), "base\n")

    def test_rejects_non_checkpoint_without_mutation(self):
        self.write("tracked.txt", "keep\n")
        self.git("add", "tracked.txt")
        index_before = (self.repo / ".git/index").read_bytes()
        for invalid in (self.head, "0" * 40, "HEAD", "--help"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.workspace.restore(invalid)
                self.assertEqual((self.repo / "tracked.txt").read_text(), "keep\n")
                self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)

    def test_untracked_overwrite_collision_fails_before_any_restore(self):
        self.write("another.txt", "checkpoint\n")
        self.git("add", "another.txt")
        sha = self.workspace.snapshot()
        self.git("rm", "--cached", "another.txt")
        self.write("another.txt", "untracked later content\n")
        self.write("tracked.txt", "keep\n")
        index_before = (self.repo / ".git/index").read_bytes()

        with self.assertRaisesRegex(ValueError, "untracked|collision"):
            self.workspace.restore(sha)

        self.assertEqual((self.repo / "another.txt").read_text(), "untracked later content\n")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "keep\n")
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)

    def test_untracked_directory_collision_preserves_its_contents(self):
        sha = self.workspace.snapshot()
        (self.repo / "tracked.txt").unlink()
        self.write("tracked.txt/later.txt", "keep\n")
        with self.assertRaisesRegex(ValueError, "untracked|collision"):
            self.workspace.restore(sha)
        self.assertEqual((self.repo / "tracked.txt/later.txt").read_text(), "keep\n")

    def test_missing_blob_fails_before_mutating_any_file(self):
        self.write("unique.txt", "unique checkpoint blob\n")
        sha = self.workspace.snapshot()
        blob = self.git("rev-parse", sha + ":unique.txt")
        object_path = self.repo / ".git/objects" / blob[:2] / blob[2:]
        object_path.chmod(stat.S_IWRITE | stat.S_IREAD)
        object_path.unlink()
        self.write("tracked.txt", "keep\n")
        index_before = (self.repo / ".git/index").read_bytes()
        with self.assertRaises(ValueError):
            self.workspace.restore(sha)
        self.assertEqual((self.repo / "tracked.txt").read_text(), "keep\n")
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)

    def test_deleted_file_and_staged_addition_restore_correctly(self):
        (self.repo / "tracked.txt").unlink()
        self.write("added.txt", "staged add\n")
        self.git("add", "added.txt")
        self.write("added.txt", "worktree add\n")
        sha = self.workspace.snapshot()
        self.write("tracked.txt", "later\n")
        self.write("added.txt", "later\n")

        self.workspace.restore(sha)

        self.assertFalse((self.repo / "tracked.txt").exists())
        self.assertEqual(self.git("show", ":tracked.txt"), "base")
        self.assertEqual(self.git("show", ":added.txt"), "staged add")
        self.assertEqual((self.repo / "added.txt").read_text(), "worktree add\n")

    def test_leading_space_in_tracked_filename_is_preserved(self):
        self.write(' leading.txt', 'saved\n')
        self.git('add', ' leading.txt')
        sha = self.workspace.snapshot()
        self.write(' leading.txt', 'changed\n')
        self.workspace.restore(sha)
        self.assertEqual((self.repo / ' leading.txt').read_text(), 'saved\n')

    def test_later_directory_at_tracked_extra_is_rejected_before_writes(self):
        sha = self.workspace.snapshot()
        self.write('extra', 'tracked later')
        self.git('add', 'extra')
        (self.repo / 'extra').unlink()
        self.write('extra/unrelated.txt', 'keep')
        self.write('tracked.txt', 'keep current')
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.workspace.restore(sha)
        self.assertEqual((self.repo / 'tracked.txt').read_text(), 'keep current')
        self.assertEqual((self.repo / 'extra/unrelated.txt').read_text(), 'keep')

    def test_index_flags_cannot_silently_omit_worktree_edits(self):
        for flag in ('--assume-unchanged', '--skip-worktree'):
            with self.subTest(flag=flag):
                self.git('update-index', flag, 'tracked.txt')
                self.write('tracked.txt', 'actual worktree edit')
                before = (self.repo / '.git/index').read_bytes()
                with self.assertRaisesRegex(ValueError, 'index flags'):
                    self.workspace.snapshot()
                self.assertEqual((self.repo / '.git/index').read_bytes(), before)
                self.git('update-index', '--no-assume-unchanged', 'tracked.txt')
                self.git('update-index', '--no-skip-worktree', 'tracked.txt')

    def test_same_size_same_timestamp_edit_is_captured_without_changing_staging(self):
        # A copied file may retain its mtime; Git's index stat cache is not a
        # content guarantee, especially with coarse timestamp configurations.
        self.git('config', 'core.trustctime', 'false')
        self.git('config', 'core.checkStat', 'minimal')
        self.write('tracked.txt', 'staged\n')
        path = self.repo / 'tracked.txt'
        old = time.time_ns() - 10_000_000_000
        os.utime(path, ns=(old, old))
        self.git('add', 'tracked.txt')
        metadata = path.stat()
        self.write('tracked.txt', 'actual\n')
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        before = (self.repo / '.git/index').read_bytes()

        sha = self.workspace.snapshot()

        self.assertEqual(self.git('show', sha + ':tracked.txt'), 'actual')
        self.assertEqual(self.git('show', sha + '^2:tracked.txt'), 'staged')
        self.assertEqual((self.repo / '.git/index').read_bytes(), before)
        self.assertEqual(path.read_text(), 'actual\n')


if __name__ == "__main__":
    unittest.main()
