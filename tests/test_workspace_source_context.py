"""Exact source contexts, typed derivations, and snapshot-bound source pages."""
import copy
import json
import unittest
from tests import test_workspace_findings as fixtures
from tools import workspace_source_context as sources
from tools.roadmap_projection import _encode, _decode
from tools.roadmap_store import StoreError, RoadmapStore


class SourceContextTests(unittest.TestCase):
    write = fixtures.FindingsTests.write
    support = fixtures.FindingsTests.support
    create = fixtures.FindingsTests.create
    finding = fixtures.FindingsTests.finding

    def setUp(self):
        fixtures.FindingsTests.setUp(self)
        (self.root/'source.txt').write_text('derived source witness', encoding='utf-8')
        self.child = self.write({'op':'register_artifact_version','artifact_id':'artifact','binary_build_id':'derived-build'})['changed_records'][0]['id']
        self.scope = {'artifact_version_id':self.child,'derivation':{'format':sources.FORMAT,'parent_version_id':self.version}}
        self.citations = [self.support(), {**self.support(),'artifact_version_id':self.child}]

    def read(self, resource='source-context', **options):
        with self.store._connection() as db:
            return sources.read(db,resource,self.project,**options)

    def error(self, status, function, *args, **kwargs):
        with self.assertRaises(StoreError) as raised:
            function(*args,**kwargs)
        self.assertEqual(raised.exception.status,status)
        return raised.exception

    def index_run(self, version, identity, publish=False):
        self.write({'op':'create_index_run','id':identity,'extractor_id':'probe','extractor_version':'1','config_hash':'a'*64,'input_version_ids':[version],'coverage':{'complete':False,'excluded':['outside fixture']}})
        record = {'artifact_version_id':version,'namespace':'source','scope_key':'Ball','locator_key':{'line':1},'identifier':'speed','value':2,'provenance':{'method':'fixture','limitations':'synthetic'}}
        batch = self.write({'op':'stage_batch','run_id':identity,'batch_key':'0','records':[record]})['changed_records'][0]['payload_hash']
        if publish:
            self.write({'op':'publish_run','run_id':identity,'manifest':{'batches':[{'key':'0','sha256':batch}],'record_count':1}})
        return identity

    def negative(self, version, identity):
        self.write({'op':'record_negative_search','id':identity,'artifact_version_id':version,'query':'missing member','method':'literal scan','searched_range':'lines 1-4','coverage':{'complete':False,'excluded':['other files']},'outcome':'not found within range'})

    def test_source_context_canonical_read_only_and_safe(self):
        before = self.store.snapshot()
        with self.store._connection() as db:
            changes = db.total_changes
            page = sources.read(db,'source-context',self.project,key=self.child)
            self.assertEqual(db.total_changes,changes)
        self.assertEqual(page['revision'],before['revision'])
        self.assertIsNone(page['next_cursor'])
        self.assertFalse(page['coverage']['complete'])
        entity = page['context']['entity']
        self.assertEqual((page['context']['entity_type'],entity['id'],entity['binary_build_id']),('source',self.child,'derived-build'))
        self.assertEqual(entity['relative_path'],'source.txt')
        self.assertTrue(entity['local_bytes_available'])
        self.assertNotIn(str(self.root),json.dumps(page))
        self.assertNotIn('root_id',entity)
        for descriptor in page['context']['related'].values():
            self.assertEqual(descriptor['params'],{'project_id':self.project,'subject_id':self.child})
        self.assertEqual(self.store.snapshot(),before)
        self.error(404,self.read,key='missing')
        with self.store._connection() as db:
            self.error(404,sources.read,db,'source-context','foreign-project',key=self.child)
        for options in ({'key':self.child,'limit':1},{'key':self.child,'subject_id':self.child},{'key':self.child,'bogus':True}):
            self.error(400,self.read,**options)

    def test_scope_and_support_exact_references(self):
        with self.store._connection() as db:
            resolved = sources.validate_scope(db,self.project,self.scope)
            self.assertEqual(resolved['parent_version']['id'],self.version)
            for mutation,status in (({'parent_version_id':self.child},400),({'parent_version_id':'missing'},404)):
                bad=copy.deepcopy(self.scope);bad['derivation'].update(mutation)
                self.error(status,sources.validate_scope,db,self.project,bad)
            self.error(404,sources.validate_scope,db,'foreign-project',self.scope)
            self.assertIsNone(sources.validate_scope(db,self.project,{'derivation':{'format':'future/v1'}}))
        staging=self.index_run(self.version,'staging')
        published=self.index_run(self.version,'published',True)
        wrong=self.index_run(self.child,'wrong-input',True)
        with self.store._connection() as db:
            for run,status in ((staging,409),(wrong,400)):
                bad=copy.deepcopy(self.scope);bad['derivation']['index_run_id']=run
                self.error(status,sources.validate_scope,db,self.project,bad)
            scope=copy.deepcopy(self.scope);scope['derivation']['index_run_id']=published
            self.assertEqual(sources.validate_scope(db,self.project,scope)['origin_run']['id'],published)
        self.error(400,sources.validate_support,self.scope,self.citations[:1])
        sources.validate_support(self.scope,self.citations)
        sources.validate_support({'derivation':{'format':'future/v1'}},[])

    def test_bundle_resolves_supplied_references_without_local_laundering(self):
        self.create(scope=self.scope,evidence=self.citations)
        records=self.store.exchange_preview(self.project,portable=True)['bundle']['records']
        receiver=RoadmapStore(self.root/'receiver.sqlite3')
        with receiver._connection() as db:
            sources.validate_bundle(db,self.project,records)
        # An existing local version cannot repair a missing incoming reference.
        with self.store._connection() as db:
            for table in ('artifact_versions','findings'):
                bad=copy.deepcopy(records);bad[table]=[]
                self.error(400,sources.validate_bundle,db,self.project,bad)
            bad=copy.deepcopy(records);bad['evidence_links']=bad['evidence_links'][:1]
            self.error(400,sources.validate_bundle,db,self.project,bad)
            bad=copy.deepcopy(records)
            bad['artifact_versions'][0]['project_id']='foreign-project'
            self.error(400,sources.validate_bundle,db,self.project,bad)

    def test_incoming_bundle_validates_published_run_inputs_and_origin_lineage(self):
        self.index_run(self.version,'origin',True)
        scope=copy.deepcopy(self.scope);scope['derivation']['index_run_id']='origin'
        first=self.create(scope=scope,evidence=self.citations)
        current=self.finding(first['id'])
        self.write({'op':'revise_finding','finding_id':first['id'],'expected_record_revision':current['record_revision'],'fields':{'uncertainty':'Edited limits'},'rationale':'Editorial'})
        records=self.store.exchange_preview(self.project,portable=True)['bundle']['records']
        receiver=RoadmapStore(self.root/'receiver.sqlite3')
        with receiver._connection() as db:
            sources.validate_bundle(db,self.project,records)
            for table in ('index_runs','index_run_inputs'):
                bad=copy.deepcopy(records);bad[table]=[]
                self.error(400,sources.validate_bundle,db,self.project,bad)
            bad=copy.deepcopy(records);bad['index_runs'][0]['state']='staging'
            self.error(409,sources.validate_bundle,db,self.project,bad)
            bad=copy.deepcopy(records);bad['index_run_inputs'][0]['artifact_version_id']=self.child
            self.error(400,sources.validate_bundle,db,self.project,bad)
            bad=copy.deepcopy(records);bad['finding_revision_lineage'][0]['evidence_origin_id']='missing'
            self.error(400,sources.validate_bundle,db,self.project,bad)

    def test_withdrawal_counts_and_bundle_keep_assertion_history(self):
        first=self.create(scope=self.scope,evidence=self.citations)
        link=self.store.read_v1('evidence-links',self.project,subject_id=first['revision_id'])['items'][0]
        self.write({'op':'set_evidence_state','evidence_link_id':link['id'],'expected_state':'available','state':'retracted','reason':'Source interpretation withdrawn'})
        page=self.read('source-derivations',subject_id=self.version)
        self.assertEqual(page['items'][0]['support_counts'],{'available':1,'retracted':1,'superseded':0})
        self.assertEqual(page['items'][0]['evidence_origin_id'],first['revision_id'])
        records=self.store.exchange_preview(self.project,portable=True)['bundle']['records']
        with self.store._connection() as db:
            sources.validate_bundle(db,self.project,records)

    def test_source_pages_use_sql_scoped_limits_and_no_mutation(self):
        self.create(scope=self.scope,evidence=self.citations)
        self.index_run(self.version,'visible',True)
        self.negative(self.version,'bounded')
        before=self.store.snapshot()
        with self.store._connection() as db:
            queries=[];db.set_trace_callback(queries.append)
            changes=db.total_changes
            for resource in ('source-derivations','observations','negative-searches'):
                page=sources.read(db,resource,self.project,subject_id=self.version,limit=1)
                self.assertEqual(len(page['items']),1)
            self.assertEqual(changes,db.total_changes)
        bounded=[query for query in queries if 'LIMIT 2 OFFSET 0' in query]
        self.assertEqual(len(bounded),3)
        self.assertTrue(all(self.project in query and self.version in query for query in bounded))
        self.assertEqual(before,self.store.snapshot())

    def test_support_requires_authored_locator_method_and_limits_only_for_marker(self):
        for field in ('locator','method','limitations'):
            bad=copy.deepcopy(self.citations);bad[0].pop(field)
            self.error(400,sources.validate_support,self.scope,bad)
        with self.store._connection() as db:
            for scope in (None,{}, {'artifact_version_id':'unresolved','derivation':{'format':'future/v1','parent_version_id':'foreign'}}):
                self.assertFalse(sources.is_derivation(scope))
                self.assertIsNone(sources.validate_scope(db,self.project,scope))
                sources.validate_support(scope,[])
                records={'finding_revisions':[{'id':'unknown','project_id':self.project,'scope_json':json.dumps(scope)}]}
                sources.validate_bundle(db,self.project,records)

    def test_historical_archived_derivations_preserve_origin_and_withdrawal(self):
        first=self.create(scope=self.scope,evidence=self.citations)
        current=self.finding(first['id'])
        self.write({'op':'revise_finding','finding_id':first['id'],'expected_record_revision':current['record_revision'],'fields':{'uncertainty':'Editorial limits clarified'},'rationale':'Clarification'})
        current=self.finding(first['id'])
        self.write({'op':'revise_finding','finding_id':first['id'],'expected_record_revision':current['record_revision'],'fields':{'scope':{'artifact_version_id':self.child}},'rationale':'Withdraw explicit derivation interpretation'})
        current=self.finding(first['id'])
        self.write({'op':'archive_finding','finding_id':first['id'],'expected_record_revision':current['record_revision'],'rationale':'Retain history'})
        for version in (self.child,self.version):
            page=self.read('source-derivations',subject_id=version)
            self.assertEqual(len(page['items']),2)
            self.assertTrue(all(row['archived'] and not row['is_current'] for row in page['items']))
            self.assertEqual({row['evidence_origin_id'] for row in page['items']},{first['revision_id']})
            self.assertEqual({row['parent_version']['id'] for row in page['items']},{self.version})
            self.assertEqual({row['child_version']['id'] for row in page['items']},{self.child})

    def test_published_observations_and_negative_searches_exact_source(self):
        self.index_run(self.version,'published',True);self.index_run(self.child,'staged')
        self.negative(self.version,'parent-search');self.negative(self.child,'child-search')
        items=self.read('observations',subject_id=self.version)['items']
        self.assertEqual(len(items),1)
        item=items[0]
        self.assertEqual(item['artifact_version_id'],self.version)
        self.assertEqual(item['locator_key'],{'line':1})
        self.assertEqual(item['candidate_type'],'observation')
        self.assertEqual(item['support_hashes'],[item['source_sha256'],item['content_hash']])
        self.assertEqual(item['value'],2)
        self.assertEqual(self.read('observations',subject_id=self.child)['items'],[])
        items=self.read('negative-searches',subject_id=self.child)['items']
        self.assertEqual([row['id'] for row in items],['child-search'])
        self.assertEqual(items[0]['coverage'],{'complete':False,'excluded':['other files']})
        self.assertEqual(items[0]['created_by'],self.actor)

    def test_pagination_default_binding_shape_and_stale_snapshot(self):
        for index in range(21):self.negative(self.version,f'search-{index:02d}')
        first=self.read('negative-searches',subject_id=self.version)
        self.assertEqual(len(first['items']),20)
        self.assertEqual(first['subject_id'],self.version)
        second=self.read('negative-searches',subject_id=self.version,cursor=first['next_cursor'])
        self.assertEqual(len(second['items']),1)
        self.assertIsNone(second['next_cursor'])
        cursor=first['next_cursor'];state=_decode(cursor)
        for resource,subject,limit in (('observations',self.version,None),('negative-searches',self.child,None),('negative-searches',self.version,1)):
            self.error(400,self.read,resource,subject_id=subject,limit=limit,cursor=cursor)
        for change in ({'extra':True},{'offset':-1},{'offset':True},{'offset':'20'}):
            bad={**state,**change}
            self.error(400,self.read,'negative-searches',subject_id=self.version,cursor=_encode(bad))
        bad=dict(state);bad.pop('offset')
        self.error(400,self.read,'negative-searches',subject_id=self.version,cursor=_encode(bad))
        for limit in (0,101,True,'20'):
            self.error(400,self.read,'negative-searches',subject_id=self.version,limit=limit)
        for options in ({'key':self.version,'subject_id':self.version},{'subject_id':self.version,'unknown':1},{}, {'subject_id':'missing'}):
            self.error(404 if options.get('subject_id')=='missing' else 400,self.read,'negative-searches',**options)
        self.negative(self.version,'new-search')
        error=self.error(409,self.read,'negative-searches',subject_id=self.version,cursor=cursor)
        self.assertTrue(error.restart_required)


if __name__ == '__main__':
    unittest.main()


