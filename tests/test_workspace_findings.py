"""Project-native finding operations and preserved support history."""
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from tools.roadmap_store import RoadmapStore, StoreError

class FindingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=RoadmapStore(self.root/'app.sqlite3')
        self.project=self.store.read_v1('projects')['items'][0]['id']
        self.actor=self.store.read_v1('capabilities',self.project)['capabilities']['local_principal_id']
        (self.root/'source.txt').write_text('class Ball: speed = 2',encoding='utf-8')
        self.write({'op':'register_root','id':'root','path':str(self.root)})
        self.write({'op':'register_artifact','id':'artifact','root_id':'root','relative_path':'source.txt','kind':'source'})
        self.write({'op':'register_artifact_version','artifact_id':'artifact'})
        self.version=self.store.read_v1('artifact-versions',self.project)['items'][0]['id']

    def write(self,*ops,request_id=None,records=None):
        payload={'project_id':self.project,'actor':{'principal_id':self.actor},'request_id':request_id or str(uuid.uuid4()),'operations':list(ops)}
        if records is None:payload['expected_revision']=self.store.snapshot()['revision']
        else:payload.update(concurrency_mode='records',expected_records=records)
        self.last_payload=payload
        return self.store.transact_v1(payload)

    def support(self):
        return {'artifact_version_id':self.version,'locator':{'line':1,'member':'Ball.speed'},'method':'Read pinned synthetic source','limitations':'Synthetic fixture, not native execution'}

    def create(self,**fields):
        result=self.write({'op':'create_finding','claim':'Ball speed is configured here','scope':{'identifier':'speed','namespace':'class','scope_key':'Ball','artifact_version_id':self.version},'classification':'source-supported','uncertainty':'Native behavior has not been tested','evidence':[self.support()],**fields})
        return next(change for change in result['changed_records'] if change['type']=='finding')

    def finding(self,identity):
        return next(row for row in self.store.read_v1('findings',self.project)['items'] if row['id']==identity)

    def test_create_and_revise_preserve_source_and_prior_revisions(self):
        first=self.create();before=self.finding(first['id'])
        self.assertEqual(before['evidence_review_state'],'unreviewed')
        self.assertEqual(before['evidence_origin_id'],first['revision_id'])
        self.write({'op':'revise_finding','finding_id':first['id'],'expected_record_revision':before['record_revision'],'fields':{'claim':'Ball speed is a source configuration, not measured motion'},'rationale':'Narrow the authored claim'})
        after=self.finding(first['id'])
        self.assertEqual(after['evidence_revision'],2)
        self.assertNotEqual(after['current_revision_id'],first['revision_id'])
        history=self.store.read_v1('finding-revisions',self.project,subject_id=first['id'])['items']
        self.assertEqual(len(history),2)
        self.assertEqual(next(row for row in history if row['id']==first['revision_id'])['claim'],before['claim'])
        links=self.store.read_v1('evidence-links',self.project,subject_type='finding_revision',subject_id=after['evidence_origin_id'])['items']
        self.assertEqual(len(links),1)
        self.assertEqual(links[0]['artifact_version_id'],self.version)
        self.assertEqual(links[0]['locator'],self.support()['locator'])

    def question(self):
        self.write({'op':'create_question','id':'q','text':'Where is speed configured?','criteria':{'any_of':[{'identifier':'speed','namespace':'class','scope_key':'Ball'}]}})

    def process(self):self.write({'op':'process_jobs','limit':100})

    def test_editorial_lineage_preserves_dismissal_and_semantic_revision_creates_successor(self):
        self.question();first=self.create();self.process()
        hint=self.store.read_v1('hints',self.project)['items'][0]
        self.write({'op':'decide_hint','hint_id':hint['id'],'state':'dismissed','reason':'Known configuration, not runtime behavior'})
        before=self.finding(first['id'])
        self.write({'op':'revise_finding','finding_id':first['id'],'expected_record_revision':before['record_revision'],'fields':{'uncertainty':'Editorial clarification of limitations'},'rationale':'Clarify uncertainty'})
        current=self.finding(first['id']);self.assertEqual(current['evidence_origin_id'],first['revision_id']);self.assertEqual(current['evidence_revision'],1)
        self.write({'op':'replay_rule','rule_version':'1'});self.process()
        hints=self.store.read_v1('hints',self.project)['items'];self.assertEqual(len(hints),1);self.assertEqual(hints[0]['state'],'dismissed')
        self.write({'op':'revise_finding','finding_id':first['id'],'expected_record_revision':current['record_revision'],'fields':{'claim':'This source contains a speed field only'},'rationale':'Narrow semantic claim'})
        self.process();hints=self.store.read_v1('hints',self.project)['items']
        self.assertEqual(len(hints),2);self.assertEqual(next(h for h in hints if h['id']==hint['id'])['state'],'stale')
        self.assertEqual(next(h for h in hints if h['id']!=hint['id'])['state'],'new')
        self.assertEqual(len(next(h for h in hints if h['id']==hint['id'])['decisions']),1)

    def test_retraction_preserves_source_fields_and_decisions_and_blocks_delayed_publication(self):
        self.question();first=self.create();self.process();hint=self.store.read_v1('hints',self.project)['items'][0]
        self.write({'op':'decide_hint','hint_id':hint['id'],'state':'useful','reason':'Useful synthetic witness'})
        link=self.store.read_v1('evidence-links',self.project,subject_id=first['revision_id'])['items'][0]
        request='withdraw-source'
        op={'op':'set_evidence_state','evidence_link_id':link['id'],'expected_state':'available','state':'retracted','reason':'The witness no longer supports this claim'}
        result=self.write(op,request_id=request);self.assertEqual(self.store.transact_v1(self.last_payload),result)
        self.process();hints=self.store.read_v1('hints',self.project)['items']
        self.assertEqual(hints[0]['state'],'stale');self.assertEqual(hints[0]['decisions'][0]['state'],'useful')
        after=self.store.read_v1('evidence-links',self.project,subject_id=first['revision_id'])['items'][0]
        self.assertEqual({k:v for k,v in after.items() if k!='state'},{k:v for k,v in link.items() if k!='state'})
        self.assertEqual(self.finding(first['id'])['evidence_review_state'],'unsupported')
        self.assertEqual(len(self.store.read_v1('evidence-changes',self.project,subject_id=link['id'])['items']),1)
        self.assertEqual(len(self.store.read_v1('research-review-notices',self.project)['items']),1)
        second=self.create(claim='Withdraw before processing')
        support=self.store.read_v1('evidence-links',self.project,subject_id=second['revision_id'])['items'][0]
        self.write({**op,'evidence_link_id':support['id']});self.process()
        self.assertFalse(any(h['candidate_id']==second['revision_id'] for h in self.store.read_v1('hints',self.project)['items']))

    def test_record_guards_scope_archive_and_bounded_reads(self):
        first=self.create();current=self.finding(first['id'])
        op={'op':'revise_finding','finding_id':first['id'],'expected_record_revision':current['record_revision'],'fields':{'claim':'Narrowed source claim'},'rationale':'Explicit edit'}
        with self.assertRaises(StoreError):self.write(op,records=[])
        self.write(op,records=[{'type':'finding','id':first['id'],'revision':current['record_revision']}])
        with self.assertRaises(StoreError):self.write(op)
        before=self.store.snapshot()['revision']
        for bad in ({'artifact_version_id':'foreign-source'}, {'task_id':'foreign-task'}):
            with self.assertRaises(StoreError):self.create(scope=bad)
        self.assertEqual(self.store.snapshot()['revision'],before)
        current=self.finding(first['id']);self.write({'op':'archive_finding','finding_id':first['id'],'expected_record_revision':current['record_revision'],'rationale':'Keep history outside active research'})
        self.assertIsNotNone(self.finding(first['id'])['archived_at'])
        page=self.store.read_v1('finding-revisions',self.project,subject_id=first['id'],limit=1)
        self.assertIsNotNone(page['next_cursor'])
        with self.assertRaises(StoreError):self.store.read_v1('finding-revisions',self.project,subject_id='other',limit=1,cursor=page['next_cursor'])

    def test_portable_import_finding_publication_and_retraction(self):
        from tools import roadmap_exchange as exchange
        first=self.create();bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        self.assertIn('finding_revision_lineage',bundle['records'])
        receiver=RoadmapStore(self.root/'receiver.sqlite3')
        local=receiver.read_v1('projects')['items'][0]['id'];actor=receiver.read_v1('capabilities',local)['capabilities']['local_principal_id']
        def write(project,*ops):
            return receiver.transact_v1({'project_id':project,'actor':{'principal_id':actor},'expected_revision':receiver.snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':list(ops)})
        write(local,{'op':'create_project','id':self.project,'slug':'joined','goal':'Review imported research'})
        write(self.project,{'op':'create_question','id':'receiver-question','text':'Where is speed?','criteria':{'any_of':[{'identifier':'speed','namespace':'class','scope_key':'Ball'}]}})
        def receive(incoming):
            preview=receiver.exchange_preview(self.project,bundle=incoming)['preview']
            choices={exchange.canonical([c['table'],c['key'],f]):'local' for c in preview['changes'] for f,a in c['fields'].items() if a['conflict']}
            write(self.project,{'op':'import_bundle','bundle':incoming,'choices':choices})
        receive(bundle)
        exported=receiver.exchange_preview(self.project,portable=True)['bundle']
        self.assertFalse(any(event['kind']=='import_publication' for event in exported['history']))
        self.assertEqual({event['id'] for event in exported['history'] if event['kind']=='publish_finding'},{event['id'] for event in bundle['history'] if event['kind']=='publish_finding'})
        write(self.project,{'op':'process_jobs','limit':100})
        hints=receiver.read_v1('hints',self.project)['items'];self.assertEqual(len(hints),1)
        self.write({'op':'register_exchange_snapshot','bundle':bundle})
        link=self.store.read_v1('evidence-links',self.project,subject_id=first['revision_id'])['items'][0]
        self.write({'op':'set_evidence_state','evidence_link_id':link['id'],'expected_state':'available','state':'retracted','reason':'Unsupported source interpretation'})
        receive(self.store.exchange_preview(self.project,ancestor_id=bundle['id'],portable=True)['bundle'])
        self.assertEqual(receiver.read_v1('hints',self.project)['items'][0]['state'],'stale')
        self.assertEqual(len(receiver.read_v1('evidence-changes',self.project)['items']),1)
        with receiver._connection() as db:
            self.assertEqual(json.loads(db.execute('SELECT bundle_json FROM exchange_snapshots WHERE project_id=? AND id=?',(self.project,bundle['id'])).fetchone()[0]),bundle)

    def test_invalid_lineage_and_source_rewrites_reject_before_import(self):
        import copy
        from tools import roadmap_exchange as exchange
        first=self.create();second=self.create(claim='A different authored claim')
        bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        snapshot=self.store.snapshot()['revision']
        malformed=copy.deepcopy(bundle)
        malformed['records']['finding_revision_lineage'][0]['evidence_origin_id']=second['revision_id'] if malformed['records']['finding_revision_lineage'][0]['revision_id']==first['revision_id'] else first['revision_id']
        malformed['id']=exchange.research.digest({k:v for k,v in malformed.items() if k!='id'})
        with self.assertRaises(StoreError):self.store.exchange_preview(self.project,bundle=malformed)
        rewritten=copy.deepcopy(bundle);rewritten['records']['evidence_links'][0]['method']='Rewritten source provenance'
        rewritten['id']=exchange.research.digest({k:v for k,v in rewritten.items() if k!='id'})
        with self.assertRaisesRegex(StoreError,'Immutable evidence'):self.store.exchange_preview(self.project,bundle=rewritten)
        self.assertEqual(self.store.snapshot()['revision'],snapshot)

    def test_supersession_and_archive_keep_history_without_reactivating_old_hint(self):
        self.question();old=self.create();replacement=self.create(claim='Alternative interpretation');self.process()
        old_link=self.store.read_v1('evidence-links',self.project,subject_id=old['revision_id'])['items'][0]
        new_link=self.store.read_v1('evidence-links',self.project,subject_id=replacement['revision_id'])['items'][0]
        self.write({'op':'set_evidence_state','evidence_link_id':old_link['id'],'expected_state':'available','state':'superseded','replacement_id':new_link['id'],'reason':'Use the explicitly narrower interpretation'})
        event=self.store.read_v1('evidence-changes',self.project,subject_id=old_link['id'])['items'][0]
        self.assertEqual(event['replacement_id'],new_link['id'])
        current=self.finding(replacement['id']);self.write({'op':'archive_finding','finding_id':replacement['id'],'expected_record_revision':current['record_revision'],'rationale':'Pause this interpretation'})
        self.process();self.assertTrue(all(h['state']=='stale' for h in self.store.read_v1('hints',self.project)['items']))
        current=self.finding(replacement['id']);self.write({'op':'restore_finding','finding_id':replacement['id'],'expected_record_revision':current['record_revision'],'rationale':'Restore for explicit review'})
        self.process();self.assertTrue(all(h['state']=='stale' for h in self.store.read_v1('hints',self.project)['items']))
        self.assertIsNone(self.finding(replacement['id'])['archived_at'])

    def test_schema11_upgrade_preserves_history_and_legacy_bundle(self):
        import sqlite3
        import sys
        from contextlib import closing
        from unittest.mock import patch
        from tools import roadmap_schema,roadmap_exchange as exchange
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server as server
        bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        for table in ('finding_revision_lineage','evidence_change_events','research_review_notices'):
            bundle['schema'].pop(table);bundle['records'].pop(table)
        bundle['id']=exchange.research.digest({k:v for k,v in bundle.items() if k!='id'})
        self.write({'op':'register_exchange_snapshot','bundle':bundle})
        events=self.store.snapshot()['events'];path=self.store.path
        with self.store._connection() as db:
            for table in ('finding_revision_lineage','evidence_change_events','research_review_notices'):db.execute('DROP TABLE '+table)
            db.execute('UPDATE store_meta SET schema_version=11')
        with patch.object(roadmap_schema,'_migration_checkpoint',side_effect=RuntimeError('injected findings upgrade')):
            with self.assertRaisesRegex(RuntimeError,'injected findings'):RoadmapStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],11)
        with server.service_lock(path):backup=server.checkpoint_before_upgrade(path);upgraded=RoadmapStore(path)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],11)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(upgraded.snapshot()['events'],events)
        with upgraded._connection() as db:
            self.assertEqual(json.loads(db.execute('SELECT bundle_json FROM exchange_snapshots WHERE id=?',(bundle['id'],)).fetchone()[0]),bundle)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_missing_editorial_lineage_cannot_downgrade_a_portable_bundle(self):
        import copy
        from tools import roadmap_exchange as exchange
        first=self.create();current=self.finding(first['id'])
        self.write({'op':'revise_finding','finding_id':first['id'],'expected_record_revision':current['record_revision'],'fields':{'uncertainty':'Editorial descendant with shared source support'},'rationale':'Clarify limitations'})
        original=self.store.exchange_preview(self.project,portable=True)['bundle']
        for omitted in (('finding_revision_lineage',),('finding_revision_lineage','evidence_change_events','research_review_notices')):
            bad=copy.deepcopy(original)
            for table in omitted:bad['schema'].pop(table);bad['records'].pop(table)
            bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
            with self.assertRaisesRegex(StoreError,'lineage'):self.store.exchange_preview(self.project,bundle=bad)
        bad=copy.deepcopy(original);bad['records']['finding_revision_lineage'].pop()
        bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
        with self.assertRaisesRegex(StoreError,'lineage'):self.store.exchange_preview(self.project,bundle=bad)

    def test_import_cannot_keep_support_available_while_accepting_its_withdrawal(self):
        import copy
        from tools import roadmap_exchange as exchange
        first=self.create();bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        link=bundle['records']['evidence_links'][0]
        for source_state in ('available','retracted'):
            incoming=copy.deepcopy(bundle);incoming['records']['evidence_links'][0]['state']=source_state
            incoming['records']['evidence_change_events'].append({'id':str(uuid.uuid4()),'project_id':self.project,'evidence_link_id':link['id'],'from_state':'available','to_state':'retracted','reason':'Immutable withdrawal requires review','replacement_id':None,'created_by':self.actor,'created_at':'2026-11-14T00:00:00Z'})
            incoming['id']=exchange.research.digest({k:v for k,v in incoming.items() if k!='id'})
            before=self.store.snapshot()['revision']
            with self.assertRaisesRegex(StoreError,'withdraw'):
                preview=self.store.exchange_preview(self.project,bundle=incoming)['preview']
                choices={exchange.canonical([c['table'],c['key'],f]):'local' for c in preview['changes'] for f,a in c['fields'].items() if a['conflict']}
                self.write({'op':'import_bundle','bundle':incoming,'choices':choices})
            self.assertEqual(self.store.snapshot()['revision'],before)
            self.assertEqual(self.store.read_v1('evidence-links',self.project,subject_id=first['revision_id'])['items'][0]['state'],'available')

if __name__=='__main__':unittest.main()
