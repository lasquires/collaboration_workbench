"""Selected-task scope metadata is stored state, never inferred editor defaults."""
import unittest
from tests import test_workspace_structure as structure_fixture

class TaskContextMetadataTests(unittest.TestCase):
 def test_scope_and_creator_projection_track_guarded_changes_without_read_writes(self):
  fixture=structure_fixture.StructureTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  task=fixture.create('context-scope')
  fixture.write(fixture.guarded('set_task_scope','context-scope',decomposition_complete=True))
  before=fixture.store.snapshot()['revision']
  entity=fixture.store.read_v1('context',fixture.project,key=task['id'])['context']['task']
  self.assertTrue(entity['decomposition_complete']);self.assertIsNone(entity['area_id'])
  self.assertEqual(entity['created_by'],fixture.principal)
  self.assertEqual(fixture.store.snapshot()['revision'],before)
  fixture.write(fixture.guarded('set_task_scope','context-scope',decomposition_complete=False))
  self.assertFalse(fixture.store.read_v1('context',fixture.project,key=task['id'])['context']['task']['decomposition_complete'])
  # Only this disposable fixture store is seeded directly; task changes use transactions.
  with fixture.store._connection() as db:
   db.execute('INSERT INTO areas(id,project_id,title) VALUES(?,?,?)',('task-area',fixture.project,'Synthetic area'));db.commit()
  fixture.write(fixture.guarded('set_task_scope','context-scope',area_id='task-area'))
  self.assertEqual(fixture.store.read_v1('context',fixture.project,key=task['id'])['context']['task']['area_id'],'task-area')
  fixture.write(fixture.guarded('set_task_scope','context-scope',area_id=None))
  self.assertIsNone(fixture.store.read_v1('context',fixture.project,key=task['id'])['context']['task']['area_id'])


class TaskNavigationHttpTests(unittest.TestCase):
 def test_dashboard_serves_scoped_task_navigation_query(self):
  from tests import test_workspace_shared_http as http_fixture
  import urllib.request
  fixture=http_fixture.SharedHttpTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  url=fixture.base+'/?panel=task-workflows&project_id='+fixture.project+'&task_id=11111111-1111-4111-8111-111111111111'
  with urllib.request.urlopen(url) as response:
   self.assertEqual(response.status,200)
   self.assertIn(b'id="taskWorkspace"',response.read())
