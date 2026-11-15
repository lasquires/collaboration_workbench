"""Direct research context reads preserve project scope and current projections."""
import unittest
import uuid
from tests import test_workspace_findings as findings_fixture
from tests import test_workspace_shared_http as http_fixture
from tools.roadmap_store import StoreError


class ResearchContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture=findings_fixture.FindingsTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.store=self.fixture.store;self.project=self.fixture.project
        self.finding=self.fixture.create(id='same-finding');self.fixture.question()

    def test_finding_context_matches_current_projection_without_history_embedding(self):
        self.fixture.write({'op':'revise_finding','finding_id':self.finding['id'],'expected_record_revision':1,'fields':{'uncertainty':'Clarified limitation'},'rationale':'Editorial clarification'})
        before=self.store.snapshot()['revision'];result=self.store.read_v1('finding-context',self.project,key='same-finding')
        context=result['context'];entity=context['entity']
        self.assertEqual(context['entity_type'],'finding');self.assertEqual(entity,self.fixture.finding('same-finding'))
        self.assertEqual(entity['record_revision'],2);self.assertEqual(entity['evidence_origin_id'],self.finding['revision_id'])
        self.assertEqual(set(context),{'entity_type','entity','related'});self.assertIsNone(result['next_cursor'])
        self.assertEqual(context['related']['evidence'],{'resource':'evidence-links','params':{'project_id':self.project,'subject_type':'finding_revision','subject_id':self.finding['revision_id']}})
        for section in context['related'].values():
            self.assertNotIn('items',section);self.assertEqual(section['params']['project_id'],self.project)
        self.assertEqual(self.store.snapshot()['revision'],before);self.assertEqual(result['revision'],before)

    def test_question_context_tracks_state_and_uses_explicit_related_question_identity(self):
        question=self.store.read_v1('questions',self.project)['items'][0]
        self.fixture.write({'op':'set_question_state','question_id':'q','expected_record_revision':question['record_revision'],'state':'investigating','rationale':'Begin investigation'})
        result=self.store.read_v1('question-context',self.project,key='q');context=result['context']
        self.assertEqual(context['entity_type'],'question');self.assertEqual(context['entity']['state'],'investigating')
        self.assertEqual(context['entity']['record_revision'],2)
        self.assertEqual(context['related']['answers'],{'resource':'answers','params':{'project_id':self.project,'subject_id':'q'}})
        self.assertEqual(context['related']['resolutions']['resource'],'question-resolutions')

    def test_same_ids_in_other_project_cannot_change_or_leak_selected_context(self):
        other=str(uuid.uuid4());self.fixture.write({'op':'create_project','id':other,'slug':'other'})
        actor=self.store.read_v1('capabilities',other)['capabilities']['local_principal_id']
        self.store.transact_v1({'project_id':other,'actor':{'principal_id':actor},'request_id':str(uuid.uuid4()),'expected_revision':self.store.snapshot()['revision'],'operations':[{'op':'create_finding','id':'same-finding','claim':'Other project private claim'},{'op':'create_question','id':'q','text':'Other project question'}]})
        self.assertEqual(self.store.read_v1('finding-context',self.project,key='same-finding')['context']['entity']['claim'],'Ball speed is configured here')
        self.assertEqual(self.store.read_v1('finding-context',other,key='same-finding')['context']['entity']['claim'],'Other project private claim')
        self.assertEqual(self.store.read_v1('question-context',self.project,key='q')['context']['entity']['text'],'Where is speed configured?')

    def test_archived_findings_remain_addressable_for_history_and_restore(self):
        self.fixture.write({'op':'archive_finding','finding_id':self.finding['id'],'expected_record_revision':1,'rationale':'Archive synthetic finding'})
        archived=self.store.read_v1('finding-context',self.project,key=self.finding['id'])['context']
        self.assertIsNotNone(archived['entity']['archived_at']);self.assertEqual(archived['entity']['record_revision'],2)
        self.assertEqual(archived['related']['revisions']['params']['subject_id'],self.finding['id'])
        self.fixture.write({'op':'restore_finding','finding_id':self.finding['id'],'expected_record_revision':2,'rationale':'Restore synthetic finding'})
        restored=self.store.read_v1('finding-context',self.project,key=self.finding['id'])['context']['entity']
        self.assertIsNone(restored['archived_at']);self.assertEqual(restored['record_revision'],3)

    def test_question_context_reflects_resolution_and_support_withdrawal(self):
        from tests import test_workspace_answers as answers_fixture
        fixture=answers_fixture.AnswersTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        answer=fixture.propose()['id'];fixture.review(answer);fixture.resolve(answer)
        resolved=fixture.store.read_v1('question-context',fixture.project,key='q')['context']['entity']
        self.assertEqual(resolved['state'],'resolved');self.assertEqual(resolved['resolution']['answer_id'],answer)
        fixture.write({'op':'set_evidence_state','evidence_link_id':fixture.link['id'],'expected_state':'available','state':'retracted','reason':'Synthetic support withdrawn'})
        reopened=fixture.store.read_v1('question-context',fixture.project,key='q')['context']['entity']
        self.assertEqual(reopened['state'],'investigating');self.assertFalse(reopened['resolution']['active'])
        self.assertGreater(reopened['record_revision'],resolved['record_revision'])

    def test_missing_context_and_unsupported_options_fail_closed(self):
        for kind in ('finding-context','question-context'):
            with self.assertRaises(StoreError) as error:self.store.read_v1(kind,self.project,key='absent')
            self.assertEqual(error.exception.status,404)
            for options in ({'limit':20},{'cursor':'unused'},{'subject_id':'q'}):
                with self.assertRaises(StoreError) as error:self.store.read_v1(kind,self.project,key='q',**options)
                self.assertEqual(error.exception.status,400)


