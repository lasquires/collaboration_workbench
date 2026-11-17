"""Remote worker URLs require HTTPS and every RPC carries private credentials."""
import argparse
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workspace
from tools import workbench_worker as worker


class WorkerRemoteTransportTests(unittest.TestCase):
    def test_url_policy_rejects_unsafe_targets_before_any_request(self):
        rejected = (
            'http://example.com',
            'https://user:pass@example.com',
            'https://example.com/path',
            'https://example.com/?project=x',
            'https://example.com/#fragment',
            'ftp://example.com',
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    worker.worker_url(url, 'private-token-file')
        with patch.object(workspace, 'request') as request, patch.object(
                workspace, 'load_manifest', return_value={'project_id': 'project'}):
            args = argparse.Namespace(repo=Path.cwd(), url='https://example.com/path',
                                      token_file='private-token-file', action='brief', task='task')
            with self.assertRaises(ValueError):
                worker.run(args)
            request.assert_not_called()

    def test_local_http_stays_loopback_only_and_https_requires_token_file(self):
        self.assertEqual(worker.worker_url('http://127.0.0.1:8771/', None),
                         'http://127.0.0.1:8771')
        for url in ('http://example.com', 'http://127.0.0.1:8771/path'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                worker.worker_url(url, 'private-token-file')
        with self.assertRaises(ValueError):
            worker.worker_url('https://workbench.example', None)
        self.assertEqual(worker.worker_url('https://workbench.example/', 'secret-file'),
                         'https://workbench.example')

    def _fake_workspace(self, claimed_by=None):
        """Return a small synthetic API and a call log; no sockets or secrets."""
        calls = []
        state = {
            'history': [], 'attempts': [], 'claim': {'by': claimed_by} if claimed_by else None,
            'status': 'ready', 'revision': 4,
        }

        def context():
            return {
                'task': {'id': 'task-id', 'title': 'Compare output', 'status': state['status'],
                         'record_revision': state['revision'],
                         'alias': 'task', 'progress': {'claim': {'state': 'active'}}},
                'fields': {'claim': state['claim']}, 'history': list(state['history']),
                'structured_attempts': list(state['attempts']), 'instructions': [],
                'reservations': [], 'acceptance': ['Compare original output'],
                'variable_leads': [],
            }

        def request(url, path, payload=None, *, token_file=None, timeout=None):
            calls.append((url, path, payload, token_file, timeout))
            if path.endswith('/capabilities'):
                return {'revision': state['revision'],
                        'capabilities': {'local_principal_id': 'principal-id'}}
            if path == '/v1/transactions':
                for op in payload['operations']:
                    if op['op'] == 'claim':
                        state['claim'] = {'by': op['claimant']}
                    elif op['op'] == 'release':
                        state['claim'] = None
                    elif op['op'] == 'record_attempt':
                        state['attempts'].append({'boundary_id': op['boundary_id'],
                                                  'outcome': op['outcome']})
                        state['status'] = 'review'
                    elif op['op'] == 'journal':
                        state['history'].append({'details': {'text': op['text']},
                                                 'request_id': 'fixture-request'})
                state['revision'] += 1
                return {'request_id': 'fixture-request', 'revision': state['revision']}
            if '/context?' in path:
                return {'context': context(), 'revision': state['revision']}
            raise AssertionError('Unexpected RPC: ' + path)

        return calls, request

    def _args(self, repo, action, **kwargs):
        return argparse.Namespace(repo=repo, url='https://workbench.example', token_file='private-token',
                                  action=action, task='task', **kwargs)

    def test_credentials_forwarded_for_brief_claim_and_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            calls, fake = self._fake_workspace()
            with patch.object(workspace, 'load_manifest', return_value={'project_id': 'project'}), \
                    patch.object(workspace, 'request', side_effect=fake):
                result = worker.run(self._args(repo, 'brief'))
                self.assertEqual(result['task'], 'task-id')
                self.assertTrue(calls)
                self.assertTrue(all(c[3:] == ('private-token', 30) for c in calls))

            calls, fake = self._fake_workspace()
            with patch.object(workspace, 'load_manifest', return_value={'project_id': 'project'}), \
                    patch.object(workspace, 'request', side_effect=fake):
                result = worker.run(self._args(repo, 'claim', worker='agent', paths=['src/probe.py'],
                                               research=False))
            self.assertEqual(result['claim']['by'], 'agent')
            self.assertTrue(any(c[1] == '/v1/transactions' for c in calls))
            self.assertTrue(all(c[3:] == ('private-token', 30) for c in calls))

    def test_credentials_forwarded_through_guarded_finish_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            calls, fake = self._fake_workspace(claimed_by='agent')
            args = self._args(Path(folder), 'finish', worker='agent', text='Measured result',
                              boundary='stationary', outcome='passed', evidence=['trace.txt'],
                              follow=None, child_title=None, child_alias=None, next_step=None,
                              pass_condition=None)
            with patch.object(workspace, 'load_manifest', return_value={'project_id': 'project'}), \
                    patch.object(workspace, 'request', side_effect=fake):
                result = worker.run(args)
            self.assertIsNone(result['child'])
            self.assertGreaterEqual(sum(c[1] == '/v1/transactions' for c in calls), 3)
            self.assertTrue(all(c[3:] == ('private-token', 30) for c in calls))
            self.assertTrue(any(c[1] == '/v1/projects/project/capabilities' for c in calls))

    def test_environment_token_file_is_used_when_cli_value_is_absent(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {'RESEARCH_WORKSPACE_TOKEN_FILE': 'env-private-token'}):
            calls, fake = self._fake_workspace()
            args = self._args(Path(folder), 'brief')
            args.token_file = None
            with patch.object(workspace, 'load_manifest', return_value={'project_id': 'project'}), \
                    patch.object(workspace, 'request', side_effect=fake):
                worker.run(args)
            self.assertTrue(calls)
            self.assertTrue(all(c[3:] == ('env-private-token', 30) for c in calls))


if __name__ == '__main__':
    unittest.main()
