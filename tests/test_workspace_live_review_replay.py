import copy
import json
import unittest
from unittest.mock import patch
from tests import test_workspace_live_client as fixtures
from tools.workspace_live_client import WorkspaceLiveClient,LiveConflict
from tools.roadmap_cli import ApiError

class ReviewReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.LiveClientTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        original=self.fixture.clients[0]
        self.client=WorkspaceLiveClient(original.store,original.project_id,original.actor_id,original.url,original.token_file,self.fixture.root/'review-replay',requester=original.requester)
    def restart(self):
        c=self.client
        self.client=WorkspaceLiveClient(c.store,c.project_id,c.actor_id,c.url,c.token_file,c.directory,requester=c.requester)
        return self.client
    def choices(self,review,side='remote'):
        return {json.dumps([c['table'],c['key'],f],separators=(',',':'),ensure_ascii=False):side for c in review['preview']['changes'] for f,a in c['fields'].items() if a['conflict']}
    def test_completed_join_confirms_exact_review_after_restart(self):
        review=self.client.preview_join();choices=self.choices(review);self.client.join(review,choices)
        revision=self.client.store.snapshot()['revision'];count=len(self.fixture.requests)
        result=self.restart().join(review,choices)
        self.assertEqual(result['reviewed_command']['state'],'acknowledged');self.assertEqual(result['reviewed_command']['kind'],'join')
        self.assertEqual(self.client.store.snapshot()['revision'],revision);self.assertEqual(len(self.fixture.requests),count)
        with self.assertRaises(LiveConflict):self.client.join(review,{'unexpected':'remote'})
    def test_completed_replay_requires_current_host_access_and_binding(self):
        review=self.client.preview_join();choices=self.choices(review);self.client.join(review,choices)
        original=self.client.requester
        self.client.requester=lambda *a,**kw:(_ for _ in ()).throw(ApiError(403,'revoked'))
        with self.assertRaises(ApiError):self.client.join(review,choices)
        for field in ('actor','store'):
            def changed(url,path,payload=None,**kw):
                result=copy.deepcopy(original(url,path,payload,**kw))
                if path.endswith('/capabilities'):
                    if field=='actor':result['capabilities']['local_principal_id']='other-principal'
                    else:result['revision']='other-store:1'
                return result
            self.client.requester=changed
            with self.assertRaises(LiveConflict):self.client.join(review,choices)
        self.client.requester=original
    def test_join_local_commit_then_state_save_failure_reuses_transaction(self):
        review=self.client.preview_join();choices=self.choices(review);save=self.client._save;failed=False
        def crash(state):
            nonlocal failed
            if state['outbox'] is None and state.get('base') and not failed:
                failed=True;raise OSError('injected after local commit')
            return save(state)
        with patch.object(self.client,'_save',side_effect=crash):
            with self.assertRaises(OSError):self.client.join(review,choices)
        revision=self.client.store.snapshot()['revision']
        result=self.restart().join(review,choices)
        self.assertEqual(result['reviewed_command']['state'],'acknowledged');self.assertEqual(self.client.store.snapshot()['revision'],revision)

    def revise(self,index,identity,text):
        store=self.fixture.stores[index];row=next(x for x in store.read_v1('questions',self.fixture.project)['items'] if x['id']==identity)
        store.transact_v1({'project_id':self.fixture.project,'actor':{'principal_id':self.fixture.actors[index]},'expected_revision':store.snapshot()['revision'],'request_id':__import__('uuid').uuid4().hex,'operations':[{'op':'revise_question','question_id':identity,'expected_record_revision':row['record_revision'],'text':text,'rationale':'Replay fixture concurrent edit'}]})
    def host_conflict(self):
        f=self.fixture;f.question(1,'question','Original');f.clients[0].sync_once();f.clients[1].sync_once()
        self.revise(1,'question','Left change');self.revise(2,'question','Right change');f.clients[0].sync_once()
        self.client=f.clients[1]
        with self.assertRaises(LiveConflict):self.client.sync_once()
        review=self.client.review_host();return review,self.choices(review,'local')
    def test_completed_host_resolution_confirms_after_restart(self):
        review,choices=self.host_conflict();first=self.client.resolve_host(review,choices)
        self.assertEqual(first['reviewed_command']['state'],'acknowledged')
        before=self.client.store.snapshot()['revision'];host_before=self.fixture.host.snapshot()['revision'];count=len(self.fixture.requests)
        result=self.restart().resolve_host(review,choices)
        self.assertEqual(result['reviewed_command']['kind'],'host');self.assertEqual(result['reviewed_command']['state'],'acknowledged')
        self.assertEqual(self.client.store.snapshot()['revision'],before);self.assertEqual(self.fixture.host.snapshot()['revision'],host_before);self.assertEqual(len(self.fixture.requests),count)
    def test_pending_host_resolution_replays_original_remote_request(self):
        review,choices=self.host_conflict();self.fixture.lose_reply=True
        result=self.client.resolve_host(review,choices);self.assertEqual(result['state'],'offline');self.assertTrue(result['pending'])
        original=next(x for x in reversed(self.fixture.requests) if x['request_id'].startswith('live-reviewed-'))
        with self.assertRaises(LiveConflict):self.client.resolve_host(review,{k:'remote' for k in choices})
        result=self.restart().resolve_host(review,choices);self.assertEqual(result['reviewed_command']['state'],'acknowledged')
        repeated=next(x for x in reversed(self.fixture.requests) if x['request_id'].startswith('live-reviewed-'));self.assertEqual(original,repeated)
    def test_completed_local_resolution_confirms_without_pushing_later_progress(self):
        f=self.fixture;self.client=f.clients[0];f.question(1,'local-review','Original');self.client.sync_once();self.revise(1,'local-review','Sent')
        original=self.client.requester;injected=False
        def interleave(url,path,payload=None,**kw):
            nonlocal injected
            result=original(url,path,payload,**kw)
            if path=='/v1/transactions' and payload['operations'][0]['op']=='import_bundle' and not injected:
                injected=True;self.revise(0,'local-review','Host after apply');self.revise(1,'local-review','Later local')
            return result
        self.client.requester=interleave
        with self.assertRaises(LiveConflict):self.client.sync_once()
        review=self.client.review_local();choices=self.choices(review,'local');first=self.client.resolve_local(review,choices)
        self.assertEqual(first['reviewed_command']['state'],'acknowledged');self.assertTrue(first['pending'])
        count=len(f.requests);revision=self.client.store.snapshot()['revision']
        result=self.restart().resolve_local(review,choices);self.assertEqual(result['reviewed_command']['kind'],'local')
        self.assertEqual(result['reviewed_command']['state'],'acknowledged');self.assertEqual(len(f.requests),count);self.assertEqual(self.client.store.snapshot()['revision'],revision)
    def test_host_ack_survives_subsequent_local_review(self):
        review,choices=self.host_conflict();original=self.client.requester;injected=False
        def interleave(url,path,payload=None,**kw):
            nonlocal injected
            result=original(url,path,payload,**kw)
            if path=='/v1/transactions' and payload['request_id'].startswith('live-reviewed-') and not injected:
                injected=True;self.revise(0,'question','Host after reviewed apply');self.revise(2,'question','Later local edit')
            return result
        self.client.requester=interleave
        result=self.client.resolve_host(review,choices)
        self.assertEqual(result['reviewed_command']['state'],'acknowledged');self.assertEqual(result['phase'],'review-local')
        local=self.client.review_local();self.client.resolve_local(local,self.choices(local,'local'))
        result=self.restart().resolve_host(review,choices)
        self.assertEqual(result['reviewed_command']['kind'],'host');self.assertEqual(result['reviewed_command']['state'],'acknowledged')
        self.assertEqual({x['kind'] for x in self.client._load()['review_receipts']},{'join','host','local'})
    def test_malformed_success_is_uncertain_and_replays_exact_remote_request(self):
        review,choices=self.host_conflict();original=self.client.requester;broken=False
        def malformed(url,path,payload=None,**kw):
            nonlocal broken
            result=original(url,path,payload,**kw)
            if path=='/v1/transactions' and payload['request_id'].startswith('live-reviewed-') and not broken:broken=True;return {}
            return result
        self.client.requester=malformed
        result=self.client.resolve_host(review,choices);self.assertTrue(result['pending']);self.assertEqual(result['state'],'offline')
        first=next(x for x in reversed(self.fixture.requests) if x['request_id'].startswith('live-reviewed-'))
        result=self.restart().resolve_host(review,choices);self.assertEqual(result['reviewed_command']['state'],'acknowledged')
        last=next(x for x in reversed(self.fixture.requests) if x['request_id'].startswith('live-reviewed-'));self.assertEqual(first,last)
    def test_replay_rejects_corrupt_receipt_and_current_local_revocation(self):
        from tools.roadmap_store import StoreError
        review=self.client.preview_join();choices=self.choices(review);self.client.join(review,choices)
        with self.client.store._connection() as db:db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',(json.dumps([]),self.client.project_id,self.client.actor_id))
        with self.assertRaises(StoreError) as error:self.client.join(review,choices)
        self.assertEqual(error.exception.status,403)
        state=self.client._load();state['review_receipts'][0]['id']='bad';self.client._save(state)
        with self.assertRaises(ValueError):self.restart()
    def test_receipt_bound_retains_order_and_is_not_portable(self):
        review=self.client.preview_join();choices=self.choices(review);self.client.join(review,choices);state=self.client._load();receipt=state['review_receipts'][0]
        for i in range(260):self.client._remember_command(state,{'reviewed_command':{**receipt,'id':format(i,'064x')}})
        self.assertEqual(len(state['review_receipts']),256);self.assertEqual(state['review_receipts'][0]['id'],format(4,'064x'))
        self.client._save(state);self.assertEqual(self.restart()._load()['review_receipts'],state['review_receipts'])
        self.assertNotIn('review_receipts',json.dumps(self.client.store.exchange_preview(self.client.project_id,portable=True)['bundle']))

