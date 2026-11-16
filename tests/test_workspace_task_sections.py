"""Task workflow sections return exact, bounded current task context."""
import unittest
import uuid

from tests import test_workspace_findings as finding_fixtures
from tools.roadmap_store import StoreError


class TaskSectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = finding_fixtures.FindingsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.project = self.fixture.project

    def create_task(self, alias, parent=None):
        self.fixture.write({
            'op': 'create',
            'alias': alias,
            'parent_id': parent,
            'record': {'title': alias, 'summary': 'Summary ' + alias,
                       'status': 'ready', 'acceptance': ['Synthetic gate']},
        })
        return self.store.read_v1('context', self.project, key=alias)['context']['task']

    def read(self, resource, task_id, **options):
        from tools import workspace_task_sections
        with self.store._connection() as db:
            return workspace_task_sections.read(
                db, resource, self.project, task_id=task_id, **options)

    def test_children_include_archived_required_children_and_real_aliases(self):
        parent = self.create_task('parent')
        child = self.create_task('child', parent['id'])
        parent = self.store.read_v1('context', self.project, key='parent')['context']['task']
        self.fixture.write({
            'op': 'archive_task', 'task_id': child['id'],
            'expected_record_revision': child['record_revision'],
            'expected_parents': {parent['id']: parent['structure_revision']},
            'rationale': 'Keep the archived child visible to its parent',
        })
        before = self.store.snapshot()['revision']
        result = self.read('task-children', parent['id'])
        self.assertEqual(result['task_id'], parent['id'])
        self.assertEqual(result['task_scope_ids'], [parent['id']])
        self.assertEqual(len(result['items']), 1)
        row = result['items'][0]
        self.assertEqual(row['id'], child['id'])
        self.assertEqual(row['title'], 'child')
        self.assertEqual(row['summary'], 'Summary child')
        self.assertEqual(row['parent_id'], parent['id'])
        self.assertEqual(row['record_revision'], 2)
        current_child = self.store.read_v1('context', self.project, key=child['id'])['context']['task']
        self.assertEqual(row['structure_revision'], current_child['structure_revision'])
        self.assertTrue(row['archived'])
        self.assertTrue(row['required_for_parent'])
        self.assertIn('child', row['aliases'])
        self.assertEqual(self.store.snapshot()['revision'], before)

    def test_research_sections_follow_merge_lineage_and_keep_current_projections(self):
        survivor = self.create_task('survivor')
        absorbed = self.create_task('absorbed')
        unrelated = self.create_task('unrelated')
        self.fixture.write({
            'op': 'create_question', 'id': 'q',
            'text': 'Question owned by the absorbed task',
            'criteria': {},
            'owner_task_id': absorbed['id'],
        })
        finding = self.fixture.create(
            id='absorbed-finding',
            scope={'identifier': 'speed', 'namespace': 'class', 'scope_key': 'Ball',
                   'artifact_version_id': self.fixture.version, 'task_id': absorbed['id']},
        )
        self.fixture.create(
            id='unrelated-finding',
            scope={'identifier': 'speed', 'namespace': 'class', 'scope_key': 'Ball',
                   'artifact_version_id': self.fixture.version, 'task_id': unrelated['id']},
        )
        preview = self.store.merge_preview(self.project, survivor['id'], absorbed['id'])['preview']
        self.fixture.write({
            'op': 'merge_tasks', 'task_id': survivor['id'],
            'expected_record_revision': survivor['record_revision'],
            'rationale': 'Test lineage sections',
            'absorbed_id': absorbed['id'], 'preview_guard': preview['guard_digest'],
            'choices': {key: 'survivor' for key in preview['field_conflicts']},
            'metadata_choices': {key: 'survivor' for key in preview['metadata_conflicts']},
            'source_references': ['Synthetic fixture only'],
        })
        current_finding = self.fixture.finding('absorbed-finding')
        self.fixture.write({
            'op': 'revise_finding', 'finding_id': 'absorbed-finding',
            'expected_record_revision': current_finding['record_revision'],
            'fields': {'claim': 'Current claim after task merge'},
            'rationale': 'Check current task section projection',
        })
        self.fixture.write({
            'op': 'set_question_state', 'question_id': 'q',
            'expected_record_revision': 1, 'state': 'investigating',
            'rationale': 'Check current owner task projection',
        })
        before = self.store.snapshot()['revision']
        findings = self.read('task-findings', survivor['id'])
        questions = self.read('task-questions', survivor['id'])
        self.assertEqual(set(findings['task_scope_ids']), {survivor['id'], absorbed['id']})
        self.assertEqual([row['id'] for row in findings['items']], [finding['id']])
        self.assertEqual(findings['items'][0]['claim'], 'Current claim after task merge')
        self.assertEqual(findings['items'][0]['record_revision'], 2)
        self.assertEqual(findings['items'][0]['scope']['task_id'], absorbed['id'])
        self.assertEqual(findings['items'][0]['evidence_review_state'], 'unreviewed')
        self.assertEqual([row['id'] for row in questions['items']], ['q'])
        self.assertEqual(questions['items'][0]['text'], 'Question owned by the absorbed task')
        self.assertEqual(questions['items'][0]['state'], 'investigating')
        self.assertNotIn('unrelated-finding', {row['id'] for row in findings['items']})
        self.assertEqual(self.store.snapshot()['revision'], before)

    def test_pages_are_bound_to_resource_project_task_limit_and_snapshot(self):
        task = self.create_task('paged')
        operations = []
        for index in range(25):
            operations.append({
                'op': 'create_finding', 'id': f'paged-{index:02d}',
                'claim': f'Claim {index}',
                'scope': {'identifier': 'speed', 'namespace': 'class', 'scope_key': 'Ball',
                          'artifact_version_id': self.fixture.version, 'task_id': task['id']},
                'classification': 'source-supported',
                'uncertainty': 'Synthetic page fixture',
                'evidence': [self.fixture.support()],
            })
        self.fixture.write(*operations)
        other = self.create_task('other')
        first = self.read('task-findings', task['id'], limit=20)
        self.assertEqual(len(first['items']), 20)
        self.assertIsNotNone(first['next_cursor'])
        second = self.read('task-findings', task['id'], limit=20, cursor=first['next_cursor'])
        self.assertEqual(len(second['items']), 5)
        self.assertIsNone(second['next_cursor'])
        with self.assertRaises(StoreError) as error:
            self.read('task-questions', other['id'], limit=20, cursor=first['next_cursor'])
        self.assertEqual(error.exception.status, 400)
        with self.assertRaises(StoreError) as error:
            self.read('task-findings', task['id'], limit=10, cursor=first['next_cursor'])
        self.assertEqual(error.exception.status, 400)
        before = self.store.snapshot()['revision']
        self.fixture.write({'op': 'create_question', 'id': 'advance-snapshot',
                            'text': 'Advance global snapshot', 'criteria': {}})
        with self.assertRaises(StoreError) as error:
            self.read('task-findings', task['id'], limit=20, cursor=first['next_cursor'])
        self.assertEqual(error.exception.status, 409)
        self.assertNotEqual(self.store.snapshot()['revision'], before)

    def test_task_identity_is_exact_uuid_and_rejects_aliases_or_missing_ids(self):
        task = self.create_task('stable-alias')
        other_project = str(uuid.uuid4())
        self.fixture.write({'op': 'create_project', 'id': other_project, 'slug': 'foreign-task-section'})
        actor = self.store.read_v1('capabilities', other_project)['capabilities']['local_principal_id']
        self.store.transact_v1({
            'project_id': other_project,
            'actor': {'principal_id': actor},
            'request_id': str(uuid.uuid4()),
            'expected_revision': self.store.snapshot()['revision'],
            'operations': [{'op': 'create', 'alias': 'foreign',
                           'record': {'title': 'Foreign', 'status': 'ready'}}],
        })
        foreign = self.store.read_v1('context', other_project, key='foreign')['context']['task']
        for identity in ('stable-alias', 'absent-task', foreign['id']):
            with self.subTest(identity=identity):
                with self.assertRaises(StoreError) as error:
                    self.read('task-children', identity)
                self.assertEqual(error.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
