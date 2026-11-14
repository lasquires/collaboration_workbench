"""Large documents retain exact durable request bodies without giant state files."""
import json
import tempfile
import unittest
from pathlib import Path

class PrivateCacheTests(unittest.TestCase):
    def test_large_document_is_deduplicated_and_verified_across_restart(self):
        from tools.workspace_json_cache import PrivateJsonCache
        with tempfile.TemporaryDirectory() as directory:
            cache=PrivateJsonCache(directory,'binding',threshold=128)
            bundle={'format':'workbench-exchange','version':2,'project_id':'p','id':'b','records':{'observations':['exact Unicode α'*50]}}
            state={'base':bundle,'outbox':{'remote_request':{'request_id':'frozen','operations':[{'bundle':bundle}]}}}
            frozen=cache.freeze(state)
            self.assertLess(len(json.dumps(frozen)),len(json.dumps(state)))
            self.assertEqual(len(list(Path(directory).glob('*.json'))),1)
            self.assertEqual(PrivateJsonCache(directory,'binding',threshold=128).thaw(frozen),state)
            blob=next(Path(directory).glob('*.json'));blob.write_text('{}',encoding='utf-8')
            with self.assertRaises(ValueError):cache.thaw(frozen)

    def test_cleanup_retains_only_published_references_and_authored_shapes_are_opaque(self):
        from tools.workspace_json_cache import PrivateJsonCache
        with tempfile.TemporaryDirectory() as directory:
            cache=PrivateJsonCache(directory,'binding',threshold=32)
            old=cache.freeze({'base':{'format':'workbench-exchange','payload':'old'*100}})
            authored={'base':{'format':'workbench-exchange','payload':'new'*100,'nested':[3,{'sha256':'../outside','byte_length':2}]}}
            current=cache.freeze(authored)
            self.assertEqual(len(list(Path(directory).glob('*.json'))),2)
            cache.prune(current)
            self.assertEqual(cache.thaw(current),authored)
            with self.assertRaises((ValueError,OSError)):cache.thaw(old)

    def test_private_references_cannot_cross_bindings_or_escape_directory(self):
        from tools.workspace_json_cache import PrivateJsonCache
        with tempfile.TemporaryDirectory() as directory:
            cache=PrivateJsonCache(directory,'one',threshold=32)
            frozen=cache.freeze({'base':{'format':'workbench-exchange','payload':'x'*100}})
            with self.assertRaises(ValueError):PrivateJsonCache(directory,'two',threshold=32).thaw(frozen)
            frozen['tree'][1]['base'][1]['sha256']='../outside'
            with self.assertRaises(ValueError):cache.thaw(frozen)