class RealHttpReviewReplayTests(unittest.TestCase):
    def test_completed_join_replay_checks_actual_authenticated_http_after_restart(self):
        import threading
        from http.server import ThreadingHTTPServer
        from tests import test_workspace_shared_http as shared
        from tools import workspace_auth as auth
        f=fixtures.LiveClientTests();f.setUp();self.addCleanup(f.doCleanups)
        old=shared.server.STORE_PATH;shared.server.STORE_PATH=f.host.path;self.addCleanup(lambda:setattr(shared.server,'STORE_PATH',old))
        http=ThreadingHTTPServer(('127.0.0.1',0),shared.server.Handler);http.shared_mode=True
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start();self.addCleanup(thread.join);self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        token=f.root/'actual-http.token';token.write_text(f.tokens[0],encoding='utf-8')
        args=(f.stores[1],f.project,f.actors[1],f'http://127.0.0.1:{http.server_port}',token,f.root/'actual-replay')
        client=WorkspaceLiveClient(*args);review=client.preview_join()
        choices={json.dumps([c['table'],c['key'],field],separators=(',',':'),ensure_ascii=False):'remote' for c in review['preview']['changes'] for field,a in c['fields'].items() if a['conflict']}
        first=client.join(review,choices);before=client.store.snapshot()['revision'];result=WorkspaceLiveClient(*args).join(review,choices)
        self.assertEqual(result['reviewed_command'],first['reviewed_command']);self.assertEqual(client.store.snapshot()['revision'],before)
        with f.host._connection() as db:
            identity=auth.authenticate(db,f.tokens[0])['token_id'];auth.revoke_token(db,identity,f.actors[0])
        with self.assertRaises(ApiError) as error:WorkspaceLiveClient(*args).join(review,choices)
        self.assertEqual(error.exception.status,401)
