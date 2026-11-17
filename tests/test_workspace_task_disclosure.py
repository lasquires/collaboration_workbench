import copy
import json
import unittest
import uuid
from tests import test_workspace_exchange as fixtures
from tools import roadmap_exchange as exchange
from tools.roadmap_store import StoreError

class TaskDisclosureTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ExchangeTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.stores=self.fixture.stores;self.project=self.fixture.project;self.actors=self.fixture.actors
        self.write(0,{'op':'create','alias':'private-task','record':{'title':'Task','status':'ready','summary':r'Inspect C:\Users\Synthetic\source.txt'}})
        with self.stores[0]._connection() as db:self.raw=dict(db.execute('SELECT * FROM tasks').fetchone())
    def write(self,index,*ops,payload=None):
        store=self.stores[index]
        payload=payload or {'project_id':self.project,'actor':{'principal_id':self.actors[index]},'expected_revision':store.snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':list(ops)}
        return store.transact_v1(payload)
    def preview(self,index=0,**kw):return self.stores[index].read_v1('task-sharing-review',self.project,**kw)
    def approve(self,index=0,preview=None):
        page=preview or self.preview(index)
        return self.write(index,{'op':'review_task_sharing','review_digest':page['review_digest'],'limit':page['limit'],'cursor':page['cursor'],'choices':{x['key']:'redacted' for x in page['items']}})
    def portable(self,index=0,ancestor=None):
        with self.stores[index]._connection() as db:return exchange.export_bundle(db,self.project,ancestor,portable=True)
    def test_roundtrip_preserves_raw_original_and_private_consent(self):
        with self.assertRaises(StoreError):self.portable()
        page=self.preview();self.assertEqual(len(page['items']),2)
        self.approve(preview=page);bundle=self.portable()
        self.assertIn('Inspect ',json.loads(bundle['records']['tasks'][0]['record_json'])['summary'])
        self.assertNotIn('Synthetic',json.dumps(bundle));self.assertEqual(len(bundle['records']['task_disclosures']),2)
        with self.stores[0]._connection() as db:
            row=dict(db.execute('SELECT * FROM tasks').fetchone());self.assertEqual(row['record_json'],self.raw['record_json']);self.assertEqual(row['original_json'],self.raw['original_json'])
            self.assertEqual(db.execute('SELECT count(*) FROM task_private_fields').fetchone()[0],2)
        self.fixture.apply(1,bundle)
        self.assertEqual(self.portable(1)['records']['task_disclosures'],bundle['records']['task_disclosures'])
        with self.stores[1]._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM task_share_choices').fetchone()[0],0)
    def test_safe_peer_status_change_never_overwrites_private_prose(self):
        self.approve();base=self.portable()
        self.fixture.apply(0,base,register=True);self.fixture.apply(1,base);self.fixture.apply(1,base,register=True)
        self.write(1,{'op':'update','task_id':'private-task','fields':{'status':'active'}})
        self.fixture.apply(0,self.portable(1,base['id']))
        with self.stores[0]._connection() as db:
            row=db.execute('SELECT * FROM tasks').fetchone()
            self.assertEqual(json.loads(row['record_json'])['summary'],json.loads(self.raw['record_json'])['summary'])
            self.assertEqual(row['original_json'],self.raw['original_json']);self.assertEqual(json.loads(row['record_json'])['status'],'active')
        self.portable()
    def test_imported_disclosure_does_not_authorize_raw_copy(self):
        self.fixture.apply(1,self.fixture.export(0));self.approve();incoming=self.portable()
        with self.stores[1]._connection() as db:preview=exchange.preview_import(db,self.project,incoming)
        choices={exchange.canonical([c['table'],c['key'],f]):'local' for c in preview['changes'] for f,alt in c['fields'].items() if alt['conflict']}
        self.fixture.apply(1,incoming,choices)
        with self.assertRaises(StoreError):self.portable(1)
    def test_changed_field_and_unsupported_authority_fail_closed(self):
        self.approve();self.write(0,{'op':'update','task_id':'private-task','fields':{'summary':r'New C:\Users\Synthetic\new.txt'}})
        with self.assertRaises(StoreError):self.portable()
        self.approve();self.write(0,{'op':'update','task_id':'private-task','fields':{'acceptance':[r'C:\Users\Synthetic\witness']}})
        page=self.preview();self.assertTrue(any(not item['proposals'] for item in page['items']))
        with self.assertRaises(StoreError):self.approve(preview=page)
        with self.assertRaises(StoreError):self.portable()
    def test_stale_review_and_tampering_are_atomic(self):
        page=self.preview();self.write(0,{'op':'update','task_id':'private-task','fields':{'status':'active'}})
        with self.assertRaises(StoreError):self.approve(preview=page)
        self.approve();bad=copy.deepcopy(self.portable());bad['records']['task_disclosures'][0]['source_sha256']='x'
        bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
        with self.assertRaises(StoreError):self.fixture.apply(1,bad)
        with self.stores[1]._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM task_disclosures').fetchone()[0],0)

    def test_safe_remote_prose_edit_keeps_private_history(self):
        self.approve();base=self.portable();self.fixture.apply(0,base,register=True);self.fixture.apply(1,base)
        self.write(1,{'op':'update','task_id':'private-task','fields':{'summary':'Peer added a concrete follow-up.'}})
        self.fixture.apply(0,self.portable(1,base['id']))
        with self.stores[0]._connection() as db:
            self.assertEqual(json.loads(db.execute('SELECT record_json FROM tasks').fetchone()[0])['summary'],'Peer added a concrete follow-up.')
            vault=[json.loads(r[0]) for r in db.execute('SELECT raw_json FROM task_private_fields')]
            self.assertIn(json.loads(self.raw['record_json'])['summary'],vault)
        self.portable()
    def test_historical_nested_routes_preserve_meaning_and_metrics(self):
        with self.stores[0]._connection() as db:
            original={'subtasks':[{'id':'old','attempts':['GET /api/tasks and POST /api/transaction passed 4/4.'],'status':'done','measure':42}]}
            db.execute('UPDATE tasks SET original_json=?',(json.dumps(original),))
        self.approve();bundle=self.portable();public=json.loads(bundle['records']['tasks'][0]['original_json'])
        child=public['subtasks'][0];self.assertEqual(child['measure'],42);self.assertEqual(child['status'],'done')
        self.assertIn('API route: api/tasks',child['attempts'][0]);self.assertIn('passed 4/4',child['attempts'][0])
    def test_exact_replay_and_revoked_review_capability(self):
        page=self.preview();op={'op':'review_task_sharing','review_digest':page['review_digest'],'limit':page['limit'],'cursor':page['cursor'],'choices':{x['key']:'redacted' for x in page['items']}}
        payload={'project_id':self.project,'actor':{'principal_id':self.actors[0]},'expected_revision':self.stores[0].snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':[op]}
        first=self.write(0,payload=payload);self.assertEqual(first,self.write(0,payload=payload))
        with self.stores[0]._connection() as db:db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',(json.dumps(['read','contribute']),self.project,self.actors[0]))
        with self.assertRaises(StoreError) as error:self.write(0,payload=payload)
        self.assertEqual(error.exception.status,403)
    def test_preview_cross_project_cursor_and_changed_known_roots_fail_closed(self):
        page=self.preview(limit=1)
        with self.assertRaises(StoreError):self.preview(limit=2,cursor=page['next_cursor'] or 'invalid')
        self.approve()
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO artifact_roots VALUES('new-root',?,'[LOCAL_PATH_REDACTED]',NULL,'today')",(self.project,))
        with self.assertRaises(StoreError):self.portable()
    def test_authority_paths_nested_in_attempts_cannot_be_hidden(self):
        self.write(0,{'op':'create','alias':'authority-attempt','record':{'title':'Historical attempt','status':'ready','attempts':[{'evidence':r'C:\Users\Synthetic\native.bin','status':'passed'}]}})
        self.assertTrue(any(not x['proposals'] for x in self.preview()['items'] if x['field']=='attempts'))
    def test_context_marks_public_history_and_keeps_local_original_available(self):
        self.approve();context=self.stores[0].read_v1('context',self.project,key='private-task')['context']
        self.assertEqual(context['task_disclosures']['total'],2)
        self.assertTrue(all(x['original_available'] for x in context['task_disclosures']['items']))
        self.fixture.apply(1,self.portable());context=self.stores[1].read_v1('context',self.project,key='private-task')['context']
        self.assertFalse(any(x['original_available'] for x in context['task_disclosures']['items']))

    def test_schema16_upgrade_is_atomic_and_preserves_original_bytes(self):
        from unittest.mock import patch
        from tools.roadmap_store import RoadmapStore
        from tools import workspace_task_disclosure as disclosure
        store=self.stores[0]
        with store._connection() as db:
            db.execute('DROP TABLE task_share_choices');db.execute('DROP TABLE task_private_fields');db.execute('DROP TABLE task_disclosures')
            db.execute('UPDATE store_meta SET schema_version=16')
            raw=json.dumps(json.loads(self.raw['original_json']),indent=3)
            db.execute('UPDATE tasks SET original_json=?',(raw,))
        with patch.object(disclosure,'create_schema',side_effect=RuntimeError('injected upgrade fault')):
            with self.assertRaises(RuntimeError):RoadmapStore(store.path)
        with store._connection() as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],16)
            self.assertEqual(db.execute('SELECT original_json FROM tasks').fetchone()[0],raw)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='task_disclosures'").fetchone())
        upgraded=RoadmapStore(store.path)
        with upgraded._connection() as db:
            from tools.roadmap_schema import SCHEMA_VERSION
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],SCHEMA_VERSION)
            self.assertEqual(db.execute('SELECT original_json FROM tasks').fetchone()[0],raw)
        self.stores[0]=upgraded;self.approve();self.fixture.apply(1,self.portable())
    def test_original_lookup_is_digest_bound_and_vault_survives_edits(self):
        page=self.preview();item=next(x for x in page['items'] if x['slot']=='record_json')
        raw=self.stores[0].read_v1('task-sharing-original',self.project,key=item['key'],source_sha256=item['source_sha256'])
        self.assertIn('Synthetic',raw['raw_json']);self.approve(preview=page)
        disclosure=item['proposals']['redacted']['id']
        self.write(0,{'op':'update','task_id':'private-task','fields':{'summary':'Current safe value'}})
        with self.assertRaises(StoreError):self.stores[0].read_v1('task-sharing-original',self.project,key=item['key'],source_sha256=item['source_sha256'])
        vault=self.stores[0].read_v1('task-sharing-original',self.project,disclosure_id=disclosure)
        self.assertEqual(vault['raw_json'],raw['raw_json']);self.assertEqual(vault['source_sha256'],raw['source_sha256'])

    def test_foreign_and_malformed_original_rejected(self):
        self.approve();good=self.portable()
        for variant in ('foreign','original'):
            bad=copy.deepcopy(good)
            if variant=='foreign':
                row=bad['records']['task_disclosures'][0];row['project_id']=str(uuid.uuid4())
                content={k:v for k,v in row.items() if k!='id'}
                row['id']=str(uuid.uuid5(uuid.NAMESPACE_URL,'workbench-task-disclosure:'+exchange.research.digest(content)))
            else:bad['records']['tasks'][0]['original_json']='[]'
            bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
            with self.assertRaises(StoreError):self.fixture.apply(1,bad)
    def test_matching_public_merge_preserves_formatted_original_bytes(self):
        with self.stores[0]._connection() as db:
            original=json.dumps(json.loads(self.raw['original_json']),indent=3)
            db.execute('UPDATE tasks SET original_json=?',(original,))
        self.approve();base=self.portable();self.fixture.apply(0,base,register=True);self.fixture.apply(1,base)
        self.write(1,{'op':'update','task_id':'private-task','fields':{'status':'active'}})
        self.fixture.apply(0,self.portable(1,base['id']))
        with self.stores[0]._connection() as db:self.assertEqual(db.execute('SELECT original_json FROM tasks').fetchone()[0],original)


