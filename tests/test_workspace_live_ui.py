import copy
import json
import subprocess
import unittest
import uuid
from unittest.mock import patch
from tests import test_workspace_live_client as fixtures
from tools.workspace_live_client import WorkspaceLiveClient
from tools.workspace_live_service import WorkspaceLiveService
from tools.workspace_live_ui import WorkspaceLiveUI
from tools.roadmap_store import StoreError

class LiveUiTests(unittest.TestCase):
    def setUp(self):
        f=self.fixture=fixtures.LiveClientTests();f.setUp();self.addCleanup(f.doCleanups)
        self.project=f.project;self.actor=f.actors[1];self.store=f.stores[1]
        self.repo=f.root/'repo';self.repo.mkdir();subprocess.run(['git','init','-q',str(self.repo)],check=True,capture_output=True)
        self.store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actor},'expected_revision':self.store.snapshot()['revision'],'request_id':uuid.uuid4().hex,'operations':[{'op':'register_repository','root':str(self.repo)}]})
        (self.repo/'.research-workspace').mkdir();(self.repo/'.research-workspace/project.json').write_text(json.dumps({'protocol':'research-workspace/v1','project_id':self.project,'name':'Fixture'}),encoding='utf-8')
        self.token=f.root/'private-ui.token';self.token.write_text('a'*64,encoding='ascii')
        self.factory=lambda *a:WorkspaceLiveClient(*a,requester=f.clients[0].requester)
        self.service=WorkspaceLiveService(self.store,client_factory=self.factory)
        self.service.configure(self.project,enabled=True,url='https://host.example',token_file=str(self.token))
        self.ui=WorkspaceLiveUI(self.service,self.project,self.actor)
    def conflicts(self,count=1):
        for i in range(count):self.fixture.question(0,'common-'+str(i),'Host value');self.fixture.question(1,'common-'+str(i),'Local value')
    def choose_page(self,page,request_id=None):
        return self.ui.choose(page['review_id'],page['offset'],page['draft_revision'],request_id or uuid.uuid4().hex,{x['key']:'local' for x in page['items']})
    def prepare(self):
        page=self.ui.preview('join')
        while True:
            page=self.choose_page(page) if page['items'] else page
            if page['next_offset'] is None:break
            page=self.ui.read(page['next_offset'])
        return self.ui.prepare(page['review_id'],page['draft_revision'],True)
    def test_compact_review_and_exact_completed_handle_replay(self):
        self.conflicts();page=self.ui.preview('join')
        self.assertTrue(page['items']);self.assertTrue(all(not x['choice'] for x in page['items']));self.assertNotIn('bundle',page)
        with self.assertRaises(StoreError):self.ui.prepare(page['review_id'],page['draft_revision'],True)
        page=self.choose_page(page);prepared=self.ui.prepare(page['review_id'],page['draft_revision'],True)
        self.assertEqual(prepared['phase'],'prepared');self.assertTrue(prepared['command_id'])
        result=self.ui.submit(page['review_id'],prepared['command_id']);self.assertEqual(result['phase'],'acknowledged')
        revision=self.store.snapshot()['revision'];restored=WorkspaceLiveUI(self.service,self.project,self.actor)
        self.assertEqual(restored.submit(page['review_id'],prepared['command_id'])['phase'],'acknowledged');self.assertEqual(self.store.snapshot()['revision'],revision)
    def test_page_choice_request_is_idempotent_and_cas_bound(self):
        self.conflicts();page=self.ui.preview('join');identity=uuid.uuid4().hex
        chosen=self.choose_page(page,identity);again=self.choose_page(page,identity)
        self.assertEqual(chosen['draft_revision'],again['draft_revision']);self.assertEqual(again['choice_ack_request_id'],identity)
        with self.assertRaises(StoreError):self.ui.choose(page['review_id'],0,page['draft_revision'],identity,{x['key']:'remote' for x in page['items']})
        with self.assertRaises(StoreError):self.choose_page(page)
    def test_stale_host_requires_new_explicit_review(self):
        self.conflicts();page=self.choose_page(self.ui.preview('join'));self.fixture.question(0,'intervening','Keep new progress')
        with self.assertRaises(StoreError) as error:self.ui.prepare(page['review_id'],page['draft_revision'],True)
        self.assertEqual(error.exception.status,409);self.assertEqual(self.ui.read()['phase'],'reviewing')
    def test_crash_after_command_ack_keeps_recoverable_handle(self):
        prepared=self.prepare();save=self.ui._save;failed=False
        def crash(state):
            nonlocal failed
            if state['phase']=='acknowledged' and not failed:failed=True;raise OSError('lost local reply after inner completion')
            return save(state)
        with patch.object(self.ui,'_save',side_effect=crash):
            with self.assertRaises(OSError):self.ui.submit(prepared['review_id'],prepared['command_id'])
        self.ui=WorkspaceLiveUI(self.service,self.project,self.actor);self.assertEqual(self.ui.read()['phase'],'pending')
        with self.assertRaises(StoreError):self.ui.discard(prepared['review_id'])
        with self.assertRaises(StoreError):self.service.configure(self.project,enabled=True,url='https://replacement.example',token_file=str(self.token))
        revision=self.store.snapshot()['revision'];result=self.ui.submit(prepared['review_id'],prepared['command_id'])
        self.assertEqual(result['phase'],'acknowledged');self.assertEqual(self.store.snapshot()['revision'],revision)
    def test_caller_and_edited_handle_fail_closed(self):
        prepared=self.prepare();foreign=WorkspaceLiveUI(self.service,self.project,'another-owner')
        with self.assertRaises(StoreError):foreign.read()
        with self.assertRaises(StoreError):self.ui.submit(prepared['review_id'],'0'*64)
        with self.assertRaises(StoreError):self.ui.value('wrong-review','0'*64,'local',0)
    def test_large_values_are_bounded_and_full_slices_remain_available(self):
        self.conflicts();original=self.service.preview_join
        def large(project):
            review=original(project)
            field=next(a for c in review['preview']['changes'] for a in c['fields'].values() if a['conflict'])
            field['local']='X'*1500000
            return review
        with patch.object(self.service,'preview_join',side_effect=large):page=self.ui.preview('join')
        self.assertLess(len(json.dumps(page)),200000);row=next(x for x in page['items'] if x['local']['truncated'])
        self.assertEqual(len(row['local']['preview']),2048)
        data=self.ui.value(page['review_id'],row['key'],'local',0);self.assertLessEqual(len(data['text']),8192);self.assertTrue(data['next_offset'])
        self.assertEqual(data['digest'],row['local']['digest']);self.assertTrue(list(self.ui.documents.directory.glob('*.json')))
        self.assertEqual(WorkspaceLiveUI(self.service,self.project,self.actor).read()['review_id'],page['review_id'])
    def test_conflict_pagination_and_explicit_discard(self):
        self.conflicts(12);page=self.ui.preview('join');self.assertLessEqual(len(page['items']),20);self.assertIsNotNone(page['next_offset'])
        page=self.choose_page(page)
        with self.assertRaises(StoreError):self.ui.preview('join')
        self.assertTrue(self.ui.read(page['next_offset'])['items']);self.ui.discard(page['review_id']);self.assertEqual(self.ui.read()['phase'],'idle')

    def test_abbreviated_metadata_still_has_full_bounded_reader(self):
        self.conflicts();original=self.service.preview_join
        def large(project):
            review=original(project)
            change=next(c for c in review['preview']['changes'] if any(a['conflict'] for a in c['fields'].values()))
            field=next(k for k,a in change['fields'].items() if a['conflict']);change['fields']['huge-'+('K'*600000)]=change['fields'].pop(field)
            return review
        with patch.object(self.service,'preview_join',side_effect=large):page=self.ui.preview('join')
        self.assertLess(len(json.dumps(page)),200000);row=next(r for r in page['items'] if r['label_truncated']['field'])
        self.assertEqual(len(row['field']),512)
        full=self.ui.value(page['review_id'],row['key'],'field',0);self.assertLessEqual(len(full['text']),8192);self.assertIsNotNone(full['next_offset'])
    def test_known_rejected_handle_can_be_confirmed_offline_without_execution(self):
        prepared=self.prepare();self.fixture.question(0,'changed-after-prepare','Host changed')
        rejected=self.ui.submit(prepared['review_id'],prepared['command_id']);self.assertEqual(rejected['phase'],'rejected')
        with patch.object(self.ui,'_client',side_effect=AssertionError('must not execute a known rejected command')):
            self.assertEqual(self.ui.submit(prepared['review_id'],prepared['command_id'])['phase'],'rejected')
    def test_corrupt_manifest_and_false_ack_fail_closed(self):
        prepared=self.prepare();original=self.service.join
        with patch.object(self.service,'join',return_value={'state':'current'}):
            result=self.ui.submit(prepared['review_id'],prepared['command_id']);self.assertEqual(result['phase'],'pending')
        self.assertEqual(self.ui.submit(prepared['review_id'],prepared['command_id'])['phase'],'acknowledged')
        state=self.ui._load();state['choice_receipts']=[{'request_id':'a','digest':'bad'}];self.ui._save(state)
        with self.assertRaises(StoreError) as error:self.ui.read()
        self.assertEqual(error.exception.status,503)

    def test_immutable_conflict_has_no_remote_choice(self):
        self.fixture.question(0,'identity-source','Source witness');self.fixture.clients[0].sync_once()
        with self.fixture.host._connection() as db:db.execute('UPDATE principals SET label=? WHERE id=?',('Changed immutable display',self.fixture.actors[0]))
        page=self.ui.preview('join');immutable=next(x for x in page['items'] if not x['remote_allowed'])
        choices={x['key']:'local' for x in page['items']};choices[immutable['key']]='remote'
        with self.assertRaises(StoreError):self.ui.choose(page['review_id'],0,page['draft_revision'],uuid.uuid4().hex,choices)
        self.assertEqual(self.ui.read()['draft_revision'],page['draft_revision'])
    def test_changed_host_and_unsafe_offsets_do_not_apply(self):
        page=self.ui.preview('join')
        for offset in (True,-1,1):
            with self.assertRaises(StoreError):self.ui.read(offset)
        self.service.configure(self.project,enabled=True,url='https://replacement.example',token_file=str(self.token))
        with self.assertRaises(StoreError):self.ui.prepare(page['review_id'],page['draft_revision'],True)
        self.ui.discard(page['review_id']);self.assertEqual(self.ui.read()['phase'],'idle')
    def test_outcome_survives_crash_before_active_manifest_publish(self):
        prepared=self.prepare();original=self.ui.documents.freeze
        def crash(state):
            if state['phase']=='acknowledged':raise OSError('crash after durable outcome')
            return original(state)
        with patch.object(self.ui.documents,'freeze',side_effect=crash):
            with self.assertRaises(OSError):self.ui.submit(prepared['review_id'],prepared['command_id'])
        restored=WorkspaceLiveUI(self.service,self.project,self.actor)
        self.assertEqual(restored.read()['phase'],'acknowledged')
        revision=self.store.snapshot()['revision']
        with patch.object(restored,'_client',side_effect=AssertionError('must not execute again')):
            result=restored.submit(prepared['review_id'],prepared['command_id'])
        self.assertTrue(result['historical_only']);self.assertEqual(self.store.snapshot()['revision'],revision)

    def test_corrupt_outcome_ledger_blocks_new_execution(self):
        prepared=self.prepare();revision=self.store.snapshot()['revision']
        self.ui.receipts_path.write_text('{broken',encoding='utf-8')
        with patch.object(self.ui,'_client',side_effect=AssertionError('corrupt receipt must fail before execution')):
            with self.assertRaises(StoreError) as error:self.ui.submit(prepared['review_id'],prepared['command_id'])
        self.assertEqual(error.exception.status,503);self.assertEqual(self.store.snapshot()['revision'],revision)
        self.assertEqual(self.ui.receipts_path.read_text(encoding='utf-8'),'{broken')

    def test_terminal_receipts_are_bounded_and_eviction_does_not_execute(self):
        prepared=self.prepare();self.ui.submit(prepared['review_id'],prepared['command_id'])
        original=self.ui._load()
        for index in range(256):
            state=json.loads(json.dumps(original));state['review_id']='retained-'+str(index)
            state['command_id']=format(index,'064x')
            state['result']['reviewed_command']['id']=state['command_id']
            self.ui._retain_outcome(state)
        rows=self.ui._outcomes();self.assertEqual(len(rows),256)
        self.assertEqual(rows[0]['review_id'],'retained-0');self.assertEqual(rows[-1]['review_id'],'retained-255')
        self.ui.discard(prepared['review_id']);revision=self.store.snapshot()['revision']
        with patch.object(self.ui,'_client',side_effect=AssertionError('evicted handle must never execute')):
            with self.assertRaises(StoreError):self.ui.submit(prepared['review_id'],prepared['command_id'])
        self.assertEqual(self.store.snapshot()['revision'],revision)

    def test_completed_handle_survives_discard_without_replaying_new_work(self):
        prepared=self.prepare();self.ui.submit(prepared['review_id'],prepared['command_id']);self.ui.discard(prepared['review_id'])
        self.ui=WorkspaceLiveUI(self.service,self.project,self.actor);revision=self.store.snapshot()['revision']
        with patch.object(self.ui,'_client',side_effect=AssertionError('historical confirmation must not execute or contact another host')):
            result=self.ui.submit(prepared['review_id'],prepared['command_id'])
        self.assertEqual(result['phase'],'acknowledged');self.assertTrue(result['historical_only']);self.assertEqual(self.store.snapshot()['revision'],revision)
        with self.assertRaises(StoreError):WorkspaceLiveUI(self.service,self.project,'other-owner').submit(prepared['review_id'],prepared['command_id'])


class HistoricalClaimReviewTests(unittest.TestCase):
    def test_saved_review_is_readable_offline_and_never_claims_current_lease(self):
        from tests import test_workspace_live_claims as fixtures
        fixture=fixtures.LiveClaimTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        review=fixture.claims[0].preview({'op':'claim_task','task_id':'TASK','rationale':'Explicit fixture review','lease_seconds':120})
        fixture.online=False;cached=fixture.claims[0].review_saved()
        self.assertEqual(cached['review'],review);self.assertEqual(cached['state'],'prepared');self.assertFalse(cached['acknowledgment_is_current_lease'])
        fixture.online=True;fixture.claims[0].submit(review['review_id']);fixture.online=False
        acknowledged=fixture.claims[0].review_saved();self.assertEqual(acknowledged['state'],'acknowledged');self.assertFalse(acknowledged['acknowledgment_is_current_lease'])
