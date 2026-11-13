"""Research contracts exercise temporary stores, never the live workspace."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.roadmap_store import RoadmapStore, StoreError


class ResearchTests(unittest.TestCase):
    def test_question_revisions_separate_editorial_and_matching_changes(self):
        criteria={'any_of':[{'identifier':'speed','namespace':'objc-field','scope_key':'Player'}]}
        self.write({'op':'create_question','id':'editable','text':'Original question','criteria':criteria})
        with self.store._connection() as db:
            original=dict(db.execute("SELECT * FROM question_revisions WHERE question_id='editable'").fetchone())
            jobs=db.execute('SELECT count(*) FROM research_outbox').fetchone()[0]
        self.write({'op':'revise_question','question_id':'editable','expected_record_revision':1,
                    'text':'Clearer question','rationale':'Clarify wording'})
        question=self.store.read_v1('questions',self.project)['items'][0]
        self.assertEqual((question['record_revision'],question['criteria_revision'],question['presentation_revision']),(2,1,2))
        with self.store._connection() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM question_revisions WHERE id=?',(original['id'],)).fetchone()),original)
            self.assertEqual(db.execute('SELECT count(*) FROM research_outbox').fetchone()[0],jobs)
        criteria['any_of'][0]['scope_key']='Ball'
        self.write({'op':'revise_question','question_id':'editable','expected_record_revision':2,
                    'criteria':criteria,'rationale':'Different research scope'})
        question=self.store.read_v1('questions',self.project)['items'][0]
        self.assertEqual((question['record_revision'],question['criteria_revision'],question['presentation_revision']),(3,2,2))
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM research_outbox').fetchone()[0],jobs+1)
        before=self.store.snapshot()['revision']
        for operation in ({'expected_record_revision':2,'text':'Stale'},
                          {'expected_record_revision':3,'criteria':{'any_of':[]}}):
            with self.assertRaises(StoreError):
                self.write({'op':'revise_question','question_id':'editable','rationale':'Rejected change',**operation})
            self.assertEqual(self.store.snapshot()['revision'],before)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RoadmapStore(self.root / 'research.sqlite3')
        self.project = self.store.read_v1('projects')['items'][0]['id']
        self.principal = self.store.read_v1('capabilities', self.project)['capabilities']['local_principal_id']
        self.seq = 0

    def write(self, *operations, request_id=None):
        self.seq += 1
        return self.store.transact_v1({'project_id': self.project,
            'actor': {'principal_id': self.principal}, 'request_id': request_id or f'test-{self.seq}',
            'expected_revision': self.store.snapshot()['revision'], 'operations': list(operations)})

    def source(self, body=b'original witness'):
        (self.root / 'witness.bin').write_bytes(body)
        self.write({'op':'register_root','id':'root','path':str(self.root)})
        self.write({'op':'register_artifact','id':'source','root_id':'root','relative_path':'witness.bin','kind':'binary'})
        result = self.write({'op':'register_artifact_version','artifact_id':'source','binary_build_id':'build-A'})
        return result['changed_records'][0]['id']

    def index_run(self, version, *, extractor='extractor', value=4, discriminator='member-a'):
        self.seq += 1
        run_id = f'run-{self.seq}'
        self.write({'op':'create_index_run','id':run_id,'extractor_id':extractor,'extractor_version':'1',
                    'config_hash':'a'*64,'input_version_ids':[version],
                    'coverage':{'complete':False,'indexed':['Player'], 'excluded':['Other classes']}})
        record = {'artifact_version_id':version,'namespace':'objc-field','scope_key':'Player',
                  'locator_key':{'address':'0x123','member':discriminator},'identifier':'speed',
                  'value':value,'provenance':{'method':'binary extraction','limitations':'bounded probe'}}
        batch = self.write({'op':'stage_batch','run_id':run_id,'batch_key':'0','records':[record]})
        return run_id, batch['changed_records'][0]['payload_hash'], record

    def publish(self, run_id, batch_hash):
        return self.write({'op':'publish_run','run_id':run_id,'manifest':{'batches':[{'key':'0','sha256':batch_hash}],'record_count':1}})

    def test_staging_atomic_publication_and_retry(self):
        version = self.source()
        run, digest, record = self.index_run(version)
        self.assertEqual(self.store.read_v1('observations', self.project)['items'], [])
        self.write({'op':'stage_batch','run_id':run,'batch_key':'0','records':[record]})
        with self.assertRaises(StoreError) as exc:
            self.write({'op':'stage_batch','run_id':run,'batch_key':'0','records':[{**record,'value':9}]})
        self.assertEqual(exc.exception.status,409)
        before = self.store.snapshot()['revision']
        with self.assertRaises(StoreError):
            self.publish(run, '0'*64)
        self.assertEqual(self.store.snapshot()['revision'],before)
        self.assertEqual(self.store.read_v1('observations', self.project)['items'], [])
        self.publish(run,digest)
        observations = self.store.read_v1('observations',self.project)['items']
        self.assertEqual(len(observations),1)
        self.assertEqual(observations[0]['value'],4)
        self.assertFalse(self.store.read_v1('observations',self.project)['coverage']['complete'])
        self.store = RoadmapStore(self.store.path)
        self.assertEqual(self.store.read_v1('observations',self.project)['items'],observations)

    def test_conflicting_extractors_and_locator_discriminators(self):
        version = self.source()
        for extractor,value,member in [('A',4,'a'),('B',5,'a'),('A',6,'b'),('A',4,'a')]:
            run,digest,_ = self.index_run(version,extractor=extractor,value=value,discriminator=member)
            self.publish(run,digest)
        observations=self.store.read_v1('observations',self.project)['items']
        self.assertEqual(len(observations),3)
        self.assertEqual(len({o['occurrence_id'] for o in observations}),2)
        self.assertEqual(sorted(o['value'] for o in observations),[4,5,6])
        self.assertTrue(all('current_observation' not in o for o in observations))

    def test_changed_bytes_new_version_and_paths_fail_closed(self):
        first=self.source()
        (self.root/'witness.bin').write_bytes(b'changed build')
        second=self.write({'op':'register_artifact_version','artifact_id':'source'})['changed_records'][0]['id']
        self.assertNotEqual(first,second)
        for path in ['../outside.bin','/absolute.bin','folder/../../outside','C:/outside']:
            with self.subTest(path=path), self.assertRaises(StoreError):
                self.write({'op':'register_artifact','id':'bad','root_id':'root','relative_path':path,'kind':'binary'})
        with self.assertRaises(StoreError):
            self.write({'op':'register_artifact_version','artifact_id':'source','expected_sha256':'0'*64})

    def test_scope_and_transaction_rollback(self):
        version=self.source()
        before=self.store.snapshot()['revision']
        with self.assertRaises(StoreError):
            self.write({'op':'create_question','id':'q','text':'What is speed?',
                        'criteria':{'any_of':[{'identifier':'speed','scope_key':'Player'}]}},
                       {'op':'create_index_run','id':'invalid','extractor_id':'A','extractor_version':'1',
                        'config_hash':'a'*64,'input_version_ids':['foreign-version'], 'coverage':{'complete':False}})
        self.assertEqual(self.store.snapshot()['revision'],before)
        self.assertEqual(self.store.read_v1('questions',self.project)['items'],[])
        self.write({'op':'record_negative_search','id':'search','artifact_version_id':version,
                    'query':'missing symbol','method':'exact identifier scan','searched_range':'Player only',
                    'coverage':{'complete':False},'outcome':'not found in this search'})
        search=self.store.read_v1('negative-searches',self.project)['items'][0]
        self.assertEqual(search['searched_range'],'Player only')
        self.assertFalse(search['coverage']['complete'])

    def test_store_matching_loop_and_pinned_inbox_survive_restart(self):
        version=self.source()
        self.write({'op':'create_question','id':'speed-question','text':'How is speed stored?',
                    'criteria':{'any_of':[{'identifier':'speed','namespace':'objc-field','scope_key':'Player'}]}})
        self.write({'op':'subscribe','target_type':'question','target_id':'speed-question'})
        run,digest,_=self.index_run(version)
        self.publish(run,digest)
        self.store.process_jobs(self.project,limit=10)
        hints=self.store.read_v1('hints',self.project)['items']
        self.assertEqual(len(hints),1)
        self.assertEqual(hints[0]['compared']['scope_key'],'Player')
        inbox=self.store.read_v1('inbox',self.project,principal_id=self.principal)['items']
        self.assertEqual(len(inbox),1)
        self.write({'op':'decide_hint','hint_id':hints[0]['id'],'state':'dismissed','reason':'Existing isolated observation is insufficient.'})
        self.store=RoadmapStore(self.store.path)
        self.store.process_jobs(self.project,limit=10)
        self.assertEqual(self.store.read_v1('hints',self.project)['items'][0]['state'],'dismissed')
        self.assertEqual(len(self.store.read_v1('inbox',self.project,principal_id=self.principal)['items']),1)
        self.assertEqual(self.store.read_v1('questions',self.project)['items'][0]['state'],'open')

    def test_question_intake_rejects_poisoned_criteria_and_optional_field_types(self):
        version=self.source()
        before=self.store.snapshot()['revision']
        invalid=[{'criteria':{'any_of':'wrong'}},{'criteria':{'any_of':[]}},
                 {'criteria':{'any_of':[{'identifier':'speed','unsupported':'Player'}]}},
                 {'criteria':{'any_of':[{'identifier':'speed','scope_key':{}}]}},
                 {'expected_evidence':{}},{'expected_evidence':[]},{'expected_evidence':None}]
        for index,fields in enumerate(invalid):
            with self.subTest(fields=fields):
                with self.assertRaises(StoreError) as exc:
                    self.write({'op':'create_question','id':'bad-'+str(index),'text':'Bad request',**fields})
                self.assertEqual(exc.exception.status,400)
                self.assertEqual(self.store.snapshot()['revision'],before)
        self.write({'op':'create_question','id':'z-good','text':'Healthy question',
                    'criteria':{'any_of':[{'identifier':'speed','scope_key':'Player'}]}})
        self.store.process_jobs(self.project)
        run,digest,_=self.index_run(version)
        self.publish(run,digest)
        self.assertEqual(self.store.process_jobs(self.project)['new_hints'],1)
        self.assertEqual(self.store.read_v1('hints',self.project)['items'][0]['question_id'],'z-good')

    def test_optional_source_metadata_wrong_json_types_are_client_errors(self):
        self.source()
        for field in ('repository_revision','binary_build_id','source_created_at'):
            with self.subTest(field=field), self.assertRaises(StoreError) as exc:
                self.write({'op':'register_artifact_version','artifact_id':'source',field:{}})
            self.assertEqual(exc.exception.status,400)


if __name__=='__main__':
    unittest.main()
