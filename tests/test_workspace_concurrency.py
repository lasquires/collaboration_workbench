import json
import tempfile
import unittest
import uuid
from pathlib import Path
from tools.roadmap_store import RoadmapStore,StoreError


class RecordConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=RoadmapStore(Path(self.temp.name)/'state.db')
        self.project=self.store.read_v1('projects')['items'][0]['id']
        self.actor=self.store.read_v1('capabilities',self.project)['capabilities']['local_principal_id']
        self.global_write([{'op':'create','alias':'ROOT','record':{'title':'Root','status':'ready'}},
            *[{'op':'create','alias':name,'parent_id':'ROOT','record':{'title':name,'status':'ready'}} for name in ('A','B')],
            {'op':'create','alias':'X','record':{'title':'Island','status':'ready'}}])

    def global_write(self,operations):
        return self.store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.actor},'request_id':str(uuid.uuid4()),'expected_revision':self.store.snapshot()['revision'],'operations':operations})

    def task(self,alias):return self.store.read_v1('context',self.project,key=alias)['context']['task']
    def guard(self,alias,structure=False):
        task=self.task(alias)
        return {'type':'task_structure' if structure else 'task','id':task['id'],'revision':task['structure_revision' if structure else 'record_revision']}
    def envelope(self,operations,guards):
        return {'project_id':self.project,'actor':{'principal_id':self.actor},'request_id':str(uuid.uuid4()),'concurrency_mode':'records','expected_records':guards,'operations':operations}

    def test_unrelated_edits_coexist_same_record_conflicts_and_exact_replay_survives(self):
        a=self.guard('A');b=self.guard('B')
        first=self.envelope([{'op':'update','task_id':'A','fields':{'summary':'First worker'}}],[a])
        result=self.store.transact_v1(first)
        self.store.transact_v1(self.envelope([{'op':'update','task_id':'B','fields':{'summary':'Second worker'}}],[b]))
        self.assertEqual(self.task('A')['summary'],'First worker');self.assertEqual(self.task('B')['summary'],'Second worker')
        self.assertEqual(self.store.transact_v1(first),result)
        with self.assertRaises(StoreError) as error:
            self.store.transact_v1(self.envelope([{'op':'update','task_id':'A','fields':{'summary':'Stale replacement'}}],[a]))
        self.assertEqual(error.exception.status,409)
        changed=json.loads(json.dumps(first));changed['operations'][0]['fields']['summary']='Different payload'
        with self.assertRaises(StoreError):self.store.transact_v1(changed)

    def test_modes_missing_guards_and_unknown_operations_fail_closed(self):
        operation={'op':'update','task_id':'A','fields':{'summary':'Changed'}}
        for payload in (self.envelope([operation],[]),{**self.envelope([operation],[self.guard('A')]),'expected_revision':self.store.snapshot()['revision']},self.envelope([{'op':'process_jobs'}],[])):
            before=self.store.snapshot()
            with self.assertRaises(StoreError) as error:self.store.transact_v1(payload)
            self.assertEqual(error.exception.status,400);self.assertEqual(before,self.store.snapshot())
        plain={'project_id':self.project,'actor':{'principal_id':self.actor},'request_id':str(uuid.uuid4()),'expected_records':[self.guard('A')],'operations':[operation]}
        with self.assertRaises(StoreError):self.store.transact_v1(plain)

    def test_stale_ancestor_scope_blocks_move_but_unrelated_text_does_not(self):
        task=self.task('A');root=self.task('ROOT');target=self.task('X')
        operation={'op':'move_task','task_id':task['id'],'parent_id':target['id'],'expected_record_revision':task['record_revision'],'expected_parents':{root['id']:root['structure_revision'],target['id']:target['structure_revision']},'rationale':'Move scoped work'}
        guards=[self.guard('A'),self.guard('ROOT',True),self.guard('X',True)]
        move=self.envelope([operation],guards)
        self.store.transact_v1(self.envelope([{'op':'update','task_id':'B','fields':{'summary':'Unrelated text'}}],[self.guard('B')]))
        self.store.transact_v1(move)
        self.assertEqual(self.task('A')['parent_id'],target['id'])
        # A later ancestor acceptance edit changes the aggregate scope guard.
        task=self.task('B');root=self.task('ROOT');target=self.task('X')
        operation={**operation,'task_id':task['id'],'expected_record_revision':task['record_revision'],'expected_parents':{root['id']:root['structure_revision'],target['id']:target['structure_revision']}}
        stale=self.envelope([operation],[self.guard('B'),self.guard('ROOT',True),self.guard('X',True)])
        self.store.transact_v1(self.envelope([{'op':'update','task_id':'ROOT','fields':{'acceptance':['Changed parent gate']}}],[self.guard('ROOT')]))
        before=self.store.snapshot()
        with self.assertRaises(StoreError) as error:self.store.transact_v1(stale)
        self.assertEqual(error.exception.status,409);self.assertEqual(before,self.store.snapshot())

    def test_missing_parent_guard_cannot_be_replaced_by_operation_specific_guard(self):
        task=self.task('A');root=self.task('ROOT');target=self.task('X')
        op={'op':'move_task','task_id':task['id'],'parent_id':target['id'],'expected_record_revision':task['record_revision'],'expected_parents':{root['id']:root['structure_revision'],target['id']:target['structure_revision']},'rationale':'Move'}
        with self.assertRaises(StoreError) as error:self.store.transact_v1(self.envelope([op],[self.guard('A')]))
        self.assertEqual(error.exception.status,400)

    def test_relation_mutation_advances_task_revision_and_stale_overwrite_rejects(self):
        original=self.guard('A');target=self.guard('B')
        self.store.transact_v1(self.envelope([{'op':'add_link','task_id':'A','target_id':'B','kind':'related','rationale':'First evidence'}],[original,target]))
        self.assertGreater(self.guard('A')['revision'],original['revision'])
        with self.assertRaises(StoreError):self.store.transact_v1(self.envelope([{'op':'remove_link','task_id':'A','target_id':'B','kind':'related'}],[original,target]))

    def test_mixed_transaction_rolls_back_and_foreign_guard_is_not_accepted(self):
        guards=[self.guard('A'),self.guard('B')]
        bad=self.envelope([{'op':'update','task_id':'A','fields':{'summary':'Must roll back'}},{'op':'update','task_id':'B','fields':{'status':'invented-state'}}],guards)
        before=self.store.snapshot()
        with self.assertRaises(StoreError):self.store.transact_v1(bad)
        self.assertEqual(self.store.snapshot(),before)
        foreign={**self.guard('A'),'id':str(uuid.uuid4())}
        with self.assertRaises(StoreError) as error:self.store.transact_v1(self.envelope([{'op':'journal','task_id':'A','kind':'observation','text':'No write'}],[foreign]))
        self.assertEqual(error.exception.status,404)

    def test_question_edits_use_scoped_record_revisions(self):
        self.global_write([{'op':'create_question','id':name,'text':'Original '+name} for name in ('q-a','q-b')])
        for name in ('q-a','q-b'):
            self.store.transact_v1(self.envelope([{'op':'revise_question','question_id':name,'expected_record_revision':1,'text':'Edited '+name,'rationale':'Independent revision'}],[{'type':'question','id':name,'revision':1}]))
        self.assertEqual({item['text'] for item in self.store.read_v1('questions',self.project)['items']},{'Edited q-a','Edited q-b'})

    def test_document_proposal_and_principal_feedback_guards(self):
        self.global_write([{'op':'create_document','id':'doc','kind':'guide','title':'Guide','authority_kind':'service_body','body':'Original'}])
        document=self.store.read_v1('documents',self.project)['items'][0];version=document['current_version_id']
        self.store.transact_v1(self.envelope([{'op':'propose_edit','id':'proposal','document_id':'doc','base_version_id':version,'proposed_body':'Reviewed body'}],[{'type':'document','id':'doc','revision':document['revision']}]))
        self.store.transact_v1(self.envelope([{'op':'accept_proposal','proposal_id':'proposal','reason':'Reviewed change'}],[{'type':'document','id':'doc','revision':document['revision']},{'type':'proposal','id':'proposal','revision':1}]))
        feedback=self.envelope([{'op':'set_feedback','document_version_id':version,'value':'helpful','reason':'Read it'}],[{'type':'feedback','id':version,'revision':0}])
        self.store.transact_v1(feedback)
        own=self.store.read_v1('feedback',self.project,document_id='doc',principal_id=self.actor)['items'][0]
        self.assertEqual(own['my_feedback_revision'],1)
        self.assertEqual(own['my_feedback']['value'],'helpful')
        with self.assertRaises(StoreError):self.store.transact_v1({**feedback,'request_id':str(uuid.uuid4())})

    def test_membership_is_rechecked_after_adapter_read_before_write_lock(self):
        original=self.store.transact
        def revoked(*args,**kwargs):
            with self.store._connection() as db:db.execute("UPDATE project_members SET capabilities_json='[]' WHERE project_id=? AND principal_id=?",(self.project,self.actor))
            return original(*args,**kwargs)
        self.store.transact=revoked
        with self.assertRaises(StoreError) as error:
            self.store.transact_v1(self.envelope([{'op':'update','task_id':'A','fields':{'summary':'Unauthorized'}}],[self.guard('A')]))
        self.assertEqual(error.exception.status,403)
        self.assertEqual(self.task('A')['summary'],'')

    def test_simultaneous_independent_and_competing_record_writes(self):
        import threading
        def execute(payloads):
            barrier=threading.Barrier(2);results=[]
            def run(payload):
                try:barrier.wait(timeout=5);self.store.transact_v1(payload);results.append(200)
                except StoreError as error:results.append(error.status)
            threads=[threading.Thread(target=run,args=(payload,)) for payload in payloads]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=10);self.assertFalse(thread.is_alive())
            return sorted(results)
        payloads=[self.envelope([{'op':'update','task_id':name,'fields':{'summary':'Concurrent '+name}}],[self.guard(name)]) for name in ('A','B')]
        self.assertEqual(execute(payloads),[200,200])
        original=self.guard('A')
        payloads=[self.envelope([{'op':'update','task_id':'A','fields':{'summary':text}}],[original]) for text in ('Left','Right')]
        self.assertEqual(execute(payloads),[200,409])

    def test_competing_merges_keep_full_affected_guards(self):
        def prepared(survivor):
            preview=self.store.merge_preview(self.project,survivor,'X')['preview']
            guards=[{'type':kind,'id':identity,'revision':values[field]} for identity,values in preview['guards'].items() for kind,field in (('task','record_revision'),('task_structure','structure_revision'))]
            operation={'op':'merge_tasks','task_id':preview['survivor_id'],'absorbed_id':preview['absorbed_id'],'expected_record_revision':self.task(survivor)['record_revision'],'preview_guard':preview['guard_digest'],'choices':{field:'survivor' for field in preview['field_conflicts']},'metadata_choices':{field:'survivor' for field in preview['metadata_conflicts']},'source_references':['Synthetic workflow evidence'],'rationale':'Review overlapping scope'}
            return self.envelope([operation],guards)
        first=prepared('A');second=prepared('B')
        self.store.transact_v1(first)
        before=self.store.snapshot()
        with self.assertRaises(StoreError) as error:self.store.transact_v1(second)
        self.assertEqual(error.exception.status,409);self.assertEqual(before,self.store.snapshot())

    def test_legacy_global_mode_still_rejects_stale_unrelated_edit(self):
        revision=self.store.snapshot()['revision']
        self.store.transact_v1(self.envelope([{'op':'update','task_id':'A','fields':{'summary':'Record mode'}}],[self.guard('A')]))
        with self.assertRaises(StoreError) as error:
            self.store.transact(revision,'legacy-stale','Legacy client',[{'op':'update','task_id':'B','fields':{'summary':'Stale global'}}])
        self.assertEqual(error.exception.status,409)

    def test_typed_task_owned_state_advances_its_record_guard(self):
        before=self.task('A')
        payload=self.envelope([{'op':'record_blocker','task_id':before['id'],'expected_record_revision':before['record_revision'],'kind':'review','rationale':'Needs explicit review'}],[self.guard('A')])
        self.store.transact_v1(payload)
        self.assertGreater(self.task('A')['record_revision'],before['record_revision'])
        blocker=self.store.read_v1('blockers',self.project,task_id=before['id'])['items'][0]
        current=self.task('A');guard=self.guard('A')
        operation={'op':'resolve_blocker','task_id':current['id'],'expected_record_revision':current['record_revision'],'blocker_id':blocker['id'],'rationale':'Reviewed resolution'}
        self.store.transact_v1(self.envelope([operation],[guard]))
        self.assertGreater(self.task('A')['record_revision'],guard['revision'])
        with self.assertRaises(StoreError):self.store.transact_v1(self.envelope([{**operation,'rationale':'Stale replacement reason'}],[guard]))

    def test_existing_foreign_task_guard_and_malformed_guards_reject(self):
        other=str(uuid.uuid4());self.global_write([{'op':'create_project','id':other,'slug':'foreign'}])
        actor=self.store.read_v1('capabilities',other)['capabilities']['local_principal_id']
        self.store.transact_v1({'project_id':other,'actor':{'principal_id':actor},'request_id':str(uuid.uuid4()),'expected_revision':self.store.snapshot()['revision'],'operations':[{'op':'create','alias':'FOREIGN','record':{'title':'Foreign','status':'ready'}}]})
        foreign=self.store.read_v1('context',other,key='FOREIGN')['context']['task']
        payload=self.envelope([{'op':'journal','task_id':'A','kind':'observation','text':'No mutation'}],[{'type':'task','id':foreign['id'],'revision':foreign['record_revision']}])
        with self.assertRaises(StoreError) as error:self.store.transact_v1(payload)
        self.assertEqual(error.exception.status,404)
        for guards in ([{**self.guard('A'),'revision':True}],[self.guard('A'),self.guard('A')],[{**self.guard('A'),'type':'unknown'}]):
            with self.assertRaises(StoreError) as error:self.store.transact_v1(self.envelope([{'op':'journal','task_id':'A','kind':'review','text':'Reject malformed guards'}],guards))
            self.assertEqual(error.exception.status,400)

    def test_imported_content_cannot_reuse_or_rewind_a_local_record_guard(self):
        from tools import roadmap_exchange as exchange
        before=self.guard('A')
        incoming=self.store.exchange_preview(self.project,portable=True)['bundle']
        row=next(row for row in incoming['records']['tasks'] if row['uid']==before['id'])
        record=json.loads(row['record_json']);record['summary']='Imported progress with same remote counter';row['record_json']=exchange.canonical(record)
        incoming['id']=exchange.research.digest({key:value for key,value in incoming.items() if key!='id'})
        preview=self.store.exchange_preview(self.project,bundle=incoming)['preview']
        choices={exchange.canonical([change['table'],change['key'],field]):'remote' for change in preview['changes'] for field,value in change['fields'].items() if value['conflict']}
        self.global_write([{'op':'import_bundle','bundle':incoming,'choices':choices}])
        self.assertGreater(self.guard('A')['revision'],before['revision'])
        with self.assertRaises(StoreError):self.store.transact_v1(self.envelope([{'op':'update','task_id':'A','fields':{'summary':'Stale overwrite'}}],[before]))
