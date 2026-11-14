"""Independent project imports preserve logical identities and historical sources."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from tools.roadmap_store import RoadmapStore, StoreError

class TaskNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.stores=[RoadmapStore(Path(self.tmp.name)/f'{i}.sqlite3',bootstrap_slug=f'project-{i}') for i in range(3)]
        self.projects=[s.read_v1('projects')['items'][0]['id'] for s in self.stores]

    def write(self,index,project,*ops):
        store=self.stores[index]
        actor=store.read_v1('capabilities',project)['capabilities']['local_principal_id']
        return store.transact_v1({'project_id':project,'actor':{'principal_id':actor},'request_id':str(uuid.uuid4()),'expected_revision':store.snapshot()['revision'],'operations':list(ops)})

    def task(self,index,project,alias):
        return self.stores[index].read_v1('context',project,key=alias)['context']['task']

    def export(self,index,project,ancestor=None):
        return self.stores[index].exchange_preview(project,ancestor_id=ancestor,portable=True)['bundle']

    def connect(self,index,source):
        project=self.projects[source]
        self.write(index,self.projects[index],{'op':'create_project','id':project,'slug':f'project-{source}','goal':'Research workspace'})
        return project

    def import_bundle(self,index,project,bundle):
        preview=self.stores[index].exchange_preview(project,bundle=bundle)['preview']
        choices={json.dumps([change['table'],change['key'],field],separators=(',',':')):'local' for change in preview['changes'] for field,alt in change['fields'].items() if alt['conflict']}
        return self.write(index,project,{'op':'import_bundle','bundle':bundle,'choices':choices})

    def seed(self,index):
        p=self.projects[index]
        self.write(index,p,{'op':'create','alias':'SAME','record':{'title':f'Project {index}','status':'ready','acceptance':['Synthetic gate']}}, {'op':'create','alias':'CHILD','parent_id':'SAME','record':{'title':'Child','status':'ready','acceptance':['Synthetic gate']}}, {'op':'create','alias':'OTHER','record':{'title':'Other','status':'ready','acceptance':['Synthetic gate']}})
        self.write(index,p,{'op':'add_link','task_id':'CHILD','target_id':'OTHER','kind':'depends_on','rationale':'Scoped relation'})
        self.write(index,p,{'op':'journal','task_id':'SAME','kind':'observation','text':'Original journal; SAME is authored prose'})

    def test_repeated_labels_import_with_scoped_links_and_original_journal(self):
        self.seed(0);self.seed(1)
        own=self.task(0,self.projects[0],'SAME')
        p=self.connect(0,1);incoming=self.export(1,p)
        self.stores[0].exchange_preview(p,incoming)
        self.import_bundle(0,p,incoming)
        self.assertEqual(self.task(0,self.projects[0],'SAME'),own)
        for alias in ('SAME','CHILD','OTHER'):
            self.assertEqual(self.task(0,p,alias)['id'],self.task(1,p,alias)['id'])
        self.assertEqual(self.task(0,p,'CHILD')['parent_id'],self.task(1,p,'SAME')['id'])
        with self.stores[0]._connection() as db:
            child=db.execute('SELECT id FROM tasks WHERE project_id=? AND uid=?',(p,self.task(1,p,'CHILD')['id'])).fetchone()[0]
            target=db.execute('SELECT id FROM tasks WHERE project_id=? AND uid=?',(p,self.task(1,p,'OTHER')['id'])).fetchone()[0]
            self.assertEqual(db.execute('SELECT target_id FROM task_links WHERE source_id=?',(child,)).fetchone()[0],target)
            received=[dict(r) for r in db.execute('SELECT * FROM task_journal_entries WHERE project_id=?',(p,))]
        with self.stores[1]._connection() as db:
            self.assertEqual(received,[dict(r) for r in db.execute('SELECT * FROM task_journal_entries WHERE project_id=?',(p,))])
        self.import_bundle(0,p,incoming)
        self.assertEqual(self.task(0,self.projects[0],'SAME'),own)

    def merge(self,index,project,survivor='SAME',absorbed='OTHER'):
        preview=self.stores[index].merge_preview(project,survivor,absorbed)['preview']
        self.write(index,project,{'op':'merge_tasks','task_id':preview['survivor_id'],'absorbed_id':preview['absorbed_id'],'expected_record_revision':self.task(index,project,survivor)['record_revision'],'preview_guard':preview['guard_digest'],'choices':{key:'survivor' for key in preview['field_conflicts']},'metadata_choices':{key:'survivor' for key in preview['metadata_conflicts']},'source_references':['Synthetic namespace fixture'],'rationale':'Review duplicate tasks'})
        with self.stores[index]._connection() as db:
            return db.execute('SELECT id FROM merge_records WHERE project_id=? ORDER BY created_at DESC LIMIT 1',(project,)).fetchone()[0]

    def compensate(self,index,project,merge):
        preview=self.stores[index].merge_compensation_preview(project,merge)['preview']
        self.write(index,project,{'op':'compensate_merge','task_id':preview['survivor_id'],'expected_record_revision':self.task(index,project,preview['survivor_id'])['record_revision'],'merge_id':merge,'preview_guard':preview['guard_digest'],'choices':{action['key']:'original' for action in preview['actions']},'source_references':['Synthetic namespace fixture'],'rationale':'Restore independently reviewed task identities'})

    def test_later_edits_link_deletion_and_third_peer_use_stable_exchange_keys(self):
        self.seed(0);self.seed(1);self.seed(2)
        p=self.connect(0,1);self.connect(2,1)
        base=self.export(1,p)
        self.write(1,p,{'op':'register_exchange_snapshot','bundle':base})
        self.import_bundle(0,p,base)
        received=self.export(0,p)
        self.assertEqual(received['records']['task_links'],base['records']['task_links'])
        self.assertEqual([r['id'] for r in received['records']['tasks']],[r['id'] for r in base['records']['tasks']])
        self.import_bundle(2,p,received)
        self.write(1,p,{'op':'update','task_id':'CHILD','fields':{'summary':'A later independent edit'}},{'op':'remove_link','task_id':'CHILD','target_id':'OTHER','kind':'depends_on'})
        changed=self.export(1,p,base['id']);self.import_bundle(0,p,changed)
        self.assertEqual(self.task(0,p,'CHILD')['summary'],'A later independent edit')
        self.assertEqual(self.export(0,p)['records']['task_links'],[])
        self.write(0,p,{'op':'update','task_id':'SAME','fields':{'summary':'Edited in remapped installation'}})
        outbound=self.export(0,p,received['id']);self.import_bundle(2,p,outbound)
        self.assertEqual(self.task(2,p,'SAME')['summary'],'Edited in remapped installation')
        self.assertEqual(self.export(2,p)['records']['task_links'],[])
        with self.stores[0]._connection() as db:
            self.assertEqual(json.loads(db.execute('SELECT bundle_json FROM exchange_snapshots WHERE project_id=? AND id=?',(p,base['id'])).fetchone()[0]),base)

    def test_imported_merge_compensation_retains_raw_history(self):
        self.seed(0);self.seed(1)
        p=self.projects[1];merge=self.merge(1,p)
        original=self.export(1,p)
        self.connect(0,1);self.import_bundle(0,p,original)
        self.write(0,p,{'op':'update','task_id':'SAME','fields':{'summary':'Work after import'}})
        self.compensate(0,p,merge)
        self.assertIsNone(self.task(0,p,'OTHER')['archive_reason'])
        self.assertEqual(self.task(0,p,'SAME')['summary'],'Work after import')
        exported=self.export(0,p)
        self.assertEqual(exported['records']['merge_records'][0]['before_json'],original['records']['merge_records'][0]['before_json'])
        self.assertEqual(exported['records']['merge_records'][0]['after_json'],original['records']['merge_records'][0]['after_json'])
        self.assertEqual(len(exported['records']['merge_compensations']),1)
        self.assertEqual(self.task(0,self.projects[0],'SAME')['status'],'ready')

    def test_merge_authored_after_remap_recovers_on_third_peer(self):
        self.seed(0);self.seed(1);self.seed(2)
        p=self.connect(0,1);self.connect(2,1);self.import_bundle(0,p,self.export(1,p))
        merge=self.merge(0,p)
        bundled=self.export(0,p)
        before=json.loads(bundled['records']['merge_records'][0]['before_json'])
        self.assertEqual({row['id'] for row in before['tasks']},{'SAME','OTHER','CHILD'})
        self.import_bundle(2,p,bundled)
        self.compensate(2,p,merge)
        self.assertIsNone(self.task(2,p,'OTHER')['archive_reason'])
        self.assertEqual(self.task(2,self.projects[2],'SAME')['status'],'ready')

    def test_rebinding_uuid_or_exchange_key_is_rejected_without_writes(self):
        import copy
        from tools import roadmap_research as research
        self.seed(0);self.seed(1)
        p=self.connect(0,1);bundle=self.export(1,p);self.import_bundle(0,p,bundle)
        revision=self.stores[0].snapshot()['revision']
        for field,value in [('uid',self.task(0,self.projects[0],'SAME')['id']),('id','replacement-storage-key')]:
            bad=copy.deepcopy(bundle);bad['records']['tasks'][0][field]=value
            if field=='id':
                # Keep unrelated references valid so rejection exercises binding.
                old=bundle['records']['tasks'][0]['id']
                for row in bad['records']['tasks']:
                    if row['parent_id']==old:row['parent_id']=value
                for row in bad['records']['task_links']:
                    for key in ('source_id','target_id'):
                        if row[key]==old:row[key]=value
            bad['id']=research.digest({key:value for key,value in bad.items() if key!='id'})
            with self.assertRaises(StoreError):self.import_bundle(0,p,bad)
            self.assertEqual(self.stores[0].snapshot()['revision'],revision)

    def test_exact_reversal_after_local_merge_uses_local_recovery_view(self):
        self.seed(0);self.seed(1)
        p=self.connect(0,1);self.import_bundle(0,p,self.export(1,p));merge=self.merge(0,p)
        preview=self.stores[0].merge_reversal_preview(p,merge)['preview']
        self.assertTrue(preview['can_reverse'])
        self.write(0,p,{'op':'reverse_merge','task_id':preview['survivor_id'],'merge_id':merge,'expected_record_revision':self.task(0,p,'SAME')['record_revision'],'preview_guard':preview['guard_digest'],'rationale':'Restore exactly unchanged merge scope'})
        self.assertIsNone(self.task(0,p,'OTHER')['archive_reason'])
        self.assertEqual(self.export(0,p)['records']['task_links'][0]['target_id'],'OTHER')

    def test_failed_cyclic_import_rolls_back_new_bindings_and_all_content(self):
        import copy
        from tools import roadmap_research as research
        self.seed(0);self.seed(1);p=self.connect(0,1);bundle=copy.deepcopy(self.export(1,p))
        bundle['records']['task_links'].append({'source_id':'OTHER','target_id':'CHILD','kind':'depends_on','rationale':'Invalid cycle','review_state':'unreviewed'})
        bundle['id']=research.digest({key:value for key,value in bundle.items() if key!='id'})
        revision=self.stores[0].snapshot()['revision']
        with self.assertRaisesRegex(StoreError,'cycle'):self.import_bundle(0,p,bundle)
        self.assertEqual(self.stores[0].snapshot()['revision'],revision)
        with self.stores[0]._connection() as db:
            for table in ('tasks','task_exchange_keys','exchange_snapshots','exchange_imports'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE project_id=?',(p,)).fetchone()[0],0)

    def test_schema10_upgrade_backup_and_rollback_preserve_raw_history(self):
        import sqlite3
        import sys
        from contextlib import closing
        from unittest.mock import patch
        from tools import roadmap_schema
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server as server
        self.seed(0);p=self.projects[0];self.merge(0,p)
        bundle=self.export(0,p)
        self.write(0,p,{'op':'register_exchange_snapshot','bundle':bundle})
        events=self.stores[0].snapshot()['events']
        path=self.stores[0].path
        with self.stores[0]._connection() as db:
            raw=dict(db.execute('SELECT * FROM merge_records WHERE project_id=?',(p,)).fetchone())
            db.execute('DROP TABLE task_exchange_keys');db.execute('UPDATE store_meta SET schema_version=10')
        with patch.object(roadmap_schema,'_migration_checkpoint',side_effect=RuntimeError('injected namespace upgrade fault')):
            with self.assertRaisesRegex(RuntimeError,'injected namespace'):RoadmapStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],10)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='task_exchange_keys'").fetchone())
        with server.service_lock(path):
            backup=server.checkpoint_before_upgrade(path);upgraded=RoadmapStore(path)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],10)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(upgraded.snapshot()['events'],events)
        with upgraded._connection() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM merge_records WHERE project_id=?',(p,)).fetchone()),raw)
            self.assertEqual(json.loads(db.execute('SELECT bundle_json FROM exchange_snapshots WHERE project_id=? AND id=?',(p,bundle['id'])).fetchone()[0]),bundle)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_portable_nested_json_keys_accept_actions_and_reject_actual_paths(self):
        from tools import roadmap_exchange as exchange
        action=json.dumps(['link',json.dumps(['task-a','task-b','related']), 'row'])
        exchange._portable_path_check(json.dumps({'choices':{action:'current'}}),())
        for value in ('C:/private/secret.txt',r'\\server\share\secret.txt','/home/private/secret.txt'):
            for payload in ({'nested':value},{value:'inside a key'}):
                with self.assertRaisesRegex(StoreError,'host path'):
                    exchange._portable_path_check(json.dumps({'encoded':json.dumps(payload)}),())

if __name__=='__main__':unittest.main()
