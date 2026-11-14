"""Shared HTTP mode closes legacy and project authorization bypasses."""
import json
import subprocess
import ssl
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import roadmap_server as server
import workspace_auth as auth
from roadmap_store import RoadmapStore


class SharedHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_path = server.STORE_PATH
        self.db = Path(self.temp.name) / 'workspace.sqlite3'
        server.STORE_PATH = self.db
        self.addCleanup(lambda: setattr(server, 'STORE_PATH', self.old_path))
        self.store = RoadmapStore(self.db)
        with self.store._connection() as db:
            projects = list(db.execute('SELECT id FROM projects ORDER BY id'))
            self.project = projects[0][0]
            local = db.execute('SELECT principal_id FROM project_members WHERE project_id=?', (self.project,)).fetchone()[0]
            self.other = '22222222-2222-4222-8222-222222222222'
            db.execute("INSERT INTO projects(id,slug,goal,created_by,created_at,updated_at) VALUES(?,?,'other',?,datetime('now'),datetime('now'))", (self.other, 'other', local))
            self.owner_token = auth.issue_owner_token(db, local)['token']
            invite = auth.issue_invite(db, self.project, local, 'member', ['contribute'])
            self.member_token = auth.redeem_invite(db, invite['secret'])['token']
            self.member_id = auth.authenticate(db, self.member_token)['principal_id']
            db.commit()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.http.shared_mode = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base = f'http://127.0.0.1:{self.http.server_port}'

    def request(self, path, token=None, body=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_live_claim_controls_cannot_proxy_owner_credentials_for_guests(self):
        for action,fields in (('claims-read',{'task_id':'TASK'}),('claims-preview',{'operation':{'op':'claim_task','task_id':'TASK','rationale':'Work'}}),('claims-submit',{'review_id':'frozen-request'})):
            body={'project_id':self.project,**fields}
            self.assertEqual(403,self.request('/v1/live-'+action,self.member_token,body)[0])
            self.assertEqual(409,self.request('/v1/live-'+action,self.owner_token,body)[0])
        body={'project_id':self.other,'task_id':'TASK'}
        self.assertEqual(403,self.request('/v1/live-claims-read',self.member_token,body)[0])

    def test_authenticated_record_mode_and_capabilities_preserve_global_contract(self):
        capabilities=self.request('/v1/projects/'+self.project+'/capabilities',self.member_token)[1]['capabilities']
        self.assertEqual(capabilities['concurrency_modes'],['global','records'])
        self.assertIn('task_structure',capabilities['record_concurrency_types'])
        initial={'project_id':self.project,'actor':{'principal_id':self.member_id},'request_id':'record-http-create','expected_revision':self.store.snapshot()['revision'],'operations':[{'op':'create','alias':name,'record':{'title':name,'status':'ready'}} for name in ('CAS-A','CAS-B')]}
        self.assertEqual(200,self.request('/v1/transactions',self.member_token,initial)[0])
        tasks=[self.store.read_v1('context',self.project,key=name)['context']['task'] for name in ('CAS-A','CAS-B')]
        payloads=[{'project_id':self.project,'actor':{'principal_id':self.member_id},'request_id':'record-http-'+str(index),'concurrency_mode':'records','expected_records':[{'type':'task','id':task['id'],'revision':task['record_revision']}],'operations':[{'op':'update','task_id':task['id'],'fields':{'summary':'Independent HTTP edit'}}]} for index,task in enumerate(tasks)]
        for payload in payloads:self.assertEqual(200,self.request('/v1/transactions',self.member_token,payload)[0])
        self.assertEqual(200,self.request('/v1/transactions',self.member_token,payloads[0])[0])
        self.assertEqual(409,self.request('/v1/transactions',self.member_token,{**payloads[0],'request_id':'record-http-stale'})[0])
        spoof={**payloads[0],'actor':{'principal_id':'another-principal'},'request_id':'record-http-spoof'}
        self.assertEqual(403,self.request('/v1/transactions',self.member_token,spoof)[0])
        self.assertEqual(403,self.request('/v1/transactions',self.member_token,{**payloads[0],'project_id':self.other})[0])
        mixed={**payloads[0],'expected_revision':self.store.snapshot()['revision'],'request_id':'record-http-mixed'}
        self.assertEqual(400,self.request('/v1/transactions',self.member_token,mixed)[0])

    def test_feedback_guard_read_is_bound_to_the_authenticated_principal(self):
        create={'project_id':self.project,'actor':{'principal_id':self.member_id},'request_id':'feedback-guard-doc','expected_revision':self.store.snapshot()['revision'],'operations':[{'op':'create_document','id':'feedback-doc','kind':'guide','title':'Guide','authority_kind':'service_body','body':'Shared guide'}]}
        self.assertEqual(200,self.request('/v1/transactions',self.member_token,create)[0])
        version=self.store.read_v1('documents',self.project)['items'][0]['current_version_id']
        vote={'project_id':self.project,'actor':{'principal_id':self.member_id},'request_id':'feedback-guard-vote','concurrency_mode':'records','expected_records':[{'type':'feedback','id':version,'revision':0}],'operations':[{'op':'set_feedback','document_version_id':version,'value':'helpful','reason':'Read this version'}]}
        self.assertEqual(200,self.request('/v1/transactions',self.member_token,vote)[0])
        path='/v1/feedback?project_id='+self.project
        own=self.request(path,self.member_token)[1]['items'][0]
        self.assertEqual(own['my_feedback_revision'],1);self.assertEqual(own['my_feedback']['value'],'helpful')
        owner=self.request(path,self.owner_token)[1]['items'][0]
        self.assertEqual(owner['my_feedback_revision'],0);self.assertIsNone(owner['my_feedback'])
        owner_id=self.store.read_v1('capabilities',self.project)['capabilities']['local_principal_id']
        self.assertEqual(403,self.request(path+'&principal_id='+owner_id,self.member_token)[0])

    def test_missing_auth_and_legacy_routes_are_blocked(self):
        self.assertEqual(401, self.request('/v1/projects')[0])
        self.assertEqual(403, self.request('/api/tasks', self.owner_token)[0])
        self.assertEqual(403, self.request('/repo/tools/roadmap_server.py', self.member_token)[0])

    def test_project_list_and_reads_are_scoped(self):
        status, result = self.request('/v1/projects', self.member_token)
        self.assertEqual(200, status)
        self.assertEqual([self.project], [item['id'] for item in result['items']])
        self.assertEqual(403, self.request('/v1/tasks?project_id=' + self.other, self.member_token)[0])
        self.assertEqual(200, self.request('/v1/tasks?project_id=' + self.project, self.member_token)[0])
        self.assertEqual(200, self.request('/v1/projects', self.owner_token)[0])
        status, capabilities = self.request(
            f'/v1/projects/{self.project}/capabilities', self.member_token)
        self.assertEqual(200, status)
        self.assertTrue(capabilities['capabilities']['authenticated'])
        self.assertEqual(self.member_id, capabilities['capabilities']['local_principal_id'])
        self.assertEqual(['contribute'], capabilities['capabilities']['granted'])

    def test_actor_spoof_and_filesystem_operations_are_denied(self):
        revision = self.store.read_v1('projects')['revision']
        base = {'project_id': self.project, 'expected_revision': revision,
                'request_id': 'shared-test-1', 'actor': {'principal_id': self.member_id},
                'operations': [{'op': 'create_task', 'record': {'title': 'test', 'status': 'ready'}}]}
        spoof = {**base, 'actor': {'principal_id': 'pretend-owner'}}
        self.assertEqual(403, self.request('/v1/transactions', self.member_token, spoof)[0])
        fs = {**base, 'operations': [{'op': 'register_root', 'path': '/tmp'}]}
        self.assertEqual(403, self.request('/v1/transactions', self.member_token, fs)[0])
        self.assertEqual(200, self.request('/v1/transactions', self.owner_token,
                         {**base, 'actor': {'principal_id': self._owner_id()}})[0])

    def test_owner_filesystem_route_and_trusted_setup_adapter(self):
        refresh = {'project_id': self.project, 'repository_id': 'missing'}
        self.assertEqual(403, self.request('/v1/repository-refresh', self.member_token, refresh)[0])
        self.assertEqual(404, self.request('/v1/repository-refresh', self.owner_token, refresh)[0])
        repo=Path(self.temp.name)/'repo';repo.mkdir()
        subprocess.run(['git','init','-q',str(repo)],check=True)
        self.assertEqual(403,self.request('/v1/repository-setup',self.member_token,{'root':str(repo),'apply':False})[0])
        status,preview=self.request('/v1/repository-setup',self.owner_token,{'root':str(repo),'apply':False})
        self.assertEqual(status,200)
        manifest=json.loads(preview['files']['.research-workspace/project.json'])
        status,applied=self.request('/v1/repository-setup',self.owner_token,{'root':str(repo),'apply':True,'manifest':manifest})
        self.assertEqual(status,200,applied)
        self.assertEqual(applied['project_id'],manifest['project_id'])
        self.assertTrue((repo/'.research-workspace/schema.json').exists())

    def test_current_membership_and_review_capability(self):
        revision = self.store.read_v1('projects')['revision']
        review = {'project_id': self.project, 'expected_revision': revision,
                  'request_id': 'review-denied', 'actor': {'principal_id': self.member_id},
                  'operations': [{'op': 'record_validation'}]}
        self.assertEqual(403, self.request('/v1/transactions', self.member_token, review)[0])
        with self.store._connection() as db:
            db.execute('DELETE FROM project_members WHERE project_id=? AND principal_id=?',
                       (self.project, self.member_id))
            db.commit()
        self.assertEqual(401, self.request('/v1/tasks?project_id=' + self.project, self.member_token)[0])

    def test_member_default_inbox_is_scoped_and_update_history_routes(self):
        status,result=self.request('/v1/inbox?project_id='+self.project,self.member_token)
        self.assertEqual(status,200)
        self.assertEqual(result['items'],[])
        self.assertEqual(self.request('/v1/inbox?project_id='+self.project+'&principal_id='+self._owner_id(),self.member_token)[0],403)
        self.assertEqual(self.request('/v1/repository-updates?project_id='+self.project+'&repository_id=missing',self.owner_token)[0],404)

    def _owner_id(self):
        with self.store._connection() as db:
            return auth.authenticate(db, self.owner_token)['principal_id']

    def test_startup_rejects_unsafe_host_and_incomplete_tls(self):
        self.assertIsNone(server.startup_security('127.0.0.1', False, None, None, self.db))
        for options in [
            ('0.0.0.0', False, None, None),
            ('127.0.0.1', True, None, None),
            ('127.0.0.1', True, 'certificate.pem', None),
            ('127.0.0.1', False, 'certificate.pem', 'key.pem'),
        ]:
            with self.subTest(options=options), self.assertRaises(server.RoadmapError):
                server.startup_security(*options, self.db)

    def test_tls_service_verifies_certificate_and_bearer(self):
        openssl=shutil.which('openssl')
        bundled=Path('C:/Program Files/Git/usr/bin/openssl.exe')
        if not openssl and bundled.is_file():openssl=str(bundled)
        if not openssl:self.skipTest('OpenSSL required only to generate ephemeral test TLS material')
        root=Path(self.temp.name);cert=root/'test-cert.pem';key=root/'test-key.pem'
        config=root/'test-tls.cnf'
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,digitalSignature,keyEncipherment\n')
        subprocess.run([openssl,'req','-x509','-newkey','rsa:2048','-nodes','-days','1','-config',str(config),'-keyout',str(key),'-out',str(cert)],check=True,capture_output=True,timeout=20)
        context=server.startup_security('127.0.0.1',True,cert,key,self.db)
        self.assertGreaterEqual(context.minimum_version,ssl.TLSVersion.TLSv1_2)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.shared_mode=True
        http.socket=context.wrap_socket(http.socket,server_side=True)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            endpoint=f'https://127.0.0.1:{http.server_port}/v1/projects'
            trusted=ssl.create_default_context(cafile=str(cert))
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(endpoint,context=trusted,timeout=3)
            self.assertEqual(missing.exception.code,401)
            request=urllib.request.Request(endpoint,headers={'Authorization':'Bearer '+self.owner_token})
            with urllib.request.urlopen(request,context=trusted,timeout=3) as response:
                self.assertTrue(json.load(response)['items'])
            with self.assertRaises(urllib.error.URLError):urllib.request.urlopen(request,timeout=3)
        finally:
            http.shutdown();http.server_close();thread.join()

    def test_scoped_portable_export_is_versioned_and_excludes_credentials(self):
        status,result=self.request('/v1/exchange-export?project_id='+self.project+'&portable=true',self.member_token)
        self.assertEqual(status,200,result)
        self.assertEqual(result['bundle']['format'],'workbench-exchange')
        self.assertEqual(result['bundle']['version'],2)
        self.assertNotIn(self.member_token,json.dumps(result))
        status,legacy=self.request('/v1/exchange-export?project_id='+self.project,self.member_token)
        self.assertEqual(status,200)
        self.assertEqual(legacy['bundle']['format'],'ssg2-exchange')
        self.assertEqual(self.request('/v1/exchange-export?project_id='+self.project+'&portable=1',self.member_token)[0],400)

    def test_live_connection_controls_are_owner_scoped_and_status_is_project_scoped(self):
        payload={'project_id':self.project,'enabled':False}
        self.assertEqual(self.request('/v1/live-configure',self.member_token,payload)[0],403)
        status,result=self.request('/v1/live-configure',self.owner_token,payload)
        self.assertEqual(status,200,result);self.assertFalse(result['enabled'])
        status,result=self.request('/v1/live-status?project_id='+self.project,self.member_token)
        self.assertEqual(status,200,result);self.assertEqual(result['claim_scope'],'local-only')
        self.assertEqual(self.request('/v1/live-status?project_id='+self.other,self.member_token)[0],403)
        self.assertEqual(self.request('/v1/live-join',self.owner_token,{'project_id':self.project,'review':{},'choices':{}})[0],400)

    def test_public_host_is_explicit_and_validated(self):
        hosts=server.accepted_service_hosts('0.0.0.0',8771,'workbench.example:443')
        self.assertIn('workbench.example:443',hosts)
        self.assertNotIn('attacker.example',hosts)
        for invalid in ('https://workbench.example','workbench.example/path','workbench.example:0','workbench.example:99999','workbench.example\nInjected:yes'):
            with self.subTest(invalid=invalid),self.assertRaises(server.RoadmapError):
                server.accepted_service_hosts('0.0.0.0',8771,invalid)

    def test_startup_requires_current_owner_credential(self):
        with self.store._connection() as db:
            db.execute('UPDATE auth_tokens SET revoked_at=datetime(\'now\') WHERE is_owner=1')
            db.commit()
        with self.assertRaises(server.RoadmapError) as caught:
            server.startup_security('127.0.0.1', True, 'certificate.pem', 'key.pem', self.db)
        self.assertEqual(503, caught.exception.status)


if __name__ == '__main__':
    unittest.main()
