from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.workspace_git_watch import compare_snapshots, snapshot_repository


class WorkspaceGitWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Watcher Test")
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        self.git("add", "seed.txt")
        self.git("commit", "-qm", "initial")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()

    def test_stable_snapshot_and_repeated_dirty_edits(self):
        index_path = self.repo / ".git" / "index"
        index_before = index_path.read_bytes()
        first = snapshot_repository(self.repo)
        self.assertEqual(first, snapshot_repository(self.repo))
        self.assertEqual(index_before, index_path.read_bytes())
        path = self.repo / "seed.txt"
        path.write_text("changed once\n", encoding="utf-8")
        dirty_one = snapshot_repository(self.repo)
        path.write_text("changed twice\n", encoding="utf-8")
        dirty_two = snapshot_repository(self.repo)
        self.assertNotEqual(dirty_one["fingerprint"], dirty_two["fingerprint"])
        self.assertEqual(compare_snapshots(dirty_one, dirty_two)["changed_paths"], ["seed.txt"])

    def test_staging_commit_branch_switch_and_deletion(self):
        path = self.repo / "seed.txt"
        path.write_text("staged\n", encoding="utf-8")
        before = snapshot_repository(self.repo)
        self.git("add", "seed.txt")
        staged = snapshot_repository(self.repo)
        self.assertTrue(compare_snapshots(before, staged)["index_changed"])
        self.git("commit", "-qm", "update")
        committed = snapshot_repository(self.repo)
        self.assertTrue(compare_snapshots(staged, committed)["head_changed"])
        self.git("switch", "-c", "alternate")
        switched = snapshot_repository(self.repo)
        self.assertTrue(compare_snapshots(committed, switched)["branch_changed"])
        path.unlink()
        deleted = snapshot_repository(self.repo)
        self.assertEqual(deleted["paths"][0]["kind"], "deleted")

    def test_unicode_untracked_path(self):
        name = "café 雪.txt"
        (self.repo / name).write_text("hello", encoding="utf-8")
        snap = snapshot_repository(self.repo)
        self.assertEqual(snap["paths"][0]["path"], name)
        self.assertEqual(snap["paths"][0]["working"]["availability"], "available")

    def test_unborn_and_detached_head(self):
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True)
        unborn = snapshot_repository(empty)
        self.assertIsNone(unborn["head"])
        self.assertIsInstance(unborn["branch"], str)
        self.git("checkout", "--detach", "-q", "HEAD")
        detached = snapshot_repository(self.repo)
        self.assertIsNone(detached["branch"])

    def test_unavailable_large_or_outside_symlink_is_explicit(self):
        (self.repo / "large").write_bytes(b"12345")
        self.git("add", "large")
        snap = snapshot_repository(self.repo, max_file_bytes=4)
        large = next(p for p in snap["paths"] if p["path"] == "large")
        self.assertEqual(large["working"]["availability"], "unavailable")
        self.assertTrue(snap["incomplete"])

    def test_clean_commit_reports_committed_changed_paths(self):
        previous = snapshot_repository(self.repo)
        (self.repo / "seed.txt").write_text("committed change\n", encoding="utf-8")
        self.git("add", "seed.txt")
        self.git("commit", "-qm", "clean update")
        current = snapshot_repository(self.repo)
        result = compare_snapshots(previous, current)
        self.assertEqual(result["changed_paths"], ["seed.txt"])
        self.assertFalse(result["incomplete"])

    def test_equal_heads_do_not_hide_repository_scope_mismatch(self):
        previous=snapshot_repository(self.repo)
        other={**previous,'root':str(self.repo.parent/'different-clone')}
        change=compare_snapshots(previous,other)
        self.assertTrue(change['incomplete'])
        self.assertTrue(change['errors'])

    def test_first_commit_lists_paths_from_unborn_baseline(self):
        current=snapshot_repository(self.repo)
        baseline={**current,'head':None,'index_identity':None,'paths':[],'fingerprint':None}
        change=compare_snapshots(baseline,current)
        self.assertEqual(change['changed_paths'],['seed.txt'])
        self.assertFalse(change['incomplete'])

    def test_output_and_scan_budgets_mark_snapshot_incomplete(self):
        (self.repo / "untracked.txt").write_text("content", encoding="utf-8")
        capped = snapshot_repository(self.repo, max_status_bytes=1)
        self.assertTrue(capped["incomplete"])
        self.assertTrue(capped["errors"])
        timed = snapshot_repository(self.repo, max_scan_seconds=0.000001)
        self.assertTrue(timed["incomplete"])
        self.assertTrue(timed["errors"])


if __name__ == "__main__":
    unittest.main()
