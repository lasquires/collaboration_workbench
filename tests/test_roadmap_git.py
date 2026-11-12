from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.roadmap_git import (
    GitUnavailable,
    InvalidSource,
    NotRepository,
    inspect_repository,
    resolve_source,
)


class RoadmapGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Roadmap Test")
        (self.repo / "nested").mkdir()
        (self.repo / "nested" / "source.txt").write_text("first\nsecond\n", encoding="utf-8")
        self.git("add", "nested/source.txt")
        self.git("commit", "-qm", "initial")
        self.commit = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout

    def test_metadata_reports_head_branch_and_dirty_state(self):
        clean = inspect_repository(self.repo)
        self.assertEqual(clean.head_commit, self.commit)
        if clean.branch is not None:
            self.assertIsInstance(clean.branch, str)
        self.assertFalse(clean.dirty)
        (self.repo / "untracked.txt").write_text("x", encoding="utf-8")
        self.assertTrue(inspect_repository(self.repo).dirty)

    def test_source_is_commit_pinned_after_worktree_changes(self):
        source = resolve_source(self.repo, "HEAD", "nested/source.txt", start_line=1, end_line=2)
        self.assertEqual(source.commit, self.commit)
        self.assertEqual(source.path, "nested/source.txt")
        self.assertTrue(source.blob)
        self.assertEqual((source.start_line, source.end_line), (1, 2))
        (self.repo / "nested" / "source.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual(resolve_source(self.repo, self.commit, "nested/source.txt").blob, source.blob)
        self.git("add", "nested/source.txt")
        self.git("commit", "-qm", "later change")
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), source.commit)
        pinned = resolve_source(self.repo, source.commit, "nested/source.txt")
        self.assertEqual(pinned.blob, source.blob)

    def test_line_bounds_match_blob_lines_including_empty_and_unterminated_files(self):
        (self.repo / "nested" / "source.txt").write_bytes(b"one\ntwo")
        self.git("add", "nested/source.txt")
        self.git("commit", "-qm", "unterminated final line")
        revision = self.git("rev-parse", "HEAD").strip()
        # Start-only ranges are accepted, and the last unterminated line counts.
        self.assertEqual(resolve_source(self.repo, revision, "nested/source.txt", start_line=2).start_line, 2)
        self.assertEqual(resolve_source(self.repo, revision, "nested/source.txt", start_line=2, end_line=2).end_line, 2)
        with self.assertRaises(InvalidSource):
            resolve_source(self.repo, revision, "nested/source.txt", start_line=3)
        with self.assertRaises(InvalidSource):
            resolve_source(self.repo, revision, "nested/source.txt", start_line=1, end_line=3)

        (self.repo / "empty.txt").write_bytes(b"")
        self.git("add", "empty.txt")
        self.git("commit", "-qm", "empty blob")
        empty_revision = self.git("rev-parse", "HEAD").strip()
        empty_source = resolve_source(self.repo, empty_revision, "empty.txt")
        self.assertEqual(empty_source.path, "empty.txt")
        self.assertIsNone(empty_source.start_line)
        with self.assertRaises(InvalidSource):
            resolve_source(self.repo, empty_revision, "empty.txt", start_line=1)

    def test_rejects_traversal_absolute_paths_and_invalid_revisions(self):
        for path in ("../secret", "nested/../../secret", "/etc/passwd", "C:/outside"):
            with self.subTest(path=path), self.assertRaises(InvalidSource):
                resolve_source(self.repo, self.commit, path)
        for revision in ("-option", "no-such-revision"):
            with self.subTest(revision=revision), self.assertRaises(InvalidSource):
                resolve_source(self.repo, revision, "nested/source.txt")

    def test_rejects_missing_blob_and_bad_line_ranges(self):
        with self.assertRaises(InvalidSource):
            resolve_source(self.repo, self.commit, "missing.txt")
        with self.assertRaises(InvalidSource):
            resolve_source(self.repo, self.commit, "nested/source.txt", end_line=3)
        with self.assertRaises(InvalidSource):
            resolve_source(self.repo, self.commit, "nested/source.txt", start_line=3, end_line=2)

    def test_non_repository_and_missing_git_are_explicit(self):
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaises(NotRepository):
                inspect_repository(outside)
        with self.assertRaises(GitUnavailable):
            inspect_repository(self.repo, git=str(self.repo / "no-such-git"))


if __name__ == "__main__":
    unittest.main()
