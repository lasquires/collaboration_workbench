"""SQLite authority and compatibility tests for the roadmap server."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import roadmap_server as roadmap
from roadmap_store import RoadmapStore


class RoadmapServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_store, self.old_board = roadmap.STORE_PATH, roadmap.BOARD
        self.db = Path(self.temp.name) / "tasks.sqlite3"
        self.board = Path(self.temp.name) / "BOARD.md"
        self.board.write_text("Historical text mentioning an old task state", encoding="utf-8")
        roadmap.STORE_PATH, roadmap.BOARD = self.db, self.board
        RoadmapStore(self.db).import_v1([{
            "id": "root", "title": "Root", "status": "active", "summary": "Fixture root",
            "dependencies": [], "evidence": [], "subtasks": [{
                "id": "child", "title": "Child", "status": "ready", "summary": "Fixture child",
                "evidence": [], "attempts": ["historical attempt"]}]
        }], "b" * 64)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), roadmap.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        roadmap.STORE_PATH, roadmap.BOARD = self.old_store, self.old_board
        self.temp.cleanup()

    def get(self, path="/api/tasks"):
        with urllib.request.urlopen(self.base + path) as response:
            return json.loads(response.read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())

    def test_tasks_read_flat_database_snapshot_not_markdown(self):
        result = self.get()
        self.assertEqual("sqlite", result["source"])
        self.assertIn(":", result["revision"])
        self.assertTrue(result["partial_coverage"])
        self.assertEqual(["root", "child"], [task["id"] for task in result["tasks"]])
        self.assertEqual("child", result["tasks"][1]["id"])
        self.assertEqual("Historical text mentioning an old task state", self.board.read_text(encoding="utf-8"))

    def test_claim_and_task_update_are_transaction_wrappers(self):
        initial = self.get()
        claimed = self.post("/api/claim", {"task_id": "root", "claimant": "Alice",
                                            "revision": initial["revision"]})
        self.assertEqual("Alice", next(t for t in claimed["tasks"] if t["id"] == "root")["record"]["claim"]["by"])
        updated = self.post("/api/task-update", {"task_id": "child", "revision": claimed["revision"],
                                                   "fields": {"status": "active"}})
        self.assertEqual("active", next(t for t in updated["tasks"] if t["id"] == "child")["record"]["status"])

    def test_compatibility_wrappers_deduplicate_lost_response_retries(self):
        initial = self.get()
        first = roadmap.update_claim("root", "Alice", initial["revision"])
        replay = roadmap.update_claim("root", "Alice", initial["revision"])
        self.assertEqual(first["revision"], replay["revision"])
        self.assertEqual(len(first["events"]), len(replay["events"]))

        current = self.get()
        first = roadmap.update_task("child", current["revision"], {"summary": "edited"})
        replay = roadmap.update_task("child", current["revision"], {"summary": "edited"})
        self.assertEqual(first["revision"], replay["revision"])
        self.assertEqual(len(first["events"]), len(replay["events"]))

    def test_compatibility_claim_rejects_non_boolean_release(self):
        initial = self.get()
        before = RoadmapStore(self.db).snapshot()
        for value in ("false", "", 0, 1, None):
            with self.assertRaises(roadmap.RoadmapError) as ctx:
                roadmap.update_claim("root", "Alice", initial["revision"], release=value)
            self.assertEqual(400, ctx.exception.status)
        after = RoadmapStore(self.db).snapshot()
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(len(before["events"]), len(after["events"]))
        self.assertIsNone(next(t for t in after["tasks"] if t["id"] == "root")["record"].get("claim"))

    def test_http_claim_rejects_non_boolean_release_without_mutation(self):
        initial = self.get()
        before = RoadmapStore(self.db).snapshot()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/claim", {"task_id": "root", "claimant": "Alice",
                                      "revision": initial["revision"], "release": "false"})
        self.assertEqual(400, ctx.exception.code)
        after = RoadmapStore(self.db).snapshot()
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(len(before["events"]), len(after["events"]))

    def test_stale_revision_conflicts_and_idempotent_transaction_replay(self):
        initial = self.get()
        request = {"expected_revision": initial["revision"], "request_id": "repeat-me",
                   "actor": "tester", "operations": [{"op": "journal", "task_id": "child",
                       "kind": "attempt", "text": "checked"}]}
        once = self.post("/api/transaction", request)
        replay = self.post("/api/transaction", request)
        self.assertEqual(once["revision"], replay["revision"])
        self.assertEqual(len(once["events"]), len(replay["events"]))
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self.post("/api/transaction", {**request, "request_id": "stale", "operations": [
                {"op": "journal", "task_id": "child", "kind": "attempt", "text": "stale"}]})
        self.assertEqual(409, conflict.exception.code)

    def test_graph_contract_projects_flat_nodes_and_typed_edges(self):
        store = RoadmapStore(self.db); snap = store.snapshot()
        store.transact(snap["revision"], "link-dependency", "tester", [
            {"op": "add_link", "task_id": "child", "target_id": "root", "kind": "related", "rationale": "fixture"}])
        graph = self.get("/api/graph")
        self.assertEqual("task-graph/v1", graph["schema"])
        self.assertEqual({"root", "child"}, {node["id"] for node in graph["nodes"]})
        self.assertEqual("contains", graph["edges"][0]["type"])
        self.assertIn("related", {edge["type"] for edge in graph["edges"]})
        self.assertIn("events", graph)
        self.assertIn("coverage", graph)

    def test_missing_and_corrupt_database_return_errors_without_markdown_fallback(self):
        self.db.unlink()
        self.board.write_text('{"id":"markdown-only"}', encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.get()
        self.assertIn(missing.exception.code, (500, 503))
        self.db.write_bytes(b"not a sqlite database")
        with self.assertRaises(urllib.error.HTTPError) as corrupt:
            self.get("/api/graph")
        self.assertEqual(503, corrupt.exception.code)

    def test_corrupt_revision_and_bad_transaction_fail_cleanly(self):
        initial = self.get()
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.post("/api/transaction", {"expected_revision": initial["revision"],
                "request_id": "bad", "actor": "tester", "operations": [{"op": "unknown"}]})
        self.assertEqual(400, bad.exception.code)
        self.assertEqual(initial["revision"], self.get()["revision"])


if __name__ == "__main__":
    unittest.main()
