"""Explicit compensation for merged task scope, using synthetic store fixtures."""
import tempfile
import unittest
import uuid
from pathlib import Path

from tools.roadmap_store import RoadmapStore, StoreError


class CompensationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RoadmapStore(Path(self.temp.name) / 'workspace.sqlite3')
        self.project = self.store.read_v1('projects')['items'][0]['id']
        self.actor = self.store.read_v1('capabilities', self.project)['capabilities']['local_principal_id']
        self.sequence = 0

    def write(self, *operations, request_id=None):
        self.sequence += 1
        return self.store.transact_v1({
            'project_id': self.project,
            'actor': {'principal_id': self.actor},
            'request_id': request_id or f'compensation-{self.sequence}',
            'expected_revision': self.store.snapshot()['revision'],
            'operations': list(operations),
        })

    def create(self, alias, *, title=None, parent=None):
        self.write({'op': 'create', 'alias': alias, 'parent_id': parent,
                    'record': {'title': title or alias, 'status': 'ready',
                               'acceptance': ['Synthetic compensation gate']}})
        return self.task(alias)

    def task(self, alias):
        return self.store.read_v1('context', self.project, key=alias)['context']['task']

    def update(self, alias, **fields):
        task = self.task(alias)
        return self.write({'op': 'update', 'task_id': task['id'], 'fields': fields})

    def merge(self, survivor='keep', absorbed='restore', *, absorbed_title=False):
        preview = self.store.merge_preview(self.project, survivor, absorbed)['preview']
        return self.write({
            'op': 'merge_tasks', 'task_id': preview['survivor_id'],
            'absorbed_id': preview['absorbed_id'],
            'expected_record_revision': self.task(survivor)['record_revision'],
            'preview_guard': preview['guard_digest'],
            'choices': {key: 'absorbed' if absorbed_title and key=='title' else 'survivor' for key in preview['field_conflicts']},
            'metadata_choices': {key: 'survivor' for key in preview['metadata_conflicts']},
            'source_references': ['Synthetic workflow fixture; no native claim'],
            'rationale': 'Exercise compensation transaction behavior',
        })

    def merge_id(self):
        with self.store._connection() as db:
            return db.execute('SELECT id FROM merge_records ORDER BY created_at DESC LIMIT 1').fetchone()[0]

    def preview(self, merge_id):
        return self.store.merge_compensation_preview(self.project, merge_id)['preview']

    def compensation(self, preview, *, choices=None, request_id=None):
        task = self.task('keep')
        return {
            'op': 'compensate_merge', 'task_id': preview['survivor_id'],
            'expected_record_revision': task['record_revision'],
            'merge_id': preview['merge_id'], 'preview_guard': preview['guard_digest'],
            'choices': choices if choices is not None else {
                action['key']: 'original' for action in preview['actions']},
            'rationale': 'Restore reviewed merge scope while retaining later work',
            'source_references': ['Synthetic workflow fixture; no native claim'],
        }

    def test_later_child_summary_survives_restored_containment(self):
        self.create('keep')
        self.create('restore')
        child = self.create('child', parent='restore')
        self.merge()
        self.update('child', summary='Child work added after merge')
        merge_id = self.merge_id()
        preview = self.preview(merge_id)
        self.write(self.compensation(preview))
        self.assertEqual(self.task('child')['id'], child['id'])
        self.assertEqual(self.task('child')['parent_id'], self.task('restore')['id'])
        self.assertEqual(self.task('child')['summary'], 'Child work added after merge')
        self.assertIsNone(self.task('restore')['archive_reason'])
        self.assertEqual(self.task('keep')['status'], 'review')

    def test_conflicting_later_title_can_explicitly_keep_current(self):
        self.create('keep', title='Original survivor')
        self.create('restore')
        self.merge(absorbed_title=True)
        self.update('keep', title='Later title')
        preview = self.preview(self.merge_id())
        title_action = next(action for action in preview['actions']
                            if action.get('task_id') == self.task('keep')['id']
                            and action.get('field') == 'title')
        self.assertTrue(title_action['conflict'])
        choices = {action['key']: 'original' for action in preview['actions']}
        choices[title_action['key']] = 'current'
        self.write(self.compensation(preview, choices=choices))
        self.assertEqual(self.task('keep')['title'], 'Later title')

    def test_stale_preview_and_missing_choices_reject_without_writes(self):
        self.create('keep')
        self.create('restore')
        self.merge()
        preview = self.preview(self.merge_id())
        operation = self.compensation(preview)
        before = self.store.snapshot()
        missing = dict(operation, choices={})
        with self.assertRaises(StoreError):
            self.write(missing)
        self.assertEqual(self.store.snapshot(), before)
        self.update('keep', summary='Concurrent later edit')
        stale_operation = dict(operation, expected_record_revision=self.task('keep')['record_revision'])
        before_stale = self.store.snapshot()
        with self.assertRaises(StoreError) as error:
            self.write(stale_operation)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.store.snapshot(), before_stale)

    def test_active_claim_on_mutated_scope_rejects_without_writes(self):
        self.create('keep')
        self.create('restore')
        self.create('child', parent='restore')
        self.merge()
        preview = self.preview(self.merge_id())
        child = self.task('child')
        self.write({'op': 'claim_task', 'task_id': child['id'],
                    'expected_record_revision': child['record_revision'],
                    'reserved_paths': ['synthetic/compensation-owned.py'],
                    'rationale': 'Synthetic claim fixture'})
        preview = self.preview(preview['merge_id'])
        before = self.store.snapshot()
        with self.assertRaises(StoreError) as error:
            self.write(self.compensation(preview))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.store.snapshot(), before)

    def test_exact_replay_is_a_single_audit_and_merge_can_follow_undo(self):
        self.create('keep')
        self.create('restore')
        self.merge()
        merge_id = self.merge_id()
        preview = self.preview(merge_id)
        operation = self.compensation(preview)
        request_id = str(uuid.uuid4())
        payload = {
            'project_id': self.project,
            'actor': {'principal_id': self.actor},
            'request_id': request_id,
            'expected_revision': self.store.snapshot()['revision'],
            'operations': [operation],
        }
        first = self.store.transact_v1(payload)
        before_replay = self.store.snapshot()
        replay = self.store.transact_v1(payload)
        self.assertEqual(replay, first)
        self.assertEqual(self.store.snapshot(), before_replay)
        with self.store._connection() as db:
            audit_count = db.execute('SELECT count(*) FROM merge_compensations').fetchone()[0]
        self.assertEqual(audit_count, 1)
        self.merge('keep', 'restore')
        self.assertEqual(self.store.read_v1('context', self.project, key='restore')['redirect']['to'],
                         self.task('keep')['id'])

    def test_incompatible_foreign_history_cannot_supply_compensation_values(self):
        import json
        self.create('keep');self.create('restore');self.merge()
        merge_id=self.merge_id()
        with self.store._connection() as db:
            before=json.loads(db.execute('SELECT before_json FROM merge_records WHERE id=?',(merge_id,)).fetchone()[0])
            before['tasks'][0]['project_id']=str(uuid.uuid4())
            db.execute('UPDATE merge_records SET before_json=? WHERE id=?',(json.dumps(before),merge_id))
        with self.assertRaises(StoreError):self.preview(merge_id)

    def test_invalid_restored_cycle_rolls_back_then_explicit_current_parent_can_resolve(self):
        self.create('parent');self.create('keep');self.create('restore',parent='parent');self.create('child',parent='restore');self.merge()
        with self.store._connection() as db:parents={row['uid']:row['structure_revision'] for row in db.execute('SELECT uid,structure_revision FROM tasks')}
        parent=self.task('parent')
        self.write({'op':'move_task','task_id':parent['id'],'parent_id':self.task('child')['id'],'expected_record_revision':parent['record_revision'],'expected_parents':parents,'rationale':'Later valid containment edit'})
        preview=self.preview(self.merge_id());before=self.store.snapshot()
        with self.assertRaises(StoreError):self.write(self.compensation(preview))
        self.assertEqual(self.store.snapshot(),before)
        with self.store._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM merge_compensations').fetchone()[0],0)
        choices={action['key']:'original' for action in preview['actions']}
        child_action=next(action for action in preview['actions'] if action['kind']=='task_column' and action['task_id']==self.task('child')['id'] and action['field']=='parent_id')
        choices[child_action['key']]='current'
        self.write(self.compensation(preview,choices=choices))
        self.assertEqual(self.task('child')['parent_id'],self.task('keep')['id'])
        self.assertEqual(self.task('restore')['parent_id'],self.task('parent')['id'])

    def test_record_mode_requires_full_scope_but_preserves_unrelated_edits(self):
        self.create('keep');self.create('restore');self.create('child',parent='restore');self.create('unrelated');self.merge()
        self.update('child',summary='Keep this later note')
        preview=self.preview(self.merge_id());operation=self.compensation(preview)
        guards=[{'type':kind,'id':identity,'revision':values[field]} for identity,values in preview['guards'].items() for kind,field in (('task','record_revision'),('task_structure','structure_revision'))]
        payload={'project_id':self.project,'actor':{'principal_id':self.actor},'request_id':str(uuid.uuid4()),'concurrency_mode':'records','expected_records':guards,'operations':[operation]}
        incomplete={**payload,'expected_records':[guard for guard in guards if not (guard['type']=='task' and guard['id']==preview['absorbed_id'])]}
        before=self.store.snapshot()
        with self.assertRaises(StoreError) as error:self.store.transact_v1(incomplete)
        self.assertEqual(error.exception.status,400);self.assertEqual(self.store.snapshot(),before)
        self.update('unrelated',summary='Concurrent independent work')
        self.store.transact_v1(payload)
        self.assertEqual(self.task('unrelated')['summary'],'Concurrent independent work')
        self.assertEqual(self.task('child')['summary'],'Keep this later note')

    def test_portable_audit_retains_native_values_without_local_ownership(self):
        import json
        from tools import roadmap_exchange as exchange
        self.write({'op':'create','alias':'keep','record':{'title':'Keep','status':'ready','native_details':{'claims':['Original hypothesis'],'session_id':'Native field meaning'}}},
                   {'op':'create','alias':'restore','record':{'title':'Restore','status':'ready','native_details':{'claims':['Other hypothesis']}}})
        self.create('untouched');self.merge()
        self.write({'op':'add_link','task_id':'keep','target_id':'untouched','kind':'related','rationale':'Later independent link'})
        untouched=self.task('untouched')
        self.write({'op':'claim_task','task_id':untouched['id'],'expected_record_revision':untouched['record_revision'],'reserved_paths':['synthetic/untouched.py'],'rationale':'Other active work remains local'})
        preview=self.preview(self.merge_id());self.write(self.compensation(preview))
        self.assertIsNone(self.store.read_v1('claims',self.project,task_id=untouched['id'])['items'][0]['released_at'])
        with self.store._connection() as db:
            audit=dict(db.execute('SELECT * FROM merge_compensations').fetchone())
            public=json.loads(audit['preview_json']);applied=json.loads(audit['applied_json'])
            self.assertEqual(public['private_context_sha256'],exchange.research.digest(preview))
            for row in applied['tasks']:
                actual=dict(db.execute('SELECT * FROM tasks WHERE uid=?',(row['uid'],)).fetchone())
                for key,value in row.items():
                    if key not in ('record_json','original_json'):self.assertEqual(value,actual[key])
                public_record=json.loads(row['record_json']);actual_record=json.loads(actual['record_json'])
                for key,value in public_record.items():self.assertEqual(value,actual_record[key])
        original=next(row for row in public['snapshots']['original']['tasks'] if row['uid']==preview['survivor_id'])
        self.assertEqual(json.loads(original['record_json'])['native_details'],{'claims':['Original hypothesis'],'session_id':'Native field meaning'})
        self.assertNotIn('claims',public['snapshots']['current'])
        guarded=next(row for row in public['snapshots']['current']['tasks'] if row['uid']==untouched['id'])
        self.assertNotIn('claim',json.loads(guarded['record_json']));self.assertNotIn('reserved_paths',json.loads(guarded['record_json']))
        bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        self.assertEqual(bundle['records']['merge_compensations'][0],audit)
        receiving=RoadmapStore(Path(self.temp.name)/'received.sqlite3',bootstrap_slug='workspace')
        admin=receiving.read_v1('projects')['items'][0]['id'];admin_caps=receiving.read_v1('capabilities',admin)
        receiving.transact_v1({'project_id':admin,'actor':{'principal_id':admin_caps['capabilities']['local_principal_id']},'request_id':'shared-project','expected_revision':admin_caps['revision'],'operations':[{'op':'create_project','id':self.project,'slug':'received'}]})
        caps=receiving.read_v1('capabilities',self.project)
        review=receiving.exchange_preview(self.project,bundle=bundle)['preview']
        choices={exchange.canonical([change['table'],change['key'],field]):'remote' for change in review['changes'] for field,value in change['fields'].items() if value['conflict']}
        receiving.transact_v1({'project_id':self.project,'actor':{'principal_id':caps['capabilities']['local_principal_id']},'request_id':'portable-compensation','expected_revision':receiving.snapshot()['revision'],'operations':[{'op':'import_bundle','bundle':bundle,'choices':choices}]})
        self.assertEqual(receiving.read_v1('claims',self.project)['items'],[])
        self.assertEqual(receiving.exchange_preview(self.project,portable=True)['bundle']['records']['merge_compensations'][0],audit)

    def test_schema9_checkpoint_preserves_original_merge_and_applied_snapshot(self):
        import json
        import sqlite3
        from contextlib import closing
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server as server
        from tools import roadmap_exchange as exchange
        self.create('keep');self.create('restore');self.merge()
        bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        bundle['schema'].pop('merge_compensations');bundle['records'].pop('merge_compensations')
        bundle['id']=exchange.research.digest({key:value for key,value in bundle.items() if key!='id'})
        self.write({'op':'import_bundle','bundle':bundle,'choices':{}})
        events=self.store.snapshot()['events']
        with self.store._connection() as db:
            original=dict(db.execute('SELECT * FROM merge_records').fetchone())
            db.execute('DROP TABLE merge_compensations');db.execute('UPDATE store_meta SET schema_version=9')
        with server.service_lock(self.store.path):
            checkpoint=server.checkpoint_before_upgrade(self.store.path);upgraded=RoadmapStore(self.store.path)
        with closing(sqlite3.connect(checkpoint)) as backup:
            self.assertEqual(backup.execute('SELECT schema_version FROM store_meta').fetchone()[0],9)
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        with upgraded._connection() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM merge_records').fetchone()),original)
            self.assertEqual(json.loads(db.execute('SELECT bundle_json FROM exchange_snapshots WHERE id=?',(bundle['id'],)).fetchone()[0]),bundle)
            self.assertEqual(db.execute('SELECT count(*) FROM merge_compensations').fetchone()[0],0)
        self.assertEqual(upgraded.snapshot()['events'],events)

    def test_two_independent_compensations_share_one_atomic_request(self):
        self.create('keep');self.create('restore');self.create('keep2');self.create('restore2')
        self.merge();self.merge('keep2','restore2')
        with self.store._connection() as db:
            first=db.execute('SELECT id FROM merge_records WHERE survivor_id=?',(self.task('keep')['id'],)).fetchone()[0]
            second=db.execute('SELECT id FROM merge_records WHERE survivor_id=?',(self.task('keep2')['id'],)).fetchone()[0]
        one=self.compensation(self.preview(first));two=self.compensation(self.preview(second))
        two['expected_record_revision']=self.task('keep2')['record_revision']
        request_id=str(uuid.uuid4());self.write(one,two,request_id=request_id)
        with self.store._connection() as db:
            rows=db.execute('SELECT id,request_id FROM merge_compensations').fetchall()
        self.assertEqual(len(rows),2);self.assertEqual({row['request_id'] for row in rows},{request_id})
        self.assertEqual(len({row['id'] for row in rows}),2)
        self.assertNotEqual(self.task('restore')['id'],self.task('keep')['id'])
        self.assertNotEqual(self.task('restore2')['id'],self.task('keep2')['id'])

    def test_audit_summaries_and_detail_are_scoped_and_snapshot_paginated(self):
        self.create('keep');self.create('restore');self.merge()
        preview=self.preview(self.merge_id());self.write(self.compensation(preview))
        summaries=self.store.read_v1('merge-compensations',self.project,task_id=self.task('restore')['id'],limit=1)
        self.assertEqual(len(summaries['items']),1)
        summary=summaries['items'][0];self.assertNotIn('preview_json',summary);self.assertNotIn('preview',summary)
        detail=self.store.read_v1('merge-compensations',self.project,compensation_id=summary['id'])['items'][0]
        self.assertEqual(detail['preview']['merge_id'],preview['merge_id'])
        self.assertEqual(detail['preview']['rationale'],'Restore reviewed merge scope while retaining later work')
        self.assertIn(self.task('restore')['id'],detail['preview']['affected_task_ids'])
        self.assertEqual(detail['choices'],{action['key']:'original' for action in preview['actions']})
        self.merge();self.write(self.compensation(self.preview(self.merge_id())))
        page=self.store.read_v1('merge-compensations',self.project,task_id=self.task('restore')['id'],limit=1)
        next_page=self.store.read_v1('merge-compensations',self.project,task_id=self.task('restore')['id'],limit=1,cursor=page['next_cursor'])
        self.assertNotEqual(page['items'][0]['id'],next_page['items'][0]['id'])
        self.write({'op':'journal','task_id':'keep','kind':'observation','text':'New progress after audit page'})
        with self.assertRaises(StoreError) as error:self.store.read_v1('merge-compensations',self.project,task_id=self.task('restore')['id'],limit=1,cursor=page['next_cursor'])
        self.assertEqual(error.exception.status,409)
        with self.assertRaises(StoreError):self.store.read_v1('merge-compensations',str(uuid.uuid4()))

    def test_malformed_portable_audit_is_rejected_before_persistence(self):
        from tools import roadmap_exchange as exchange
        self.create('keep');self.create('restore');self.merge();self.write(self.compensation(self.preview(self.merge_id())))
        bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        row=bundle['records']['merge_compensations'][0];row['id']=str(uuid.uuid4());row['preview_json']='{not json'
        bundle['id']=exchange.research.digest({key:value for key,value in bundle.items() if key!='id'})
        before=self.store.snapshot()
        with self.assertRaises(StoreError):self.write({'op':'import_bundle','bundle':bundle,'choices':{}})
        self.assertEqual(self.store.snapshot(),before)
        with self.store._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM merge_compensations').fetchone()[0],1)


if __name__ == '__main__':
    unittest.main()
