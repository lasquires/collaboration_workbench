"""A verified rollback is distinct from a failed or uncertain HTTP response."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from tools.roadmap_store import RoadmapStore,StoreError
from tests import test_workspace_shared_http as http_fixture


class TransactionOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=RoadmapStore(Path(self.temp.name)/'app.sqlite3')
        self.project=self.store.read_v1('projects')['items'][0]['id']
        self.actor=self.store.read_v1('capabilities',self.project)['capabilities']['local_principal_id']

    def payload(self,*operations):
        return {'project_id':self.project,'actor':{'principal_id':self.actor},'request_id':str(uuid.uuid4()),'expected_revision':self.store.snapshot()['revision'],'operations':list(operations)}

    def assert_marker(self,error,body):
        self.assertEqual(error.transaction_rejection,{'project_id':body['project_id'],'principal_id':body['actor']['principal_id'],'request_id':body['request_id'],'expected_revision':body['expected_revision'],'state':'not_applied'})

    def test_stale_global_request_gets_exact_verified_rollback_marker(self):
        stale=self.payload({'op':'create_question','id':'stale','text':'Retain this draft'})
        self.store.transact_v1(self.payload({'op':'create_question','id':'new-progress','text':'Keep newer progress'}))
        before=self.store.snapshot()['revision']
        with self.assertRaises(StoreError) as error:self.store.transact_v1(stale)
        self.assertEqual(error.exception.status,409);self.assert_marker(error.exception,stale)
        self.assertEqual(self.store.snapshot()['revision'],before)
        self.assertEqual([q['id'] for q in self.store.read_v1('questions',self.project)['items']],['new-progress'])

    def test_partial_domain_changes_are_rolled_back_before_marker(self):
        body=self.payload({'op':'create_finding','id':'partial','claim':'This must roll back'}, {'op':'revise_finding','finding_id':'absent','expected_record_revision':1,'fields':{'claim':'Invalid missing target'},'rationale':'Fixture'})
        before=self.store.snapshot()['revision']
        with self.assertRaises(StoreError) as error:self.store.transact_v1(body)
        self.assert_marker(error.exception,body);self.assertEqual(self.store.snapshot()['revision'],before)
        self.assertEqual(self.store.read_v1('findings',self.project)['items'],[])
        with self.store._connection() as db:self.assertIsNone(db.execute('SELECT 1 FROM transactions WHERE request_id=?',(body['request_id'],)).fetchone())

    def test_committed_request_and_changed_body_never_get_false_not_applied_proof(self):
        body=self.payload({'op':'create_question','id':'committed','text':'Already committed'})
        result=self.store.transact_v1(body);self.assertEqual(self.store.transact_v1(body),result)
        changed=json.loads(json.dumps(body));changed['operations'][0]['text']='Different body'
        with self.assertRaises(StoreError) as error:self.store.transact_v1(changed)
        self.assertFalse(hasattr(error.exception,'transaction_rejection'))
        self.assertEqual(len(self.store.read_v1('questions',self.project)['items']),1)

    def test_failure_before_cached_reply_cannot_mark_a_known_committed_request_unapplied(self):
        body=self.payload({'op':'create_question','id':'known','text':'This was applied'})
        self.store.transact_v1(body);before=self.store.snapshot()['revision']
        with patch.object(self.store,'_meta',side_effect=StoreError(409,'Synthetic metadata failure before cached response')):
            with self.assertRaises(StoreError) as error:self.store.transact_v1(body)
        self.assertFalse(hasattr(error.exception,'transaction_rejection'))
        self.assertEqual(self.store.snapshot()['revision'],before)
        self.assertEqual(len(self.store.read_v1('questions',self.project)['items']),1)

    def test_record_mode_and_unknown_failure_do_not_get_global_rejection_proof(self):
        body=self.payload({'op':'create_question','text':'Record scoped'})
        body.pop('expected_revision');body.update(concurrency_mode='records',expected_records=[{'type':'project','id':self.project,'revision':999}])
        with self.assertRaises(StoreError) as error:self.store.transact_v1(body)
        self.assertFalse(hasattr(error.exception,'transaction_rejection'))
        body=self.payload({'op':'create_question','text':'Unknown failure'})
        with patch('tools.roadmap_research.apply_operation',side_effect=RuntimeError('Unknown failure')):
            with self.assertRaises(RuntimeError) as error:self.store.transact_v1(body)
        self.assertFalse(hasattr(error.exception,'transaction_rejection'))


class TransactionOutcomeHttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture=http_fixture.SharedHttpTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)

    def body(self):
        f=self.fixture
        return {'project_id':f.project,'actor':{'principal_id':f.member_id},'request_id':str(uuid.uuid4()),'expected_revision':f.store.snapshot()['revision'],'operations':[{'op':'create_question','id':'http-outcome','text':'Keep exact identity'}]}

    def test_http_stale_rejection_proof_is_request_scoped_but_reads_have_none(self):
        f=self.fixture;body=self.body();body['expected_revision']=body['expected_revision'].split(':')[0]+':999'
        code,error=f.request('/v1/transactions',f.member_token,body);self.assertEqual(code,409,error)
        self.assertEqual(error['transaction_rejection']['request_id'],body['request_id'])
        self.assertEqual(error['transaction_rejection']['principal_id'],f.member_id)
        self.assertEqual(error['transaction_rejection']['project_id'],f.project)
        self.assertEqual(error['transaction_rejection']['state'],'not_applied')
        code,error=f.request('/v1/questions/absent/context?project_id='+f.project,f.member_token)
        self.assertEqual(code,404,error);self.assertNotIn('transaction_rejection',error)

    def test_response_failure_after_commit_has_no_rollback_proof(self):
        f=self.fixture;body=self.body();original=http_fixture.server.Handler._json
        def fail_response(handler,status,value):
            if handler.path=='/v1/transactions' and status==200:raise http_fixture.server.StoreError(413,'Synthetic response failure after commit')
            return original(handler,status,value)
        with patch.object(http_fixture.server.Handler,'_json',new=fail_response):
            code,error=f.request('/v1/transactions',f.member_token,body)
        self.assertEqual(code,413,error);self.assertNotIn('transaction_rejection',error)
        self.assertEqual(len(f.store.read_v1('questions',f.project)['items']),1)
        code,result=f.request('/v1/transactions',f.member_token,body)
        self.assertEqual(code,200,result);self.assertEqual(result['request_id'],body['request_id'])

    def test_eligible_status_codes_from_postcommit_response_failure_have_no_proof(self):
        f=self.fixture;original=http_fixture.server.Handler._json
        for status in (400,404,409,422,503):
            body=self.body();body['operations'][0]['id']='postcommit-'+str(status)
            def fail_response(handler,code,value):
                if handler.path=='/v1/transactions' and code==200:raise http_fixture.server.StoreError(status,'Synthetic postcommit response failure')
                return original(handler,code,value)
            with patch.object(http_fixture.server.Handler,'_json',new=fail_response):
                code,error=f.request('/v1/transactions',f.member_token,body)
            self.assertEqual(code,status,error);self.assertNotIn('transaction_rejection',error)
            self.assertEqual(f.request('/v1/transactions',f.member_token,body)[0],200)
        self.assertEqual(len(f.store.read_v1('questions',f.project)['items']),5)

    def test_revoked_credential_cannot_turn_a_committed_request_into_not_applied(self):
        from tools import workspace_auth
        f=self.fixture;body=self.body();self.assertEqual(f.request('/v1/transactions',f.member_token,body)[0],200)
        with f.store._connection() as db:
            token=workspace_auth.authenticate(db,f.member_token);owner=workspace_auth.authenticate(db,f.owner_token)
            workspace_auth.revoke_token(db,token['token_id'],owner['principal_id']);db.commit()
        code,error=f.request('/v1/transactions',f.member_token,body)
        self.assertEqual(code,401,error);self.assertNotIn('transaction_rejection',error)
        self.assertEqual(len(f.store.read_v1('questions',f.project)['items']),1)