class AnswerContextTests(unittest.TestCase):
    def setUp(self):
        from tests import test_workspace_answers as answers_fixture
        self.fixture=answers_fixture.AnswersTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.store=self.fixture.store;self.project=self.fixture.project
        support=[{**self.fixture.fixture.support(),'locator':{'line':index+1,'member':'Ball.speed'}} for index in range(23)]
        finding=self.fixture.fixture.create(id='many-sources',evidence=support)
        self.links=self.store.read_v1('evidence-links',self.project,subject_id=finding['revision_id'])['items']
        self.answer=self.fixture.propose(id='pinned-answer',evidence_link_ids=[row['id'] for row in self.links])['id']

    def test_answer_context_pins_question_and_pages_exact_reused_support(self):
        before=self.store.snapshot()['revision'];context=self.store.read_v1('answer-context',self.project,key=self.answer)['context']
        self.assertEqual(context['entity_type'],'answer');self.assertEqual(context['entity'],self.fixture.answer(self.answer))
        self.assertEqual(context['question']['id'],'q');self.assertEqual(context['question']['record_revision'],self.fixture.question()['record_revision'])
        descriptor=context['related']['support'];self.assertEqual(descriptor,{'resource':'answer-support','params':{'project_id':self.project,'subject_id':self.answer}})
        first=self.store.read_v1('answer-support',self.project,subject_id=self.answer,limit=20)
        second=self.store.read_v1('answer-support',self.project,subject_id=self.answer,limit=20,cursor=first['next_cursor'])
        self.assertEqual(len(first['items']),20);self.assertEqual(len(second['items']),3);self.assertIsNone(second['next_cursor'])
        rows=first['items']+second['items'];self.assertEqual({row['id'] for row in rows},{row['id'] for row in self.links})
        self.assertNotIn(self.fixture.link['id'],{row['id'] for row in rows})
        version=self.store.read_v1('artifact-versions',self.project)['items'][0]
        self.assertTrue(all(row['source_sha256']==version['sha256'] and row['local_bytes_available'] for row in rows))
        self.assertTrue(all(row['subject_type']=='finding_revision' for row in rows))
        self.assertEqual(self.store.snapshot()['revision'],before)

    def test_support_cursor_expires_and_withdrawn_reference_remains_visible(self):
        first=self.store.read_v1('answer-support',self.project,subject_id=self.answer,limit=20)
        withdrawn=self.links[0]['id'];self.fixture.write({'op':'set_evidence_state','evidence_link_id':withdrawn,'expected_state':'available','state':'retracted','reason':'Review the pinned witness'})
        with self.assertRaises(StoreError) as error:self.store.read_v1('answer-support',self.project,subject_id=self.answer,limit=20,cursor=first['next_cursor'])
        self.assertEqual(error.exception.status,409)
        fresh=self.store.read_v1('answer-support',self.project,subject_id=self.answer,limit=100)
        self.assertEqual(next(row for row in fresh['items'] if row['id']==withdrawn)['state'],'retracted')
        context=self.store.read_v1('answer-context',self.project,key=self.answer)['context']
        self.assertEqual(context['entity']['review_state'],'stale')

    def test_support_cursor_and_context_reject_other_identity_filters(self):
        first=self.store.read_v1('answer-support',self.project,subject_id=self.answer,limit=20)
        another=self.fixture.propose(id='another-answer')['id']
        # Reread after the new proposal so only the filter differs, not snapshot.
        first=self.store.read_v1('answer-support',self.project,subject_id=self.answer,limit=20)
        with self.assertRaises(StoreError) as error:self.store.read_v1('answer-support',self.project,subject_id=another,limit=20,cursor=first['next_cursor'])
        self.assertEqual(error.exception.status,400)
        with self.assertRaises(StoreError) as error:self.store.read_v1('answer-support',self.project,subject_id='absent',limit=20)
        self.assertEqual(error.exception.status,404)
        with self.assertRaises(StoreError) as error:self.store.read_v1('answer-context',self.project,key=self.answer,limit=20)
        self.assertEqual(error.exception.status,400)


class ResearchContextHttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture=http_fixture.SharedHttpTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        f.store.transact_v1({'project_id':f.project,'actor':{'principal_id':f.member_id},'request_id':str(uuid.uuid4()),'expected_revision':f.store.snapshot()['revision'],'operations':[{'op':'create_finding','id':'http-finding','claim':'HTTP scoped finding'},{'op':'create_question','id':'http-question','text':'HTTP question'}]})

    def test_direct_routes_require_project_membership_and_reject_list_parameters(self):
        f=self.fixture
        for resource,identity in (('findings','http-finding'),('questions','http-question')):
            path='/v1/'+resource+'/'+identity+'/context?project_id='+f.project
            self.assertEqual(f.request(path)[0],401)
            code,page=f.request(path,f.member_token);self.assertEqual(code,200,page)
            self.assertEqual(page['context']['entity']['id'],identity);self.assertEqual(page['project_id'],f.project)
            self.assertEqual(f.request(path+'&limit=20',f.member_token)[0],400)
            foreign=path.replace(f.project,f.other)
            self.assertEqual(f.request(foreign,f.member_token)[0],403)
            self.assertEqual(f.request(foreign,f.owner_token)[0],404)
            missing=path.replace(identity,'absent')
            self.assertEqual(f.request(missing,f.member_token)[0],404)


    def test_answer_context_and_support_http_scope(self):
        from pathlib import Path
        f=self.fixture;root=Path(f.temp.name);(root/'source.txt').write_text('Pinned fixture source',encoding='utf-8')
        owner=f.store.read_v1('capabilities',f.project)['capabilities']['local_principal_id']
        f.store.transact_v1({'project_id':f.project,'actor':{'principal_id':owner},'request_id':str(uuid.uuid4()),'expected_revision':f.store.snapshot()['revision'],'operations':[{'op':'register_root','id':'http-root','path':str(root)},{'op':'register_artifact','id':'http-source','root_id':'http-root','relative_path':'source.txt','kind':'source'},{'op':'register_artifact_version','artifact_id':'http-source'}]})
        version=f.store.read_v1('artifact-versions',f.project)['items'][0]['id']
        f.store.transact_v1({'project_id':f.project,'actor':{'principal_id':f.member_id},'request_id':str(uuid.uuid4()),'expected_revision':f.store.snapshot()['revision'],'operations':[{'op':'create_answer','id':'http-answer','question_id':'http-question','expected_question_revision':1,'claim':'Pinned HTTP source','evidence':[{'artifact_version_id':version,'locator':{'line':1},'method':'Read synthetic source','limitations':'Not native evidence'}]}]})
        for path in ('/v1/answers/http-answer/context?project_id=','/v1/answer-support?subject_id=http-answer&limit=20&project_id='):
            code,result=f.request(path+f.project,f.member_token);self.assertEqual(code,200,result)
            self.assertEqual(result['project_id'],f.project)
            self.assertEqual(f.request(path+f.other,f.member_token)[0],403)
            self.assertEqual(f.request(path+f.other,f.owner_token)[0],404)
        code,context=f.request('/v1/answers/http-answer/context?project_id='+f.project,f.member_token)
        self.assertEqual(context['context']['question']['id'],'http-question')
