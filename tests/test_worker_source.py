"""Verify local source registration through the HTTP contract."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
import workspace
from tools import roadmap_server as server
from tools.roadmap_store import RoadmapStore
from tools.workbench_source import register
from roadmap_cli import ApiError


class WorkerSource(unittest.TestCase):
    def test_registration_and_wrong_root_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            self.addCleanup(setattr, server, "STORE_PATH", server.STORE_PATH)
            server.STORE_PATH = repo / "store.sqlite3"
            store = RoadmapStore(server.STORE_PATH)
            project = store.read_v1("projects")["items"][0]["id"]
            (repo / ".research-workspace").mkdir()
            (repo / ".research-workspace/project.json").write_text(json.dumps({
                "protocol": workspace.PROTOCOL, "project_id": project, "name": "Fixture"}))
            http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            http.shared_mode = False
            threading.Thread(target=http.serve_forever, daemon=True).start()
            self.addCleanup(http.server_close)
            self.addCleanup(http.shutdown)
            url = "http://127.0.0.1:" + str(http.server_port)
            other = repo / "other"
            other.mkdir()
            (repo / "source.bin").write_bytes(b"original bytes")
            (other / "source.bin").write_bytes(b"different bytes")
            workspace.transaction(url, project, [
                dict(op="create", alias="task", record={"title": "Compare source", "status": "ready"}),
                dict(op="register_root", id="root", path=str(repo)),
                dict(op="register_root", id="wrong", path=str(other))])
            first = register(repo, url, "task", "root", "source", "source.bin", "binary", "build")
            repeat = register(repo, url, "task", "root", "different-proposed-id", "source.bin", "binary", "build")
            self.assertEqual(first["artifact_version_id"], repeat["artifact_version_id"])
            self.assertEqual(repeat["artifact_id"], "source")
            with self.assertRaises(ValueError):
                register(repo, url, "task", "root", "source", "../outside", "binary", "build")
            before = store.snapshot()["revision"]
            with self.assertRaises(ApiError) as caught:
                register(repo, url, "task", "wrong", "bad", "source.bin", "binary", "build")
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(store.snapshot()["revision"], before)
            ids = {item["id"] for item in store.read_v1("artifacts", project)["items"]}
            self.assertNotIn("bad", ids)


if __name__ == "__main__":
    unittest.main()
