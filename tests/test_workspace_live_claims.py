import json
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from tools import workspace_auth
from tools.roadmap_store import RoadmapStore,StoreError
from tools.roadmap_cli import ApiError
from tools.workspace_live_client import WorkspaceLiveClient
from tools.workspace_live_claims import WorkspaceLiveClaims


class LiveClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.host=RoadmapStore(self.root/'host.db')
        self.project=self.host.read_v1('projects')['items'][0]['id']
        caps=self.host.read_v1('capabilities',self.project);owner=caps['capabilities']['local_principal_id']
        self.host.transact_v1({'project_id':self.project,'actor':{'principal_id':owner},'request_id':'tasks','expected_revision':caps['revision'],'operations':[{'op':'create','alias':name,'record':{'title':name,'status':'ready'}} for name in ('TASK','OTHER')]})
        self.online=True;self.lose=False;self.requests=[];self.clients=[];self.tokens=[]
        for index in range(2):
            with self.host._connection() as db:
                invite=workspace_auth.issue_invite(db,self.project,owner,'Worker '+str(index),['contribute'])
                self.tokens.append(workspace_auth.redeem_invite(db,invite['secret'])['token'])
            local=RoadmapStore(self.root/str(index)/'state.db');admin=local.read_v1('projects')['items'][0]['id'];local_caps=local.read_v1('capabilities',admin)
            local.transact_v1({'project_id':admin,'actor':{'principal_id':local_caps['capabilities']['local_principal_id']},'request_id':'project','expected_revision':local_caps['revision'],'operations':[{'op':'create_project','id':self.project,'slug':'shared'}]})
            actor=local.read_v1('capabilities',self.project)['capabilities']['local_principal_id']
            def requester(url,path,payload=None,*,token_file=None,index=index):
                if not self.online:raise ApiError(0,'offline')
                with self.host._connection() as db:principal=workspace_auth.authenticate(db,self.tokens[index])
                try:
                    if path.endswith('/capabilities'):
                        result=self.host.read_v1('capabilities',self.project);result['capabilities'].update(authenticated=True,local_principal_id=principal['principal_id']);return result
                    parsed=urlsplit(path);params=parse_qs(parsed.query)
                    if parsed.path=='/v1/claims':return self.host.read_v1('claims',self.project,task_id=params['task_id'][0],**({'cursor':params['cursor'][0]} if 'cursor' in params else {}))
                    if parsed.path.startswith('/v1/tasks/') and parsed.path.endswith('/context'):
                        return self.host.read_v1('context',self.project,key=parsed.path.split('/')[3])
                    if path=='/v1/transactions':
                        self.requests.append(json.loads(json.dumps(payload)))
                        result=self.host.transact_v1(payload,bearer_token=self.tokens[index])
                        if self.lose:self.lose=False;raise ApiError(0,'lost after commit')
                        return result
                    raise AssertionError(path)
                except StoreError as error:raise ApiError(error.status,str(error)) from None
            self.clients.append(WorkspaceLiveClient(local,self.project,actor,'https://host.example',self.root/'private.token',self.root/('client-'+str(index)),requester=requester))
        self.claims=[WorkspaceLiveClaims(client) for client in self.clients]

    def operation(self,task='TASK',**extra):
        return {'op':'claim_task','task_id':task,'rationale':'Coordinate shared work',**extra}

    def test_competing_claims_and_overlapping_paths_are_rejected(self):
        review=self.claims[0].preview(self.operation(reserved_paths=['tools/source.py']))
        self.assertFalse(self.host.read_v1('claims',self.project)['items'])
        self.assertEqual(self.claims[0].submit(review['review_id'])['state'],'acknowledged')
        competing=self.claims[1].preview(self.operation())
        with self.assertRaises(ApiError):self.claims[1].submit(competing['review_id'])
        overlapping=self.claims[1].preview(self.operation('OTHER',reserved_paths=['tools/source.py']))
        with self.assertRaises(ApiError):self.claims[1].submit(overlapping['review_id'])
        self.assertEqual(len(self.host.read_v1('claims',self.project)['items']),1)
        self.assertEqual(self.claims[0].read('TASK')['scope'],'host')
        self.assertFalse(self.clients[0].store.read_v1('claims',self.project)['items'])

    def test_lost_response_restarts_and_replays_exact_command(self):
        review=self.claims[0].preview(self.operation());self.lose=True
        with self.assertRaises(ApiError):self.claims[0].submit(review['review_id'])
        original=self.requests[-1]
        restarted=WorkspaceLiveClaims(self.clients[0])
        self.assertTrue(restarted.status()['pending'])
        with self.assertRaises(StoreError):restarted.preview(self.operation('OTHER'))
        result=restarted.submit(review['review_id'])
        self.assertEqual(result['state'],'acknowledged');self.assertEqual(original,self.requests[-1])
        self.assertEqual(len(self.host.read_v1('claims',self.project)['items']),1)
        self.assertNotIn(self.tokens[0],restarted.path.read_text(encoding='utf-8'))

    def test_offline_never_reports_a_shared_lease_and_retry_binds_principal(self):
        review=self.claims[0].preview(self.operation());self.online=False
        with self.assertRaises(ApiError):self.claims[0].submit(review['review_id'])
        self.assertTrue(self.claims[0].status()['pending'])
        self.assertFalse(self.host.read_v1('claims',self.project)['items'])
        self.online=True;self.tokens[0]=self.tokens[1]
        with self.assertRaises(StoreError):self.claims[0].submit(review['review_id'])
        self.assertFalse(self.host.read_v1('claims',self.project)['items'])

    def test_stale_review_and_unknown_review_never_rebase_silently(self):
        review=self.claims[0].preview(self.operation())
        other=self.claims[1].preview(self.operation('OTHER'));self.claims[1].submit(other['review_id'])
        with self.assertRaises(ApiError):self.claims[0].submit(review['review_id'])
        self.assertEqual(self.claims[0].status()['state'],'rejected')
        fresh=self.claims[0].preview(self.operation())
        with self.assertRaises(StoreError):self.claims[0].submit(review['review_id'])
        self.assertEqual(self.claims[0].submit(fresh['review_id'])['state'],'acknowledged')

    def test_heartbeat_and_release_use_host_authority_and_capabilities(self):
        prepared=self.claims[0].preview(self.operation())
        self.claims[0].submit(prepared['review_id'])
        forbidden=self.claims[1].preview({'op':'release_claim','task_id':'TASK','rationale':'Not the claimant'})
        with self.assertRaises(ApiError):self.claims[1].submit(forbidden['review_id'])
        self.assertEqual(self.claims[1].status()['state'],'rejected')
        heartbeat=self.claims[0].preview({'op':'heartbeat_claim','task_id':'TASK','rationale':'Still working','lease_seconds':120})
        self.claims[0].submit(heartbeat['review_id'])
        release=self.claims[0].preview({'op':'release_claim','task_id':'TASK','rationale':'Finished this work'})
        self.claims[0].submit(release['review_id'])
        self.assertIsNotNone(self.claims[0].read('TASK')['items'][0]['released_at'])
        self.assertFalse(self.clients[0].store.read_v1('claims',self.project)['items'])

    def test_real_http_service_controls_retain_uncertain_claim_and_block_host_replacement(self):
        import sys
        import threading
        import subprocess
        from http.server import ThreadingHTTPServer
        from tools.roadmap_cli import request
        from tools.workspace_live_service import WorkspaceLiveService
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server as server
        previous=server.STORE_PATH;server.STORE_PATH=self.host.path
        self.addCleanup(lambda:setattr(server,'STORE_PATH',previous))
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.shared_mode=True
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        client=self.clients[0];repo=self.root/'repo';repo.mkdir()
        subprocess.run(['git','init','-q',str(repo)],check=True)
        caps=client.store.read_v1('capabilities',self.project)
        client.store.transact_v1({'project_id':self.project,'actor':{'principal_id':client.actor_id},'request_id':'repo','expected_revision':caps['revision'],'operations':[{'op':'register_repository','root':str(repo)}]})
        directory=repo/'.research-workspace';directory.mkdir()
        (directory/'project.json').write_text(json.dumps({'protocol':'research-workspace/v1','project_id':self.project,'name':'Shared'}),encoding='utf-8')
        token=self.root/'received.token';token.write_text(self.tokens[0],encoding='utf-8')
        lose=[True]
        def requester(url,path,payload=None,**kwargs):
            result=request(url,path,payload,**kwargs)
            if path=='/v1/transactions' and lose[0]:lose[0]=False;raise ApiError(0,'Lost committed response')
            return result
        def factory(*args):return WorkspaceLiveClient(*args,requester=requester)
        service=WorkspaceLiveService(client.store,client_factory=factory)
        origin='http://127.0.0.1:'+str(http.server_port)
        service.configure(self.project,enabled=True,url=origin,token_file=str(token))
        review=service.claims_preview(self.project,self.operation())
        with self.assertRaises(ApiError):service.claims_submit(self.project,review['review_id'])
        self.assertTrue(service.read(self.project)['host_claims']['pending'])
        service.configure(self.project,enabled=False)
        self.assertTrue(service.read(self.project)['host_claims']['pending'])
        with self.assertRaises(StoreError):service.configure(self.project,enabled=True,url='https://other.example',token_file=str(token))
        service.configure(self.project,enabled=True,url=origin,token_file=str(token))
        restarted=WorkspaceLiveService(client.store,client_factory=factory)
        self.assertEqual(restarted.claims_submit(self.project,review['review_id'])['state'],'acknowledged')
        claims=restarted.claims_read(self.project,'TASK')['items']
        self.assertEqual(len(claims),1);self.assertIsNone(claims[0]['released_at'])
        portable=client.store.exchange_preview(self.project,portable=True)['bundle']
        self.assertNotIn('claims',portable['records']);self.assertNotIn(self.tokens[0],json.dumps(portable))

    def test_retry_refuses_a_different_host_installation(self):
        review=self.claims[0].preview(self.operation())
        original=self.clients[0].requester
        def replaced(url,path,payload=None,**kwargs):
            result=original(url,path,payload,**kwargs)
            if path.endswith('/capabilities'):result['revision']=str(uuid.uuid4())+':1'
            return result
        self.clients[0].requester=replaced
        with self.assertRaisesRegex(StoreError,'installation changed'):
            self.claims[0].submit(review['review_id'])
        self.assertFalse(self.requests)
        self.assertTrue(self.claims[0].status()['pending'])

    def test_mismatched_acknowledgment_stays_uncertain_until_exact_replay(self):
        review=self.claims[0].preview(self.operation());original=self.clients[0].requester
        def wrong_receipt(url,path,payload=None,**kwargs):
            result=original(url,path,payload,**kwargs)
            if path=='/v1/transactions':result={**result,'request_id':'another-command'}
            return result
        self.clients[0].requester=wrong_receipt
        with self.assertRaises(ApiError):self.claims[0].submit(review['review_id'])
        self.assertTrue(self.claims[0].status()['pending'])
        self.clients[0].requester=original
        self.assertEqual(self.claims[0].submit(review['review_id'])['state'],'acknowledged')
        self.assertEqual(len(self.host.read_v1('claims',self.project)['items']),1)
