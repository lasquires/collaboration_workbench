"""Reclaiming one's own expired lease must renew it, not just repeat its label."""
import argparse
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import workspace
from tools import roadmap_server as server
from tools.roadmap_store import RoadmapStore
from tools.workbench_worker import run
from roadmap_cli import ApiError


class WorkerClaimRenewalTests(unittest.TestCase):
    def test_expired_own_claim_is_renewed_and_other_owner_is_not_stolen(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            self.addCleanup(setattr, server, 'STORE_PATH', server.STORE_PATH)
            server.STORE_PATH = repo / 'fixture.sqlite3'
            store = RoadmapStore(server.STORE_PATH)
            project = store.read_v1('projects')['items'][0]['id']
            (repo / '.research-workspace').mkdir()
            (repo / '.research-workspace/project.json').write_text(json.dumps({
                'protocol': workspace.PROTOCOL, 'project_id': project, 'name': 'Fixture'}))
            http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            http.shared_mode = False
            threading.Thread(target=http.serve_forever, daemon=True).start()
            self.addCleanup(http.server_close)
            self.addCleanup(http.shutdown)
            url = 'http://127.0.0.1:' + str(http.server_port)
            workspace.transaction(url, project, [{'op': 'create', 'alias': 'task', 'record': {
                'title': 'Compare code', 'status': 'ready', 'acceptance': ['Compare original output']}}])
            def claim(worker):
                return run(argparse.Namespace(repo=repo, url=url, action='claim', task='task',
                                              worker=worker, paths=['tools/probe.py'], research=False))
            claim('worker-one')
            # Fixture only: make an existing lease expired without sleeping15minutes.
            with store._connection() as db:
                db.execute("UPDATE claims SET expires_at='2000-01-01T00:00:00+00:00' WHERE released_at IS NULL")
            result = claim('worker-one')
            self.assertEqual(result['claim']['by'], 'worker-one')
            self.assertNotEqual(result['claim_state']['state'], 'expired')
            self.assertGreater(result['claim_state']['expires_at'], '2000-01-01')
            self.assertTrue(result['reserved_paths'])
            with self.assertRaises(ApiError) as rejected:
                claim('worker-two')
            self.assertEqual(rejected.exception.status, 409)
            after = store.read_v1('context', project, key='task')['context']
            self.assertEqual(after['fields']['claim']['by'], 'worker-one')


if __name__ == '__main__':
    unittest.main()
