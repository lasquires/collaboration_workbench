import json
import hashlib
import shutil
import subprocess
import tempfile
import unittest
import uuid
from tools import roadmap_exchange as exchange
from pathlib import Path

from tools.roadmap_store import RoadmapStore
from tools.workspace_sync import WorkspaceGitSync, SyncConflict, PrivacyBlocked


class WorkspaceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        first = self.root / 'first.sqlite3'
        store = RoadmapStore(first)
        self.project = store.read_v1('projects')['items'][0]['id']
        with store._connection() as db:
            self.actor = db.execute('SELECT principal_id FROM project_members WHERE project_id=?', (self.project,)).fetchone()[0]
            db.execute("INSERT INTO areas(id,project_id,title) VALUES('base',?,'Base')", (self.project,))
        second = self.root / 'second.sqlite3'
        shutil.copy2(first, second)
        self.stores = [store, RoadmapStore(second)]
        self.remote = self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', str(self.remote)], capture_output=True, check=True)
        self.syncs = [WorkspaceGitSync(s, self.project, self.actor, str(self.remote), self.root / f'cache-{i}', export_selected=True) for i, s in enumerate(self.stores)]

    def title(self, index, area='base'):
        with self.stores[index]._connection() as db:
            return db.execute('SELECT title FROM areas WHERE id=?', (area,)).fetchone()[0]

    def change_title(self, index, title):
        with self.stores[index]._connection() as db:
            db.execute('UPDATE areas SET title=? WHERE id=?', (title, 'base'))

    def test_compatible_additions_survive_two_clients(self):
        self.syncs[0].sync_now()
        self.syncs[1].sync_now()
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO areas(id,project_id,title) VALUES('a',?,'A')", (self.project,))
        with self.stores[1]._connection() as db:
            db.execute("INSERT INTO areas(id,project_id,title) VALUES('b',?,'B')", (self.project,))
        self.syncs[0].sync_now()
        self.syncs[1].sync_now()
        self.syncs[0].sync_now()
        self.assertEqual(self.title(0, 'b'), 'B')
        self.assertEqual(self.title(1, 'a'), 'A')

    def test_service_tasks_questions_and_documents_roundtrip(self):
        self.syncs[0].sync_now(); self.syncs[1].sync_now()
        operations = [
            {'op': 'create', 'alias': 'sync-task', 'record': {'title': 'Shared task', 'status': 'ready'}},
            {'op': 'create_question', 'id': 'sync-question', 'text': 'What changed?'},
            {'op': 'create_document', 'id': 'sync-doc', 'kind': 'guide', 'title': 'Shared guide',
             'authority_kind': 'service_body', 'body': 'Shared text'},
        ]
        for index, op in enumerate(operations):
            revision = self.stores[0].exchange_preview(self.project)['revision']
            self.stores[0].transact_v1({'project_id': self.project,
                'actor': {'principal_id': self.actor}, 'request_id': f'content-{index}',
                'expected_revision': revision, 'operations': [op]})
        self.syncs[0].sync_now(); self.syncs[1].sync_now()
        with self.stores[1]._connection() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM tasks WHERE id='sync-task'").fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM questions WHERE id='sync-question'").fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM documents WHERE id='sync-doc'").fetchone())
    def test_same_field_conflict_keeps_local_and_remote(self):
        self.syncs[0].sync_now(); self.syncs[1].sync_now()
        self.change_title(0, 'Left'); self.change_title(1, 'Right')
        self.syncs[0].sync_now()
        with self.assertRaises(SyncConflict) as ctx:
            self.syncs[1].sync_now()
        self.assertEqual(self.title(1), 'Right')
        self.assertEqual(self.title(0), 'Left')
        self.assertTrue(ctx.exception.alternatives)

    def test_portable_artifact_root_has_no_host_path(self):
        self.syncs[0].sync_now(); self.syncs[1].sync_now()
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO artifact_roots VALUES('root',?,'C:/private',NULL,'today')", (self.project,))
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO artifacts VALUES('artifact',?,'root','native.bin','binary',NULL,'today',1)", (self.project,))
            source_hash = hashlib.sha256(b'original').hexdigest()
            db.execute("INSERT INTO artifact_versions VALUES('version',?,'artifact',?,8,NULL,NULL,'missing',NULL,NULL,'today')", (self.project, source_hash))
        self.syncs[0].sync_now(); result = self.syncs[1].sync_now()
        remote = self.syncs[0].transport.fetch().payload
        self.assertNotIn('C:/private', json.dumps(remote))
        self.assertEqual(remote['records']['artifact_roots'][0]['path'], 'workspace-root:root')
        self.assertEqual(remote['records']['artifact_blobs'], [])
        self.assertIn(source_hash, result.missing_sources)

    def test_due_resets_quiet_period_and_sync_now_bypasses_it(self):
        sync = self.syncs[0]
        sync.note_dirty(now=1000)
        self.assertFalse(sync.due(now=1029))
        sync.note_dirty(now=1025)
        self.assertFalse(sync.due(now=1050))
        self.assertTrue(sync.due(now=1055))
        self.assertIsNotNone(sync.sync_now(now=1050))
        self.assertFalse(sync.due(now=1055))

    def test_edit_during_push_remains_pending_and_is_published_next_sync(self):
        self.syncs[0].sync_now()
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO areas(id,project_id,title) VALUES('a',?,'A')", (self.project,))
        transport = self.syncs[0].transport
        original_run = transport._run
        raced = False

        def run_with_edit(*args, **kwargs):
            nonlocal raced
            if args and args[0] == 'push' and not raced:
                raced = True
                with self.stores[0]._connection() as db:
                    db.execute("INSERT INTO areas(id,project_id,title) VALUES('late',?,'Late')", (self.project,))
            return original_run(*args, **kwargs)

        transport._run = run_with_edit
        result = self.syncs[0].sync_now()
        self.assertTrue(result.pending_local)
        self.assertEqual(self.title(0, 'late'), 'Late')
        self.syncs[0].sync_now()
        self.assertFalse(self.syncs[0].sync_now().pending_local)
    def test_preview_revision_change_does_not_apply_stale_choice(self):
        self.syncs[0].sync_now(); self.syncs[1].sync_now()
        self.change_title(0, 'Remote')
        self.syncs[0].sync_now()
        store = self.stores[1]
        original_preview = store.exchange_preview
        injected = False

        def preview_with_concurrent_edit(project_id, bundle=None, ancestor_id=None):
            nonlocal injected
            result = original_preview(project_id, bundle=bundle, ancestor_id=ancestor_id)
            if bundle is not None and not injected:
                injected = True
                self.change_title(1, 'Concurrent local')
                store.transact_v1({'project_id': self.project,
                    'actor': {'principal_id': self.actor},
                    'request_id': 'concurrent-edit-revision',
                    'expected_revision': result['revision'],
                    'operations': [{'op': 'create_question', 'id': 'concurrent-q',
                                    'text': 'Was there a concurrent edit?'}]})
            return result

        store.exchange_preview = preview_with_concurrent_edit
        with self.assertRaises(SyncConflict):
            self.syncs[1].sync_now()
        self.assertEqual(self.title(1), 'Concurrent local')
        self.assertEqual(self.title(0), 'Remote')
    def test_uncertain_apply_recovers_without_duplicate_import(self):
        store = self.stores[0]
        original = store.transact_v1
        lost = False

        def lost_reply(payload):
            nonlocal lost
            result = original(payload)
            if payload['operations'][0]['op'] == 'import_bundle' and not lost:
                lost = True
                raise RuntimeError('reply lost after commit')
            return result

        store.transact_v1 = lost_reply
        with self.assertRaisesRegex(RuntimeError, 'reply lost'):
            self.syncs[0].sync_now()
        with store._connection() as db:
            before = db.execute('SELECT count(*) FROM exchange_imports').fetchone()[0]
            revision_before = db.execute('SELECT revision FROM store_meta').fetchone()[0]
        self.assertEqual(before, 1)
        store.transact_v1 = original
        self.syncs[0].sync_now()
        with store._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM exchange_imports').fetchone()[0], before)
            self.assertEqual(db.execute('SELECT revision FROM store_meta').fetchone()[0], revision_before)
    def test_reviewed_remote_choice_requires_same_tip_and_local_revision(self):
        self.syncs[0].sync_now(); self.syncs[1].sync_now()
        self.change_title(0, 'Remote')
        self.change_title(1, 'Local')
        self.syncs[0].sync_now()
        review = self.syncs[1].preview_remote()
        self.assertEqual(review['tip'], self.syncs[0].transport.fetch().tip)
        self.change_title(0, 'New remote')
        self.syncs[0].sync_now()
        with self.assertRaises(SyncConflict):
            self.syncs[1].apply_remote({}, review['tip'], review['bundle_id'], review['local_revision'])
        self.assertEqual(self.title(1), 'Local')

    def test_bootstrap_acknowledges_only_applied_remote_tip(self):
        self.syncs[0].sync_now()
        first_tip = self.syncs[0].transport.fetch().tip
        original_save = self.syncs[1].transport._save_accepted
        advanced = False

        def advance_before_ack(snapshot):
            nonlocal advanced
            if not advanced:
                advanced = True
                with self.stores[0]._connection() as db:
                    db.execute("INSERT INTO areas(id,project_id,title) VALUES('later',?,'Later')", (self.project,))
                self.syncs[0].sync_now()
            original_save(snapshot)

        self.syncs[1].transport._save_accepted = advance_before_ack
        applied = self.syncs[1].bootstrap_remote({})
        self.assertTrue(advanced)
        self.assertEqual(applied.tip, first_tip)
        self.assertEqual(self.syncs[1].transport.accepted().tip, first_tip)
        self.assertNotEqual(self.syncs[1].transport.fetch().tip, first_tip)
        self.syncs[1].sync_now()
        self.assertEqual(self.title(1, 'later'), 'Later')
    def test_independent_installations_bootstrap_and_exchange_additions(self):
        project = str(uuid.uuid4())
        stores = [RoadmapStore(self.root / f'independent-{i}.sqlite3', bootstrap_slug='workspace') for i in range(2)]
        actors = []
        for store in stores:
            admin = store.read_v1('projects')['items'][0]['id']
            caps = store.read_v1('capabilities', admin)
            store.transact_v1({'project_id': admin,
                'actor': {'principal_id': caps['capabilities']['local_principal_id']},
                'request_id': 'create-shared', 'expected_revision': caps['revision'],
                'operations': [{'op': 'create_project', 'id': project, 'slug': 'shared'}]})
            actors.append(store.read_v1('capabilities', project)['capabilities']['local_principal_id'])
        self.assertNotEqual(*actors)
        clients = [WorkspaceGitSync(stores[i], project, actors[i], str(self.remote),
                   self.root / f'independent-cache-{i}', export_selected=True) for i in range(2)]
        clients[0].sync_now()
        remote = clients[1].transport.fetch().payload
        preview = stores[1].exchange_preview(project, bundle=remote)['preview']
        choices = {exchange.canonical([change['table'], change['key'], field]): 'remote'
                   for change in preview['changes'] for field, alt in change['fields'].items()
                   if alt['conflict']}
        clients[1].bootstrap_remote(choices)
        for i, store in enumerate(stores):
            with store._connection() as db:
                db.execute('INSERT INTO areas(id,project_id,title) VALUES(?,?,?)',
                           (f'independent-{i}', project, f'Area {i}'))
        clients[0].sync_now(); clients[1].sync_now(); clients[0].sync_now()

        with stores[0]._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM areas WHERE project_id=?', (project,)).fetchone()[0], 2)
        stable_tip=clients[0].transport.fetch().tip
        for _ in range(2):
            for client in clients:client.sync_now()
        self.assertEqual(clients[0].transport.fetch().tip,stable_tip,'converged independent installations must not publish metadata-only churn')

    def test_export_is_one_read_snapshot_during_concurrent_write(self):
        from unittest.mock import patch
        store=self.stores[0]
        with store._connection() as db:db.execute('PRAGMA journal_mode=WAL')
        original=exchange.export_bundle
        def interleaved(db,*args,**kwargs):
            db.execute('SELECT title FROM areas WHERE id=?',('base',)).fetchone()
            with store._connection() as writer:writer.execute("UPDATE areas SET title='Later' WHERE id='base'")
            return original(db,*args,**kwargs)
        with patch('tools.workspace_sync.exchange.export_bundle',side_effect=interleaved):
            exported=self.syncs[0]._export()
        self.assertEqual(exported['records']['areas'][0]['title'],'Base')
        self.assertEqual(self.title(0),'Later')

    def test_export_selection_required(self):
        sync = WorkspaceGitSync(self.stores[0], self.project, self.actor, str(self.remote), self.root / 'private-cache')
        with self.assertRaises(PrivacyBlocked):
            sync.sync_now()


if __name__ == '__main__':
    unittest.main()
