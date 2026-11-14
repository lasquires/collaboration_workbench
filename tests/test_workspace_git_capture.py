import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.workspace_git_capture import capture_commit


class GitCaptureTests(unittest.TestCase):
    def make_repo(self, files):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        for name, data in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        return temp, root, commit

    def test_pins_commit_blobs_and_unicode_identifier_locations(self):
        temp, root, commit = self.make_repo({"src/a.py": "α = 1\nname = α\n".encode()})
        self.addCleanup(temp.cleanup)
        (root / "src/a.py").write_text("dirty = 999\n", encoding="utf-8")
        result = capture_commit(root, commit)
        self.assertEqual(result["format"], "workbench-git-capture/v1")
        item = result["files"][0]
        original = "α = 1\nname = α\n".encode()
        self.assertEqual(item["body"], original)
        self.assertEqual(item["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(item["tokens"], [
            {"identifier": "α", "byte_offset": 0, "byte_length": 2, "line": 1, "column": 1},
            {"identifier": "name", "byte_offset": 7, "byte_length": 4, "line": 2, "column": 1},
            {"identifier": "α", "byte_offset": 14, "byte_length": 2, "line": 2, "column": 8},
        ])
        self.assertTrue(result["coverage"]["complete"])

    def test_unicode_combining_mark_stays_in_identifier_with_exact_offsets(self):
        source = "café = 1\n".encode("utf-8")
        temp, root, commit = self.make_repo({"unicode.py": source})
        self.addCleanup(temp.cleanup)
        token = capture_commit(root, commit)["files"][0]["tokens"][0]
        self.assertEqual(token, {"identifier": "café", "byte_offset": 0, "byte_length": 6,
                                 "line": 1, "column": 1})

    def test_binary_is_pinned_without_semantic_tokens(self):
        temp, root, commit = self.make_repo({"image.bin": b"A\x00B"})
        self.addCleanup(temp.cleanup)
        item = capture_commit(root, commit)["files"][0]
        self.assertEqual(item["availability"], "available")
        self.assertEqual(item["text_state"], "binary")
        self.assertEqual(item["body"], b"A\x00B")
        self.assertEqual(item["tokens"], [])

    def test_resource_limits_are_explicit_exclusions(self):
        temp, root, commit = self.make_repo({"a.txt": b"alpha", "b.txt": b"beta"})
        self.addCleanup(temp.cleanup)
        result = capture_commit(root, commit, max_file_bytes=4)
        self.assertTrue(any(f["availability"] == "excluded" for f in result["files"]))
        self.assertFalse(result["coverage"]["complete"])
        self.assertFalse(result["coverage"]["pinned_complete"])

    def test_capture_error_is_compatible_with_git_watcher_errors(self):
        from tools.workspace_git_capture import GitCaptureError
        from tools.workspace_git_watch import GitWatchError
        self.assertTrue(issubclass(GitCaptureError, GitWatchError))

    def test_rejects_non_full_commit_identity(self):
        with self.assertRaises(ValueError):
            capture_commit(".", "HEAD")


    def test_crlf_and_bare_cr_advance_line_and_reset_column(self):
        temp, root, commit = self.make_repo({"lines.txt": b"x\r\ny\rz"})
        self.addCleanup(temp.cleanup)
        tokens = capture_commit(root, commit)["files"][0]["tokens"]
        self.assertEqual([(t["identifier"], t["line"], t["column"], t["byte_offset"]) for t in tokens],
                         [("x", 1, 1, 0), ("y", 2, 1, 3), ("z", 3, 1, 5)])

    def test_limits_cover_file_count_total_bytes_and_tokens(self):
        temp, root, commit = self.make_repo({"a file.txt": b"alpha beta", "é.txt": b"gamma"})
        self.addCleanup(temp.cleanup)
        count = capture_commit(root, commit, max_files=1)
        self.assertFalse(count["coverage"]["pinned_complete"])
        self.assertLessEqual(len(count["files"]), 1)
        self.assertEqual(count["coverage"]["excluded"][0]["count"], 1)
        total = capture_commit(root, commit, max_total_bytes=5)
        self.assertFalse(total["coverage"]["pinned_complete"])
        token = capture_commit(root, commit, max_tokens=1)
        self.assertFalse(token["coverage"]["lexical_complete"])
        self.assertEqual(token["coverage"]["token_count"], 1)
        self.assertIn("UTF8 lexical identifiers", token["coverage"]["lexical_scope"])

    def test_symlink_and_submodule_entries_are_excluded_without_following(self):
        temp, root, commit = self.make_repo({"seed": b"ok"})
        self.addCleanup(temp.cleanup)
        symlink_oid = subprocess.check_output(
            ["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=b"../../outside", text=False
        ).decode().strip()
        subprocess.run(["git", "-C", str(root), "update-index", "--add", "--cacheinfo",
                        f"120000,{symlink_oid},link"], check=True)
        fake_commit_oid = "1" * 40
        subprocess.run(["git", "-C", str(root), "update-index", "--add", "--cacheinfo",
                        f"160000,{fake_commit_oid},submodule"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "special entries"], check=True)
        special_commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        by_path = {f["path"]: f for f in capture_commit(root, special_commit)["files"]}
        self.assertEqual(by_path["link"]["availability"], "excluded")
        self.assertEqual(by_path["submodule"]["availability"], "excluded")

    def test_old_commit_survives_later_deletion_and_replacement_refs_are_ignored(self):
        temp, root, old_commit = self.make_repo({"old.py": b"original_name\n"})
        self.addCleanup(temp.cleanup)
        old_index = root / ".git" / "index"
        (root / "old.py").unlink()
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "delete"], check=True)
        replacement_commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(root), "replace", old_commit, replacement_commit], check=True)
        index_before = old_index.read_bytes()
        result = capture_commit(root, old_commit)
        self.assertEqual([f["path"] for f in result["files"]], ["old.py"])
        self.assertEqual(result["files"][0]["body"], b"original_name\n")
        self.assertEqual(old_index.read_bytes(), index_before)

    def test_rejects_nonfinite_deadlines(self):
        import math
        temp, root, commit = self.make_repo({"a": b"x"})
        self.addCleanup(temp.cleanup)
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                capture_commit(root, commit, max_seconds=value)

    def test_rejects_nested_repository_root(self):
        temp, root, commit = self.make_repo({"a": b"x"})
        self.addCleanup(temp.cleanup)
        nested = root / "nested"
        nested.mkdir()
        with self.assertRaises(Exception):
            capture_commit(nested, commit)