class SourceRoundtripTests(unittest.TestCase):
    def test_default_20000_committed_tokens_cross_real_git_and_authenticated_live_http(self):
        self._roundtrip(20000)

    @unittest.skipUnless(__import__('os').environ.get('WORKBENCH_SCALE_GATE')=='1','Explicit extended 50k observation/10k task scale gate')
    def test_declared_50000_observations_and_10000_tasks(self):
        self._roundtrip(50000,10000)

    def _roundtrip(self,token_count,task_count=0):
        import subprocess
        import threading
        from http.server import ThreadingHTTPServer
        from tests import test_workspace_live_client as live_fixtures
        from tests import test_workspace_shared_http as shared
        from tools.workspace_live_client import WorkspaceLiveClient
        from tools.workspace_git_transport import GitWorkspaceTransport
        fixture=live_fixtures.LiveClientTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        old=shared.server.STORE_PATH;shared.server.STORE_PATH=fixture.host.path
        self.addCleanup(lambda:setattr(shared.server,'STORE_PATH',old))
        http=ThreadingHTTPServer(('127.0.0.1',0),shared.server.Handler);http.shared_mode=True
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        clients=[]
        for index in (1,2):
            credential=fixture.root/('real-'+str(index)+'.token');credential.write_text(fixture.tokens[index-1],encoding='utf-8')
            client=WorkspaceLiveClient(fixture.stores[index],fixture.project,fixture.actors[index],f'http://127.0.0.1:{http.server_port}',credential,fixture.root/('real-client-'+str(index)))
            review=client.preview_join()
            choices={json.dumps([c['table'],c['key'],field],separators=(',',':'),ensure_ascii=False):'remote' for c in review['preview']['changes'] for field,a in c['fields'].items() if a['conflict']}
            client.join(review,choices);clients.append(client)
        repo=fixture.root/'committed-source';repo.mkdir()
        def git(*args):return subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],check=True,capture_output=True)
        git('init','-q');(repo/'tokens.txt').write_text(' '.join('token'+str(i) for i in range(token_count)),encoding='utf-8')
        git('add','tokens.txt');git('commit','-qm','Synthetic scale source')
        source=fixture.stores[1]
        source.transact_v1({'project_id':fixture.project,'actor':{'principal_id':fixture.actors[1]},'expected_revision':source.snapshot()['revision'],'request_id':'source-repo','operations':[{'op':'register_repository','root':str(repo)}]})
        connection=source.read_v1('repositories',fixture.project)['items'][0]['id']
        if token_count>20000:
            from tools import workspace_git_capture as capture
            from unittest.mock import patch
            original=capture.capture_commit
            # The extended fixture declares a larger capture budget; production defaults stay 20k.
            with patch.object(capture,'capture_commit',side_effect=lambda root,head:original(root,head,max_tokens=token_count)):
                source.refresh_repository(fixture.project,connection)
        else:source.refresh_repository(fixture.project,connection)
        for start in range(0,task_count,500):
            source.transact_v1({'project_id':fixture.project,'actor':{'principal_id':fixture.actors[1]},'expected_revision':source.snapshot()['revision'],'request_id':'scale-tasks-'+str(start),'operations':[{'op':'create','alias':'scale-'+str(i),'record':{'title':'Synthetic task '+str(i),'status':'ready'}} for i in range(start,min(task_count,start+500))]})
        if task_count:
            source.transact_v1({'project_id':fixture.project,'actor':{'principal_id':fixture.actors[1]},'expected_revision':source.snapshot()['revision'],'request_id':'scale-journal','operations':[{'op':'journal','task_id':'scale-0','kind':'observation','text':'Synthetic scale witness: full journal text and exact provenance must survive.','evidence':['docs/synthetic-evidence.md']}]})
        bundle=source.exchange_preview(fixture.project,portable=True)['bundle']
        self.assertEqual(len(bundle['records']['observations']),token_count)
        self.assertGreater(len(json.dumps(bundle).encode()),32*1024*1024)
        remote=fixture.root/'data.git';subprocess.run(['git','init','--bare','-q',str(remote)],check=True,capture_output=True)
        transport=GitWorkspaceTransport(fixture.root/'git-sender',str(remote))
        first=transport.sync(bundle,lambda base,local,remote:local)
        received=GitWorkspaceTransport(fixture.root/'git-receiver',str(remote)).fetch()
        self.assertEqual(received.payload,bundle);self.assertFalse(transport.sync(bundle,lambda base,local,remote:local).pushed)
        self.assertTrue(first.pushed)
        for client in clients:
            result=client.sync_once();state=client._load();self.assertEqual(result['state'],'current',{'status':result,'last_http_status':state.get('last_http_status'),'phase':state['outbox']['phase'] if state['outbox'] else None})
        for store in (fixture.host,fixture.stores[2]):
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM observations WHERE project_id=?',(fixture.project,)).fetchone()[0],token_count)
                self.assertEqual(db.execute('SELECT count(*) FROM tasks WHERE project_id=?',(fixture.project,)).fetchone()[0],task_count)
                self.assertEqual(db.execute('SELECT count(*) FROM git_source_manifests WHERE project_id=?',(fixture.project,)).fetchone()[0],1)
                for table in ('observations','occurrences','task_journal_entries'):
                    expected={row['id']:row for row in bundle['records'][table]}
                    actual=0
                    for row in db.execute('SELECT * FROM '+table+' WHERE project_id=?',(fixture.project,)):
                        self.assertEqual(dict(row),expected[row['id']]);actual+=1
                    self.assertEqual(actual,len(expected))
                expected_tasks={row['uid']:row for row in bundle['records']['tasks']}
                for row in db.execute('SELECT uid,record_json,original_json FROM tasks WHERE project_id=?',(fixture.project,)):
                    for field in ('record_json','original_json'):self.assertEqual(row[field],expected_tasks[row['uid']][field])
                for event in bundle['history']:
                    if event['kind']=='import_bundle':continue
                    retained=db.execute('SELECT body_json FROM portable_event_history WHERE project_id=? AND id=?',(fixture.project,event['id'])).fetchone()
                    self.assertIsNotNone(retained);self.assertEqual(json.loads(retained[0]),event)

        self.assertTrue(any(path.stat().st_size>32*1024*1024 for path in (clients[0].directory/'documents').glob('*.json')))
        restarted=WorkspaceLiveClient(source,fixture.project,fixture.actors[1],clients[0].url,clients[0].token_file,clients[0].directory)
        self.assertEqual(restarted.status()['state'],'current')

if __name__=='__main__':unittest.main()
