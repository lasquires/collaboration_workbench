"""Explicit reviewed projections never mutate raw journal evidence or grant authority."""
import copy
import json
import unittest
import uuid
from tests import test_workspace_exchange as exchange_fixtures
from tools.roadmap_store import StoreError

class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.fixture=exchange_fixtures.ExchangeTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.stores=self.fixture.stores;self.project=self.fixture.project;self.actors=self.fixture.actors
        self.write(0,{'op':'create','alias':'note-task','record':{'title':'Shared note','status':'ready'}},{'op':'journal','task_id':'note-task','kind':'observation','text':r'Measured C:\Users\Synthetic\private.txt; provisional.','evidence':['docs/witness.md']})
        with self.stores[0]._connection() as db:self.original=dict(db.execute('SELECT * FROM task_journal_entries').fetchone())

    def write(self,index,*ops,payload=None):
        store=self.stores[index]
        payload=payload or {'project_id':self.project,'actor':{'principal_id':self.actors[index]},'expected_revision':store.snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':list(ops)}
        return store.transact_v1(payload)

    def preview(self,index=0,**options):return self.stores[index].read_v1('journal-sharing-review',self.project,**options)

    def approve(self,mode='redacted',preview=None,index=0):
        preview=preview or self.preview(index)
        operation={'op':'review_journal_sharing','review_digest':preview['review_digest'],'limit':preview['limit'],'cursor':preview['cursor'],'choices':{item['source_event_id']:mode for item in preview['items']}}
        payload={'project_id':self.project,'actor':{'principal_id':self.actors[index]},'expected_revision':self.stores[index].snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':[operation]}
        return self.write(index,payload=payload),payload

    def portable(self,index=0):return self.stores[index].exchange_preview(self.project,portable=True)['bundle']

    def test_redacted_projection_roundtrips_without_rewriting_original(self):
        with self.assertRaises(StoreError):self.portable()
        preview=self.preview();self.assertEqual(len(preview['items']),1)
        self.assertNotIn('Synthetic',json.dumps(preview));self.assertIn('redacted',preview['items'][0]['proposals'])
        result,payload=self.approve(preview=preview)
        self.assertEqual(self.write(0,payload=payload),result)
        with self.stores[0]._connection() as db:self.assertEqual(dict(db.execute('SELECT * FROM task_journal_entries').fetchone()),self.original)
        bundle=self.portable();self.assertEqual(bundle['records']['task_journal_entries'],[])
        disclosure=bundle['records']['journal_disclosures'][0]
        self.assertEqual(disclosure['source_sha256'],self.original['source_sha256'])
        self.assertNotIn('Synthetic',json.dumps(bundle));self.assertNotEqual(disclosure['public_sha256'],disclosure['source_sha256'])
        self.fixture.apply(1,bundle)
        history=self.stores[1].read_v1('history',self.project,key='note-task')['items']
        note=next(item for item in history if item.get('disclosure'))
        self.assertEqual(note['details']['text'],'[LOCAL_PATH_REDACTED]')
        self.assertEqual(note['details']['evidence'],['docs/witness.md'])
        self.assertEqual(note['disclosure']['mode'],'redacted');self.assertFalse(note['disclosure']['original_available'])
        self.assertEqual(self.portable(1)['records']['journal_disclosures'],[disclosure])
        with self.stores[1]._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM journal_share_choices').fetchone()[0],0)

    def test_omission_is_explicit_and_local_raw_history_is_retained(self):
        self.approve('omitted');bundle=self.portable()
        self.assertEqual(bundle['records']['journal_disclosures'][0]['mode'],'omitted')
        self.assertIn('withheld',bundle['records']['journal_disclosures'][0]['details_json'])
        history=self.stores[0].read_v1('history',self.project,key='note-task')['items']
        self.assertTrue(any('Synthetic' in item['details'].get('text','') for item in history))
        self.assertFalse(any(item.get('disclosure') for item in history))

    def test_missing_extra_and_stale_review_choices_fail_closed(self):
        preview=self.preview();base={'op':'review_journal_sharing','review_digest':preview['review_digest'],'limit':preview['limit'],'cursor':preview['cursor']}
        for choices in ({},{self.original['id']:'redacted','extra':'omitted'}):
            with self.assertRaises(StoreError):self.write(0,{**base,'choices':choices})
        self.write(0,{'op':'create_question','id':'changed','text':'Changed since review?'})
        with self.assertRaises(StoreError) as error:self.approve(preview=preview)
        self.assertEqual(error.exception.status,409)
        with self.assertRaises(StoreError):self.portable()

    def test_imported_disclosure_does_not_authorize_an_unsafe_original(self):
        self.fixture.apply(1,self.fixture.export(0))  # Explicit local full exchange, not portable network data.
        self.approve();self.fixture.apply(1,self.portable())
        with self.assertRaises(StoreError):self.portable(1)
        with self.stores[1]._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM journal_share_choices').fetchone()[0],0)

    def test_unsupported_record_paths_still_block_after_journal_review(self):
        self.approve();self.write(0,{'op':'update','task_id':'note-task','fields':{'summary':r'Private C:\Users\Synthetic\other.txt'}})
        with self.assertRaises(StoreError):self.portable()

    def test_tampered_disclosure_and_stale_cursor_reject_without_partial_writes(self):
        self.write(0,{'op':'journal','task_id':'note-task','kind':'attempt','text':r'Other C:\Users\Synthetic\second.txt'})
        page=self.preview(limit=1);self.assertIsNotNone(page['next_cursor'])
        self.approve(preview=page)
        with self.assertRaises(StoreError) as failure:self.preview(limit=1,cursor=page['next_cursor'])
        self.assertEqual(failure.exception.status,409)
        self.approve();bundle=self.portable();bad=copy.deepcopy(bundle)
        bad['records']['journal_disclosures'][0]['source_sha256']='0'*64
        from tools import roadmap_exchange as exchange
        bad['id']=exchange.research.digest({key:value for key,value in bad.items() if key!='id'})
        with self.assertRaises(StoreError):self.fixture.apply(1,bad)
        with self.stores[1]._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM journal_disclosures').fetchone()[0],0)

    def test_correction_can_reference_disclosed_original_and_context_labels_projection(self):
        self.approve();self.fixture.apply(1,self.portable())
        context=self.stores[1].read_v1('context',self.project,key='note-task')
        self.assertTrue(any(item.get('disclosure') for item in context['context']['history']))
        self.write(1,{'op':'journal','task_id':'note-task','kind':'review','text':'A safe append-only correction.','supersedes':self.original['id']})
        exported=self.portable(1)
        self.assertEqual(len(exported['records']['journal_disclosures']),1)
        self.assertTrue(any(self.original['id']==json.loads(row['details_json']).get('supersedes') for row in exported['records']['task_journal_entries']))

    def test_foreign_raw_event_cannot_shadow_correction_of_this_project_disclosure(self):
        self.approve();self.fixture.apply(1,self.portable())
        store=self.stores[1];other=str(uuid.uuid4())
        self.write(1,{'op':'create_project','id':other,'slug':'other-history'})
        actor=store.read_v1('capabilities',other)['capabilities']['local_principal_id']
        store.transact_v1({'project_id':other,'actor':{'principal_id':actor},'expected_revision':store.snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':[{'op':'create','alias':'foreign-note','record':{'title':'Foreign task','status':'ready'}}]})
        with store._connection() as db:
            foreign=db.execute('SELECT uid FROM tasks WHERE project_id=?',(other,)).fetchone()[0]
            raw=dict(self.original);raw.update(project_id=other,task_id=foreign)
            db.execute('INSERT INTO task_journal_entries('+','.join(raw)+') VALUES('+','.join('?' for _ in raw)+')',tuple(raw.values()))
        self.write(1,{'op':'journal','task_id':'note-task','kind':'review','text':'Correction still resolves within this project.','supersedes':self.original['id']})

    def test_schema15_upgrade_and_injected_failure_preserve_raw_journal(self):
        from unittest.mock import patch
        from tools import workspace_journal_disclosure as module
        from tools.roadmap_store import RoadmapStore
        store=self.stores[0]
        with store._connection() as db:
            db.execute('DROP TABLE journal_share_choices');db.execute('DROP TABLE journal_disclosures')
            db.execute('UPDATE store_meta SET schema_version=15')
        original=module.create_schema
        def fail(db):original(db);raise RuntimeError('injected disclosure migration failure')
        with patch.object(module,'create_schema',side_effect=fail):
            with self.assertRaises(RuntimeError):RoadmapStore(store.path)
        with store._connection() as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],15)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='journal_disclosures'").fetchone())
            self.assertEqual(dict(db.execute('SELECT * FROM task_journal_entries').fetchone()),self.original)
        upgraded=RoadmapStore(store.path)
        with upgraded._connection() as db:
            from tools.roadmap_schema import SCHEMA_VERSION
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],SCHEMA_VERSION)
            self.assertEqual(dict(db.execute('SELECT * FROM task_journal_entries').fetchone()),self.original)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_review_preview_does_not_label_a_mismatched_receipt_as_current(self):
        self.approve('redacted');self.approve('omitted')
        with self.stores[0]._connection() as db:db.execute("UPDATE journal_share_choices SET mode='redacted'")
        self.assertIsNone(self.preview()['items'][0]['current_choice'])
        with self.assertRaises(StoreError) as failure:self.portable()
        self.assertTrue(failure.exception.sharing_review_required)

    def test_disclosure_requires_its_task_in_the_incoming_snapshot_even_if_local_task_exists(self):
        self.approve('redacted');self.fixture.apply(1,self.portable())
        self.approve('omitted');bundle=self.portable()
        bundle['records']['tasks']=[];bundle['records']['containment']=[];bundle['records']['entity_aliases']=[]
        from tools import roadmap_exchange as exchange
        bundle['id']=exchange.research.digest({key:value for key,value in bundle.items() if key!='id'})
        with self.assertRaises(StoreError):self.fixture.apply(1,bundle)

    def test_rehashed_disclosure_cannot_reassign_an_existing_original_identity(self):
        self.approve('redacted');self.fixture.apply(1,self.portable())
        self.approve('omitted');bundle=self.portable()
        from tools import roadmap_exchange as exchange
        row=next(row for row in bundle['records']['journal_disclosures'] if row['mode']=='omitted')
        row['source_sha256']='0'*64
        content={key:value for key,value in row.items() if key not in ('id','public_sha256')}
        row['public_sha256']=exchange.research.digest(content)
        row['id']=str(uuid.uuid5(uuid.NAMESPACE_URL,'workbench-journal-disclosure:'+row['public_sha256']))
        bundle['id']=exchange.research.digest({key:value for key,value in bundle.items() if key!='id'})
        with self.assertRaises(StoreError):self.fixture.apply(1,bundle)

    def test_safe_legacy_journal_json_formatting_does_not_require_disclosure(self):
        self.approve()
        from tools import roadmap_exchange as exchange
        raw=dict(self.original);raw['id']=str(uuid.uuid4())
        source=json.loads(raw['source_event_json']);source['event_id']=raw['id']
        source['actor_json']=json.dumps(json.loads(source['actor_json']),indent=2)
        raw['details_json']=json.dumps({'text':'Safe older note with original formatting.'},indent=2)
        source['details_json']=raw['details_json']
        raw['source_event_json']=json.dumps(source,indent=2);raw['source_sha256']=exchange.research.digest(source)
        with self.stores[0]._connection() as db:db.execute('INSERT INTO task_journal_entries('+','.join(raw)+') VALUES('+','.join('?' for _ in raw)+')',tuple(raw.values()))
        bundle=self.portable()
        self.assertIn(raw,bundle['records']['task_journal_entries'])
        self.fixture.apply(1,bundle)
        with self.stores[1]._connection() as db:self.assertEqual(dict(db.execute('SELECT * FROM task_journal_entries WHERE id=?',(raw['id'],)).fetchone()),raw)

    def test_revoked_review_capability_blocks_exact_replay(self):
        _,payload=self.approve()
        with self.stores[0]._connection() as db:db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',(json.dumps(['contribute']),self.project,self.actors[0]))
        with self.assertRaises(StoreError) as error:self.write(0,payload=payload)
        self.assertEqual(error.exception.status,403)

