"""Real clone conflict review, retained history and source checkout isolation."""
import json
import subprocess
import unittest
import uuid
from tests import test_workspace_sync as fixtures
from tools.workspace_sync import SyncConflict


class GitRoundtripAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WorkspaceSyncTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.root
        self.project = self.f.project
        with self.f.stores[1]._connection() as db:
            db.execute('UPDATE store_meta SET store_id=?', (str(uuid.uuid4()),))
        seed = self.root / 'seed'
        seed.mkdir()
        self.git(seed, 'init', '-q', '-b', 'main')
        (seed / 'source.txt').write_text('committed source', encoding='utf-8')
        self.git(seed, 'add', 'source.txt')
        self.git(seed, 'commit', '-qm', 'Synthetic source')
        self.git(seed, 'remote', 'add', 'origin', str(self.f.remote))
        self.git(seed, 'push', '-q', 'origin', 'main')
        self.clones = []
        for name in ('left-source', 'right-source'):
            clone = self.root / name
            self.git(self.root, 'clone', '-q', '-b', 'main', str(self.f.remote), str(clone))
            (clone / 'source.txt').write_text('staged source ' + name, encoding='utf-8')
            self.git(clone, 'add', 'source.txt')
            (clone / 'source.txt').write_text('unstaged source ' + name, encoding='utf-8')
            self.clones.append(clone)

    def git(self, cwd, *args):
        return subprocess.run(['git', '-c', 'user.name=Acceptance fixture',
                               '-c', 'user.email=fixture@example.invalid', *args],
                              cwd=cwd, check=True, capture_output=True).stdout

    def write(self, index, *operations):
        store = self.f.stores[index]
        return store.transact_v1({'project_id': self.project,
            'actor': {'principal_id': self.f.actor}, 'request_id': str(uuid.uuid4()),
            'expected_revision': store.snapshot()['revision'], 'operations': list(operations)})

    def checkout_state(self, clone):
        return (self.git(clone, 'symbolic-ref', 'HEAD'), self.git(clone, 'rev-parse', 'HEAD'),
                self.git(clone, 'status', '--porcelain'), (clone / '.git/index').read_bytes(),
                (clone / 'source.txt').read_bytes())

    def test_reviewed_conflict_converges_without_source_or_history_loss(self):
        self.write(0, {'op': 'create', 'alias': 'shared-task',
                       'record': {'title': 'Common task', 'status': 'ready'}},
                   {'op': 'journal', 'task_id': 'shared-task', 'kind': 'observation',
                    'text': 'Full shared journal text from the common task',
                    'evidence': ['docs/synthetic-evidence.md']})
        left, right = self.f.syncs
        left.sync_now()
        with self.assertRaises(SyncConflict):
            right.sync_now()
        right.bootstrap_remote({})
        right.sync_now()
        before = [self.checkout_state(c) for c in self.clones]
        source_tip = self.git(self.root, '--git-dir', str(self.f.remote), 'rev-parse', 'refs/heads/main')
        self.write(0, {'op': 'update', 'task_id': 'shared-task', 'fields': {'title': 'Left authored title'}})
        self.write(1, {'op': 'update', 'task_id': 'shared-task', 'fields': {'title': 'Right authored title'}})
        left.sync_now()
        with self.assertRaises(SyncConflict):
            right.sync_now()
        review = right.preview_remote()
        conflicts = [(change, field, values) for change in review['preview']['changes']
                     for field, values in change['fields'].items() if values['conflict']]
        self.assertTrue(conflicts, 'same-field edits must require explicit review')
        choices = {json.dumps([change['table'], change['key'], field], separators=(',', ':'),
                              ensure_ascii=False): 'remote' for change, field, _ in conflicts}
        right.apply_remote(choices, review['tip'], review['bundle_id'], review['local_revision'])
        right.sync_now()
        left.sync_now()
        for store in self.f.stores:
            task = store.read_v1('context', self.project, key='shared-task')['context']['task']
            self.assertEqual(task['title'], 'Left authored title')
            history = store.read_v1('history', self.project, key='shared-task')['items']
            self.assertTrue(any(item.get('details', {}).get('text') ==
                                'Full shared journal text from the common task' for item in history))
        with self.f.stores[1]._connection() as db:
            retained = [json.loads(row[0]) for row in db.execute(
                "SELECT after_json FROM workspace_events WHERE kind='import_bundle'")]
            self.assertTrue(any('Right authored title' in json.dumps(event) and
                                'Left authored title' in json.dumps(event) for event in retained),
                            'reviewed losing and selected alternatives must remain in history')
        # Once both stores have converged, repeated synchronization is idempotent.
        left.sync_now()
        right.sync_now()
        before_imports = []
        for store in self.f.stores:
            with store._connection() as db:
                before_imports.append(db.execute('SELECT count(*) FROM exchange_imports').fetchone()[0])
        tip = left.transport.fetch().tip
        self.assertFalse(left.sync_now().pushed)
        self.assertFalse(right.sync_now().pushed)
        self.assertEqual(left.transport.fetch().tip, tip)
        for store, count in zip(self.f.stores, before_imports):
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM exchange_imports').fetchone()[0], count)
        self.assertEqual([self.checkout_state(c) for c in self.clones], before)
        self.assertEqual(self.git(self.root, '--git-dir', str(self.f.remote), 'rev-parse',
                                 'refs/heads/main'), source_tip)


class RegisteredCheckoutIsolationTests(unittest.TestCase):
    def test_registered_sync_service_preserves_full_source_checkout(self):
        from tests import test_workspace_sync_service as service_fixtures
        fixture = service_fixtures.SyncServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        repo = fixture.repo

        def git(*args):
            return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Acceptance fixture',
                                   '-c', 'user.email=fixture@example.invalid', *args],
                                  check=True, capture_output=True).stdout

        source = repo / 'source.txt'
        source.write_text('Committed source baseline', encoding='utf-8')
        git('add', 'source.txt')
        git('commit', '-qm', 'Source baseline')
        git('push', '-q', 'origin', 'HEAD:refs/heads/main')
        source.write_text('Staged source edit', encoding='utf-8')
        git('add', 'source.txt')
        source.write_text('Unstaged source edit', encoding='utf-8')

        def state():
            return (git('symbolic-ref', 'HEAD'), git('rev-parse', 'HEAD'),
                    git('status', '--porcelain'), (repo / '.git/index').read_bytes(),
                    source.read_bytes(), git('ls-remote', 'origin', 'refs/heads/main'),
                    sorted((str(path.relative_to(repo)), path.read_bytes())
                           for path in repo.rglob('*') if path.is_file()
                           and path.relative_to(repo).parts[0] != '.git'))

        before = state()
        fixture.service.configure(fixture.project, fixture.repo_id,
                                  enabled=True, export_selected=True)
        fixture.service.request_sync(fixture.project, fixture.repo_id)
        fixture.service.tick(now=1000)
        self.assertEqual(fixture.service.read(fixture.project, fixture.repo_id)['state'], 'current')
        self.assertEqual(state(), before,
                         'the registered service must retain source HEAD, index and staged/unstaged bytes')
        first = git('ls-remote', 'origin', 'refs/heads/workbench-data')
        self.assertTrue(first, 'the service actually pushed the separate workspace-data ref')
        fixture.service.tick(now=1001)
        self.assertEqual(git('ls-remote', 'origin', 'refs/heads/workbench-data'), first)
        self.assertEqual(state(), before)


if __name__ == '__main__':
    unittest.main()
