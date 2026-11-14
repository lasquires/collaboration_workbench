import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools import roadmap_exchange as exchange, workspace_auth
from tools.roadmap_store import RoadmapStore, StoreError


class ExchangeApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.stores = [RoadmapStore(Path(self.tmp.name) / f'{i}.sqlite3') for i in range(2)]
        self.project = self.stores[0].read_v1('projects')['items'][0]['id']
        self.actors = []
        for store in self.stores:
            with store._connection() as db:
                db.execute('PRAGMA foreign_keys=OFF')
                old = db.execute('SELECT id FROM projects').fetchone()[0]
                db.execute('UPDATE projects SET id=? WHERE id=?', (self.project, old))
                db.execute('UPDATE project_members SET project_id=? WHERE project_id=?', (self.project, old))
                db.execute('UPDATE local_principals SET project_id=? WHERE project_id=?', (self.project, old))
                db.commit(); db.execute('PRAGMA foreign_keys=ON')
                self.actors.append(db.execute('SELECT principal_id FROM project_members').fetchone()[0])
                db.execute("INSERT INTO areas(id,project_id,title) VALUES('area',?,'Original')", (self.project,))

    def export(self, index, ancestor=None):
        with self.stores[index]._connection() as db:
            return exchange.export_bundle(db, self.project, ancestor)

    def transact(self, index, operation, request):
        store = self.stores[index]
        return store.transact_v1({'project_id': self.project, 'actor': {'principal_id': self.actors[index]},
                                  'request_id': request, 'expected_revision': store.snapshot()['revision'],
                                  'operations': [operation]})

    def test_malformed_collections_are_structured_rejections_and_rollback(self):
        good=self.export(0);before=self.stores[0].snapshot()
        variants=[]
        bad=copy.deepcopy(good);bad['records']=list(exchange.TABLES);variants.append(bad)
        bad=copy.deepcopy(good);bad['history']={'unexpected':'value'};variants.append(bad)
        bad=copy.deepcopy(good);bad['records']['areas']=[None];variants.append(bad)
        bad=copy.deepcopy(good);bad['version']=True;variants.append(bad)
        for index,bundle in enumerate(variants):
            bundle['id']=exchange.research.digest({k:v for k,v in bundle.items() if k!='id'})
            with self.subTest(index=index):
                with self.assertRaises(StoreError) as caught:
                    self.transact(0,{'op':'import_bundle','bundle':bundle},'malformed-'+str(index))
                self.assertEqual(caught.exception.status,400)
                self.assertEqual(self.stores[0].snapshot(),before)

    def test_import_uses_cas_replay_and_preserves_remote_text(self):
        base = self.export(0)
        for i in range(2):
            self.transact(i, {'op': 'register_exchange_snapshot', 'bundle': base}, f'base-{i}')
        with self.stores[1]._connection() as db:
            db.execute("UPDATE areas SET title='Remote' WHERE id='area'")
        incoming = self.export(1, base['id'])
        stale = self.stores[0].snapshot()['revision']
        payload = {'project_id': self.project, 'actor': {'principal_id': self.actors[0]},
                   'request_id': 'import', 'expected_revision': stale,
                   'operations': [{'op': 'import_bundle', 'bundle': incoming}]}
        result = self.stores[0].transact_v1(payload)
        self.assertEqual(result, self.stores[0].transact_v1(payload))
        self.assertEqual(result['changed_records'][0]['type'], 'exchange_import')
        with self.stores[0]._connection() as db:
            self.assertEqual(db.execute('SELECT title FROM areas').fetchone()[0], 'Remote')
            self.assertEqual(db.execute('SELECT count(*) FROM exchange_imports').fetchone()[0], 1)
        with self.assertRaisesRegex(StoreError, 'revision changed'):
            self.stores[0].transact_v1({**payload, 'request_id':'stale'})

    def test_remote_passed_gate_is_stale_until_local_review(self):
        with self.stores[1]._connection() as db:
            record = {'id':'T', 'title':'Remote task', 'status':'done', 'acceptance_gate_passed':True, 'evidence':['claimed']}
            db.execute('INSERT INTO tasks(id,sibling_order,record_json,original_json,uid,project_id,acceptance_state,gate_revision,validation_id) VALUES(?,0,?,?,?,? ,\'passed\',1,\'gate\')',
                       ('T', json.dumps(record), '{}', 'task-uid', self.project))
            db.execute('INSERT INTO validation_records VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('gate', self.project, 'task-uid', 1, 'passed', '{}', '{}', '[]', self.actors[1], 'today'))
        result = self.transact(0, {'op':'import_bundle', 'bundle':self.export(1)}, 'remote-gate')
        self.assertEqual(result['changed_records'][0]['type'], 'exchange_import')
        with self.stores[0]._connection() as db:
            row = db.execute("SELECT record_json,acceptance_state FROM tasks WHERE id='T'").fetchone()
            self.assertEqual(json.loads(row['record_json'])['status'], 'review')
            self.assertNotEqual(row['acceptance_state'], 'passed')
            self.assertIsNone(db.execute('SELECT 1 FROM project_members WHERE principal_id=?', (self.actors[1],)).fetchone())

    def test_conflict_requires_explicit_field_choice(self):
        base = self.export(0)
        for i in range(2): self.transact(i, {'op':'register_exchange_snapshot','bundle':base}, f'common-{i}')
        for i,title in enumerate(('Local','Remote')):
            with self.stores[i]._connection() as db: db.execute('UPDATE areas SET title=? WHERE id=?',(title,'area'))
        incoming = self.export(1, base['id'])
        with self.assertRaisesRegex(StoreError, 'Explicit local/remote'):
            self.transact(0, {'op':'import_bundle','bundle':incoming}, 'conflict')
        with self.stores[0]._connection() as db:
            self.assertEqual(db.execute('SELECT title FROM areas').fetchone()[0], 'Local')


    def test_exchange_cannot_share_a_transaction_with_other_operations(self):
        bundle = self.export(0)
        payload = {'project_id':self.project, 'actor':{'principal_id':self.actors[0]},
                   'request_id':'mixed', 'expected_revision':self.stores[0].snapshot()['revision'],
                   'operations':[{'op':'register_exchange_snapshot','bundle':bundle},
                                 {'op':'process_jobs','limit':1}]}
        with self.assertRaisesRegex(StoreError, 'one scoped operation'):
            self.stores[0].transact_v1(payload)
        with self.stores[0]._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM exchange_snapshots').fetchone()[0],0)

    def test_import_rejects_parent_cycle_in_final_state(self):
        with self.stores[1]._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('PRAGMA defer_foreign_keys=ON')
            for task, parent in (('A','B'), ('B','A')):
                record = {'id':task,'title':task,'status':'ready'}
                db.execute('INSERT INTO tasks(id,parent_id,sibling_order,record_json,original_json,uid,project_id) VALUES(?,?,0,?,?,?,?)',
                           (task,parent,json.dumps(record),'{}',task+'-uid',self.project))
            db.commit()
        bundle = self.export(1)
        with self.assertRaisesRegex(StoreError,'cycle'):
            self.transact(0,{'op':'import_bundle','bundle':bundle},'cycle')
        with self.stores[0]._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM exchange_imports').fetchone()[0],0)

    def test_bearer_is_rechecked_inside_write_transaction_even_on_replay(self):
        with self.stores[0]._connection() as db:
            credential = workspace_auth.issue_owner_token(db, self.actors[0])
            db.commit()
        payload = {'project_id':self.project, 'actor':{'principal_id':self.actors[0]},
                   'request_id':'credential-bound', 'expected_revision':self.stores[0].snapshot()['revision'],
                   'operations':[{'op':'register_exchange_snapshot','bundle':self.export(0)}]}
        result = self.stores[0].transact_v1(payload,bearer_token=credential['token'])
        self.assertEqual(result,self.stores[0].transact_v1(payload,bearer_token=credential['token']))
        with self.stores[0]._connection() as db:
            workspace_auth.revoke_token(db, credential['id'], self.actors[0])
            db.commit()
        with self.assertRaisesRegex(StoreError,'Invalid credential'):
            self.stores[0].transact_v1(payload,bearer_token=credential['token'])

    def test_bearer_actor_cannot_spoof_owner_or_other_project(self):
        with self.stores[0]._connection() as db:
            invite = workspace_auth.issue_invite(db,self.project,self.actors[0],'exchange member',['contribute'])
            member = workspace_auth.redeem_invite(db,invite['secret'])
            db.commit()
        payload = {'project_id':self.project, 'actor':{'principal_id':self.actors[0]},
                   'request_id':'spoof', 'expected_revision':self.stores[0].snapshot()['revision'],
                   'operations':[{'op':'register_exchange_snapshot','bundle':self.export(0)}]}
        with self.assertRaisesRegex(StoreError,'differs from credential'):
            self.stores[0].transact_v1(payload,bearer_token=member['token'])
        payload['actor']['principal_id'] = member['principal_id']
        payload['operations'] = [{'op':'register_root','path':'/tmp'}]
        with self.assertRaisesRegex(StoreError,'owner credential'):
            self.stores[0].transact_v1(payload,bearer_token=member['token'])


if __name__ == '__main__': unittest.main()
