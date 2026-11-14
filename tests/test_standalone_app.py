"""Standalone startup and repository onboarding use isolated app state."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import socket
import time
import urllib.request
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
import workspace
import roadmap_server
from roadmap_store import RoadmapStore
from http.server import ThreadingHTTPServer

class StandaloneTests(unittest.TestCase):
    def test_initial_home_is_generic_outside_project_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)/'app-data'
            path=workspace.initialize_home(home)
            store=RoadmapStore(path)
            self.assertEqual(store.read_v1('projects')['items'][0]['slug'],'workspace')
            before=store.snapshot()
            self.assertEqual(workspace.initialize_home(home),path)
            self.assertEqual(store.snapshot(),before)

    def test_setup_two_repositories_manifest_identity_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)/'state';path=workspace.initialize_home(home)
            repos=[Path(folder)/name for name in ('first','second')]
            for repo in repos:
                repo.mkdir();subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
            with patch.object(roadmap_server,'STORE_PATH',path):
                server=ThreadingHTTPServer(('127.0.0.1',0),roadmap_server.Handler)
                worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                try:
                    url=f'http://127.0.0.1:{server.server_port}'
                    first=workspace.setup_project(repos[0],url,apply=True)
                    registered=RoadmapStore(path).snapshot()
                    again=workspace.setup_project(repos[0],url,apply=True)
                    self.assertEqual(RoadmapStore(path).snapshot(),registered,'repeated setup is a state no-op')
                    second=workspace.setup_project(repos[1],url,apply=True)
                    self.assertEqual(first['project_id'],again['project_id'])
                    self.assertNotEqual(first['project_id'],second['project_id'])
                    manifest=(repos[0]/'.research-workspace/project.json').read_text()
                    self.assertNotIn(str(repos[0]),manifest)
                    self.assertTrue((repos[0]/'.research-workspace/AGENTS.md').is_file())
                    contract=json.loads((repos[0]/'.research-workspace/schema.json').read_text())
                    self.assertEqual(contract['contract_version'],1)
                    self.assertIn('tasks',contract['schema']['portable']['schema']['tables'])
                    self.assertEqual(contract['schema']['portable']['schema']['format'],'workbench-exchange')
                    report=workspace.diagnose(repos[0],url)
                    self.assertTrue(report['connected'])
                    self.assertEqual(report['project_id'],first['project_id'])
                    self.assertEqual(len(RoadmapStore(path).read_v1('projects')['items']),3)
                finally:
                    server.shutdown();server.server_close();worker.join()

    def test_two_installations_keep_distinct_local_actors_for_same_project(self):
        import uuid
        with tempfile.TemporaryDirectory() as folder:
            stores=[RoadmapStore(Path(folder)/name,bootstrap_slug='workspace') for name in ('a.sqlite3','b.sqlite3')]
            project=str(uuid.uuid4());actors=[]
            for number,store in enumerate(stores):
                admin=store.read_v1('projects')['items'][0]['id'];caps=store.read_v1('capabilities',admin)
                creator=caps['capabilities']['local_principal_id']
                store.transact_v1({'project_id':admin,'actor':{'principal_id':creator},'request_id':'create',
                    'expected_revision':caps['revision'],'operations':[{'op':'create_project','id':project,'slug':'shared'}]})
                actors.append(store.read_v1('capabilities',project)['capabilities']['local_principal_id'])
                with store._connection() as db:
                    self.assertIsNotNone(db.execute('SELECT 1 FROM project_members WHERE project_id=? AND principal_id=?',(project,creator)).fetchone())
            self.assertNotEqual(*actors)
            with stores[1]._connection() as db:
                db.execute("INSERT INTO principals(id,label,identity_kind) VALUES(?,'local','trusted_local_label')",(actors[0],))
                db.execute('INSERT INTO project_members VALUES(?,?,?)',(project,actors[0],'["contribute"]'))
                db.commit()
            self.assertEqual(stores[1].read_v1('capabilities',project)['capabilities']['local_principal_id'],actors[1])

    def test_owner_bootstrap_is_private_idempotent_and_requires_stopped_service(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)/'state'
            result=workspace.bootstrap_host(home)
            self.assertNotIn('token',result)
            self.assertNotIn('secret',json.dumps(result))
            secret=(home/'owner.token').read_text().strip()
            from tools import workspace_auth
            with RoadmapStore(home/'workspace.sqlite3')._connection() as db:
                self.assertTrue(workspace_auth.authenticate(db,secret)['is_owner'])
                self.assertEqual(db.execute('SELECT count(*) FROM auth_tokens').fetchone()[0],1)
            with self.assertRaises(FileExistsError):workspace.bootstrap_host(home)
            (home/'owner.token').unlink()
            with roadmap_server.service_lock(home/'workspace.sqlite3'):
                with self.assertRaisesRegex(Exception,'holds the database lock'):workspace.bootstrap_host(home)
            self.assertFalse((home/'owner.token').exists())

    def test_member_credentials_are_project_scoped_and_revocable_offline(self):
        from tools import workspace_auth
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)/'state';workspace.bootstrap_host(home)
            store=RoadmapStore(home/'workspace.sqlite3');project=store.read_v1('projects')['items'][0]['id']
            result=workspace.provision_member(home,project,'Collaborator',['contribute'])
            self.assertNotIn('token',result)
            token=Path(result['credential_file']).read_text().strip()
            with store._connection() as db:
                access=workspace_auth.authenticate(db,token)
                self.assertFalse(access['is_owner']);self.assertEqual(access['project_id'],project)
                self.assertEqual(access['capabilities'],['contribute'])
                self.assertNotIn(token,json.dumps([dict(r) for r in db.execute('SELECT * FROM auth_tokens')]))
            workspace.revoke_credential(home,result['credential_id'])
            with store._connection() as db:
                with self.assertRaises(Exception):workspace_auth.authenticate(db,token)

    def test_launcher_serves_generic_ui_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            process=subprocess.Popen([sys.executable,'-B',str(ROOT/'workspace.py'),'serve',
                '--home',str(Path(folder)/'state'),'--port',str(port)],cwd=folder,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                body=None
                for _ in range(80):
                    if process.poll() is not None:
                        out,err=process.communicate();self.fail(err.decode() or out.decode())
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=1) as response:
                            body=response.read().decode();break
                    except OSError:time.sleep(.05)
                self.assertIsNotNone(body,'standalone launcher becomes ready')
                self.assertIn('<title>Research Workspace</title>',body)
                self.assertNotIn('SSG2 restoration',body)
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/assets/workspace-repositories.js') as response:
                    self.assertEqual(response.headers.get_content_type(),'text/javascript')
                    self.assertIn('WorkspaceRepositories',response.read().decode())
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/repo/docs/project-agent-setup.md') as response:
                    self.assertIn('Preview and apply project setup',response.read().decode())
            finally:
                process.terminate();process.communicate(timeout=10)

    def test_v4_upgrade_is_backed_up_and_preserves_project_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path=workspace.initialize_home(Path(folder)/'state');store=RoadmapStore(path)
            project=store.read_v1('projects')['items'][0]['id']
            with store._connection() as db:
                db.execute('DROP TABLE repository_connections')
                db.execute('UPDATE store_meta SET schema_version=4')
            with roadmap_server.service_lock(path):
                checkpoint=roadmap_server.checkpoint_before_upgrade(path)
                self.assertTrue(Path(checkpoint).is_file())
                upgraded=RoadmapStore(path)
            self.assertEqual(upgraded.read_v1('projects')['items'][0]['id'],project)
            self.assertEqual(upgraded.read_v1('repositories',project)['items'],[])
            import sqlite3
            with contextlib.closing(sqlite3.connect(Path(checkpoint).as_uri()+'?mode=ro',uri=True)) as backup:
                self.assertEqual(backup.execute('SELECT schema_version FROM store_meta').fetchone()[0],4)
                self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_http_setup_pins_preview_identity_and_rejects_cross_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=workspace.initialize_home(root/'state');repo=root/'repo';repo.mkdir()
            subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
            with patch.object(roadmap_server,'STORE_PATH',path):
                server=ThreadingHTTPServer(('127.0.0.1',0),roadmap_server.Handler)
                worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                try:
                    url=f'http://127.0.0.1:{server.server_port}'
                    def post(body,origin=None):
                        headers={'Content-Type':'application/json'}
                        if origin:headers['Origin']=origin
                        request=urllib.request.Request(url+'/v1/repository-setup',data=json.dumps(body).encode(),headers=headers)
                        with urllib.request.urlopen(request) as response:return json.load(response)
                    before=RoadmapStore(path).snapshot()
                    preview=post({'root':str(repo),'apply':False})
                    manifest=json.loads(preview['files']['.research-workspace/project.json'])
                    self.assertEqual(RoadmapStore(path).snapshot(),before)
                    self.assertFalse((repo/'.research-workspace').exists())
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        post({'root':str(repo),'apply':True,'manifest':manifest},'https://unrelated.example')
                    self.assertEqual(error.exception.code,403)
                    applied=post({'root':str(repo),'apply':True,'manifest':manifest},url)
                    self.assertEqual(applied['project_id'],manifest['project_id'])
                    self.assertEqual(workspace.load_manifest(repo),manifest)
                    registered=RoadmapStore(path).snapshot()
                    post({'root':str(repo),'apply':True,'manifest':manifest},url)
                    self.assertEqual(RoadmapStore(path).snapshot(),registered)
                finally:server.shutdown();server.server_close();worker.join()

    def test_setup_preview_never_writes_project_or_service(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Path(folder);subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
            preview=workspace.setup_project(repo,'http://127.0.0.1:1',apply=False)
            self.assertFalse((repo/'.research-workspace').exists())
            self.assertIn('project.json',str(preview['files']))
            self.assertIn('.research-workspace/schema.json',preview['files'])
            manifest=json.loads(preview['files']['.research-workspace/project.json'])
            self.assertEqual(manifest['synchronization']['data_ref'],'refs/heads/workbench-data')
            self.assertIsNone(manifest['synchronization']['repository'])

    def test_setup_preview_binds_only_project_remote_and_rejects_stale_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=Path(folder);subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
            subprocess.run(['git','-C',str(repo),'remote','add','origin','https://alice:secret@example.com/project.git'],check=True)
            preview=workspace.setup_project(repo,'http://127.0.0.1:1')
            manifest=json.loads(preview['files']['.research-workspace/project.json'])
            self.assertEqual(manifest['synchronization']['repository'],'https://example.com/project.git')
            self.assertNotIn('secret',json.dumps(preview))
            subprocess.run(['git','-C',str(repo),'remote','set-url','origin','https://example.com/other.git'],check=True)
            with self.assertRaisesRegex(workspace.ApiError,'remote changed'):
                workspace.setup_project(repo,'http://127.0.0.1:1',apply=True,proposed_manifest=manifest)
