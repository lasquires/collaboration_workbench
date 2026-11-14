"""Private staging verifies complete RPCs before applying domain authority."""
import base64
import hashlib
import json
import unittest
import uuid
from tests import test_workspace_shared_http as http_fixtures

class TransferTests(unittest.TestCase):
    def setUp(self):
        fixture=http_fixtures.SharedHttpTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.http=fixture;self.store=fixture.store;self.project=fixture.project;self.actor=fixture.member_id;self.token=fixture.member_token

    def raw(self,path,body=None,token=None):return self.http.request(path,token or self.token,body)

    def payload(self):return {'project_id':self.project,'actor':{'principal_id':self.actor},'expected_revision':self.store.snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':[{'op':'create_question','id':'staged-q','text':'Was the complete staged request applied?'}]}

    def test_complete_staged_transaction_applies_once_and_staging_does_not_advance_revision(self):
        payload=self.payload();raw=json.dumps(payload,sort_keys=True,separators=(',',':')).encode();sha=hashlib.sha256(raw).hexdigest()
        manifest={'format':'workbench-chunked-json','version':1,'payload_bytes':len(raw),'payload_sha256':sha,'chunk_bytes':4*1024*1024,'chunks':[{'sha256':sha,'byte_length':len(raw)}]}
        transfer=str(uuid.uuid4());before=self.store.snapshot()['revision']
        code,result=self.raw('/v1/transfer-begin',{'project_id':self.project,'transfer_id':transfer,'target':'/v1/transactions','manifest':manifest})
        self.assertEqual(code,200,result)
        self.assertEqual(self.store.snapshot()['revision'],before)
        code,result=self.raw('/v1/transfer-finish',{'project_id':self.project,'transfer_id':transfer});self.assertNotEqual(code,200)
        self.assertEqual(self.store.read_v1('questions',self.project)['items'],[])
        code,result=self.raw('/v1/transfer-chunk',{'project_id':self.project,'transfer_id':transfer,'sha256':sha,'data':base64.b64encode(raw).decode()});self.assertEqual(code,200,result)
        self.assertEqual(self.store.snapshot()['revision'],before)
        code,result=self.raw('/v1/transfer-finish',{'project_id':self.project,'transfer_id':transfer});self.assertEqual(code,200,result)
        self.assertEqual(self.raw('/v1/transfer-finish',{'project_id':self.project,'transfer_id':transfer}),(code,result))
        self.assertEqual(len(self.store.read_v1('questions',self.project)['items']),1)

    def test_opted_in_large_response_is_scoped_and_verified(self):
        import urllib.request
        from tools import workspace_bundle_chunks as codec
        from unittest.mock import patch
        original=http_fixtures.server.workspace_read
        large={'project_id':self.project,'exact':'α'*3000000}
        def read(parsed):
            if parsed.path=='/v1/questions':return large
            return original(parsed)
        req=urllib.request.Request(self.http.base+'/v1/questions?project_id='+self.project,headers={'Authorization':'Bearer '+self.token,'X-Workspace-Chunked':'v1'})
        with patch.object(http_fixtures.server,'workspace_read',side_effect=read):
            with urllib.request.urlopen(req) as reply:envelope=json.load(reply)
        self.assertEqual(envelope.get('format'),'workbench-chunked-response')
        body={'project_id':self.project,'transfer_id':envelope['transfer_id']}
        pieces={}
        for ref in envelope['manifest']['chunks']:
            code,result=self.raw('/v1/transfer-download',{**body,'sha256':ref['sha256']})
            self.assertEqual(code,200,result);pieces[ref['sha256']]=base64.b64decode(result['data'])
        self.assertEqual(codec.unpack(envelope['manifest'],pieces.__getitem__),large)
        self.assertEqual(self.raw('/v1/transfer-download',{**body,'sha256':next(iter(pieces))},token=self.http.owner_token)[0],403)

    def test_chunk_aware_client_roundtrips_large_rpc_with_bounded_wire_requests(self):
        from tools import roadmap_cli as client
        from unittest.mock import patch
        from pathlib import Path
        token_file=Path(self.http.temp.name)/'transport.token';token_file.write_text(self.token,encoding='utf-8')
        payload=self.payload()
        payload['operations']=[{'op':'create_question','id':'q'+str(i),'text':'x'*1900} for i in range(5000)]
        before=self.store.snapshot()['revision'];seen=[];original_wire=client._request_wire
        def wire(base,path,body=None,**kwargs):
            if body is not None:self.assertLessEqual(len(json.dumps(body).encode()),8*1024*1024)
            seen.append(path)
            return original_wire(base,path,body,**kwargs)
        # Isolate transport framing from the separately tested domain transaction gate.
        with patch.object(http_fixtures.server.RoadmapStore,'transact_v1',side_effect=lambda decoded,**kwargs:{'echo':decoded}) as apply, patch.object(client,'_request_wire',side_effect=wire):
            result=client.request(self.http.base,'/v1/transactions',payload,token_file=token_file,chunked=True,timeout=30)
        self.assertEqual(result,{'echo':payload});self.assertEqual(apply.call_count,1)
        self.assertIn('/v1/transfer-begin',seen);self.assertIn('/v1/transfer-download',seen)
        self.assertEqual(self.store.snapshot()['revision'],before)

    def test_client_preserves_authentication_failure_during_response_download(self):
        from tools import roadmap_cli as client
        from tools import workspace_bundle_chunks as codec
        from unittest.mock import patch
        manifest,_=codec.pack({'value':'x'*200})
        envelope={'format':'workbench-chunked-response','version':1,'project_id':self.project,'transfer_id':str(uuid.uuid4()),'manifest':manifest}
        with patch.object(client,'_request_wire',side_effect=[envelope,client.ApiError(401,'revoked')]):
            with self.assertRaises(client.ApiError) as failure:client.request(self.http.base,'/v1/questions?project_id='+self.project,chunked=True)
        self.assertEqual(failure.exception.status,401)
        envelope['transfer_id']=[]
        with patch.object(client,'_request_wire',return_value=envelope):
            with self.assertRaises(client.ApiError):client.request(self.http.base,'/v1/questions?project_id='+self.project,chunked=True)

    def begin(self,payload=None,target='/v1/transactions'):
        from tools import workspace_bundle_chunks as codec
        payload=payload or self.payload();manifest,chunks=codec.pack(payload,chunk_bytes=128)
        transfer=str(uuid.uuid4());body={'project_id':self.project,'transfer_id':transfer}
        code,result=self.raw('/v1/transfer-begin',{**body,'target':target,'manifest':manifest})
        self.assertEqual(code,200,result)
        return body,manifest,chunks

    def upload(self,body,chunks):
        for digest,raw in chunks.items():
            code,result=self.raw('/v1/transfer-chunk',{**body,'sha256':digest,'data':base64.b64encode(raw).decode()})
            self.assertEqual(code,200,result)

    def test_tampered_chunk_rejected_then_resumed_after_new_store_instance(self):
        body,manifest,chunks=self.begin();digest=next(iter(chunks))
        code,_=self.raw('/v1/transfer-chunk',{**body,'sha256':digest,'data':base64.b64encode(b'x'*len(chunks[digest])).decode()})
        self.assertEqual(code,400)
        self.upload(body,chunks)
        # Every HTTP call already opens a fresh store/connection; cached chunks are durable.
        self.assertEqual(self.raw('/v1/transfer-finish',body)[0],200)

    def test_transfer_identity_and_principal_cannot_be_substituted(self):
        body,manifest,chunks=self.begin()
        changed=dict(manifest);changed['payload_sha256']='0'*64
        self.assertEqual(self.raw('/v1/transfer-begin',{**body,'target':'/v1/transactions','manifest':changed})[0],409)
        self.assertEqual(self.raw('/v1/transfer-finish',body,token=self.http.owner_token)[0],403)
        self.assertEqual(self.raw('/v1/transfer-finish',{**body,'project_id':self.http.other})[0],403)
        self.assertEqual(self.store.read_v1('questions',self.project)['items'],[])

    def test_authority_rechecks_actor_and_membership(self):
        payload=self.payload();payload['actor']['principal_id']='spoofed'
        body,manifest,chunks=self.begin(payload);self.upload(body,chunks)
        self.assertEqual(self.raw('/v1/transfer-finish',body)[0],403)
        with self.store._connection() as db:
            db.execute('DELETE FROM project_members WHERE project_id=? AND principal_id=?',(self.project,self.actor))
        self.assertEqual(self.raw('/v1/transfer-finish',body)[0],401)
        self.assertEqual(self.store.read_v1('questions',self.project)['items'],[])

    def test_project_binding_and_manifest_limits_reject_before_authority(self):
        payload=self.payload();payload['project_id']=self.http.other
        body,manifest,chunks=self.begin(payload);self.upload(body,chunks)
        self.assertEqual(self.raw('/v1/transfer-finish',body)[0],403)
        manifest['payload_bytes']=256*1024*1024+1
        self.assertEqual(self.raw('/v1/transfer-begin',{**body,'transfer_id':str(uuid.uuid4()),'target':'/v1/transactions','manifest':manifest})[0],400)
        self.assertEqual(self.store.read_v1('questions',self.project)['items'],[])

    def test_staged_owner_controls_cannot_bypass_original_dispatch_authorization(self):
        from unittest.mock import patch
        body,manifest,chunks=self.begin({'project_id':self.project,'review':{},'choices':{}},target='/v1/live-join')
        self.upload(body,chunks)
        with patch.object(http_fixtures.server.WorkspaceLiveService,'join') as join:
            self.assertEqual(self.raw('/v1/transfer-finish',body)[0],403)
            join.assert_not_called()

    def test_expired_staging_is_private_and_never_exports_or_advances_revision(self):
        body,_,_=self.begin();before=self.store.snapshot()['revision']
        exported=self.store.exchange_preview(self.project,portable=True)['bundle']
        self.assertFalse(any(table.startswith('transfer_') for table in exported['records']))
        with self.store._connection() as db:db.execute('UPDATE transfer_jobs SET expires_at=0')
        self.assertEqual(self.raw('/v1/transfer-finish',body)[0],404)
        self.assertEqual(self.store.snapshot()['revision'],before)

    def test_stale_revision_needs_explicit_reconciliation(self):
        body,manifest,chunks=self.begin();self.upload(body,chunks)
        other=self.payload();other['operations'][0]['id']='other-q'
        self.assertEqual(self.raw('/v1/transactions',other)[0],200)
        self.assertEqual(self.raw('/v1/transfer-finish',body)[0],409)
        self.assertEqual(len(self.store.read_v1('questions',self.project)['items']),1)

if __name__=='__main__':unittest.main()
