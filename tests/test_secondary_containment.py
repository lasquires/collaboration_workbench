import tempfile
import unittest
import uuid
import sqlite3
import sys
import json
from contextlib import closing
from pathlib import Path

from tools.roadmap_store import RoadmapStore, StoreError


class SecondaryContainmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=RoadmapStore(Path(self.tmp.name)/'workspace.sqlite3',bootstrap_slug='containment')
        self.project=self.store.read_v1('projects')['items'][0]['id']

    def write(self,*operations):
        actor=self.store.read_v1('capabilities',self.project)['capabilities']['local_principal_id']
        return self.store.transact_v1({'project_id':self.project,'actor':{'principal_id':actor},
            'request_id':str(uuid.uuid4()),'expected_revision':self.store.snapshot()['revision'],
            'operations':list(operations)})

    @staticmethod
    def write_to(store,project,*operations):
        actor=store.read_v1('capabilities',project)['capabilities']['local_principal_id']
        return store.transact_v1({'project_id':project,'actor':{'principal_id':actor},
            'request_id':str(uuid.uuid4()),'expected_revision':store.snapshot()['revision'],
            'operations':list(operations)})

    def import_bundle(self,store,project,bundle):
        preview=store.exchange_preview(project,bundle=bundle)['preview']
        choices={json.dumps([change['table'],change['key'],field],separators=(',',':')):'remote'
                 for change in preview['changes'] for field,alt in change['fields'].items() if alt['conflict']}
        return self.write_to(store,project,{'op':'import_bundle','bundle':bundle,'choices':choices})

    def create(self):
        self.write(
            {'op':'create','alias':'primary','record':{'title':'Primary parent','status':'ready'}},
            {'op':'create','alias':'secondary','record':{'title':'Secondary parent','status':'ready'}},
            {'op':'create','alias':'child','parent_id':'primary','record':{'title':'Shared child','status':'ready'}})
        return {alias:self.store.read_v1('context',self.project,key=alias)['context']['task']
                for alias in ('primary','secondary','child')}

    def test_transaction_preserves_primary_parent_and_projects_both_paths(self):
        tasks=self.create()
        self.write({'op':'add_containment','task_id':tasks['child']['id'],'parent_id':tasks['secondary']['id'],'required_for_parent':False})
        child=self.store.read_v1('context',self.project,key=tasks['child']['id'])['context']['task']
        self.assertEqual(child['parent_id'],tasks['primary']['id'])
        self.assertEqual(child['contains'],[
            {'parent_id':tasks['primary']['id'],'required_for_parent':True},
            {'parent_id':tasks['secondary']['id'],'required_for_parent':False}])
        parent_context=self.store.read_v1('context',self.project,key=tasks['secondary']['id'])['context']
        self.assertEqual(parent_context['progress']['optional']['total'],1)
        self.assertGreater(parent_context['gate_revision'],1)
        for parent in ('primary','secondary'):
            graph=self.store.read_v1('neighborhood',self.project,key=tasks[parent]['id'],depth=2)
            self.assertEqual(sum(node['id']==tasks['child']['id'] for node in graph['nodes']),1)
            self.assertIn({'id':f"contains:{tasks[parent]['id']}:{tasks['child']['id']}",'kind':'contains',
                'source_id':tasks[parent]['id'],'target_id':tasks['child']['id'],
                'required_for_parent':parent=='primary'},graph['edges'])
        before=self.store.snapshot()['revision']
        with self.assertRaisesRegex(StoreError,'Secondary containment is optional'):
            self.write({'op':'add_containment','task_id':tasks['child']['id'],'parent_id':tasks['secondary']['id'],'required_for_parent':True})
        self.assertEqual(self.store.snapshot()['revision'],before)

    def test_cycle_across_primary_and_secondary_edges_is_rejected(self):
        tasks=self.create()
        self.write({'op':'set_parent','task_id':tasks['primary']['id'],'parent_id':tasks['secondary']['id']})
        before=self.store.snapshot()['revision']
        with self.assertRaisesRegex(StoreError,'Cyclic containment graph'):
            self.write({'op':'add_containment','task_id':tasks['secondary']['id'],'parent_id':tasks['child']['id']})
        self.assertEqual(self.store.snapshot()['revision'],before)

    def test_secondary_links_are_project_scoped(self):
        tasks=self.create()
        other=RoadmapStore(Path(self.tmp.name)/'other.sqlite3',bootstrap_slug='other')
        other_project=other.read_v1('projects')['items'][0]['id']
        actor=other.read_v1('capabilities',other_project)['capabilities']['local_principal_id']
        with self.assertRaises(StoreError):
            other.transact_v1({'project_id':other_project,'actor':{'principal_id':actor},'request_id':str(uuid.uuid4()),
                'expected_revision':other.snapshot()['revision'],'operations':[{'op':'add_containment','task_id':tasks['child']['id'],'parent_id':tasks['secondary']['id']}]})

    def test_schema_17_startup_checkpoint_and_migration(self):
        tasks=self.create()
        path=self.store.path
        with closing(sqlite3.connect(path)) as db:
            db.execute('BEGIN')
            db.execute('DROP TABLE secondary_containment')
            db.execute('UPDATE store_meta SET schema_version=17')
            db.commit()
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server
        try: checkpoint=roadmap_server.checkpoint_before_upgrade(path)
        finally: sys.path.pop(0)
        self.assertTrue(checkpoint.is_file())
        upgraded=RoadmapStore(path)
        with upgraded._connection() as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],18)
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='secondary_containment'").fetchone())
        self.assertEqual(upgraded.read_v1('context',self.project,key='child')['context']['task']['parent_id'],tasks['primary']['id'])

    def test_exchange_roundtrip_and_explicit_secondary_link_removal(self):
        tasks=self.create()
        base=self.store.exchange_preview(self.project,portable=True)['bundle']
        self.write({'op':'register_exchange_snapshot','bundle':base})
        destination=RoadmapStore(Path(self.tmp.name)/'destination.sqlite3',bootstrap_slug='destination')
        destination_home=destination.read_v1('projects')['items'][0]['id']
        self.write_to(destination,destination_home,{'op':'create_project','id':self.project,'slug':'shared-containment','goal':'Containment exchange fixture'})
        self.import_bundle(destination,self.project,base)
        self.write({'op':'add_containment','task_id':tasks['child']['id'],'parent_id':tasks['secondary']['id'],'required_for_parent':False})
        linked=self.store.exchange_preview(self.project,ancestor_id=base['id'],portable=True)['bundle']
        self.import_bundle(destination,self.project,linked)
        imported=destination.read_v1('context',self.project,key='child')['context']['task']
        self.assertEqual(imported['contains'],[
            {'parent_id':destination.read_v1('context',self.project,key='primary')['context']['task']['id'],'required_for_parent':True},
            {'parent_id':destination.read_v1('context',self.project,key='secondary')['context']['task']['id'],'required_for_parent':False}])
        self.write({'op':'remove_containment','task_id':tasks['child']['id'],'parent_id':tasks['secondary']['id']})
        removed=self.store.exchange_preview(self.project,ancestor_id=linked['id'],portable=True)['bundle']
        self.import_bundle(destination,self.project,removed)
        imported=destination.read_v1('context',self.project,key='child')['context']['task']
        self.assertEqual(imported['contains'],[{'parent_id':destination.read_v1('context',self.project,key='primary')['context']['task']['id'],'required_for_parent':True}])


if __name__=='__main__': unittest.main()
