"""Evidence-pinned answer review and explicit question resolution."""
import json
import unittest
import uuid
from tests import test_workspace_findings as finding_fixtures
from tools.roadmap_store import StoreError
from tools import roadmap_schema

class AnswersTests(unittest.TestCase):
    def setUp(self):
        fixture=finding_fixtures.FindingsTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.store=fixture.store;self.project=fixture.project;self.actor=fixture.actor;self.root=fixture.root
        fixture.question();finding=fixture.create();self.finding_id=finding['id']
        self.link=self.store.read_v1('evidence-links',self.project,subject_id=finding['revision_id'])['items'][0]
        with self.store._connection() as db:
            self.reviewer=roadmap_schema.principal_for_label(db,self.project,'independent reviewer')
            db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',(json.dumps(['contribute','review']),self.project,self.reviewer))

    def question(self):return next(q for q in self.store.read_v1('questions',self.project)['items'] if q['id']=='q')

    def write(self,*ops,actor=None,records=None,request_id=None):
        payload={'project_id':self.project,'actor':{'principal_id':actor or self.actor},'request_id':request_id or str(uuid.uuid4()),'operations':list(ops)}
        if records is None:payload['expected_revision']=self.store.snapshot()['revision']
        else:payload.update(concurrency_mode='records',expected_records=records)
        self.last_payload=payload;return self.store.transact_v1(payload)

    def propose(self,**extra):
        result=self.write({'op':'create_answer','question_id':'q','expected_question_revision':self.question()['record_revision'],'claim':'The pinned source configures speed in Ball','evidence_link_ids':[self.link['id']],**extra})
        return next(c for c in result['changed_records'] if c['type']=='answer')

    def answer(self,identity):return next(a for a in self.store.read_v1('answers',self.project,subject_id='q')['items'] if a['id']==identity)

    def review(self,answer,outcome='accepted',actor=None,supersedes=None):
        result=self.write({'op':'review_answer','answer_id':answer,'expected_question_revision':self.question()['record_revision'],'outcome':outcome,'rationale':'Independent review of the pinned synthetic source','supersedes':supersedes or []},actor=actor or self.reviewer)
        return next(c for c in result['changed_records'] if c['type']=='answer_review')

    def resolve(self,answer):
        return self.write({'op':'resolve_question','question_id':'q','answer_id':answer,'expected_record_revision':self.question()['record_revision'],'rationale':'Select the independently reviewed answer'})

    def test_propose_review_and_explicit_resolution_are_distinct(self):
        proposed=self.propose();identity=proposed['id']
        self.assertEqual(self.answer(identity)['review_state'],'unreviewed')
        self.assertEqual(self.question()['state'],'open')
        with self.assertRaises(StoreError):self.resolve(identity)
        with self.assertRaises(StoreError):self.review(identity,actor=self.actor)
        reviewed=self.review(identity)
        self.assertEqual(self.answer(identity)['review_state'],'accepted')
        self.assertEqual(self.question()['state'],'open')
        self.resolve(identity)
        question=self.question();self.assertEqual(question['state'],'resolved')
        self.assertEqual(question['resolution']['answer_id'],identity)
        self.assertEqual(self.store.read_v1('answer-reviews',self.project,subject_id=identity)['items'][0]['id'],reviewed['id'])
        self.assertEqual(len(self.store.read_v1('question-resolutions',self.project,subject_id='q')['items']),1)

    def test_withdrawal_reopens_resolution_without_erasing_review_history(self):
        answer=self.propose()['id'];self.review(answer);self.resolve(answer)
        self.write({'op':'set_evidence_state','evidence_link_id':self.link['id'],'expected_state':'available','state':'retracted','reason':'Witness no longer supports this conclusion'})
        question=self.question();self.assertEqual(question['state'],'investigating');self.assertEqual(question['recorded_state'],'investigating')
        self.assertEqual(question['resolution']['active'],0)
        self.assertEqual(self.answer(answer)['review_state'],'stale')
        self.assertEqual(self.store.read_v1('answer-reviews',self.project,subject_id=answer)['items'][0]['outcome'],'accepted')
        self.assertEqual({e['action'] for e in self.store.read_v1('question-resolutions',self.project,subject_id='q')['items']},{'resolved','invalidated'})
        notices=self.store.read_v1('research-review-notices',self.project,subject_type='answer',subject_id=answer)['items'];self.assertEqual(len(notices),1)
        with self.assertRaises(StoreError):self.resolve(answer)

    def test_review_disagreement_and_explicit_own_supersession_do_not_use_time(self):
        answer=self.propose()['id'];accepted=self.review(answer)
        with self.store._connection() as db:
            reviewer2=roadmap_schema.principal_for_label(db,self.project,'another reviewer')
            db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',(json.dumps(['contribute','review']),self.project,reviewer2))
        rejected=self.review(answer,'rejected',reviewer2)
        self.assertEqual(self.answer(answer)['review_state'],'disputed')
        with self.assertRaises(StoreError):self.resolve(answer)
        with self.assertRaises(StoreError):self.review(answer,'accepted',reviewer2,[accepted['id']])
        self.review(answer,'accepted',reviewer2,[rejected['id']])
        self.assertEqual(self.answer(answer)['review_state'],'accepted');self.resolve(answer)
        self.assertEqual(len(self.store.read_v1('answer-reviews',self.project,subject_id=answer)['items']),3)

    def test_finding_editorial_change_preserves_resolution_but_semantic_change_invalidates(self):
        answer=self.propose()['id'];self.review(answer);self.resolve(answer)
        finding=self.fixture.finding(self.finding_id)
        self.write({'op':'revise_finding','finding_id':self.finding_id,'expected_record_revision':finding['record_revision'],'fields':{'uncertainty':'Editorial clarification only'},'rationale':'Clarify scope of uncertainty'})
        self.assertEqual(self.question()['state'],'resolved')
        finding=self.fixture.finding(self.finding_id)
        self.write({'op':'revise_finding','finding_id':self.finding_id,'expected_record_revision':finding['record_revision'],'fields':{'claim':'Only source configuration is known'},'rationale':'Narrow semantic interpretation'})
        self.assertEqual(self.question()['state'],'investigating');self.assertEqual(self.answer(answer)['review_state'],'stale')

    def test_direct_support_replay_question_guards_and_withdrawn_state(self):
        result=self.propose(evidence_link_ids=[],evidence=[self.fixture.support()]);payload=self.last_payload
        self.assertEqual(self.store.transact_v1(payload)['changed_records'][0]['id'],result['id'])
        answer=self.answer(result['id']);self.assertEqual(len(answer['evidence']),1)
        with self.assertRaises(StoreError):self.propose(evidence_link_ids=[],evidence=[])
        with self.assertRaises(StoreError):self.propose(evidence_link_ids=['foreign-link'])
        with self.assertRaises(StoreError):self.propose(expected_question_revision=999)
        op={'op':'review_answer','answer_id':result['id'],'expected_question_revision':self.question()['record_revision'],'outcome':'accepted','rationale':'Scoped independent check','supersedes':[]}
        with self.assertRaises(StoreError):self.write(op,actor=self.reviewer,records=[])
        self.write(op,actor=self.reviewer,records=[{'type':'question','id':'q','revision':self.question()['record_revision']}])
        self.resolve(result['id'])
        self.write({'op':'set_question_state','question_id':'q','expected_record_revision':self.question()['record_revision'],'state':'withdrawn','rationale':'Question no longer belongs to active scope'})
        with self.assertRaises(StoreError):self.propose()
        self.assertEqual(self.question()['state'],'withdrawn')

    def test_question_changes_and_review_capability_revocation_remain_visible(self):
        answer=self.propose()['id'];self.review(answer);self.resolve(answer)
        with self.store._connection() as db:
            db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',(json.dumps(['contribute']),self.project,self.reviewer))
        self.assertEqual(self.question()['recorded_state'],'resolved');self.assertEqual(self.question()['state'],'investigating')
        with self.assertRaises(StoreError):self.review(answer)
        self.write({'op':'set_question_state','question_id':'q','expected_record_revision':self.question()['record_revision'],'state':'open','rationale':'Revisit the requirements explicitly'})
        self.write({'op':'revise_question','question_id':'q','expected_record_revision':self.question()['record_revision'],'text':'Where is speed measured rather than configured?','rationale':'Change the actual question'})
        self.assertEqual(self.answer(answer)['review_state'],'stale')
        self.assertIn('Question requirements changed',self.answer(answer)['review_concerns'][0])

    def test_imported_review_history_cannot_mint_local_review_authority(self):
        from tools.roadmap_store import RoadmapStore
        from tools import roadmap_exchange as exchange
        answer=self.propose()['id'];self.review(answer);self.resolve(answer)
        bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        self.assertNotIn('answer_review_receipts',bundle['records'])
        receiver=RoadmapStore(self.root/'peer.sqlite3');p=receiver.read_v1('projects')['items'][0]['id'];actor=receiver.read_v1('capabilities',p)['capabilities']['local_principal_id']
        def write(project,*ops):return receiver.transact_v1({'project_id':project,'actor':{'principal_id':actor},'expected_revision':receiver.snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':list(ops)})
        write(p,{'op':'create_project','id':self.project,'slug':'joined-research','goal':'Review the shared conclusions'})
        # Even granting this reviewer local membership cannot authenticate a row imported from Git.
        with receiver._connection() as db:
            remote=dict(next(row for row in bundle['records']['principals'] if row['id']==self.reviewer))
            columns=list(remote);db.execute('INSERT INTO principals('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[remote[k] for k in columns])
            db.execute('INSERT INTO project_members VALUES(?,?,?)',(self.project,self.reviewer,json.dumps(['contribute','review'])))
        preview=receiver.exchange_preview(self.project,bundle=bundle)['preview']
        choices={exchange.canonical([c['table'],c['key'],f]):'local' for c in preview['changes'] for f,a in c['fields'].items() if a['conflict']}
        write(self.project,{'op':'import_bundle','bundle':bundle,'choices':choices})
        received=receiver.read_v1('answers',self.project,subject_id='q')['items'][0]
        self.assertEqual(received['review_state'],'needs_review')
        question=receiver.read_v1('questions',self.project)['items'][0]
        self.assertEqual(question['recorded_state'],'resolved');self.assertEqual(question['state'],'investigating')
        with receiver._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM answer_review_receipts').fetchone()[0],0)
        with self.assertRaises(StoreError):write(self.project,{'op':'resolve_question','question_id':'q','answer_id':answer,'expected_record_revision':question['record_revision'],'rationale':'Imported review is not permission'})
        exported=receiver.exchange_preview(self.project,portable=True)['bundle']
        self.assertEqual(exported['records']['answer_reviews'],bundle['records']['answer_reviews'])

    def test_review_replay_rechecks_current_capability(self):
        answer=self.propose()['id'];self.review(answer);payload=self.last_payload
        with self.store._connection() as db:db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',(json.dumps(['contribute']),self.project,self.reviewer))
        with self.assertRaises(StoreError) as error:self.store.transact_v1(payload)
        self.assertEqual(error.exception.status,403)
        self.assertEqual(self.answer(answer)['review_state'],'needs_review')

    def test_forged_context_missing_provenance_and_review_cycles_reject_import(self):
        import copy
        from tools import roadmap_exchange as exchange
        answer=self.propose()['id'];review=self.review(answer)
        bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        for alteration in ('context','missing','cycle'):
            bad=copy.deepcopy(bundle)
            if alteration=='context':bad['records']['answer_contexts'][0]['support_digest']='0'*64
            elif alteration=='missing':
                for table in ('answer_contexts','answer_reviews','question_answer_selections','question_resolution_events'):bad['schema'].pop(table);bad['records'].pop(table)
            else:bad['records']['answer_reviews'][0]['supersedes_json']=json.dumps([review['id']])
            bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
            with self.assertRaises(StoreError):self.store.exchange_preview(self.project,bundle=bad)

    def test_schema12_upgrade_does_not_promote_legacy_answer_gates(self):
        import sqlite3
        import sys
        from contextlib import closing
        from unittest.mock import patch
        from tools import roadmap_exchange as exchange
        from pathlib import Path
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server as server
        with self.store._connection() as db:
            db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?,?)',('legacy-answer',self.project,'q','Historical accepted claim',json.dumps([self.link['id']]),'accepted',self.actor,'historical'))
            original=dict(db.execute("SELECT * FROM answers WHERE id='legacy-answer'").fetchone())
        bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        for table in ('answer_contexts','answer_reviews','question_answer_selections','question_resolution_events'):bundle['schema'].pop(table);bundle['records'].pop(table)
        bundle['id']=exchange.research.digest({k:v for k,v in bundle.items() if k!='id'})
        self.write({'op':'register_exchange_snapshot','bundle':bundle});events=self.store.snapshot()['events']
        with self.store._connection() as db:
            for table in ('answer_review_receipts','question_resolution_events','question_answer_selections','answer_reviews','answer_contexts'):db.execute('DROP TABLE '+table)
            db.execute('UPDATE store_meta SET schema_version=12')
        with patch.object(roadmap_schema,'_migration_checkpoint',side_effect=RuntimeError('injected answer migration')):
            with self.assertRaisesRegex(RuntimeError,'injected answer'):type(self.store)(self.store.path)
        with closing(sqlite3.connect(self.store.path)) as db:self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],12)
        with server.service_lock(self.store.path):backup=server.checkpoint_before_upgrade(self.store.path);upgraded=type(self.store)(self.store.path)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],12)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        with upgraded._connection() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM answers WHERE id='legacy-answer'").fetchone()),original)
            self.assertEqual(json.loads(db.execute('SELECT bundle_json FROM exchange_snapshots WHERE id=?',(bundle['id'],)).fetchone()[0]),bundle)
        self.assertEqual(upgraded.snapshot()['events'],events)
        self.assertEqual(upgraded.read_v1('answers',self.project)['items'][0]['review_state'],'stale')

if __name__=='__main__':unittest.main()
