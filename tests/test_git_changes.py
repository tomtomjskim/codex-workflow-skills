import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.workflow_coordination.git_changes import (
    GitStateError,
    collect_changed_paths,
    require_clean_base,
)


class GitChangeCollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Workflow Test")
        self.git("config", "user.email", "workflow@example.invalid")
        (self.repo / "owned").mkdir()
        (self.repo / "owned" / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "owned" / "delete.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "owned" / "rename.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.tree_hash = self.git("rev-parse", "HEAD^{tree}").stdout.strip()

    def git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def collect(self):
        return collect_changed_paths(self.repo, self.tree_hash)

    def test_collects_unstaged_tracked_modification(self):
        (self.repo / "owned" / "tracked.txt").write_text("changed\n", encoding="utf-8")

        self.assertEqual(self.collect(), ("owned/tracked.txt",))

    def test_collects_staged_modification_and_addition(self):
        (self.repo / "owned" / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (self.repo / "owned" / "added.txt").write_text("added\n", encoding="utf-8")
        self.git("add", "owned/tracked.txt", "owned/added.txt")

        self.assertEqual(
            self.collect(),
            ("owned/added.txt", "owned/tracked.txt"),
        )

    def test_collects_staged_and_unstaged_deletions(self):
        (self.repo / "owned" / "delete.txt").unlink()
        self.git("rm", "-q", "owned/rename.txt")

        self.assertEqual(
            self.collect(),
            ("owned/delete.txt", "owned/rename.txt"),
        )

    def test_collects_both_sides_of_rename_with_no_rename_detection(self):
        self.git("mv", "owned/rename.txt", "owned/renamed.txt")

        self.assertEqual(
            self.collect(),
            ("owned/rename.txt", "owned/renamed.txt"),
        )

    def test_collects_untracked_file(self):
        (self.repo / "owned" / "untracked.txt").write_text("new\n", encoding="utf-8")

        self.assertEqual(self.collect(), ("owned/untracked.txt",))

    def test_nul_protocol_preserves_newline_filename(self):
        name = "line\nbreak.txt"
        (self.repo / "owned" / name).write_text("new\n", encoding="utf-8")

        self.assertEqual(self.collect(), ("owned/" + name,))

    def test_untracked_symlink_is_reported_without_traversing_target(self):
        outside = self.repo.parent / "outside"
        outside.mkdir()
        marker = outside / "secret.txt"
        marker.write_text("secret\n", encoding="utf-8")
        (self.repo / "owned" / "outside-link").symlink_to(
            outside, target_is_directory=True
        )

        self.assertEqual(self.collect(), ("owned/outside-link",))
        self.assertEqual(marker.read_text(encoding="utf-8"), "secret\n")

    def test_rejects_invalid_utf8_reported_path(self):
        tree = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=(self.tree_hash + "\n").encode("ascii"),
            stderr=b"",
        )
        status = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=b"?? owned/bad-\xff\0", stderr=b""
        )
        with mock.patch(
            "scripts.workflow_coordination.git_changes.subprocess.run",
            side_effect=(tree, status, tree),
        ):
            with self.assertRaisesRegex(GitStateError, "UTF-8"):
                self.collect()

    def test_rejects_unsafe_reported_path(self):
        completed = subprocess.CompletedProcess(
            args=["git", "status"], returncode=0, stdout=b"?? ../escape\0", stderr=b""
        )
        tree = subprocess.CompletedProcess(
            args=["git", "rev-parse"],
            returncode=0,
            stdout=(self.tree_hash + "\n").encode("ascii"),
            stderr=b"",
        )
        with mock.patch(
            "scripts.workflow_coordination.git_changes.subprocess.run",
            side_effect=(tree, completed, tree),
        ):
            with self.assertRaisesRegex(GitStateError, "unsafe Git path"):
                self.collect()

    def test_git_nonzero_exit_fails_closed(self):
        failed = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout=b"", stderr=b"fatal: injected\n"
        )
        with mock.patch(
            "scripts.workflow_coordination.git_changes.subprocess.run",
            return_value=failed,
        ):
            with self.assertRaisesRegex(GitStateError, "Git command failed"):
                self.collect()

    def test_head_tree_change_during_collection_is_stale(self):
        first = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=(self.tree_hash + "\n").encode("ascii"),
            stderr=b"",
        )
        status = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=b"", stderr=b""
        )
        changed = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=("0" * 40 + "\n").encode("ascii"), stderr=b""
        )
        with mock.patch(
            "scripts.workflow_coordination.git_changes.subprocess.run",
            side_effect=(first, status, changed),
        ):
            with self.assertRaisesRegex(GitStateError, "changed during collection"):
                self.collect()

    def test_dirty_base_is_rejected(self):
        (self.repo / "owned" / "untracked.txt").write_text("new\n", encoding="utf-8")

        with self.assertRaisesRegex(GitStateError, "base worktree must be clean"):
            require_clean_base(self.repo, self.tree_hash)


if __name__ == "__main__":
    unittest.main()
