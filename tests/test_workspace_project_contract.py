import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from tools.workspace_project_contract import build_contract, resolve_project_remote, validate_manifest


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class ProjectContractTests(unittest.TestCase):
    def test_contract_and_legacy_manifest(self):
        project = str(uuid.uuid4())
        self.assertEqual(validate_manifest({"protocol": "research-workspace/v1", "project_id": project, "name": "Alpha"}),
                         {"protocol": "research-workspace/v1", "project_id": project, "name": "Alpha"})
        contract = build_contract(2, "schema.sql", {"tables": ["tasks"]})
        self.assertEqual(contract["schema"]["portable"]["schema"], {"tables": ["tasks"]})
        self.assertIn("SQLite", contract["schema"]["storage"])

    def test_two_project_remotes_are_scoped_and_credentials_removed(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a", Path(td) / "b"
            for repo in (a, b):
                repo.mkdir()
                git(repo, "init", "-q")
            git(a, "remote", "add", "origin", "https://alice:secret@example.com/org/alpha.git")
            git(b, "remote", "add", "origin", "git@github.com:org/beta.git")
            ca, cb = resolve_project_remote(a), resolve_project_remote(b)
            self.assertEqual(ca["repository"], "https://example.com/org/alpha.git")
            self.assertNotIn("secret", str(ca))
            self.assertEqual(cb["repository"], "ssh://github.com/org/beta.git")
            self.assertEqual(ca["data_ref"], cb["data_ref"])
            self.assertEqual(ca["data_ref"], "refs/heads/workbench-data")

    def test_no_remote_ambiguous_remotes_and_local_bare_remote(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            self.assertIsNone(resolve_project_remote(repo)["repository"])
            git(repo, "remote", "add", "one", "https://example.com/one.git")
            git(repo, "remote", "add", "two", "https://example.com/two.git")
            with self.assertRaisesRegex(ValueError, "multiple remotes"):
                resolve_project_remote(repo)
            self.assertEqual(resolve_project_remote(repo, "two")["repository"], "https://example.com/two.git")
            bare = root / "private.git"
            git(root, "init", "--bare", "-q", str(bare))
            git(repo, "remote", "remove", "one")
            git(repo, "remote", "remove", "two")
            git(repo, "remote", "add", "origin", str(bare))
            local = resolve_project_remote(repo)
            self.assertEqual(local["remote_name"], "origin")
            self.assertIsNone(local["repository"])

    def test_rejects_source_refs_credentials_local_paths_and_bad_settings(self):
        base = {"protocol": "research-workspace/v1", "project_id": str(uuid.uuid4()), "name": "Alpha"}
        bad_syncs = [
            {"mode": "git-only", "data_ref": "refs/heads/main"},
            {"mode": "git-only", "data_ref": "refs/heads/master"},
            {"mode": "git-only", "data_ref": "refs/heads/workbench-data/../main"},
            {"mode": "live", "repository": "https://u:p@example.com/x"},
            {"mode": "live", "repository": "C:\\Users\\someone\\repo.git"},
            {"mode": "oops"},
            {"mode": "hybrid", "min_interval_seconds": 1},
            {"unknown": True},
            {"data_ref": "refs/heads/workbench-data/.hidden"},
            {"data_ref": "refs/heads/workbench-data/cache.lock"},
        ]
        for sync in bad_syncs:
            with self.subTest(sync=sync), self.assertRaises(ValueError):
                validate_manifest({**base, "synchronization": sync})


if __name__ == "__main__":
    unittest.main()
