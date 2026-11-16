"""Authenticated task sections expose only the selected project and task."""
import unittest
import uuid
from urllib.parse import urlencode
from tests import test_workspace_shared_http as fixtures


class TaskSectionHttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedHttpTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture.project
        self.store = self.fixture.store
        self.write([{'op': 'create', 'alias': 'parent', 'record': {'title': 'Parent', 'status': 'ready'}}])
        self.task = self.store.read_v1('context', self.project, key='parent')['context']['task']['id']
        self.write([{'op': 'create', 'alias': 'child-' + str(i), 'parent_id': self.task,
                     'record': {'title': 'Child ' + str(i), 'status': 'ready'}} for i in range(3)])

    def write(self, operations):
        code, result = self.fixture.request('/v1/transactions', self.fixture.member_token, {
            'project_id': self.project, 'actor': {'principal_id': self.fixture.member_id},
            'request_id': str(uuid.uuid4()), 'expected_revision': self.store.snapshot()['revision'],
            'operations': operations})
        self.assertEqual(code, 200, result)

    def read(self, resource, token=True, **params):
        query = {'project_id': self.project, 'task_id': self.task, **params}
        return self.fixture.request('/v1/' + resource + '?' + urlencode(query),
                                    self.fixture.member_token if token else None)

    def test_scoped_resources_require_auth_and_exact_task(self):
        before = self.store.snapshot()['revision']
        for resource in ('task-children', 'task-findings', 'task-questions'):
            self.assertEqual(self.read(resource, token=False)[0], 401)
            self.assertEqual(self.read(resource, project_id=self.fixture.other)[0], 403)
            code, page = self.read(resource)
            self.assertEqual(code, 200, page)
            self.assertEqual(page['task_id'], self.task)
            self.assertEqual(page['project_id'], self.project)
            self.assertIn(self.task, page['task_scope_ids'])
            self.assertEqual(self.read(resource, task_id=str(uuid.uuid4()))[0], 404)
            self.assertEqual(self.read(resource, task_id='parent')[0], 404)
            self.assertEqual(self.read(resource, order='desc')[0], 400)
            self.assertEqual(self.fixture.request('/v1/' + resource + '?project_id=' + self.project,
                                                 self.fixture.member_token)[0], 400)
        self.assertEqual(self.store.snapshot()['revision'], before)

    def test_pagination_binds_scope_limit_and_revision(self):
        code, first = self.read('task-children', limit=2)
        self.assertEqual(code, 200, first)
        self.assertEqual(len(first['items']), 2)
        cursor = first['next_cursor']
        self.assertTrue(cursor)
        code, second = self.read('task-children', limit=2, cursor=cursor)
        self.assertEqual(code, 200, second)
        self.assertEqual(len(second['items']), 1)
        self.assertFalse(set(x['id'] for x in first['items']) & set(x['id'] for x in second['items']))
        self.assertEqual(self.read('task-questions', limit=2, cursor=cursor)[0], 400)
        self.assertEqual(self.read('task-children', limit=1, cursor=cursor)[0], 400)
        self.write([{'op': 'journal', 'task_id': 'parent', 'kind': 'observation', 'text': 'New revision'}])
        code, stale = self.read('task-children', limit=2, cursor=cursor)
        self.assertEqual(code, 409, stale)
        self.assertTrue(stale['restart_required'])


if __name__ == '__main__':
    unittest.main()
