import json
import threading
import unittest
import tempfile
import contextlib
import io
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tools import roadmap_cli
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import roadmap_server
from roadmap_store import RoadmapStore


class RoadmapCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []
        cls.snapshot = {"revision": "rev-1", "partial_coverage": True, "tasks": [
            {"id": "P", "parent_id": None, "record": {"title": "Parent", "status": "active", "owner": "lead"}},
            {"id": "T", "parent_id": "P", "record": {"title": "Task", "status": "ready", "summary": "Do it", "acceptance": "Works"}},
            {"id": "Q", "parent_id": None, "record": {"title": "Prerequisite", "status": "done", "claim": {"by": "agent-q"}}},
        ], "links": [{"source_id": "T", "target_id": "Q", "kind": "depends_on"}],
            "events": [{"task_id": "T", "sequence": 4, "kind": "attempt", "details": {"text": "previous try"}}]}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps(cls.snapshot).encode())
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.requests.append((self.path, body))
                if getattr(cls, "conflict", False):
                    self.send_response(409); self.send_header("Content-Type", "application/json"); self.end_headers()
                    self.wfile.write(b'{"error":"Task revision changed"}')
                    return
                result = {"revision": "rev-2"}
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps(result).encode())

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        self.requests.clear(); type(self).conflict = False

    def test_context_prints_handoff_and_event_history(self):
        rendered = roadmap_cli.format_context(self.snapshot, "T")
        self.assertIn("Parent: P — Parent", rendered)
        self.assertIn("Prerequisites: Q [done]", rendered)
        self.assertIn("Acceptance: \"Works\"", rendered)
        self.assertIn("attempt: previous try", rendered)

    def test_list_outputs_stable_task_columns_and_ownership(self):
        with patch("sys.stdout") as out:
            self.assertEqual(roadmap_cli.main(["--url", self.url, "list"]), 0)
        rendered = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertIn("WARNING: task coverage is partial", rendered)
        self.assertIn("ID | STATUS | TITLE | PARENT | CLAIM / OWNER", rendered)
        self.assertIn("P | active | Parent | - | owner=lead", rendered)
        self.assertIn("T | ready | Task | P | -", rendered)
        self.assertIn("Q | done | Prerequisite | - | claim=agent-q", rendered)

    def test_list_status_filter(self):
        with patch("sys.stdout") as out:
            self.assertEqual(roadmap_cli.main(["--url", self.url, "list", "--status", "ready"]), 0)
        rendered = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertIn("T | ready | Task", rendered)
        self.assertNotIn("P | active", rendered)
        self.assertNotIn("Q | done", rendered)

    def test_create_subtask_posts_flat_record_with_parent(self):
        record = {"id": "T.child", "title": "Discovery", "status": "ready", "summary": "Inspect"}
        with patch("sys.stderr"):
            code = roadmap_cli.main(["--url", self.url, "create", "--actor", "agent",
                                     "--parent", "T", "--request-id", "create-1",
                                     "--record", json.dumps(record)])
        self.assertEqual(code, 0)
        path, payload = self.requests[0]
        self.assertEqual(path, "/api/transaction")
        self.assertEqual(payload["expected_revision"], "rev-1")
        self.assertEqual(payload["request_id"], "create-1")
        self.assertEqual(payload["operations"], [{"op": "create", "record": record, "parent_id": "T"}])

    def test_claim_uses_transaction_revision_and_request_id(self):
        with patch("sys.stderr"):
            code = roadmap_cli.main(["--url", self.url, "claim", "T", "agent", "--request-id", "retry-7"])
        self.assertEqual(code, 0)
        path, payload = self.requests[0]
        self.assertEqual(path, "/api/transaction")
        self.assertEqual(payload["expected_revision"], "rev-1")
        self.assertEqual(payload["request_id"], "retry-7")
        self.assertEqual(payload["operations"], [{"op": "claim", "task_id": "T", "claimant": "agent"}])

    def test_update_and_journal_operations(self):
        with patch("sys.stderr"):
            self.assertEqual(roadmap_cli.main(["--url", self.url, "update", "T", "--actor", "agent", "--field", 'status="active"']), 0)
            self.assertEqual(roadmap_cli.main(["--url", self.url, "journal", "T", "--actor", "agent", "--kind", "attempt", "--text", "run"]), 0)
        self.assertEqual(self.requests[0][1]["operations"][0]["fields"], {"status": "active"})
        self.assertEqual(self.requests[1][1]["operations"][0]["kind"], "attempt")
        self.assertTrue(self.requests[1][1]["request_id"])

    def test_conflict_is_explicit_and_not_retried(self):
        type(self).conflict = True
        with patch("sys.stderr") as err:
            code = roadmap_cli.main(["--url", self.url, "release", "T", "agent"])
        self.assertEqual(code, 2)
        self.assertIn("STALE CONFLICT (409)", str(err.write.call_args_list))
        self.assertEqual(len(self.requests), 1)

    def test_exchange_export_file_is_direct_preview_input(self):
        bundle = {'id': 'bundle-hash', 'records': {'tasks': []}}
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'exchange.json'
            with patch.object(roadmap_cli, 'request', return_value={'bundle': bundle}) as api:
                with patch('sys.stdout'):
                    self.assertEqual(roadmap_cli.main(['exchange-export', '--project', 'project-x',
                        '--ancestor', 'ancestor / one', '--output', str(output)]), 0)
                self.assertEqual(api.call_args.args[1],
                    '/v1/exchange-export?project_id=project-x&ancestor_id=ancestor+%2F+one')
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), bundle)
            with patch.object(roadmap_cli, 'request', return_value={'preview': {}}) as api:
                with patch('sys.stdout'):
                    self.assertEqual(roadmap_cli.main(['exchange-preview', '--project', 'project-x',
                        '--file', str(output)]), 0)
                self.assertEqual(api.call_args.args[1:], ('/v1/exchange-preview',
                    {'project_id': 'project-x', 'bundle': bundle}))
                self.assertEqual(api.call_count, 1, 'preview does not submit a transaction')

    def test_exchange_export_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'exchange.json'
            output.write_text('precious existing bundle', encoding='utf-8')
            with patch.object(roadmap_cli, 'request', return_value={'bundle': {}}):
                with patch('sys.stderr'):
                    self.assertEqual(roadmap_cli.main(['exchange-export', '--project', 'p',
                        '--output', str(output)]), 2)
            self.assertEqual(output.read_text(encoding='utf-8'), 'precious existing bundle')


