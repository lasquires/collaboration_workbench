"""Foundation contracts use temporary databases only; never the checkout store."""
import json
import sqlite3
import tempfile
import unittest
import uuid
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tools.roadmap_store import RoadmapStore, StoreError, _SCHEMA
from tools import roadmap_schema
from tools.roadmap_projection import resolve_entity
from tools.roadmap_schema import principal_for_label


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'workspace.sqlite3'
        self.store = RoadmapStore(self.path)
        self.records = [{'id': 'OLD.7', 'title': 'Historical parent', 'status': 'ready',
                         'unknown': {'date': None}, 'claim': {'by': 'old worker'},
                         'reserved_paths': ['web/owned.js'],
                         'subtasks': [{'id': 'OLD-child', 'title': 'Historical done',
                                      'status': 'done', 'source_created_at': None}]}]
        self.store.import_v1(self.records, 'a' * 64)

    def project(self):
        return next(p['id'] for p in self.store.read_v1('projects')['items'] if p['slug']=='ssg2')

    def test_scoped_identity_progress_context_and_restart(self):
        project = self.project()
        rows = self.store.read_v1('tasks', project)['items']
        parent = next(row for row in rows if row['alias'] == 'OLD.7')
        uuid.UUID(parent['id'])
        context = self.store.read_v1('context', project, key='OLD.7')['context']
        self.assertEqual(context['task']['id'], parent['id'])
        self.assertEqual(context['progress']['required']['accepted_done'], 0)
        self.assertFalse(context['progress']['decomposition_complete'])
        self.assertEqual(context['progress']['claim']['state'], 'handoff_needed')
        self.assertEqual(context['reservations'], ['web/owned.js'])
        self.assertEqual(context['findings'], [])
        self.assertIsNone(context['validation'])
        self.assertNotIn('validation', context['omitted_sections'])
        before = self.store.snapshot()
        for _ in range(2):
            self.store = RoadmapStore(self.path)
            self.assertEqual(self.store.read_v1('tasks', project)['items'], rows)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.store.export_legacy_records(), self.records)

    def test_migration_preserves_legacy_and_bumps_once_with_rollback(self):
        legacy_path = Path(self.tmp.name) / 'v1.sqlite3'
        with closing(sqlite3.connect(legacy_path, isolation_level=None)) as db:
            db.executescript(_SCHEMA)
            db.execute('INSERT INTO store_meta VALUES(1,1,?,7,NULL,NULL,NULL)', ('old-store',))
            raw = json.dumps({'id':'HIST','title':'Old','status':'done','unknown':None})
            db.execute('INSERT INTO tasks VALUES(?,NULL,0,?,?,NULL,NULL)', ('HIST',raw,raw))
            db.execute("INSERT INTO transactions VALUES('historical','unchanged-hash','{\"unknown\":null}')")
            db.execute("INSERT INTO events(event_id,request_id,task_id,actor_json,recorded_at,historical_at,kind,details_json) VALUES('old-event','historical','HIST',NULL,'old-recorded',NULL,'legacy_attempt','{\"unknown\":null}')")
            old_events=list(db.execute('SELECT * FROM events'))
        with patch.object(roadmap_schema, '_migration_checkpoint', side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                RoadmapStore(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as db:
            self.assertEqual(db.execute('SELECT schema_version,revision FROM store_meta').fetchone(), (1,7))
            self.assertNotIn('uid', [r[1] for r in db.execute('PRAGMA table_info(tasks)')])
        migrated = RoadmapStore(legacy_path)
        self.assertEqual(migrated.snapshot()['revision'], 'old-store:8')
        self.assertEqual(migrated.snapshot()['store_id'], 'old-store')
        self.assertEqual(migrated.export_legacy_records(), [json.loads(raw)])
        with closing(sqlite3.connect(legacy_path)) as db:
            self.assertEqual(list(db.execute('SELECT * FROM events')),old_events)
            self.assertEqual(db.execute("SELECT * FROM transactions WHERE request_id='historical'").fetchone(),('historical','unchanged-hash','{"unknown":null}'))
        RoadmapStore(legacy_path)
        self.assertEqual(migrated.snapshot()['revision'], 'old-store:8')

    def test_v1_write_stales_legacy_and_retry_survives_restart(self):
        project = self.project()
        caps = self.store.read_v1('capabilities', project)['capabilities']
        old_revision = self.store.snapshot()['revision']
        payload = {'project_id':project, 'actor':{'principal_id':caps['local_principal_id']},
                   'request_id':'v1-edit', 'expected_revision':old_revision,
                   'operations':[{'op':'update','task_id':'OLD.7','fields':{'title':'Changed'}}]}
        result = self.store.transact_v1(payload)
        self.assertNotEqual(result['revision'], old_revision)
        self.assertEqual(result['changed_records'][0]['type'], 'task')
        with self.assertRaises(StoreError) as exc:
            self.store.transact(old_revision, 'stale', 'legacy', [{'op':'update','task_id':'OLD.7','fields':{'title':'Stale'}}])
        self.assertEqual(exc.exception.status, 409)
        self.assertEqual(RoadmapStore(self.path).transact_v1(payload), result)
        payload['operations'][0]['fields']['title'] = 'Different'
        with self.assertRaises(StoreError) as exc:
            self.store.transact_v1(payload)
        self.assertEqual(exc.exception.status, 409)
        events = self.store.read_v1('events', project)['items']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['actor_id'], caps['local_principal_id'])

    def test_namespace_isolation_and_scoped_alias_foreign_keys(self):
        project = self.project()
        foreign = str(uuid.uuid4())
        with self.store._connection() as db:
            db.execute('INSERT INTO projects(id,slug,goal) VALUES(?,?,?)', (foreign,'other','Other'))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO containment(project_id,child_id,parent_id,sibling_order) SELECT ?,uid,NULL,0 FROM tasks LIMIT 1", (foreign,))
            with self.assertRaises(StoreError) as exc:
                resolve_entity(db, foreign, 'task', 'OLD.7')
            self.assertEqual(exc.exception.status, 404)
        self.assertEqual(self.store.read_v1('tasks', foreign)['items'], [])
        self.assertFalse(self.store.read_v1('tasks', foreign)['coverage']['complete'])
        with self.assertRaises(StoreError) as exc:
            self.store.read_v1('context', foreign, key=self.store.read_v1('tasks', project)['items'][0]['id'])
        self.assertEqual(exc.exception.status, 404)

    def test_malformed_v1_identifiers_fail_without_mutation(self):
        project = self.project()
        principal = self.store.read_v1('capabilities', project)['capabilities']['local_principal_id']
        before = self.store.snapshot()
        base = {'project_id': project, 'actor': {'principal_id': principal},
                'request_id': 'invalid-input', 'expected_revision': before['revision'],
                'operations': [{'op': 'update', 'task_id': 'OLD.7', 'fields': {'title': 'Changed'}}]}
        variants = [
            {'request_id': []},
            {'actor': {'principal_id': principal, 'session_id': []}},
            {'operations': [{'op': 'update', 'task_id': [], 'fields': {'title': 'Changed'}}]},
            {'operations': [{'op': 'update', 'task_id': 'OLD.7', 'fields': {'dependencies': [[]]}}]},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(StoreError) as exc:
                    self.store.transact_v1({**base, **variant})
                self.assertEqual(exc.exception.status, 400)
                self.assertEqual(self.store.snapshot(), before)

    def test_cursor_bounds_snapshot_filter_and_neighborhood_frontier(self):
        project = self.project()
        first = self.store.read_v1('tasks', project, limit=1)
        self.assertIsNotNone(first['next_cursor'])
        second = self.store.read_v1('tasks', project, limit=1, cursor=first['next_cursor'])
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        for options in ({'limit':101}, {'limit':0}, {'cursor':'broken'}):
            with self.assertRaises(StoreError):
                self.store.read_v1('tasks', project, **options)
        hood = self.store.read_v1('neighborhood', project, key='OLD.7', depth=0, limit=1)
        self.assertEqual(len(hood['nodes']), 1)
        self.assertEqual(hood['frontier'], [{'id':hood['root_id'],'has_more_children':True}])
        self.store.transact(self.store.snapshot()['revision'], 'legacy-edit', 'old', [{'op':'update','task_id':'OLD.7','fields':{'title':'Edit'}}])
        with self.assertRaises(StoreError) as exc:
            self.store.read_v1('tasks', project, limit=1, cursor=first['next_cursor'])
        self.assertEqual(exc.exception.status, 409)
        self.assertTrue(exc.exception.restart_required)

    def test_context_does_not_inherit_child_specific_guidance(self):
        project = self.project()
        with self.store._connection() as db:
            root_area, child_area = str(uuid.uuid4()), str(uuid.uuid4())
            db.execute('INSERT INTO areas(id,project_id,title) VALUES(?,?,?)', (root_area,project,'Root'))
            db.execute('INSERT INTO areas(id,project_id,parent_id,title) VALUES(?,?,?,?)', (child_area,project,root_area,'Child'))
            db.execute('UPDATE tasks SET area_id=? WHERE id=?', (root_area,'OLD.7'))
            db.execute('UPDATE tasks SET area_id=? WHERE id=?', (child_area,'OLD-child'))
            for area, scope, text in ((root_area,'descendants','Broad'), (child_area,'self','Local inference')):
                db.execute('INSERT INTO guidance_links(id,project_id,area_id,inheritance_scope,body,document_version_id) VALUES(?,?,?,?,?,?)', (str(uuid.uuid4()),project,area,scope,text,'version-pinned'))
        parent = self.store.read_v1('context',project,key='OLD.7')['context']['instructions']
        child = self.store.read_v1('context',project,key='OLD-child')['context']['instructions']
        self.assertEqual([r['body'] for r in parent], ['Broad'])
        self.assertEqual([r['body'] for r in child], ['Broad','Local inference'])
        self.assertTrue(all(r['document_version_id']=='version-pinned' for r in child))

    def test_scoped_create_duplicate_alias_retry_and_history_pagination(self):
        foreign=str(uuid.uuid4())
        with self.store._connection() as db:
            db.execute('INSERT INTO projects(id,slug,goal) VALUES(?,?,?)',(foreign,'other','Other'))
            principal=principal_for_label(db,foreign,'other actor')
        payload={'project_id':foreign,'actor':{'principal_id':principal},'request_id':'scoped-create',
                 'expected_revision':self.store.snapshot()['revision'],
                 'operations':[{'op':'create','alias':'OLD.7','record':{'title':'Other parent','status':'ready'}}]}
        result=self.store.transact_v1(payload)
        self.assertEqual(RoadmapStore(self.path).transact_v1(payload),result)
        row=self.store.read_v1('tasks',foreign)['items'][0]
        self.assertEqual(row['alias'],'OLD.7')
        self.assertNotEqual(row['legacy_id'],'OLD.7')
        self.assertNotEqual(row['id'],self.store.read_v1('context',self.project(),key='OLD.7')['context']['task']['id'])
        with self.store._connection() as db:
            created=db.execute('SELECT created_by,created_at FROM tasks WHERE uid=?',(row['id'],)).fetchone()
            self.assertEqual(created['created_by'],principal)
            self.assertIsNotNone(created['created_at'])
        self.store.transact(self.store.snapshot()['revision'],'journals','writer',[
            {'op':'journal','task_id':'OLD.7','kind':'attempt','text':'Evidence '+str(i),'evidence':['docs/GOAL.md']} for i in range(55)])
        context=self.store.read_v1('context',self.project(),key='OLD.7')['context']
        self.assertEqual(len(context['history']),50)
        self.assertEqual(context['history'][0]['details']['text'],'Evidence 54')
        continuation=context['section_cursors']['history']
        older=self.store.read_v1('history',self.project(),key='OLD.7',cursor=continuation['cursor'],limit=continuation['limit'])
        self.assertEqual(len(older['items']),5)
        self.assertEqual(older['items'][-1]['details']['text'],'Evidence 0')

    def test_clock_expiry_is_read_only_and_cycles_are_rejected(self):
        project=self.project()
        with self.store._connection() as db:
            row=db.execute("SELECT record_json FROM tasks WHERE id='OLD.7'").fetchone()
            record=json.loads(row[0]);record['claim']['expires_at']='2026-11-12T12:00:00Z'
            db.execute("UPDATE tasks SET record_json=? WHERE id='OLD.7'",(json.dumps(record),))
            ids={r['id']:r['uid'] for r in db.execute('SELECT id,uid FROM tasks')}
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE containment SET parent_id=? WHERE child_id=?',(ids['OLD-child'],ids['OLD.7']))
        before=self.store.snapshot()
        live=self.store.read_v1('context',project,key='OLD.7',now='2026-11-12T11:00:00Z')['context']['progress']['claim']
        expired=self.store.read_v1('context',project,key='OLD.7',now='2026-11-12T13:00:00Z')['context']['progress']['claim']
        self.assertEqual(live['state'],'live')
        self.assertEqual(expired['state'],'expired')
        self.assertEqual(self.store.snapshot(),before)

    def test_new_claimant_on_existing_containment_gets_stable_identity(self):
        self.store.transact(self.store.snapshot()['revision'],'release-old','old worker',[
            {'op':'release','task_id':'OLD.7','claimant':'old worker'}])
        self.store.transact(self.store.snapshot()['revision'],'claim-via-ui','coordinator',[
            {'op':'claim','task_id':'OLD.7','claimant':'new worker'}])
        claim=self.store.read_v1('context',self.project(),key='OLD.7')['context']['progress']['claim']
        self.assertIsNotNone(claim['principal_id'])
        with self.store._connection() as db:
            label=db.execute('SELECT label FROM principals WHERE id=?',(claim['principal_id'],)).fetchone()[0]
        self.assertEqual(label,'new worker')

    def test_shared_fixture_matches_neighborhood_record_shape(self):
        fixture=json.loads((Path(__file__).parent/'fixtures'/'research_workspace_v1.json').read_text())
        response=self.store.read_v1('neighborhood',self.project(),key='OLD.7')
        self.assertEqual(set(response),set(fixture)-{'conflict_example'})
        self.assertEqual(set(response['nodes'][0]),set(fixture['nodes'][0]))
        self.assertEqual(set(response['nodes'][0]['progress']),set(fixture['nodes'][0]['progress']))
        self.assertEqual(set(response['edges'][0]),set(fixture['edges'][0]))


class FoundationHttpTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server
        self.api=roadmap_server
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'http.sqlite3'
        self.original=self.api.STORE_PATH
        self.api.STORE_PATH=self.path
        RoadmapStore(self.path).import_v1([{'id':'HTTP','title':'HTTP fixture','status':'ready'}],'b'*64)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),self.api.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        self.api.STORE_PATH=self.original
        self.tmp.cleanup()

    def request(self,path,payload=None):
        request=urllib.request.Request(self.base+path,data=json.dumps(payload).encode() if payload is not None else None,headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status,json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code,json.loads(error.read())

    def test_routes_scoped_errors_and_transaction_contract(self):
        status,projects=self.request('/v1/projects')
        self.assertEqual(status,200)
        project=projects['items'][0]['id']
        status,caps=self.request('/v1/projects/'+project+'/capabilities')
        self.assertEqual(status,200)
        self.assertFalse(caps['capabilities']['authenticated'])
        self.assertEqual(self.request('/v1/tasks')[0],400)
        status,limited=self.request('/v1/tasks?project_id='+project+'&limit=101')
        self.assertEqual(status,400);self.assertIn('limits',limited)
        status,context=self.request('/v1/tasks/HTTP/context?project_id='+project)
        self.assertEqual(status,200)
        payload={'project_id':project,'actor':{'principal_id':caps['capabilities']['local_principal_id']},'request_id':'http-edit','expected_revision':context['revision'],'operations':[{'op':'update','task_id':context['context']['task']['id'],'fields':{'title':'HTTP changed'}}]}
        status,result=self.request('/v1/transactions',payload)
        self.assertEqual(status,200)
        self.assertEqual(self.request('/v1/transactions',payload),(200,result))
        payload['request_id']='http-stale'
        status,error=self.request('/v1/transactions',payload)
        self.assertEqual(status,409)
        self.assertEqual(error['code'],'conflict')
        self.assertEqual(error['latest_revision'],result['revision'])
        status,events=self.request('/v1/events?project_id='+project)
        self.assertEqual(status,200)
        self.assertEqual(events['items'][0]['actor_id'],payload['actor']['principal_id'])
        status,empty=self.request('/v1/events?project_id='+project+'&after='+events['event_cursor'])
        self.assertEqual(status,200);self.assertEqual(empty['items'],[])
        status,hood=self.request('/v1/tasks/HTTP/neighborhood?project_id='+project+'&depth=0')
        self.assertEqual(status,200);self.assertEqual(hood['nodes'][0]['title'],'HTTP changed')
        status,legacy=self.request('/api/tasks')
        self.assertEqual(status,200);self.assertEqual(legacy['tasks'][0]['id'],'HTTP')

    def test_scoped_collaboration_reads_and_exchange_previews_are_read_only(self):
        _,projects=self.request('/v1/projects')
        project=projects['items'][0]['id']
        revision=projects['revision']
        for resource in ('attempts','validations','blockers','claims'):
            status,result=self.request('/v1/'+resource+'?project_id='+project+'&task_id=HTTP')
            self.assertEqual(status,200,result)
            self.assertEqual(result['items'],[])
        status,export=self.request('/v1/exchange-export?project_id='+project)
        self.assertEqual(status,200,export)
        self.assertEqual(export['bundle']['project_id'],project)
        status,preview=self.request('/v1/exchange-preview',{'project_id':project,'bundle':export['bundle']})
        self.assertEqual(status,200,preview)
        self.assertTrue(all(c['status']=='identical' for c in preview['preview']['changes']))
        _,after=self.request('/v1/projects')
        self.assertEqual(after['revision'],revision)


if __name__ == '__main__':
    unittest.main()
