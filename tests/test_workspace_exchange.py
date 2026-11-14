import copy
import hashlib
import json
import tempfile
import subprocess
import unittest
from pathlib import Path

from tools import roadmap_exchange as exchange
from tools.roadmap_store import RoadmapStore, StoreError


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.stores=[RoadmapStore(Path(self.tmp.name)/f'{i}.sqlite3') for i in range(2)]
        self.project=self.stores[0].read_v1('projects')['items'][0]['id']
        self.actors=[]
        # Explicit common-project bootstrap, before any records or worker state.
        for store in self.stores:
            with store._connection() as db:
                db.execute('PRAGMA foreign_keys=OFF')
                old=db.execute('SELECT id FROM projects').fetchone()[0]
                db.execute('UPDATE projects SET id=? WHERE id=?',(self.project,old))
                db.execute('UPDATE project_members SET project_id=? WHERE project_id=?',(self.project,old))
                db.execute('UPDATE local_principals SET project_id=? WHERE project_id=?',(self.project,old))
                db.commit(); db.execute('PRAGMA foreign_keys=ON')
                exchange.create_schema(db)
                self.actors.append(db.execute('SELECT principal_id FROM project_members').fetchone()[0])
                db.execute("INSERT INTO areas(id,project_id,title) VALUES('area',?,'Original')",(self.project,))

    def export(self,index,ancestor=None):
        with self.stores[index]._connection() as db:
            return exchange.export_bundle(db,self.project,ancestor)

    def apply(self,index,bundle,choices=None,register=False):
        with self.stores[index]._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            request='request-'+bundle['id']+str(register)
            db.execute('INSERT OR IGNORE INTO transactions VALUES(?,?,?)',(request,'test','{}'))
            result=exchange.apply_operation(db,self.project,self.actors[index],{'op':'register_exchange_snapshot' if register else 'import_bundle','bundle':bundle,'choices':choices or {}},request)
            db.commit(); return result

    def test_determinism_readonly_and_schema_rejection(self):
        a=self.export(0); self.assertEqual(a,self.export(0))
        with self.stores[0]._connection() as db:
            count=db.total_changes
            exchange.preview_import(db,self.project,a)
            self.assertEqual(count,db.total_changes)
            bad=copy.deepcopy(a); bad['schema']['tasks'].pop(); bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
            with self.assertRaises(StoreError): exchange.preview_import(db,self.project,bad)

    def test_ancestor_conflicts_explicit_choices_history_and_retry(self):
        base=self.export(0)
        for i in range(2): self.apply(i,base,register=True)
        for i,title in enumerate(('Local','Remote')):
            with self.stores[i]._connection() as db: db.execute('UPDATE areas SET title=? WHERE id=?',(title,'area'))
        incoming=self.export(1,base['id'])
        with self.assertRaises(StoreError): self.apply(0,incoming)
        with self.stores[0]._connection() as db: preview=exchange.preview_import(db,self.project,incoming)
        change=next(c for c in preview['changes'] if c['table']=='areas')
        self.assertEqual(change['fields']['title']['base'],'Original')
        token=exchange.canonical(['areas',change['key'],'title'])
        result=self.apply(0,incoming,{token:'remote'})
        self.assertEqual(result,self.apply(0,incoming))
        with self.stores[0]._connection() as db:
            self.assertEqual(db.execute('SELECT title FROM areas').fetchone()[0],'Remote')
            self.assertEqual(db.execute('SELECT count(*) FROM exchange_imports').fetchone()[0],1)
            event=json.loads(db.execute("SELECT after_json FROM workspace_events WHERE kind='import_bundle'").fetchone()[0])
            self.assertEqual(next(c for c in event['alternatives']['changes'] if c['table']=='areas')['fields']['title']['local'],'Local')

    def test_missing_sources_and_hash_mismatch(self):
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO artifact_roots VALUES('root',?,'/untrusted',NULL,'today')",(self.project,))
            db.execute("INSERT INTO artifacts VALUES('artifact',?,'root','missing','binary',NULL,'today',1)",(self.project,))
            digest=hashlib.sha256(b'original').hexdigest()
            db.execute("INSERT INTO artifact_versions VALUES('version',?,'artifact',?,8,NULL,NULL,'missing',NULL,NULL,'today')",(self.project,digest))
        bundle=self.export(0)
        with self.stores[1]._connection() as db:
            self.assertEqual(exchange.preview_import(db,self.project,bundle)['missing_sources'],[digest])
            bad=copy.deepcopy(bundle)
            bad['records']['artifact_blobs']=[{'project_id':self.project,'sha256':digest,'body':{'base64':'bm90IG9yaWdpbmFs'}}]
            bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
            with self.assertRaises(StoreError): exchange.preview_import(db,self.project,bad)

    def test_claims_remain_local_and_task_fields_merge(self):
        for index in range(2):
            with self.stores[index]._connection() as db:
                record={'id':'T','title':'Original','summary':'Original','claim':{'by':'worker'},'reserved_paths':['private/']}
                db.execute('INSERT INTO tasks(id,sibling_order,record_json,original_json,uid,project_id) VALUES(?,0,?,?,?,?)',('T',json.dumps(record),'{}','stable-task',self.project))
        base=self.export(0)
        self.assertNotIn('claim',json.loads(base['records']['tasks'][0]['record_json']))
        for index in range(2): self.apply(index,base,register=True)
        with self.stores[0]._connection() as db:
            row=json.loads(db.execute('SELECT record_json FROM tasks').fetchone()[0]); row['title']='Local title'; row['custom']={'local':True}; row['nullable']=None
            db.execute('UPDATE tasks SET record_json=?',(json.dumps(row),))
        with self.stores[1]._connection() as db:
            row=json.loads(db.execute('SELECT record_json FROM tasks').fetchone()[0]); row['summary']='Remote summary'
            db.execute('UPDATE tasks SET record_json=?',(json.dumps(row),))
        self.apply(0,self.export(1,base['id']))
        with self.stores[0]._connection() as db:
            merged=json.loads(db.execute('SELECT record_json FROM tasks').fetchone()[0])
            self.assertEqual(merged['title'],'Local title'); self.assertEqual(merged['summary'],'Remote summary')
            self.assertEqual(merged['custom'],{'local':True}); self.assertIn('nullable',merged); self.assertIsNone(merged['nullable'])
            self.assertEqual(merged['claim'],{'by':'worker'})

    def test_schema4_accepted_validation_sources_and_redirects_roundtrip(self):
        body=b'native source'; digest=hashlib.sha256(body).hexdigest()
        pinned={'artifact_version_id':'native-v1','sha256':digest,'scope':'bounded original behavior'}
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO principals(id,label,identity_kind) VALUES('reviewer','Independent reviewer','trusted_local_label')")
            for task in ('survivor','absorbed'):
                db.execute('INSERT INTO tasks(id,sibling_order,record_json,original_json,uid,project_id) VALUES(?,0,?,?,?,?)',(task,json.dumps({'id':task,'status':'done'}),'{}',task+'-uid',self.project))
            db.execute("INSERT INTO artifact_roots VALUES('root',?,'/untrusted',NULL,'today')",(self.project,))
            db.execute("INSERT INTO artifacts VALUES('artifact',?,'root','native.bin','binary',NULL,'today',1)",(self.project,))
            db.execute('INSERT INTO artifact_blobs VALUES(?,?,?)',(self.project,digest,body))
            db.execute("INSERT INTO artifact_versions VALUES('native-v1',?,'artifact',?,?,NULL,'build-original','available',NULL,NULL,'today')",(self.project,digest,len(body)))
            db.execute("INSERT INTO validation_records VALUES('gate',?,'survivor-uid',1,'passed',?,'[]',?,'reviewer','today')",(self.project,json.dumps(pinned),json.dumps([pinned])))
            db.execute("UPDATE tasks SET acceptance_state='passed',validation_id='gate' WHERE id='survivor'")
            db.execute("INSERT INTO attempts VALUES('attempt',?,'survivor-uid','bounded','passed','resolved',?,NULL,'today','today','tested')",(self.project,json.dumps([pinned])))
            db.execute("INSERT INTO task_blockers VALUES('blocker',?,'absorbed-uid','review',NULL,'Historical blocker','today',NULL,'today')",(self.project,))
            db.execute("INSERT INTO merge_records VALUES('merge',?,'survivor-uid','absorbed-uid','{}','{}','{}',NULL,NULL,'today')",(self.project,))
            db.execute("INSERT INTO task_redirects VALUES(?,'absorbed-uid','survivor-uid','merge',1)",(self.project,))
            db.execute("INSERT INTO entity_aliases VALUES(?,'task','original-alias','absorbed-uid')",(self.project,))
        bundle=self.export(0)
        self.assertIn('reviewer',{r['id'] for r in bundle['records']['principals']})
        self.assertEqual(self.apply(1,bundle)['type'],'exchange_import')
        with self.stores[1]._connection() as db:
            self.assertEqual(dict(db.execute("SELECT uid,project_id,acceptance_state,validation_id FROM tasks WHERE id='survivor'").fetchone()),{'uid':'survivor-uid','project_id':self.project,'acceptance_state':'passed','validation_id':'gate'})
            self.assertEqual(json.loads(db.execute('SELECT inputs_json FROM validation_records').fetchone()[0]),pinned)
            self.assertEqual(db.execute('SELECT body FROM artifact_blobs').fetchone()[0],body)
            self.assertEqual(db.execute('SELECT active FROM task_redirects').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT entity_id FROM entity_aliases').fetchone()[0],'absorbed-uid')
            self.assertIsNone(db.execute("SELECT 1 FROM project_members WHERE principal_id='reviewer'").fetchone())
            for table in ('attempts','task_blockers','merge_records'): self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0],1)
        exported=self.export(1)
        self.assertIn('reviewer',{r['id'] for r in exported['records']['principals']})
        bad=copy.deepcopy(bundle); bad['records']['validation_records']=[]
        bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
        with self.stores[1]._connection() as db:
            with self.assertRaisesRegex(StoreError,'matching validation'): exchange.preview_import(db,self.project,bad)
        bad=copy.deepcopy(bundle); bad['records']['tasks'][1]['uid']='changed-uid'
        bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
        with self.stores[1]._connection() as db:
            with self.assertRaisesRegex(StoreError,'identity collision'): exchange.preview_import(db,self.project,bad)

    def test_script_namespace_import_and_snapshot_result_type(self):
        command=[str(Path(__import__('sys').executable)),'-c',"import sys; sys.path.insert(0,'tools'); import roadmap_exchange; print(roadmap_exchange.OPS)"]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.apply(0,self.export(0),register=True)['type'],'exchange_snapshot')

    def test_git_clones_transport_conflicting_text(self):
        root=Path(self.tmp.name); origin=root/'transport'; origin.mkdir()
        def git(cwd,*args):
            result=subprocess.run(['git','-c','user.name=Exchange fixture','-c','user.email=fixture@example.invalid',*args],cwd=cwd,capture_output=True,text=True)
            if result.returncode: self.fail(result.stderr)
        git(origin,'init')
        base=self.export(0)
        (origin/'exchange.json').write_text(exchange.canonical(base),encoding='utf-8')
        git(origin,'add','exchange.json'); git(origin,'commit','-m','common snapshot')
        for name in ('left','right'): git(root,'clone',str(origin),name)
        for index,title in enumerate(('Left edit','Right edit')):
            self.apply(index,base,register=True)
            with self.stores[index]._connection() as db: db.execute('UPDATE areas SET title=?',(title,))
            target=root/('left' if index==0 else 'right')
            (target/'exchange.json').write_text(exchange.canonical(self.export(index,base['id'])),encoding='utf-8')
            git(target,'add','exchange.json'); git(target,'commit','-m',title)
        incoming=json.loads((root/'right'/'exchange.json').read_text(encoding='utf-8'))
        with self.stores[0]._connection() as db:
            preview=exchange.preview_import(db,self.project,incoming)
        self.assertEqual(next(c for c in preview['changes'] if c['table']=='areas')['status'],'conflict')


if __name__=='__main__': unittest.main()