class ExchangeCliHttpTests(unittest.TestCase):
    def test_real_service_export_preview_preserves_store_and_determinism(self):
        with tempfile.TemporaryDirectory() as folder:
            store_path = Path(folder) / 'roadmap.sqlite3'
            store = RoadmapStore(store_path)
            store.import_v1([{'id':'task','title':'Unicode evidence α','status':'ready'}], 'a'*64)
            project = store.read_v1('projects')['items'][0]['id']
            before = store.snapshot()
            with patch.object(roadmap_server, 'STORE_PATH', store_path):
                server = ThreadingHTTPServer(('127.0.0.1', 0), roadmap_server.Handler)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    base = ['--url', f'http://127.0.0.1:{server.server_port}']
                    first, second = (Path(folder)/name for name in ('first.json','second.json'))
                    with contextlib.redirect_stdout(io.StringIO()):
                        for output in (first, second):
                            self.assertEqual(roadmap_cli.main(base + ['exchange-export', '--project',
                                project, '--output', str(output)]), 0)
                    self.assertEqual(first.read_bytes(), second.read_bytes())
                    self.assertIn('α', first.read_text(encoding='utf-8'))
                    with contextlib.redirect_stdout(io.StringIO()) as response:
                        self.assertEqual(roadmap_cli.main(base + ['exchange-preview', '--project',
                            project, '--file', str(first)]), 0)
                    changes = json.loads(response.getvalue())['preview']['changes']
                    self.assertTrue(changes)
                    self.assertTrue(all(change['status'] == 'identical' for change in changes))
                    self.assertEqual(store.snapshot(), before)
                finally:
                    server.shutdown(); server.server_close(); worker.join()


if __name__ == "__main__":
    unittest.main()
