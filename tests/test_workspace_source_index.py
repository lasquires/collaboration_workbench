"""Connected Git commits become immutable source evidence, never worktree guesses."""
import json
import unittest
from tests import test_workspace_repository_service as repo_fixtures

class SourceIndexTests(unittest.TestCase):
    def setUp(self):
        fixture=repo_fixtures.RepositoryServiceTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.store=fixture.store;self.project=fixture.project;self.connection=fixture.connection;self.repo=fixture.repo

    def test_refresh_pins_exact_committed_bytes_and_old_versions_survive_changes(self):
        self.store.refresh_repository(self.project,self.connection)
        versions=self.store.read_v1('artifact-versions',self.project)['items']
        self.assertTrue(versions,'Git source refresh must publish citable committed versions')
        old=versions[0]
        with self.store._connection() as db:self.assertEqual(db.execute('SELECT body FROM artifact_blobs WHERE project_id=? AND sha256=?',(self.project,old['sha256'])).fetchone()[0],b'original')
        self.assertIn('original',{row['identifier'] for row in self.store.read_v1('observations',self.project)['items']})
        (self.repo/'seed.txt').write_text('dirty replacement',encoding='utf-8')
        self.store.refresh_repository(self.project,self.connection)
        self.assertEqual(len(self.store.read_v1('artifact-versions',self.project)['items']),1)
        (self.repo/'seed.txt').write_text('committed replacement',encoding='utf-8');self.fixture.git('add','seed.txt');self.fixture.git('commit','-qm','new source')
        self.store.refresh_repository(self.project,self.connection)
        self.assertEqual(len(self.store.read_v1('artifact-versions',self.project)['items']),2)
        with self.store._connection() as db:self.assertEqual(db.execute('SELECT body FROM artifact_blobs WHERE project_id=? AND sha256=?',(self.project,old['sha256'])).fetchone()[0],b'original')
        row=self.store.read_v1('repositories',self.project)['items'][0]
        self.assertEqual(row['evidence_head'],row['head_commit']);self.assertIsNotNone(row['evidence_manifest_id'])
        revision=self.store.snapshot()['revision'];self.assertFalse(self.store.refresh_repository(self.project,self.connection)['changed'])
        self.assertEqual(self.store.snapshot()['revision'],revision)

    def write(self,*ops):
        import uuid
        actor=self.store.read_v1('capabilities',self.project)['capabilities']['local_principal_id']
        return self.store.transact_v1({'project_id':self.project,'actor':{'principal_id':actor},'expected_revision':self.store.snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':list(ops)})

    def test_parallel_clones_share_generated_identity_without_timestamp_conflicts(self):
        import subprocess
        import uuid
        from tools.roadmap_store import RoadmapStore
        from tools import roadmap_exchange as exchange
        self.store.refresh_repository(self.project,self.connection)
        clone=self.fixture.root/'clone';subprocess.run(['git','clone','-q',str(self.repo),str(clone)],check=True)
        peer=RoadmapStore(self.fixture.root/'peer.sqlite3');default=peer.read_v1('projects')['items'][0]['id']
        def write(project,*ops):
            actor=peer.read_v1('capabilities',project)['capabilities']['local_principal_id']
            return peer.transact_v1({'project_id':project,'actor':{'principal_id':actor},'expected_revision':peer.snapshot()['revision'],'request_id':str(uuid.uuid4()),'operations':list(ops)})
        write(default,{'op':'create_project','id':self.project,'slug':'source-peer','goal':'Same committed project'})
        write(self.project,{'op':'register_repository','root':str(clone)})
        connection=peer.read_v1('repositories',self.project)['items'][0]['id'];peer.refresh_repository(self.project,connection)
        incoming=peer.exchange_preview(self.project,portable=True)['bundle']
        own=self.store.exchange_preview(self.project,portable=True)['bundle']
        self.assertEqual(own['records']['git_source_manifests'][0]['id'],incoming['records']['git_source_manifests'][0]['id'])
        self.assertNotEqual(own['records']['observations'][0]['created_at'],incoming['records']['observations'][0]['created_at'])
        preview=self.store.exchange_preview(self.project,bundle=incoming)['preview']
        generated={'artifact_roots','artifacts','artifact_versions','index_runs','index_batches','occurrences','observations','git_source_manifests'}
        self.assertFalse([c for c in preview['changes'] if c['table'] in generated and c['status']=='conflict'])
        self.assertEqual(incoming['records']['artifact_blobs'],[])
        self.assertNotIn(str(clone),json.dumps(incoming))
        choices={exchange.canonical([c['table'],c['key'],f]):'local' for c in preview['changes'] for f,a in c['fields'].items() if a['conflict']}
        self.write({'op':'import_bundle','bundle':incoming,'choices':choices})
        with self.store._connection() as db:self.assertEqual(json.loads(db.execute('SELECT bundle_json FROM exchange_snapshots WHERE id=?',(incoming['id'],)).fetchone()[0]),incoming)

    def test_unchanged_file_observations_and_dismissals_survive_an_unrelated_commit(self):
        self.store.refresh_repository(self.project,self.connection)
        self.write({'op':'create_question','id':'source-q','text':'Where is original written?','criteria':{'any_of':[{'identifier':'original','namespace':'source-token','scope_key':'seed.txt'}]}},{'op':'process_jobs','limit':100})
        hint=self.store.read_v1('hints',self.project)['items'][0]
        self.write({'op':'decide_hint','hint_id':hint['id'],'state':'dismissed','reason':'Already read this exact source token'})
        old={r['id'] for r in self.store.read_v1('observations',self.project)['items'] if r['identifier']=='original'}
        (self.repo/'other.txt').write_text('unrelated tokens',encoding='utf-8');self.fixture.git('add','other.txt');self.fixture.git('commit','-qm','unrelated file')
        self.store.refresh_repository(self.project,self.connection);self.write({'op':'process_jobs','limit':100})
        self.assertEqual(old,{r['id'] for r in self.store.read_v1('observations',self.project)['items'] if r['identifier']=='original'})
        hints=self.store.read_v1('hints',self.project)['items'];self.assertEqual(len(hints),1);self.assertEqual(hints[0]['state'],'dismissed')

    def test_partial_capture_reports_coverage_and_failure_keeps_old_evidence(self):
        from unittest.mock import patch
        from tools import workspace_git_capture as capture
        (self.repo/'seed.txt').write_text('alpha beta gamma',encoding='utf-8');self.fixture.git('add','seed.txt');self.fixture.git('commit','-qm','tokens')
        original=capture.capture_commit
        with patch.object(capture,'capture_commit',side_effect=lambda root,head:original(root,head,max_tokens=1)):
            self.store.refresh_repository(self.project,self.connection)
        row=self.store.read_v1('repositories',self.project)['items'][0];self.assertEqual(row['evidence_state'],'partial')
        manifest=row['evidence_manifest_id'];detail=self.store.read_v1('source-manifests',self.project,manifest_id=manifest)['items'][0]
        self.assertFalse(detail['coverage']['complete']);self.assertEqual(detail['coverage']['token_count'],1)
        (self.repo/'seed.txt').write_text('next committed source',encoding='utf-8');self.fixture.git('add','seed.txt');self.fixture.git('commit','-qm','next')
        with patch.object(capture,'capture_commit',side_effect=capture.GitCaptureError('simulated object read failure')):
            self.store.refresh_repository(self.project,self.connection)
        failed=self.store.read_v1('repositories',self.project)['items'][0]
        self.assertEqual(failed['evidence_state'],'failed');self.assertEqual(failed['evidence_manifest_id'],manifest)
        self.assertNotEqual(failed['evidence_head'],failed['head_commit'])
        self.store.refresh_repository(self.project,self.connection)
        self.assertEqual(self.store.read_v1('repositories',self.project)['items'][0]['evidence_state'],'current')

    def test_manifest_scope_and_digest_are_checked_before_import(self):
        import copy
        from tools import roadmap_exchange as exchange
        from tools.roadmap_store import StoreError
        self.store.refresh_repository(self.project,self.connection)
        incoming=self.store.exchange_preview(self.project,portable=True)['bundle'];bad=copy.deepcopy(incoming)
        row=bad['records']['git_source_manifests'][0];files=json.loads(row['files_json']);files[0]['artifact_version_id']='foreign-version';row['files_json']=exchange.canonical(files)
        bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
        before=self.store.snapshot()['revision']
        with self.assertRaises(StoreError):self.store.exchange_preview(self.project,bundle=bad)
        self.assertEqual(self.store.snapshot()['revision'],before)

    def test_cross_platform_git_names_are_preserved_without_aborting_publication(self):
        import subprocess
        def git_input(*args,data):
            return subprocess.run(['git','-C',str(self.repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],input=data,check=True,capture_output=True).stdout.strip()
        body=b'exact historical source'
        blob=git_input('hash-object','-w','--stdin',data=body).decode()
        tree=git_input('mktree',data=f'100644 blob {blob}\todd:name.py\n'.encode()).decode()
        parent=self.fixture.git('rev-parse','HEAD').stdout.strip().decode()
        commit=git_input('commit-tree',tree,'-p',parent,data=b'Historical cross-platform filename\n').decode()
        self.fixture.git('update-ref','HEAD',commit)
        self.store.refresh_repository(self.project,self.connection)
        row=self.store.read_v1('repositories',self.project)['items'][0]
        self.assertNotEqual(row['evidence_state'],'failed')
        files=self.store.read_v1('source-manifests',self.project,manifest_id=row['evidence_manifest_id'])['items'][0]['files']
        item=files[0];self.assertEqual(item['display_path'],'odd:name.py')
        self.assertNotIn(':',item['path'])
        self.assertEqual(bytes.fromhex(item['git_path_hex']).decode(),'odd:name.py')
        bundle=self.store.exchange_preview(self.project,portable=True)['bundle']
        self.assertEqual(len(bundle['records']['git_source_manifests']),1)

    def test_overlong_identifier_keeps_bytes_and_reports_partial_lexical_coverage(self):
        source='x'*2001+' usable'
        (self.repo/'seed.txt').write_text(source,encoding='utf-8');self.fixture.git('add','seed.txt');self.fixture.git('commit','-qm','generated long token')
        self.store.refresh_repository(self.project,self.connection)
        row=self.store.read_v1('repositories',self.project)['items'][0]
        self.assertEqual(row['evidence_state'],'partial')
        self.assertEqual({r['identifier'] for r in self.store.read_v1('observations',self.project)['items']},{'usable'})
        self.assertTrue(self.store.read_v1('artifact-versions',self.project)['items'])
        coverage=self.store.read_v1('source-manifests',self.project,manifest_id=row['evidence_manifest_id'])['items'][0]['coverage']
        self.assertTrue(any(e['reason']=='identifier_limit' for e in coverage['excluded']))

    def test_schema13_upgrade_preserves_watch_history_and_rolls_back_on_fault(self):
        import sqlite3
        import sys
        from pathlib import Path
        from contextlib import closing
        from unittest.mock import patch
        from tools import roadmap_schema,workspace_git_watch
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server as server
        snapshot=workspace_git_watch.snapshot_repository(self.repo)
        with self.store._connection() as db:
            db.execute('INSERT INTO repository_snapshots VALUES(?,?,?,?,?,?,?,?)',('legacy-scan',self.project,self.connection,1,snapshot['fingerprint'],json.dumps(snapshot),json.dumps({'changed_paths':[]}), 'historical'))
            db.execute('UPDATE repository_connections SET source_revision=1,snapshot_id=? WHERE id=?',('legacy-scan',self.connection))
            original=dict(db.execute('SELECT * FROM repository_snapshots').fetchone())
            for column in ('evidence_state','evidence_head','evidence_manifest_id','evidence_error'):db.execute('ALTER TABLE repository_connections DROP COLUMN '+column)
            db.execute('DROP TABLE git_source_manifests');db.execute('UPDATE store_meta SET schema_version=13')
        with patch.object(roadmap_schema,'_migration_checkpoint',side_effect=RuntimeError('injected source migration')):
            with self.assertRaisesRegex(RuntimeError,'injected source'):type(self.store)(self.store.path)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],13)
            self.assertNotIn('evidence_state',{r[1] for r in db.execute('PRAGMA table_info(repository_connections)')})
        with server.service_lock(self.store.path):backup=server.checkpoint_before_upgrade(self.store.path);upgraded=type(self.store)(self.store.path)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],13)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        with upgraded._connection() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM repository_snapshots').fetchone()),original)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        upgraded.refresh_repository(self.project,self.connection)
        self.assertTrue(upgraded.read_v1('artifact-versions',self.project)['items'])
        self.assertEqual(len(upgraded.read_v1('repository-updates',self.project,repository_id=self.connection)['items']),2)

    def test_non_utf8_git_name_keeps_original_bytes_and_does_not_block_commit_evidence(self):
        import subprocess
        def git_input(*args,data):
            return subprocess.run(['git','-C',str(self.repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],input=data,check=True,capture_output=True).stdout.strip()
        body=b'legacy identifier'
        blob=git_input('hash-object','-w','--stdin',data=body)
        tree=git_input('mktree',data=b'100644 blob '+blob+b'\tlegacy-\xff.txt\n').decode()
        parent=self.fixture.git('rev-parse','HEAD').stdout.strip().decode()
        commit=git_input('commit-tree',tree,'-p',parent,data=b'Historical byte pathname\n').decode();self.fixture.git('update-ref','HEAD',commit)
        self.store.refresh_repository(self.project,self.connection)
        row=self.store.read_v1('repositories',self.project)['items'][0]
        self.assertNotEqual(row['evidence_state'],'failed')
        manifest=self.store.read_v1('source-manifests',self.project,manifest_id=row['evidence_manifest_id'])['items'][0]
        item=manifest['files'][0];self.assertEqual(bytes.fromhex(item['git_path_hex']),b'legacy-\xff.txt')
        self.assertEqual(item['display_path_encoding'],'escaped-bytes')
        self.assertEqual({o['identifier'] for o in self.store.read_v1('observations',self.project)['items']},{'legacy','identifier'})
        self.assertTrue(self.store.exchange_preview(self.project,portable=True)['bundle']['records']['git_source_manifests'])

    def test_generated_source_records_cannot_drop_commit_provenance(self):
        import copy
        from tools import roadmap_exchange as exchange
        from tools.roadmap_store import StoreError
        self.store.refresh_repository(self.project,self.connection)
        bad=copy.deepcopy(self.store.exchange_preview(self.project,portable=True)['bundle'])
        bad['schema'].pop('git_source_manifests');bad['records'].pop('git_source_manifests')
        bad['id']=exchange.research.digest({k:v for k,v in bad.items() if k!='id'})
        with self.assertRaisesRegex(StoreError,'provenance'):self.store.exchange_preview(self.project,bundle=bad)

    def test_journal_variable_reference_preserves_binary_locator(self):
        from tools.workspace_variable_nodes import lookup_variable_nodes
        self.write(dict(op="create",alias="score-color",record={"title":"Score color","status":"ready"}))
        task=self.store.read_v1("context",self.project,key="score-color")["context"]["task"]["id"]
        reference={"variables":["modifier"],"source":"recovered/apk/lib/armeabi-v7a/libgame.so",
                   "locator":"0x3ffb5c","claim":"Controlled original color method"}
        self.write(dict(op="journal",task_id=task,kind="observation",
                        text="Reconstruction v1\n"+json.dumps(reference)))
        hits=lookup_variable_nodes(self.store,self.project,"modifier")["items"]
        hit=next(item for item in hits if item["node_type"]=="task_variable_reference")
        self.assertIn(reference["source"]+" @ "+reference["locator"],hit["source_locator"])
        self.assertIn("lead",hit["provenance"])

if __name__=='__main__':unittest.main()
