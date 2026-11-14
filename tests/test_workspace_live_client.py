import json
import tempfile
import unittest
import uuid
import sys
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from tools.roadmap_store import RoadmapStore,StoreError
from tools.roadmap_cli import ApiError
from tools import workspace_auth
from tools.workspace_live_client import WorkspaceLiveClient,LiveConflict

class LiveClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=str(uuid.uuid4())
        self.stores=[RoadmapStore(self.root/str(i)/'state.sqlite3',bootstrap_slug='workspace') for i in range(3)]
        self.actors=[]
        for store in self.stores:
            admin=store.read_v1('projects')['items'][0]['id'];caps=store.read_v1('capabilities',admin)
            store.transact_v1({'project_id':admin,'actor':{'principal_id':caps['capabilities']['local_principal_id']},'expected_revision':caps['revision'],'request_id':'project','operations':[{'op':'create_project','id':self.project,'slug':'shared'}]})
            self.actors.append(store.read_v1('capabilities',self.project)['capabilities']['local_principal_id'])
        self.host=self.stores[0];self.online=True;self.requests=[];self.lose_reply=False
        self.clients=[];self.tokens=[]
        for i in (1,2):
            with self.host._connection() as db:
                invitation=workspace_auth.issue_invite(db,self.project,self.actors[0],'Collaborator '+str(i),['contribute'])
                token=workspace_auth.redeem_invite(db,invitation['secret'])['token']
                principal=workspace_auth.authenticate(db,token)['principal_id']
                self.tokens.append(token)
            def requester(url,path,payload=None,*,token_file=None,credential=token,actor=principal):
                if not self.online:raise ApiError(0,'offline')
                if path.endswith('/capabilities'):
                    result=self.host.read_v1('capabilities',self.project);result['capabilities']['local_principal_id']=actor;return result
                params=parse_qs(urlsplit(path).query)
                if path.startswith('/v1/exchange-export'):
                    return self.host.exchange_preview(self.project,ancestor_id=params.get('ancestor_id',[None])[0],portable=True)
                if path=='/v1/exchange-preview':return self.host.exchange_preview(self.project,bundle=payload['bundle'])
                if path=='/v1/transactions':
                    self.requests.append(json.loads(json.dumps(payload)))
                    try:result=self.host.transact_v1(payload,bearer_token=credential)
                    except StoreError as error:raise ApiError(error.status,str(error)) from None
                    if self.lose_reply and payload['operations'][0]['op']=='import_bundle':
                        self.lose_reply=False;raise ApiError(0,'reply lost after commit')
                    return result
                raise AssertionError(path)
            client=WorkspaceLiveClient(self.stores[i],self.project,self.actors[i],'https://host.example',self.root/'private.token',self.root/('client-'+str(i)),requester=requester)
            preview=client.preview_join()
            choices={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False):'remote' for change in preview['preview']['changes'] for field,alternative in change['fields'].items() if alternative['conflict']}
            client.join(preview,choices);self.clients.append(client)

    def question(self,index,identity,text):
        store=self.stores[index];caps=store.read_v1('capabilities',self.project)
        store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[index]},'request_id':'q-'+identity,'expected_revision':caps['revision'],'operations':[{'op':'create_question','id':identity,'text':text}]})

    def test_offline_queue_survives_restart_and_merges_two_clients(self):
        self.online=False;self.question(1,'left','Left offline question');self.question(2,'right','Right offline question')
        for client in self.clients:
            result=client.sync_once();self.assertEqual(result['state'],'offline');self.assertTrue(result['pending'])
        first=self.clients[0];first=WorkspaceLiveClient(first.store,self.project,self.actors[1],first.url,first.token_file,first.directory,requester=first.requester)
        self.online=True
        first.sync_once();self.clients[1].sync_once();first.sync_once()
        self.assertEqual({r['id'] for r in self.host.read_v1('questions',self.project)['items']},{'left','right'})
        self.assertEqual({r['id'] for r in first.store.read_v1('questions',self.project)['items']},{'left','right'})

    def test_lost_commit_reply_reuses_exact_request_and_does_not_duplicate_import(self):
        self.question(1,'uncertain','Was this committed?');self.lose_reply=True
        self.assertEqual(self.clients[0].sync_once()['state'],'offline')
        saved=[r for r in self.requests if r['operations'][0]['op']=='import_bundle'][-1]
        self.clients[0].sync_once()
        repeated=[r for r in self.requests if r['operations'][0]['op']=='import_bundle'][-1]
        self.assertEqual(saved,repeated)
        self.assertEqual(len(self.host.read_v1('questions',self.project)['items']),1)

    def test_credentials_never_enter_private_outbox_payload_and_idle_is_noop(self):
        self.clients[0].sync_once();count=len(self.requests);self.clients[0].sync_once()
        self.assertEqual(len(self.requests),count)
        self.online=False;self.question(1,'queued','Queued safely');self.clients[0].sync_once()
        state=json.loads((self.clients[0].directory/'live-state.json').read_text())
        self.assertNotIn('Authorization',json.dumps(state));self.assertNotIn('private.token',json.dumps(state))

    def test_same_field_host_conflict_requires_fresh_explicit_choices(self):
        self.question(1,'shared-question','Original');self.clients[0].sync_once();self.clients[1].sync_once()
        for index,text in ((1,'Left change'),(2,'Right change')):
            store=self.stores[index];row=store.read_v1('questions',self.project)['items'][0]
            store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[index]},'request_id':'edit-'+str(index),'expected_revision':store.snapshot()['revision'],'operations':[{'op':'revise_question','question_id':row['id'],'expected_record_revision':row['record_revision'],'text':text,'rationale':'Concurrent offline edit'}]})
        self.clients[0].sync_once()
        with self.assertRaises(LiveConflict):self.clients[1].sync_once()
        review=self.clients[1].review_host()
        choices={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False):'local' for change in review['preview']['changes'] for field,alternative in change['fields'].items() if alternative['conflict']}
        self.question(0,'host-intervening','New host progress')
        with self.assertRaises(LiveConflict):self.clients[1].resolve_host(review,choices)
        fresh=self.clients[1].review_host()
        choices={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False):'local' for change in fresh['preview']['changes'] for field,alternative in change['fields'].items() if alternative['conflict']}
        self.clients[1].resolve_host(fresh,choices)
        for store in (self.host,self.stores[2]):
            question=next(row for row in store.read_v1('questions',self.project)['items'] if row['id']=='shared-question')
            self.assertEqual(question['text'],'Left change')
        self.assertEqual(self.clients[1].status()['claim_scope'],'local-only')

    def test_real_http_host_stop_restart_reconciles_durable_client(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server as server
        def start(port):
            http=ThreadingHTTPServer(('127.0.0.1',port),server.Handler);http.shared_mode=True
            worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
            return http,worker
        token_file=self.root/'http-private.token';token_file.write_text(self.tokens[0],encoding='utf-8')
        with patch.object(server,'STORE_PATH',self.host.path):
            http,worker=start(0);port=http.server_port
            try:
                client=WorkspaceLiveClient(self.stores[1],self.project,self.actors[1],f'http://127.0.0.1:{port}',token_file,self.root/'real-http-client')
                preview=client.preview_join()
                choices={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False):'remote' for change in preview['preview']['changes'] for field,alternative in change['fields'].items() if alternative['conflict']}
                client.join(preview,choices)
                http.shutdown();http.server_close();worker.join()
                self.question(1,'http-offline','Created while the host was stopped')
                self.assertEqual(client.sync_once()['state'],'offline')
                queued=client.status()['operation_id'];self.assertIsNotNone(queued)
                http,worker=start(port)
                restored=WorkspaceLiveClient(self.stores[1],self.project,self.actors[1],client.url,token_file,client.directory)
                result=restored.sync_once()
                self.assertEqual(result['state'],'current',result)
                self.assertEqual(self.host.read_v1('questions',self.project)['items'][0]['id'],'http-offline')
            finally:
                http.shutdown();http.server_close();worker.join()

    def test_concurrent_host_progress_reconciles_snapshot_registration_retry(self):
        client=self.clients[0];self.question(1,'client-queued','Client progress')
        original=client.requester;interleaved=False
        def race(url,path,payload=None,**kwargs):
            nonlocal interleaved
            if path=='/v1/transactions' and payload['operations'][0]['op']=='register_exchange_snapshot' and not interleaved:
                interleaved=True;self.question(0,'host-race','Concurrent host progress')
            return original(url,path,payload,**kwargs)
        client.requester=race
        self.assertTrue(client.sync_once()['pending'])
        self.assertEqual(client.sync_once()['state'],'current')
        self.assertEqual({r['id'] for r in self.host.read_v1('questions',self.project)['items']},{'client-queued','host-race'})
        attempts=[r for r in self.requests if r['operations'][0]['op']=='register_exchange_snapshot']
        self.assertEqual(len(attempts),2)
        self.assertNotEqual(attempts[0]['request_id'],attempts[1]['request_id'])
        self.assertEqual(attempts[0]['operations'],attempts[1]['operations'])

    def test_new_edit_during_host_post_stays_pending_and_is_sent_next(self):
        client=self.clients[0];self.question(1,'during-post','Original')
        original=client.requester;injected=False
        def edit(text,request_id):
            store=self.stores[1];row=store.read_v1('questions',self.project)['items'][0]
            store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[1]},'request_id':request_id,'expected_revision':store.snapshot()['revision'],'operations':[{'op':'revise_question','question_id':row['id'],'expected_record_revision':row['record_revision'],'text':text,'rationale':'Edit while a request is in flight'}]})
        def racing(url,path,payload=None,**kwargs):
            nonlocal injected
            result=original(url,path,payload,**kwargs)
            if path=='/v1/transactions' and payload['operations'][0]['op']=='import_bundle' and not injected:
                injected=True;edit('Newer local edit','edit-during-post')
            return result
        client.requester=racing
        result=client.sync_once()
        self.assertEqual(result['state'],'pending');self.assertTrue(result['pending'])
        self.assertEqual(self.stores[1].read_v1('questions',self.project)['items'][0]['text'],'Newer local edit')
        client.sync_once()
        self.assertEqual(self.host.read_v1('questions',self.project)['items'][0]['text'],'Newer local edit')

    def test_uncertain_join_cannot_be_replaced_by_another_join(self):
        client=self.clients[0];client.path.unlink()
        review=client.preview_join()
        choices={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False):'remote' for change in review['preview']['changes'] for field,alternative in change['fields'].items() if alternative['conflict']}
        original=client.store.transact_v1;lost=False;packets=[]
        def lost_response(payload):
            nonlocal lost
            packets.append(json.loads(json.dumps(payload)));result=original(payload)
            if not lost:lost=True;raise StoreError(503,'Local commit reply lost')
            return result
        client.store.transact_v1=lost_response
        with self.assertRaises(StoreError):client.join(review,choices)
        with self.assertRaises(LiveConflict):client.preview_join()
        with self.assertRaises(LiveConflict):client.join(review,choices)
        self.assertEqual(client.sync_once()['state'],'current')
        self.assertEqual(packets[0],packets[-1])

    def test_host_snapshot_hash_is_validated_before_local_mutation(self):
        client=self.clients[0];self.question(0,'host-only','Host change')
        before=client.store.snapshot();original=client.requester
        def corrupt(url,path,payload=None,**kwargs):
            result=original(url,path,payload,**kwargs)
            if path.startswith('/v1/exchange-export'):
                result=json.loads(json.dumps(result));result['bundle']['id']='0'*64
            return result
        client.requester=corrupt
        with self.assertRaises(LiveConflict):client.sync_once()
        self.assertEqual(client.store.snapshot(),before)
        client.requester=original;client.sync_once()
        self.assertEqual(client.store.read_v1('questions',self.project)['items'][0]['id'],'host-only')

    def test_local_edits_after_host_apply_require_guarded_local_review(self):
        client=self.clients[0];identity='local-review';self.question(1,identity,'Original');client.sync_once()
        def revise(index,text):
            store=self.stores[index];row=next(r for r in store.read_v1('questions',self.project)['items'] if r['id']==identity)
            store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[index]},'request_id':'revise-'+str(uuid.uuid4()),'expected_revision':store.snapshot()['revision'],'operations':[{'op':'revise_question','question_id':identity,'expected_record_revision':row['record_revision'],'text':text,'rationale':'Concurrent reconciliation edit'}]})
        revise(1,'Client sent');original=client.requester;injected=False
        def interleaved(url,path,payload=None,**kwargs):
            nonlocal injected
            result=original(url,path,payload,**kwargs)
            if path=='/v1/transactions' and payload['operations'][0]['op']=='import_bundle' and not injected:
                injected=True;revise(0,'Host after apply');revise(1,'Newer local')
            return result
        client.requester=interleaved
        with self.assertRaises(LiveConflict):client.sync_once()
        self.assertEqual(client.status()['phase'],'review-local')
        review=client.review_local()
        choices={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False):'local' for change in review['preview']['changes'] for field,alternative in change['fields'].items() if alternative['conflict']}
        self.question(1,'local-intervening','Keep this later progress')
        with self.assertRaises(LiveConflict):client.resolve_local(review,choices)
        fresh=client.review_local()
        choices={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False):'local' for change in fresh['preview']['changes'] for field,alternative in change['fields'].items() if alternative['conflict']}
        result=client.resolve_local(fresh,choices)
        self.assertEqual(result['state'],'pending');self.assertTrue(result['pending'])
        client.sync_once()
        question=next(r for r in self.host.read_v1('questions',self.project)['items'] if r['id']==identity)
        self.assertEqual(question['text'],'Newer local')
        with self.host._connection() as db:
            versions={row[0] for row in db.execute('SELECT text FROM question_revisions WHERE project_id=? AND question_id=?',(self.project,identity))}
        self.assertTrue({'Host after apply','Newer local'}.issubset(versions))

    def test_same_installation_cannot_be_joined_as_its_own_live_client(self):
        store=self.stores[1]
        def own_service(url,path,payload=None,**kwargs):
            return store.exchange_preview(self.project,portable=True)
        client=WorkspaceLiveClient(store,self.project,self.actors[1],'https://self.example',self.root/'private.token',self.root/'self-client',requester=own_service)
        before=store.snapshot()
        with self.assertRaises(LiveConflict):client.preview_join()
        self.assertEqual(store.snapshot(),before)

    def test_service_api_worker_uses_git_fallback_then_reconnects(self):
        import importlib.util
        import time
        import urllib.request
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server as host_module
        spec=importlib.util.spec_from_file_location('isolated_local_server',Path(host_module.__file__))
        local_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(local_module)
        local_module.STORE_PATH=self.stores[1].path
        repo=self.root/'fallback-project';repo.mkdir();remote=self.root/'fallback.git'
        for args in (['init','-q',str(repo)],['init','--bare','-q',str(remote)],['-C',str(repo),'remote','add','origin',str(remote)]):
            import subprocess
            subprocess.run(['git',*args],check=True,capture_output=True)
        store=self.stores[1]
        store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[1]},'request_id':'fallback-repo','expected_revision':store.snapshot()['revision'],'operations':[{'op':'register_repository','root':str(repo)}]})
        connection=store.read_v1('repositories',self.project)['items'][0]['id']
        manifest=repo/'.research-workspace/project.json';manifest.parent.mkdir()
        manifest.write_text(json.dumps({'protocol':'research-workspace/v1','project_id':self.project,'name':'Fallback project','synchronization':{'mode':'hybrid','remote_name':'origin','repository':None,'quiet_seconds':0,'min_interval_seconds':300}}),encoding='utf-8')
        token=self.root/'fallback-private.token';token.write_text(self.tokens[0],encoding='utf-8')
        def start(module,port,shared):
            http=ThreadingHTTPServer(('127.0.0.1',port),module.Handler);http.shared_mode=shared
            worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start();return http,worker
        def close(http,worker):http.shutdown();http.server_close();worker.join()
        with patch.object(host_module,'STORE_PATH',self.host.path):
            host,host_thread=start(host_module,0,True);host_port=host.server_port
            local,local_thread=start(local_module,0,False);endpoint=f'http://127.0.0.1:{local.server_port}'
            sync_stop=None;sync_worker=None
            def api(path,payload=None):
                data=None if payload is None else json.dumps(payload).encode('utf-8')
                request=urllib.request.Request(endpoint+path,data=data,headers={'Content-Type':'application/json'} if data else {})
                with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)
            def wait_for(predicate):
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    if predicate():return
                    time.sleep(.1)
                self.fail('Backend transport did not reach its expected state')
            try:
                api('/v1/git-sync-configure',{'project_id':self.project,'repository_id':connection,'enabled':True,'export_selected':True})
                configured=api('/v1/live-configure',{'project_id':self.project,'enabled':True,'url':f'http://127.0.0.1:{host_port}','token_file':str(token)})
                self.assertNotIn(str(token),json.dumps(configured))
                review=api('/v1/live-join-preview',{'project_id':self.project})
                choices={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False):'remote' for change in review['preview']['changes'] for field,alternative in change['fields'].items() if alternative['conflict']}
                api('/v1/live-join',{'project_id':self.project,'review':review,'choices':choices})
                sync_stop,sync_worker=local_module.start_git_sync_worker(store.path,interval=.05)
                git_status='/v1/git-sync-status?project_id='+self.project+'&repository_id='+connection
                live_status='/v1/live-status?project_id='+self.project
                wait_for(lambda:api(git_status)['state']=='live-standby')
                close(host,host_thread)
                self.question(1,'automatic-fallback','Written while the live host is stopped')
                wait_for(lambda:api(live_status)['state']=='offline')
                wait_for(lambda:api(git_status)['state']=='current')
                shared=json.loads(subprocess.check_output(['git','--git-dir',str(remote),'show','refs/heads/workbench-data:workspace.json']))
                self.assertIn('automatic-fallback',{row['id'] for row in shared['records']['questions']})
                host,host_thread=start(host_module,host_port,True)
                wait_for(lambda:api(live_status)['state']=='current')
                wait_for(lambda:api(git_status)['state']=='live-standby')
                self.assertIn('automatic-fallback',{row['id'] for row in self.host.read_v1('questions',self.project)['items']})
            finally:
                if sync_stop is not None:sync_stop.set();sync_worker.join(timeout=12)
                close(local,local_thread);close(host,host_thread)
