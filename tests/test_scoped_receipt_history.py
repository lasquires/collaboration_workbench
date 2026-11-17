"""Scoped receipts must not serialize unrelated task/event history under a write lock."""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tools.roadmap_store import RoadmapStore


class ScopedReceiptHistoryTests(unittest.TestCase):
    def test_scoped_receipt_and_exact_retry_do_not_build_legacy_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RoadmapStore(Path(tmp) / 'fixture.sqlite3')
            project = store.read_v1('projects')['items'][0]['id']
            caps = store.read_v1('capabilities', project)
            body = {'project_id': project,
                    'actor': {'principal_id': caps['capabilities']['local_principal_id']},
                    'expected_revision': caps['revision'], 'request_id': str(uuid.uuid4()),
                    'operations': [{'op': 'create', 'alias': 'code-home',
                                    'record': {'title': 'Code home', 'status': 'ready'}}]}
            with patch.object(store, '_snapshot', side_effect=AssertionError('unrelated full history read')):
                result = store.transact_v1(body)
                retry = store.transact_v1(body)
            self.assertEqual(result, retry)
            self.assertEqual(set(result), {'revision', 'event_cursor', 'changed_records', 'request_id'})
            self.assertEqual(result['request_id'], body['request_id'])
            self.assertEqual(len(result['changed_records']), 1)
            snapshot = store.snapshot()
            self.assertEqual(result['revision'], snapshot['revision'])
            self.assertEqual(snapshot['tasks'][0]['record']['title'], 'Code home')
            self.assertTrue(snapshot['events'], 'history must still be retained')


if __name__ == '__main__':
    unittest.main()
