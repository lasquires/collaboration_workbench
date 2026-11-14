import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from tools.roadmap_store import RoadmapStore,StoreError
from tools.workspace_live_service import WorkspaceLiveService

class FakeClient:
    states={}
    calls=[]
    online=True
    def __init__(self,store,project,actor,url,token_file,directory):
        self.key=str(directory);self.url=url
        self.data=self.states.setdefault(self.key,{'state':'not-joined','pending':False,'phase':None,'base_id':None,'claim_scope':'local-only','error':None})
    def status(self):return dict(self.data)
    def preview_join(self):return {'bundle':{'id':'reviewed'},'local_revision':'local:1','preview':{'changes':[]}}
    def join(self,review,choices):self.data.update(state='current',base_id='joined');return self.status()
    def sync_once(self):
        self.calls.append(self.url);self.data.update(state='current' if self.online else 'offline',pending=not self.online)
        return self.status()

class LiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=self.root/'project';self.repo.mkdir()
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        self.store=RoadmapStore(self.root/'private'/'state.sqlite3',bootstrap_slug='workspace')
        self.project=self.store.read_v1('projects')['items'][0]['id'];caps=self.store.read_v1('capabilities',self.project)
        self.store.transact_v1({'project_id':self.project,'actor':{'principal_id':caps['capabilities']['local_principal_id']},'request_id':'repository','expected_revision':caps['revision'],'operations':[{'op':'register_repository','root':str(self.repo)}]})
        directory=self.repo/'.research-workspace';directory.mkdir()
        self.manifest=directory/'project.json';self.manifest.write_text(json.dumps({'protocol':'research-workspace/v1','project_id':self.project,'name':'Project'}),encoding='utf-8')
        self.token=self.root/'received.token';self.token.write_text('a'*64,encoding='utf-8')
        FakeClient.states={};FakeClient.calls=[];FakeClient.online=True
        self.service=WorkspaceLiveService(self.store,client_factory=FakeClient)

    def configure(self):return self.service.configure(self.project,enabled=True,url='https://host.example',token_file=str(self.token))

    def test_explicit_configuration_and_secret_paths_stay_private(self):
        self.assertEqual(self.service.read(self.project)['state'],'off')
        self.service.tick(now=100)
        self.assertEqual(FakeClient.calls,[])
        self.configure();status=self.service.read(self.project)
        self.assertEqual(status['state'],'join-required')
        self.assertNotIn(str(self.token),json.dumps(status));self.assertNotIn('a'*64,json.dumps(status))
        with self.assertRaises(StoreError):self.service.configure(self.project,enabled=True,url='http://remote.example',token_file=str(self.token))
        secret=self.repo/'bad.token';secret.write_text('a'*64)
        with self.assertRaises(StoreError):self.service.configure(self.project,enabled=True,url='https://host.example',token_file=str(secret))

    def test_availability_controls_fallback_and_pending_survives_disable(self):
        self.configure();review=self.service.preview_join(self.project);self.service.join(self.project,review,{})
        self.assertEqual(self.service.tick(now=100),{self.project})
        FakeClient.online=False
        self.assertEqual(self.service.tick(now=103),set())
        self.assertTrue(self.service.read(self.project)['pending'])
        self.service.configure(self.project,enabled=False)
        self.assertTrue(self.service.read(self.project)['pending'])
        self.assertEqual(self.service.tick(now=120),set())
        with self.assertRaises(StoreError):self.service.configure(self.project,enabled=True,url='https://other.example',token_file=str(self.token))
        self.configure();FakeClient.online=True
        restarted=WorkspaceLiveService(self.store,client_factory=FakeClient)
        self.assertEqual(restarted.tick(now=130),{self.project})
        self.assertEqual(restarted.read(self.project)['claim_scope'],'local-only')

    def test_changed_project_manifest_pauses_connection(self):
        self.configure();review=self.service.preview_join(self.project);self.service.join(self.project,review,{})
        self.manifest.write_text(json.dumps({'protocol':'research-workspace/v1','project_id':'22222222-2222-4222-8222-222222222222','name':'Changed'}))
        self.assertEqual(self.service.tick(now=100),set())
        self.assertEqual(self.service.read(self.project)['state'],'failed')
        self.assertEqual(FakeClient.calls,[])
