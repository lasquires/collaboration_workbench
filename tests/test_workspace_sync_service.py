import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from tools.roadmap_store import RoadmapStore,StoreError
from tools.workspace_sync_service import WorkspaceSyncService

class SyncServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=self.root/'project';self.repo.mkdir()
        self.remote=self.root/'remote.git'
        for args in (['init','-q',str(self.repo)],['init','--bare','-q',str(self.remote)],['-C',str(self.repo),'remote','add','origin',str(self.remote)]):
            subprocess.run(['git',*args],check=True,capture_output=True)
        self.store=RoadmapStore(self.root/'app'/'workspace.sqlite3',bootstrap_slug='workspace')
        self.project=self.store.read_v1('projects')['items'][0]['id']
        caps=self.store.read_v1('capabilities',self.project)
        self.store.transact_v1({'project_id':self.project,'actor':{'principal_id':caps['capabilities']['local_principal_id']},'expected_revision':caps['revision'],'request_id':'repo','operations':[{'op':'register_repository','root':str(self.repo)}]})
        self.repo_id=self.store.read_v1('repositories',self.project)['items'][0]['id']
        directory=self.repo/'.research-workspace';directory.mkdir()
        (directory/'project.json').write_text(json.dumps({'protocol':'research-workspace/v1','project_id':self.project,'name':'Project','synchronization':{'mode':'git-only','remote_name':'origin','repository':None}}))
        self.service=WorkspaceSyncService(self.store)

    def test_disabled_no_network_and_explicit_selection_required(self):
        self.assertEqual(self.service.read(self.project,self.repo_id)['state'],'off')
        self.service.tick(now=1000)
        self.assertEqual(subprocess.run(['git','--git-dir',str(self.remote),'show-ref'],capture_output=True).stdout,b'')
        with self.assertRaises(StoreError):self.service.configure(self.project,self.repo_id,enabled=True,export_selected=False)
        with self.assertRaises(StoreError):self.service.read('wrong-project',self.repo_id)

    def test_manual_request_survives_restart_and_pushes_only_data_ref(self):
        staged=self.repo/'source.txt';staged.write_text('staged source')
        subprocess.run(['git','-C',str(self.repo),'add','source.txt'],check=True)
        before=subprocess.run(['git','-C',str(self.repo),'diff','--cached','--binary'],capture_output=True,check=True).stdout
        self.service.configure(self.project,self.repo_id,enabled=True,export_selected=True)
        self.service.request_sync(self.project,self.repo_id)
        restarted=WorkspaceSyncService(self.store);restarted.tick(now=1000)
        status=restarted.read(self.project,self.repo_id)
        self.assertEqual(status['state'],'current',status)
        self.assertFalse(status['requested'])
        refs=subprocess.run(['git','--git-dir',str(self.remote),'show-ref'],capture_output=True,check=True).stdout.decode()
        self.assertIn('refs/heads/workbench-data',refs);self.assertNotIn('refs/heads/master',refs)
        self.assertEqual(before,subprocess.run(['git','-C',str(self.repo),'diff','--cached','--binary'],capture_output=True,check=True).stdout)
        tip=refs.split()[0];restarted.tick(now=1001)
        self.assertEqual(tip,subprocess.run(['git','--git-dir',str(self.remote),'rev-parse','refs/heads/workbench-data'],capture_output=True,check=True).stdout.decode().strip())
        self.assertNotIn(str(self.remote),json.dumps(status))

    def test_remote_repoint_stops_before_network(self):
        self.service.configure(self.project,self.repo_id,enabled=True,export_selected=True)
        subprocess.run(['git','-C',str(self.repo),'remote','set-url','origin',str(self.root/'other.git')],check=True)
        self.service.request_sync(self.project,self.repo_id);self.service.tick(now=1000)
        self.assertEqual(self.service.read(self.project,self.repo_id)['state'],'failed')
        self.assertFalse((self.root/'other.git').exists())

    def test_new_local_records_show_pending_during_debounce(self):
        self.service.configure(self.project,self.repo_id,enabled=True,export_selected=True)
        self.service.request_sync(self.project,self.repo_id);self.service.tick(now=1000)
        caps=self.store.read_v1('capabilities',self.project)
        self.store.transact_v1({'project_id':self.project,'actor':{'principal_id':caps['capabilities']['local_principal_id']},'expected_revision':caps['revision'],'request_id':'new-local-question','operations':[{'op':'create_question','id':'local-question','text':'Pending shared question'}]})
        self.service.tick(now=1001)
        self.assertEqual(self.service.read(self.project,self.repo_id)['state'],'pending')
        payload=json.loads(subprocess.check_output(['git','--git-dir',str(self.remote),'show','refs/heads/workbench-data:workspace.json']))
        self.assertEqual(payload['records']['questions'],[],'quiet period does not push each local edit')
        self.service.tick(now=1301)
        self.assertEqual(self.service.read(self.project,self.repo_id)['state'],'current')

    def test_live_host_standby_and_automatic_git_fallback(self):
        self.service.configure(self.project,self.repo_id,enabled=True,export_selected=True)
        self.service.tick(now=1000,skip_projects={self.project})
        self.assertEqual(self.service.read(self.project,self.repo_id)['state'],'live-standby')
        self.assertEqual(subprocess.run(['git','--git-dir',str(self.remote),'show-ref'],capture_output=True).stdout,b'')
        self.service.tick(now=1001,skip_projects=set())
        self.assertEqual(self.service.read(self.project,self.repo_id)['state'],'pending')
        self.service.tick(now=1032,skip_projects=set())
        self.assertEqual(self.service.read(self.project,self.repo_id)['state'],'current')
        self.assertTrue(subprocess.check_output(['git','--git-dir',str(self.remote),'rev-parse','refs/heads/workbench-data']).strip())
