"""Tests for the SQLite-backed roadmap HTTP boundary."""
import json
import contextlib
import io
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


class RoadmapTransactionsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_db, self.old_board = roadmap.STORE_PATH, roadmap.BOARD
        self.db = Path(self.temp.name) / "roadmap.sqlite3"
        self.board = Path(self.temp.name) / "historical.md"
        self.board.write_text("obsolete board text", encoding="utf-8")
        roadmap.STORE_PATH, roadmap.BOARD = self.db, self.board
        RoadmapStore(self.db).import_v1([
            {"id": "root", "title": "Root", "status": "active", "summary": "fixture",
             "dependencies": [], "evidence": [], "subtasks": [
                 {"id": "child", "title": "Child", "status": "ready", "summary": "deep",
                  "evidence": [], "attempts": []}]}
        ], "a" * 64)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), roadmap.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        roadmap.STORE_PATH, roadmap.BOARD = self.old_db, self.old_board
        self.temp.cleanup()

    def get(self, path="/api/tasks"):
        with urllib.request.urlopen(self.base + path) as response:
            return json.loads(response.read())

    def post(self, path, payload):
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def test_reads_use_database_flat_snapshot_and_graph_contract(self):
        tasks = self.get()
        self.assertEqual("sqlite", tasks["source"])
        self.assertEqual(["root", "child"], [t["id"] for t in tasks["tasks"]])
        graph = self.get("/api/graph")
        self.assertEqual("task-graph/v1", graph["schema"])
        self.assertEqual({"root", "child"}, {n["id"] for n in graph["nodes"]})
        self.assertTrue(graph["nodes"][0]["provenance"])

    def test_merge_preview_is_scoped_readonly_and_requires_both_tasks(self):
        from urllib.parse import urlencode
        project=self.get('/v1/projects')['items'][0]['id']
        before=self.get()
        query=urlencode({'project_id':project,'survivor_id':'root','absorbed_id':'child'})
        result=self.get('/v1/tasks/merge-preview?'+query)
        self.assertEqual(result['revision'],before['revision'])
        self.assertFalse(result['preview']['apply_available'])
        self.assertIn('containment_ancestor',[item['kind'] for item in result['preview']['problems']])
        self.assertEqual(self.get(),before)
        for params in ({'project_id':project,'survivor_id':'root'},
                       {'project_id':project,'survivor_id':'root','absorbed_id':'child','limit':1}):
            with self.assertRaises(urllib.error.HTTPError) as exc:
                self.get('/v1/tasks/merge-preview?'+urlencode(params))
            self.assertEqual(exc.exception.code,400)

    def test_transaction_and_compatibility_wrappers_share_store(self):
        current = self.get()
        updated = self.post("/api/transaction", {
            "expected_revision": current["revision"], "request_id": "api-test-1",
            "actor": "tester", "operations": [{"op": "update", "task_id": "child",
                                                   "fields": {"status": "active"}}]})
        self.assertEqual("active", next(t for t in updated["tasks"] if t["id"] == "child")["record"]["status"])
        claimed = self.post("/api/claim", {"task_id": "root", "claimant": "tester",
                                             "revision": updated["revision"]})
        self.assertEqual("tester", next(t for t in claimed["tasks"] if t["id"] == "root")["record"]["claim"]["by"])

    def test_arbitrary_depth_descendant_updates_by_stable_id(self):
        store = RoadmapStore(self.db)
        snap = store.snapshot()
        store.transact(snap["revision"], "add-grandchild", "tester", [{"op": "create", "parent_id": "child",
            "record": {"id": "grandchild", "title": "Grandchild", "status": "ready", "summary": "deep fixture"}}])
        current = self.get()
        result = self.post("/api/task-update", {"task_id": "grandchild", "revision": current["revision"],
                                                  "fields": {"status": "active"}})
        self.assertEqual("active", next(t for t in result["tasks"] if t["id"] == "grandchild")["record"]["status"])

    def test_missing_and_corrupt_database_never_fall_back(self):
        self.db.unlink()
        self.board.write_text("<!-- ROADMAP_TASK_RECORDS:START --> [] <!-- ROADMAP_TASK_RECORDS:END -->", encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(self.base + "/api/tasks")
        self.assertIn(missing.exception.code, (500, 503))
        self.db.write_bytes(b"not sqlite")
        with self.assertRaises(urllib.error.HTTPError) as corrupt:
            urllib.request.urlopen(self.base + "/api/graph")
        self.assertEqual(503, corrupt.exception.code)

    def test_service_lock_refuses_second_writer_for_same_canonical_path(self):
        first = roadmap.service_lock(self.db)
        first.__enter__()
        try:
            with self.assertRaises(roadmap.RoadmapError) as ctx:
                with roadmap.service_lock(self.db):
                    pass
            self.assertEqual(503, ctx.exception.status)
        finally:
            first.__exit__(None, None, None)

    def test_cli_uses_explicit_store_path_and_missing_store_fails_closed(self):
        with self.assertRaises(SystemExit) as missing:
            roadmap.main(["--store", str(Path(self.temp.name) / "missing.sqlite")])
        self.assertIn("missing", str(missing.exception).lower())
        old_server = roadmap.ThreadingHTTPServer
        observed = {}
        class OneShotServer:
            def __init__(self, address, handler):
                observed["address"] = address
            def serve_forever(self):
                return
            def server_close(self):
                return
        roadmap.ThreadingHTTPServer = OneShotServer
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                roadmap.main(["--store", str(self.db), "--port", "0"])
            self.assertEqual("127.0.0.1", observed["address"][0])
        finally:
            roadmap.ThreadingHTTPServer = old_server


if __name__ == "__main__":
    unittest.main()