class TaskDisclosureTransportTests(unittest.TestCase):
    def test_real_git_and_two_authenticated_http_clients_keep_private_originals(self):
        import subprocess
        import threading
        from http.server import ThreadingHTTPServer
        from tests import test_workspace_live_client as live, test_workspace_shared_http as shared
        from tools.workspace_live_client import WorkspaceLiveClient,LiveConflict
        from tools.workspace_sync import WorkspaceGitSync
        from tools.workspace_git_transport import GitWorkspaceTransport
        fixture=live.LiveClientTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        old=shared.server.STORE_PATH;shared.server.STORE_PATH=fixture.host.path
        self.addCleanup(lambda:setattr(shared.server,'STORE_PATH',old))
        http=ThreadingHTTPServer(('127.0.0.1',0),shared.server.Handler);http.shared_mode=True
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        clients=[]
        for index in (1,2):
            token=fixture.root/('task-disclosure-'+str(index)+'.token');token.write_text(fixture.tokens[index-1],encoding='utf-8')
            client=WorkspaceLiveClient(fixture.stores[index],fixture.project,fixture.actors[index],f'http://127.0.0.1:{http.server_port}',token,fixture.root/('task-live-'+str(index)))
            review=client.preview_join()
            choices={exchange.canonical([c['table'],c['key'],f]):'remote' for c in review['preview']['changes'] for f,a in c['fields'].items() if a['conflict']}
            client.join(review,choices);clients.append(client)
        source=fixture.stores[1];project=fixture.project
        def write(index,*ops):
            return fixture.stores[index].transact_v1({'project_id':project,'actor':{'principal_id':fixture.actors[index]},'request_id':str(uuid.uuid4()),'expected_revision':fixture.stores[index].snapshot()['revision'],'operations':list(ops)})
        write(1,{'op':'create','alias':'shared-task','record':{'title':'Task sharing','status':'ready','summary':r'Inspect C:\Users\Synthetic\network.txt and record measurements.'}})
        with self.assertRaises(LiveConflict):clients[0].sync_once()
        self.assertEqual(clients[0].status()['state'],'sharing-review-required')
        page=source.read_v1('task-sharing-review',project)
        write(1,{'op':'review_task_sharing','review_digest':page['review_digest'],'limit':page['limit'],'cursor':page['cursor'],'choices':{x['key']:'redacted' for x in page['items']}})
        remote=fixture.root/'task-disclosure.git';subprocess.run(['git','init','--bare','-q',str(remote)],check=True,capture_output=True)
        git=WorkspaceGitSync(source,project,fixture.actors[1],str(remote),fixture.root/'task-sender',export_selected=True)
        self.assertTrue(git.sync_now().pushed);self.assertFalse(git.sync_now().pushed)
        published=GitWorkspaceTransport(fixture.root/'task-receiver',str(remote)).fetch().payload
        self.assertNotIn('Synthetic',json.dumps(published));self.assertEqual(len(published['records']['task_disclosures']),2)
        self.assertEqual(clients[0].sync_once()['state'],'current');self.assertEqual(clients[1].sync_once()['state'],'current')
        write(2,{'op':'update','task_id':'shared-task','fields':{'status':'active'}})
        self.assertEqual(clients[1].sync_once()['state'],'current');self.assertEqual(clients[0].sync_once()['state'],'current')
        context=source.read_v1('context',project,key='shared-task')['context']
        self.assertEqual(context['fields']['status'],'active');self.assertIn('Synthetic',context['fields']['summary'])
        for store in (fixture.host,fixture.stores[2]):
            context=store.read_v1('context',project,key='shared-task')['context']
            self.assertNotIn('Synthetic',json.dumps(context));self.assertEqual(context['task_disclosures']['total'],2)
            with store._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM task_share_choices').fetchone()[0],0)
        self.assertEqual(clients[0].sync_once()['state'],'current')
