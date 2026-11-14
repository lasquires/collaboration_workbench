"""Repository scans publish through the owning service, never Git writers."""
import json
import subprocess
import tempfile
import unittest
import sys
import time
from pathlib import Path
from tools.roadmap_store import RoadmapStore,StoreError
from tools import workspace_git_watch

class RepositoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.git('init','-q');(self.repo/'seed.txt').write_text('original',encoding='utf-8')
        self.git('add','seed.txt');self.git('commit','-qm','initial')
        self.store=RoadmapStore(self.root/'state.sqlite3',bootstrap_slug='workspace')
        self.project=self.store.read_v1('projects')['items'][0]['id']
        caps=self.store.read_v1('capabilities',self.project)
        self.store.transact_v1({'project_id':self.project,'actor':{'principal_id':caps['capabilities']['local_principal_id']},
            'request_id':'register','expected_revision':caps['revision'],'operations':[{'op':'register_repository','root':str(self.repo)}]})
        self.connection=self.store.read_v1('repositories',self.project)['items'][0]['id']

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],check=True,capture_output=True)

    def test_refresh_publishes_history_and_noop_does_not_bump_revision(self):
        first=self.store.refresh_repository(self.project,self.connection)
        self.assertTrue(first['changed']);self.assertEqual(first['source_revision'],1)
        before=self.store.snapshot()['revision']
        self.assertFalse(self.store.refresh_repository(self.project,self.connection)['changed'])
        self.assertEqual(self.store.snapshot()['revision'],before)
        (self.repo/'seed.txt').write_text('dirty change',encoding='utf-8')
        changed=self.store.refresh_repository(self.project,self.connection)
        self.assertIn('seed.txt',changed['changes']['changed_paths'])
        row=self.store.read_v1('repositories',self.project)['items'][0]
        self.assertEqual(row['source_state'],'current');self.assertTrue(row['dirty'])
        self.assertEqual(row['sync_state'],'pending','source refresh is not shared-research synchronization')
        history=self.store.read_v1('repository-updates',self.project,repository_id=self.connection)['items']
        self.assertEqual(len(history),2)
        self.assertEqual(self.store.snapshot()['tasks'],[],'Git updates do not infer task progress')

    def test_clean_commit_and_branch_changes_are_visible_after_restart(self):
        self.store.refresh_repository(self.project,self.connection)
        (self.repo/'seed.txt').write_text('committed',encoding='utf-8');self.git('add','seed.txt');self.git('commit','-qm','update')
        result=self.store.refresh_repository(self.project,self.connection)
        self.assertTrue(result['changes']['head_changed']);self.assertIn('seed.txt',result['changes']['changed_paths'])
        self.git('checkout','-qb','other')
        self.store.refresh_repository(self.project,self.connection)
        restarted=RoadmapStore(self.store.path)
        row=restarted.read_v1('repositories',self.project)['items'][0]
        self.assertEqual(row['branch'],'other');self.assertEqual(row['source_revision'],3)

    def test_stale_scan_cannot_overwrite_a_newer_scan(self):
        def concurrent(root):
            snapshot=workspace_git_watch.snapshot_repository(root)
            self.store.refresh_repository(self.project,self.connection)
            return snapshot
        with self.assertRaises(StoreError) as error:
            self.store.refresh_repository(self.project,self.connection,snapshotter=concurrent)
        self.assertEqual(error.exception.status,409)
        self.assertEqual(len(self.store.read_v1('repository-updates',self.project,repository_id=self.connection)['items']),1)

    def test_background_worker_detects_changes_without_manual_refresh(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server
        stop,worker=roadmap_server.start_repository_worker(self.store.path,interval=.05)
        def wait_for(predicate):
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                row=self.store.read_v1('repositories',self.project)['items'][0]
                if predicate(row):return row
                time.sleep(.05)
            self.fail('Background Git source refresh did not publish its update')
        try:
            first=wait_for(lambda row:row['source_state']=='current')
            (self.repo/'seed.txt').write_text('background edit',encoding='utf-8')
            updated=wait_for(lambda row:row['source_revision']>first['source_revision'])
            self.assertIn('seed.txt',updated['latest_update']['changes']['changed_paths'])
            self.assertTrue(updated['dirty'])
        finally:stop.set();worker.join(timeout=15)
        self.assertFalse(worker.is_alive())

    def test_wrong_project_is_rejected_before_reading_git(self):
        with self.assertRaises(StoreError) as error:
            self.store.refresh_repository('unknown-project',self.connection,snapshotter=lambda root:self.fail('foreign Git read'))
        self.assertEqual(error.exception.status,404)

    def test_snapshot_history_is_immutable_and_local_roots_are_not_exported(self):
        self.store.refresh_repository(self.project,self.connection)
        with self.assertRaises(StoreError):
            with self.store._connection() as db:db.execute('UPDATE repository_snapshots SET fingerprint=?',('bad',))
        bundle=self.store.exchange_preview(self.project)['bundle']
        self.assertNotIn('repository_connections',bundle['records']);self.assertNotIn('repository_snapshots',bundle['records'])
        self.assertNotIn(str(self.repo),json.dumps(bundle,ensure_ascii=False).replace(chr(92)*2,chr(92)))
