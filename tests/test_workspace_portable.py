import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from tools import roadmap_exchange as exchange
from tools.roadmap_store import RoadmapStore, StoreError


class PortableExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.stores = [RoadmapStore(Path(self.tmp.name) / f'{i}.db') for i in range(2)]
        self.project = self.stores[0].read_v1('projects')['items'][0]['id']
        self.actors = []
        for store in self.stores:
            with store._connection() as db:
                db.execute('PRAGMA foreign_keys=OFF')
                old = db.execute('SELECT id FROM projects').fetchone()[0]
                db.execute('UPDATE projects SET id=? WHERE id=?',(self.project,old))
                db.execute('UPDATE project_members SET project_id=? WHERE project_id=?',(self.project,old))
                db.execute('UPDATE local_principals SET project_id=? WHERE project_id=?',(self.project,old))
                db.commit(); db.execute('PRAGMA foreign_keys=ON')
                self.actors.append(db.execute('SELECT principal_id FROM project_members').fetchone()[0])

    def export(self, index, portable=True, ancestor=None):
        with self.stores[index]._connection() as db:
            return exchange.export_bundle(db,self.project,ancestor,portable=portable)

    def apply(self, index, bundle, request, choices=None):
        store = self.stores[index]
        return store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[index]},
                                  'request_id':request,'expected_revision':store.snapshot()['revision'],
                                  'operations':[{'op':'import_bundle','bundle':bundle,'choices':choices or {}}]})

    def source(self, index, root_path, *, with_blob=True):
        body=b'native source'; digest=hashlib.sha256(body).hexdigest()
        with self.stores[index]._connection() as db:
            db.execute('INSERT INTO artifact_roots VALUES(?,?,?,?,?)',('root',self.project,str(root_path),self.actors[index],'now'))
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,1)',('artifact',self.project,'root','source.bin','binary',self.actors[index],'now'))
            db.execute('INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',('version',self.project,'artifact',digest,len(body),None,None,'available',None,self.actors[index],'now'))
            if with_blob: db.execute('INSERT INTO artifact_blobs VALUES(?,?,?)',(self.project,digest,body))
        return digest

    def test_portable_export_has_logical_root_no_blob_and_curated_history(self):
        physical=Path(self.tmp.name)/'private-native-files'
        physical.mkdir()
        digest=self.source(0,physical)
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO transactions VALUES('history','digest','{}')")
            db.execute('INSERT INTO workspace_events(id,project_id,request_id,actor_id,kind,after_json,rationale,recorded_at) VALUES(?,?,?,?,?,?,?,?)',
                       ('event',self.project,'history',self.actors[0],'note',json.dumps({'nested':{'path':str(physical)}}),str(physical),'now'))
        bundle=self.export(0)
        self.assertEqual((bundle['format'],bundle['version']),('workbench-exchange',2))
        self.assertEqual(bundle['records']['artifact_roots'][0]['path'],'workspace-root:root')
        self.assertEqual(bundle['records']['artifact_blobs'],[])
        self.assertEqual(bundle['records']['artifact_versions'][0]['sha256'],digest)
        self.assertNotIn(str(physical),exchange.canonical(bundle))
        self.assertIn('payload_sha256',bundle['history'][0])
        self.assertNotIn('after_json',bundle['history'][0])
        raw=self.export(0,portable=False)
        self.assertEqual(raw['records']['artifact_roots'][0]['path'],str(physical))
        self.assertTrue(raw['records']['artifact_blobs'])
        self.assertIn('after_json',raw['history'][0])

    def test_portable_preview_ignores_local_physical_root_and_preserves_binding(self):
        physical=Path(self.tmp.name)/'local'; physical.mkdir()
        self.source(0,physical)
        self.source(1,Path(self.tmp.name)/'remote')
        for store in self.stores:
            with store._connection() as db: db.execute('UPDATE artifact_roots SET created_by=NULL')
        bundle=self.export(1)
        with self.stores[0]._connection() as db:
            preview=exchange.preview_import(db,self.project,bundle)
            root=next(c for c in preview['changes'] if c['table']=='artifact_roots')
            self.assertEqual(root['status'],'identical')
        choices={exchange.canonical([change['table'],change['key'],field]):'local'
                 for change in preview['changes'] for field,alt in change['fields'].items() if alt['conflict']}
        self.apply(0,bundle,'same-root',choices)
        with self.stores[0]._connection() as db:
            self.assertEqual(db.execute('SELECT path FROM artifact_roots').fetchone()[0],str(physical))

    def test_remote_new_root_is_unbound_and_bytes_absent(self):
        digest=self.source(1,Path(self.tmp.name)/'remote')
        bundle=self.export(1)
        with self.stores[0]._connection() as db:
            self.assertEqual(exchange.preview_import(db,self.project,bundle)['missing_sources'],[digest])
        self.apply(0,bundle,'new-root')
        with self.stores[0]._connection() as db:
            self.assertEqual(db.execute('SELECT path FROM artifact_roots').fetchone()[0],'workspace-root:root')
            self.assertIsNone(db.execute('SELECT 1 FROM artifact_blobs').fetchone())
        version=self.stores[0].read_v1('artifact-versions',self.project)['items'][0]
        self.assertFalse(version['local_bytes_available'])
        with self.assertRaisesRegex(StoreError,'unbound'):
            self.stores[0].transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[0]},
                'request_id':'cannot-read-root','expected_revision':self.stores[0].snapshot()['revision'],
                'operations':[{'op':'register_artifact_version','artifact_id':'artifact'}]})

    def test_portable_export_fails_closed_on_authored_host_path(self):
        physical=Path(self.tmp.name)/'secret'; physical.mkdir()
        with self.stores[0]._connection() as db:
            db.execute('INSERT INTO areas(id,project_id,title) VALUES(?,?,?)',('area',self.project,'Evidence at '+str(physical)))
        with self.assertRaisesRegex(StoreError,'host path'):
            self.export(0)


    def test_nested_record_path_fails_closed(self):
        with self.stores[0]._connection() as db:
            record={'id':'T','title':'Source note','status':'ready','provenance':{'machine':{'path':'C:\\private\\native.bin'}}}
            db.execute('INSERT INTO tasks(id,sibling_order,record_json,original_json,uid,project_id) VALUES(?,0,?,?,?,?)',
                       ('T',json.dumps(record),'{}','task-uid',self.project))
        with self.assertRaisesRegex(StoreError,'host path'):
            self.export(0)

    def test_imported_portable_history_survives_host_reexport_without_import_churn(self):
        source=self.stores[1]
        source.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[1]},'request_id':'question-history','expected_revision':source.snapshot()['revision'],'operations':[{'op':'create_question','id':'history-question','text':'Retain this source event'}]})
        incoming=self.export(1);self.assertTrue(incoming['history'])
        self.apply(0,incoming,'history-import')
        exported=self.export(0)
        self.assertEqual(exported['history'],incoming['history'],'host reexport preserves original event digests and excludes its local import audit')
        self.assertEqual(exported,self.export(0))

    def test_journal_text_evidence_and_correction_survive_portable_roundtrip(self):
        source=self.stores[1]
        def transact(request,operations):
            return source.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[1]},'request_id':request,'expected_revision':source.snapshot()['revision'],'operations':operations})
        transact('journal-task',[{'op':'create','alias':'JOURNAL','record':{'title':'Shared progress','status':'ready'}}])
        transact('journal-note',[{'op':'journal','task_id':'JOURNAL','kind':'observation','text':'Compared 3 samples; tolerance is 0.25.','evidence':['docs/probe.md'],'boundary':'native input','outcome':'inconclusive'}])
        original=next(e for e in source.snapshot()['events'] if e['request_id']=='journal-note')
        transact('journal-correction',[{'op':'journal','task_id':'JOURNAL','kind':'review','text':'Correction: 4 samples.','supersedes':original['event_id']}])
        incoming=self.export(1)
        entries=incoming['records']['task_journal_entries']
        self.assertEqual(len(entries),2)
        self.apply(0,incoming,'journal-import')
        history=self.stores[0].read_v1('history',self.project,key='JOURNAL')['items']
        notes={entry['event_id']:entry for entry in history if entry['kind'] in ('observation','review')}
        self.assertEqual(notes[original['event_id']]['details'],original['details'])
        self.assertEqual(notes[original['event_id']]['actor'],original['actor'])
        self.assertEqual(notes[original['event_id']]['provenance']['origin_store_id'],source.snapshot()['store_id'])
        self.assertTrue(any(e['details'].get('supersedes')==original['event_id'] for e in notes.values()))
        exported=self.export(0)
        self.assertEqual(exported['records']['task_journal_entries'],entries)
        self.assertEqual(exported,self.export(0))
        # A collaborator can correct imported history without forging the source entry.
        self.stores[0].transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[0]},'request_id':'remote-correction','expected_revision':self.stores[0].snapshot()['revision'],'operations':[{'op':'journal','task_id':'JOURNAL','kind':'review','text':'Independent review pending.','supersedes':original['event_id']}]})
        self.assertEqual(len(self.export(0)['records']['task_journal_entries']),3)

    def test_previous_snapshot_without_journal_table_remains_a_valid_ancestor(self):
        source=self.stores[1]
        source.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[1]},'request_id':'old-schema-note','expected_revision':source.snapshot()['revision'],'operations':[{'op':'create','alias':'OLD-JOURNAL','record':{'title':'Old snapshot','status':'ready'}},{'op':'journal','task_id':'OLD-JOURNAL','kind':'review','text':'Recover this from the original installation.'}]})
        old=self.export(1)
        old['records'].pop('task_journal_entries');old['schema'].pop('task_journal_entries')
        old['id']=exchange.research.digest({key:value for key,value in old.items() if key!='id'})
        self.apply(0,old,'old-schema-import')
        self.assertEqual(self.stores[0].read_v1('history',self.project,key='OLD-JOURNAL')['items'],[])
        source.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[1]},'request_id':'old-schema-register','expected_revision':source.snapshot()['revision'],'operations':[{'op':'register_exchange_snapshot','bundle':old}]})
        current=self.export(1,ancestor=old['id'])
        self.apply(0,current,'new-schema-journal')
        history=self.stores[0].read_v1('history',self.project,key='OLD-JOURNAL')['items']
        self.assertEqual(history[0]['details']['text'],'Recover this from the original installation.')
        with self.stores[0]._connection() as db:
            saved=json.loads(db.execute('SELECT bundle_json FROM exchange_snapshots WHERE id=?',(old['id'],)).fetchone()[0])
        self.assertEqual(saved,old)

    def test_schema8_journal_backfill_preserves_raw_events_and_memberships(self):
        source=self.stores[1]
        source.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[1]},'request_id':'backfill-task','expected_revision':source.snapshot()['revision'],'operations':[{'op':'create','alias':'BACKFILL','record':{'title':'Backfill','status':'ready'}},{'op':'journal','task_id':'BACKFILL','kind':'handoff','text':'Keep the original handoff.','evidence':['docs/evidence.md']}]})
        raw=source.snapshot()['events']
        with source._connection() as db:
            memberships=[tuple(row) for row in db.execute('SELECT * FROM project_members')]
            db.execute('DROP TABLE task_journal_entries')
            db.execute('UPDATE store_meta SET schema_version=8')
        reopened=RoadmapStore(source.path)
        self.assertEqual(reopened.snapshot()['events'],raw)
        with reopened._connection() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM project_members')],memberships)
            entry=dict(db.execute('SELECT * FROM task_journal_entries').fetchone())
            self.assertEqual(entry['created_by'],self.actors[1])
            original=json.loads(entry['source_event_json'])
            self.assertEqual(original['event_id'],entry['id'])
            self.assertEqual(json.loads(original['details_json'])['text'],'Keep the original handoff.')
            self.assertEqual(entry['source_sha256'],exchange.research.digest(original))

    def test_import_rejects_tampered_journal_content_without_writes(self):
        source=self.stores[1]
        source.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[1]},'request_id':'tamper-journal','expected_revision':source.snapshot()['revision'],'operations':[{'op':'create','alias':'TAMPER','record':{'title':'Original','status':'ready'}},{'op':'journal','task_id':'TAMPER','kind':'review','text':'Original review.'}]})
        incoming=self.export(1)
        incoming['records']['task_journal_entries'][0]['details_json']=json.dumps({'text':'Forged replacement'})
        incoming['id']=exchange.research.digest({key:value for key,value in incoming.items() if key!='id'})
        before=self.stores[0].snapshot()
        with self.assertRaisesRegex(StoreError,'provenance mismatch'):
            self.apply(0,incoming,'tampered-journal')
        self.assertEqual(self.stores[0].snapshot(),before)

    def test_schema7_upgrade_backfills_only_applied_portable_history(self):
        import sys
        import sqlite3
        from contextlib import closing
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server
        source=self.stores[1]
        source.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[1]},'request_id':'migration-history','expected_revision':source.snapshot()['revision'],'operations':[{'op':'create_question','id':'migrated-question','text':'Keep imported provenance'}]})
        incoming=self.export(1)
        old_audit={**incoming['history'][0],'id':'old-transport-audit','kind':'import_bundle'}
        incoming['history'].append(old_audit)
        incoming['id']=exchange.research.digest({key:value for key,value in incoming.items() if key!='id'})
        self.apply(0,incoming,'old-import')
        path=self.stores[0].path
        with self.stores[0]._connection() as db:
            db.execute('DROP TABLE portable_event_history')
            db.execute('UPDATE store_meta SET schema_version=7')
        with roadmap_server.service_lock(path):
            checkpoint=roadmap_server.checkpoint_before_upgrade(path)
            upgraded=RoadmapStore(path)
        with closing(sqlite3.connect(checkpoint)) as backup:
            self.assertEqual(backup.execute('SELECT schema_version FROM store_meta').fetchone()[0],7)
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        with upgraded._connection() as db:
            exported=exchange.export_bundle(db,self.project,portable=True)
            saved=json.loads(db.execute('SELECT bundle_json FROM exchange_snapshots WHERE project_id=? AND id=?',(self.project,incoming['id'])).fetchone()[0])
            self.assertIn(old_audit,saved['history'],'raw original snapshot remains unchanged')
        self.assertEqual({event['id'] for event in exported['history']},{incoming['history'][0]['id']})

    def test_portable_snapshot_is_deterministic_and_conflict_is_explicit(self):
        for store in self.stores:
            with store._connection() as db: db.execute("INSERT INTO areas(id,project_id,title) VALUES('area',?,'Original')",(self.project,))
        base=self.export(0)
        self.assertEqual(base,self.export(0))
        for index in range(2):
            store=self.stores[index]
            store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[index]},
                'request_id':'base-'+str(index),'expected_revision':store.snapshot()['revision'],
                'operations':[{'op':'register_exchange_snapshot','bundle':base}]})
        for index,title in enumerate(('Local','Remote')):
            with self.stores[index]._connection() as db: db.execute('UPDATE areas SET title=? WHERE id=?',(title,'area'))
        incoming=self.export(1,ancestor=base['id'])
        with self.stores[0]._connection() as db:
            preview=exchange.preview_import(db,self.project,incoming)
        area=next(change for change in preview['changes'] if change['table']=='areas')
        self.assertTrue(area['fields']['title']['conflict'])
        with self.assertRaisesRegex(StoreError,'Explicit local/remote'):
            self.apply(0,incoming,'unresolved')


    def test_local_binding_and_verified_hydration_do_not_change_portable_payload(self):
        body=b'native source'; digest=hashlib.sha256(body).hexdigest()
        version_id=str(uuid.uuid5(uuid.NAMESPACE_URL,exchange.canonical([self.project,'artifact',digest])))
        with self.stores[1]._connection() as db:
            db.execute('INSERT INTO artifact_roots VALUES(?,?,?,?,?)',('root',self.project,'/remote-only',self.actors[1],'now'))
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,1)',('artifact',self.project,'root','source.bin','binary',self.actors[1],'now'))
            db.execute('INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (version_id,self.project,'artifact',digest,len(body),None,None,'available',None,self.actors[1],'now'))
        self.apply(0,self.export(1),'import-hydrate')
        before=self.export(0)
        self.assertFalse(self.stores[0].read_v1('artifact-versions',self.project)['items'][0]['local_bytes_available'])
        local=Path(self.tmp.name)/'bound'; local.mkdir()
        (local/'source.bin').write_bytes(body)
        store=self.stores[0]
        store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[0]},
            'request_id':'bind','expected_revision':store.snapshot()['revision'],
            'operations':[{'op':'bind_root','root_id':'root','path':str(local)}]})
        self.assertEqual(store.read_v1('artifact-versions',self.project)['items'][0]['local_bytes_available'],False)
        (local/'source.bin').write_bytes(b'wrong source')
        with self.assertRaisesRegex(StoreError,'Source changed'):
            store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[0]},
                'request_id':'wrong-hydrate','expected_revision':store.snapshot()['revision'],
                'operations':[{'op':'register_artifact_version','artifact_id':'artifact','expected_sha256':digest}]})
        self.assertFalse(store.read_v1('artifact-versions',self.project)['items'][0]['local_bytes_available'])
        (local/'source.bin').write_bytes(body)
        store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actors[0]},
            'request_id':'hydrate','expected_revision':store.snapshot()['revision'],
            'operations':[{'op':'register_artifact_version','artifact_id':'artifact','expected_sha256':digest}]})
        self.assertTrue(store.read_v1('artifact-versions',self.project)['items'][0]['local_bytes_available'])
        with store._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM artifact_versions').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT body FROM artifact_blobs').fetchone()[0],body)
        after=self.export(0)
        self.assertEqual(before,after)

    def test_local_only_events_are_omitted_from_portable_history(self):
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO transactions VALUES('history','digest','{}')")
            for kind in ('bind_root','repository_snapshot','register_repository','user_note'):
                db.execute('INSERT INTO workspace_events(id,project_id,request_id,actor_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?)',
                           (kind,self.project,'history',self.actors[0],kind,'{}','now'))
        self.assertEqual([event['kind'] for event in self.export(0)['history']],['user_note'])
        self.assertEqual(len(self.export(0,portable=False)['history']),4)


    def test_existing_global_principal_is_reused_without_membership_transfer(self):
        for index,store in enumerate(self.stores):
            with store._connection() as db:
                db.execute("INSERT INTO principals(id,label,identity_kind) VALUES('shared','Shared reviewer','trusted_local_label')")
                if index == 1:
                    db.execute("INSERT INTO project_members(project_id,principal_id,capabilities_json) VALUES(?,'shared','[\"review\"]')",(self.project,))
        bundle=self.export(1)
        with self.stores[0]._connection() as db:
            preview=exchange.preview_import(db,self.project,bundle)
            self.assertEqual(next(c['status'] for c in preview['changes'] if c['table']=='principals' and 'shared' in c['key']),'addition')
        self.apply(0,bundle,'reuse-principal')
        with self.stores[0]._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM principals WHERE id='shared'").fetchone()[0],1)
            self.assertIsNone(db.execute("SELECT 1 FROM project_members WHERE principal_id='shared'").fetchone())

    def test_conflicting_global_principal_is_rejected_without_overwrite(self):
        with self.stores[0]._connection() as db:
            db.execute("INSERT INTO principals(id,label,identity_kind) VALUES('shared','Local label','trusted_local_label')")
        with self.stores[1]._connection() as db:
            db.execute("INSERT INTO principals(id,label,identity_kind) VALUES('shared','Remote label','trusted_local_label')")
            db.execute("INSERT INTO project_members(project_id,principal_id,capabilities_json) VALUES(?,'shared','[\"review\"]')",(self.project,))
        with self.assertRaisesRegex(StoreError,'principal identity'):
            self.apply(0,self.export(1),'conflicting-principal')
        with self.stores[0]._connection() as db:
            self.assertEqual(db.execute("SELECT label FROM principals WHERE id='shared'").fetchone()[0],'Local label')
            self.assertEqual(db.execute('SELECT count(*) FROM exchange_imports').fetchone()[0],0)


if __name__=='__main__': unittest.main()
