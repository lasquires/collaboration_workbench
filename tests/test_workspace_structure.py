"""Task collaboration gates use temporary stores and synthetic workflow evidence."""
import json
from datetime import datetime, timedelta, timezone

from tests.test_research_ingestion import ResearchTests
from tools.roadmap_store import StoreError


class StructureTests(ResearchTests):
    def task(self, alias):
        return self.store.read_v1('context',self.project,key=alias)['context']['task']

    def create(self, alias, parent=None):
        self.write({'op':'create','alias':alias,'parent_id':parent,
                    'record':{'title':alias,'status':'ready','acceptance':['Synthetic gate for '+alias]}})
        return self.task(alias)

    def guarded(self,kind,alias,**fields):
        task=self.task(alias)
        return {'op':kind,'task_id':task['id'],'expected_record_revision':task['record_revision'],
                'rationale':'Synthetic structure acceptance',**fields}

    def evidence(self):
        return [{'artifact_version_id':self.source(b'Synthetic test result, not native evidence.'),
                 'locator':{'line':1},'method':'synthetic workflow fixture','limitations':'Unit test only.'}]

    def test_move_detach_and_cycle_guards_preserve_stable_ids(self):
        root=self.create('root'); child=self.create('child','root'); other=self.create('other')
        self.write(self.guarded('move_task','child',parent_id=other['id'],
                    expected_parents={root['id']:self.task('root')['structure_revision'],other['id']:other['structure_revision']},
                    required_for_parent=False))
        self.assertEqual(self.task('child')['parent_id'],other['id'])
        self.assertEqual(self.task('child')['id'],child['id'])
        progress=self.store.read_v1('context',self.project,key='other')['context']['progress']
        self.assertEqual(progress['optional']['total'],1)
        with self.assertRaises(StoreError):
            self.write(self.guarded('move_task','other',parent_id=child['id'],expected_parents={other['id']:self.task('other')['structure_revision'],child['id']:self.task('child')['structure_revision']}))
        current=self.task('other')
        self.write(self.guarded('detach_child','child',expected_parents={other['id']:current['structure_revision']}))
        self.assertIsNone(self.task('child')['parent_id'])

    def test_expired_lease_retains_reservation_until_explicit_release(self):
        self.create('a'); self.create('b')
        now=datetime(2026,11,12,12,tzinfo=timezone.utc)
        from unittest.mock import patch
        with patch('tools.roadmap_structure.now',return_value=now):
            self.write(self.guarded('claim_task','a',reserved_paths=[{'path':'Web/owned','kind':'tree'}],lease_seconds=60))
        context=self.store.read_v1('context',self.project,key='a',now=(now+timedelta(seconds=61)).isoformat())['context']
        self.assertEqual(context['progress']['claim']['state'],'expired')
        with self.assertRaises(StoreError) as exc:
            self.write(self.guarded('claim_task','b',reserved_paths=[{'path':'web/owned/child.js','kind':'file'}]))
        self.assertEqual(exc.exception.status,409)
        self.write(self.guarded('release_claim','a'))
        self.write(self.guarded('claim_task','b',reserved_paths=[{'path':'web/owned/child.js','kind':'file'}]))

    def test_parent_gate_is_independent_and_acceptance_changes_invalidate(self):
        self.create('parent'); self.create('child','parent')
        evidence=self.evidence()
        with self.assertRaises(StoreError):
            self.write(self.guarded('record_validation','child',outcome='passed',inputs={},outputs={},evidence=evidence))
        from tools.roadmap_schema import principal_for_label
        with self.store._connection() as db:
            self.principal=principal_for_label(db,self.project,'independent reviewer')
            db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',
                       (json.dumps(['contribute','review']),self.project,self.principal))
        with self.assertRaises(StoreError):
            self.write(self.guarded('complete_task','parent'))
        self.write(self.guarded('record_validation','child',outcome='passed',inputs={'fixture':'synthetic'},
                    outputs={'command':'synthetic unit fixture','exit_code':0},evidence=evidence))
        self.write(self.guarded('complete_task','child'))
        with self.assertRaises(StoreError):
            self.write(self.guarded('complete_task','parent'))
        self.write(self.guarded('record_validation','parent',outcome='passed',inputs={'fixture':'synthetic'},outputs={'exit_code':0},evidence=evidence))
        self.write(self.guarded('set_task_scope','parent',decomposition_complete=True))
        # Declaring scope invalidates the earlier gate, requiring a current review.
        with self.assertRaises(StoreError):
            self.write(self.guarded('complete_task','parent'))
        self.write(self.guarded('record_validation','parent',outcome='passed',inputs={'fixture':'synthetic'},outputs={'exit_code':0},evidence=evidence))
        self.write(self.guarded('complete_task','parent'))
        with self.assertRaises(StoreError):
            self.write({'op':'create','alias':'late','parent_id':'parent','record':{'title':'late','status':'ready'}})
        self.write(self.guarded('reopen_task','parent'))
        self.write({'op':'update','task_id':'parent','fields':{'acceptance':['Changed gate']}})
        context=self.store.read_v1('context',self.project,key='parent')['context']
        self.assertEqual(context['acceptance_state'],'invalidated')

    def test_attempt_rollups_count_each_failed_descendant_once(self):
        self.create('parent'); self.create('middle','parent'); self.create('leaf','middle')
        for index in range(2):
            self.write(self.guarded('record_attempt','leaf',boundary_id='same-boundary',outcome='failed',
                        evidence=[{'note':'Synthetic failed attempt '+str(index)}]))
        context=self.store.read_v1('context',self.project,key='parent')['context']
        self.assertEqual(context['progress']['failed_descendant_tasks'],1)
        self.assertFalse(context['progress']['unresolved_failure'])
        leaf=self.store.read_v1('context',self.project,key='leaf')['context']
        self.assertTrue(leaf['progress']['unresolved_failure'])
        self.assertTrue(leaf['progress']['routing_review_required'])

    def test_stale_move_guard_and_archive_claim_fail_closed(self):
        self.create('a'); self.create('b'); self.create('child','a')
        old=self.guarded('move_task','child',parent_id=self.task('b')['id'],expected_parents={self.task('a')['id']:self.task('a')['structure_revision'],self.task('b')['id']:self.task('b')['structure_revision']})
        self.write({'op':'update','task_id':'child','fields':{'summary':'Concurrent change'}})
        with self.assertRaises(StoreError) as exc:self.write(old)
        self.assertEqual(exc.exception.status,409)
        self.write(self.guarded('claim_task','child',reserved_paths=[]))
        with self.assertRaises(StoreError):self.write(self.guarded('archive_task','child'))
        self.write(self.guarded('release_claim','child'))
        before=self.task('a')
        self.write(self.guarded('archive_task','child',expected_parents={before['id']:before['structure_revision']}))
        self.assertIsNotNone(self.task('child')['archive_reason'])

    def test_context_attempt_continuation_has_no_skips_or_duplicates(self):
        self.create('investigation')
        for index in range(52):
            self.write(self.guarded('record_attempt','investigation',boundary_id='case-'+str(index),
                outcome='passed',evidence=[{'note':'Synthetic attempt '+str(index)}]))
        context=self.store.read_v1('context',self.project,key='investigation')['context']
        first=context['structured_attempts']
        self.assertEqual(len(first),50)
        continuation=context['section_cursors']['structured_attempts']
        self.assertTrue(continuation.get('cursor'))
        older=self.store.read_v1('attempts',self.project,task_id=self.task('investigation')['id'],
            limit=continuation['limit'],cursor=continuation['cursor'],order='desc')['items']
        self.assertEqual(len(older),2)
        self.assertEqual(len({item['id'] for item in first+older}),52)

    def test_bootstrap_capabilities_preserve_explicit_guidance_grants(self):
        from tools.roadmap_structure import bootstrap_local_capabilities
        from tools.roadmap_schema import principal_for_label
        with self.store._connection() as db:
            worker=principal_for_label(db,self.project,'ordinary worker')
            db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',
                (json.dumps(['contribute','approve_guidance']),self.project,self.principal))
            bootstrap_local_capabilities(db)
            local_caps=json.loads(db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(self.project,self.principal)).fetchone()[0])
            worker_caps=json.loads(db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(self.project,worker)).fetchone()[0])
        self.assertIn('approve_guidance',local_caps)
        self.assertEqual(worker_caps,['contribute'])