class NetworkDisclosureTests(unittest.TestCase):
    def test_reviewed_disclosure_crosses_git_and_live_without_reexporting_raw_note(self):
        import subprocess
        from tests import test_workspace_live_client as live_fixtures
        from tools.workspace_live_client import LiveConflict
        from tools.workspace_sync import WorkspaceGitSync
        from tools.workspace_git_transport import GitWorkspaceTransport
        fixture=live_fixtures.LiveClientTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        import threading
        from http.server import ThreadingHTTPServer
        from tests import test_workspace_shared_http as shared
        from tools.workspace_live_client import WorkspaceLiveClient
        old=shared.server.STORE_PATH;shared.server.STORE_PATH=fixture.host.path
        self.addCleanup(lambda:setattr(shared.server,'STORE_PATH',old))
        http=ThreadingHTTPServer(('127.0.0.1',0),shared.server.Handler);http.shared_mode=True
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        actual=[]
        for index in (1,2):
            token=fixture.root/('disclosure-'+str(index)+'.token');token.write_text(fixture.tokens[index-1],encoding='utf-8')
            client=WorkspaceLiveClient(fixture.stores[index],fixture.project,fixture.actors[index],f'http://127.0.0.1:{http.server_port}',token,fixture.root/('disclosure-live-'+str(index)))
            review=client.preview_join()
            choices={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False):'remote' for change in review['preview']['changes'] for field,a in change['fields'].items() if a['conflict']}
            client.join(review,choices);actual.append(client)
        fixture.clients=actual
        source=fixture.stores[1];project=fixture.project;actor=fixture.actors[1]
        def write(*ops):return source.transact_v1({'project_id':project,'actor':{'principal_id':actor},'request_id':str(uuid.uuid4()),'expected_revision':source.snapshot()['revision'],'operations':list(ops)})
        write({'op':'create','alias':'shared-private-note','record':{'title':'Reviewed sharing','status':'ready'}},{'op':'journal','task_id':'shared-private-note','kind':'observation','text':r'Private C:\Users\Synthetic\network.txt','evidence':['docs/shared-witness.md']})
        with self.assertRaises(LiveConflict) as error:fixture.clients[0].sync_once()
        self.assertEqual(error.exception.alternatives['side'],'sharing')
        self.assertEqual(fixture.clients[0].status()['state'],'sharing-review-required')
        preview=source.read_v1('journal-sharing-review',project)
        write({'op':'review_journal_sharing','review_digest':preview['review_digest'],'limit':preview['limit'],'cursor':preview['cursor'],'choices':{item['source_event_id']:'redacted' for item in preview['items']}})
        remote=fixture.root/'disclosure.git';subprocess.run(['git','init','--bare','-q',str(remote)],check=True,capture_output=True)
        git=WorkspaceGitSync(source,project,actor,str(remote),fixture.root/'disclosure-sender',export_selected=True)
        self.assertTrue(git.sync_now().pushed);self.assertFalse(git.sync_now().pushed)
        exported=GitWorkspaceTransport(fixture.root/'disclosure-receiver',str(remote)).fetch().payload
        self.assertNotIn('Synthetic',json.dumps(exported));self.assertEqual(exported['records']['task_journal_entries'],[])
        self.assertEqual(fixture.clients[0].sync_once()['state'],'current')
        self.assertEqual(fixture.clients[1].sync_once()['state'],'current')
        for store in (fixture.host,fixture.stores[2]):
            history=store.read_v1('history',project,key='shared-private-note')['items']
            projected=next(item for item in history if item.get('disclosure'))
            self.assertEqual(projected['details']['text'],'[LOCAL_PATH_REDACTED]')
            self.assertEqual(projected['details']['evidence'],['docs/shared-witness.md'])
            with store._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM journal_share_choices').fetchone()[0],0)
        self.assertEqual(fixture.clients[0].sync_once()['state'],'current')
        host=fixture.host
        host.transact_v1({'project_id':project,'actor':{'principal_id':fixture.actors[0]},'request_id':str(uuid.uuid4()),'expected_revision':host.snapshot()['revision'],'operations':[{'op':'create','alias':'host-private-note','record':{'title':'Host journal','status':'ready'}},{'op':'journal','task_id':'host-private-note','kind':'observation','text':r'Host path C:\Users\Synthetic\host.txt'}]})
        before=source.snapshot()['revision']
        with self.assertRaises(LiveConflict) as blocked:fixture.clients[0].sync_once()
        self.assertEqual(blocked.exception.alternatives['side'],'host-sharing')
        self.assertEqual(fixture.clients[0].status()['state'],'host-sharing-review-required')
        self.assertEqual(source.snapshot()['revision'],before)

if __name__=='__main__':unittest.main()
