"""Source context uses authenticated routes and the real finding transaction gates."""
import copy
import json
import unittest
import uuid
from pathlib import Path
from tests import test_workspace_shared_http as fixtures


class SourceContextHttpTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SharedHttpTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.project = self.f.project
        with self.f.store._connection() as db:
            self.actor = fixtures.auth.authenticate(db, self.f.owner_token)['principal_id']
        root = Path(self.f.temp.name)
        self.write({'op': 'register_root', 'id': 'source-root', 'path': str(root)})
        self.versions = []
        for name in ('parent', 'derived'):
            (root / (name + '.txt')).write_text('Synthetic ' + name + ' bytes', encoding='utf-8')
            self.write({'op': 'register_artifact', 'id': name, 'root_id': 'source-root',
                        'relative_path': name + '.txt', 'kind': 'source'})
            result = self.write({'op': 'register_artifact_version', 'artifact_id': name})
            self.versions.append(next(x['id'] for x in result['changed_records'] if x['type'] == 'artifact_version'))
        self.parent, self.child = self.versions

    def write(self, *operations, expected=200):
        code, result = self.f.request('/v1/transactions', self.f.owner_token, {
            'project_id': self.project, 'actor': {'principal_id': self.actor},
            'expected_revision': self.f.store.snapshot()['revision'], 'request_id': str(uuid.uuid4()),
            'operations': list(operations)})
        self.assertEqual(code, expected, result)
        return result

    def read(self, resource, **options):
        from urllib.parse import urlencode
        return self.f.request('/v1/' + resource + '?' + urlencode({'project_id': self.project, **options}),
                              self.f.member_token)

    def derivation(self, **overrides):
        op = {'op': 'create_finding', 'id': 'derived-assertion', 'claim': 'The report derives from the parent source',
              'scope': {'artifact_version_id': self.child,
                        'derivation': {'format': 'workbench-derived-source/v1', 'parent_version_id': self.parent}},
              'classification': 'provenance-assertion', 'uncertainty': 'Authored source assertion only',
              'evidence': [{'artifact_version_id': version, 'locator': {'line': 1},
                            'method': 'Read synthetic fixture', 'limitations': 'Synthetic, no native proof'}
                           for version in self.versions]}
        return {**op, **overrides}

    def test_context_and_search_reads_are_exact_and_authenticated(self):
        route = '/v1/artifact-versions/' + self.child + '/context?project_id=' + self.project
        self.assertEqual(self.f.request(route)[0], 401)
        self.assertEqual(self.f.request(route.replace(self.project, self.f.other), self.f.member_token)[0], 403)
        code, page = self.f.request(route, self.f.member_token)
        self.assertEqual(code, 200, page)
        self.assertEqual(page['context']['entity_type'], 'source')
        self.assertEqual(page['context']['entity']['id'], self.child)
        self.assertIsNone(page['context']['entity']['binary_build_id'])
        self.assertNotIn(self.f.temp.name, json.dumps(page))
        self.assertEqual(self.f.request(route + '&limit=20', self.f.member_token)[0], 400)
        self.write({'op': 'record_negative_search', 'id': 'bounded-search', 'artifact_version_id': self.child,
                    'query': 'missing symbol', 'method': 'Exact synthetic scan', 'searched_range': 'Line 1',
                    'coverage': {'complete': False, 'excluded': ['Other ranges']},
                    'outcome': 'Not found in this search; not proof of absence'})
        before = self.f.store.snapshot()['revision']
        code, searches = self.read('negative-searches', subject_id=self.child, limit=20)
        self.assertEqual(code, 200, searches)
        self.assertEqual(searches['subject_id'], self.child)
        self.assertEqual([x['id'] for x in searches['items']], ['bounded-search'])
        self.assertFalse(searches['items'][0]['coverage']['complete'])
        self.assertEqual(self.read('negative-searches', subject_id=self.parent)[1]['items'], [])
        self.assertEqual(self.read('negative-searches', subject_id=self.child, order='desc')[0], 400)
        self.assertEqual(self.f.store.snapshot()['revision'], before)

    def test_live_finding_scope_and_support_reject_forged_derivations(self):
        op = self.derivation()
        bad = copy.deepcopy(op)
        bad['scope']['derivation']['parent_version_id'] = str(uuid.uuid4())
        self.write(bad, expected=404)
        self.write(self.derivation(evidence=[op['evidence'][1]]), expected=400)
        self.write(self.derivation(scope={'artifact_version_id': self.child,
                   'derivation': {'format': 'future-unknown', 'parent_version_id': 'unknown-metadata'}}))
        current = self.f.store.read_v1('finding-context', self.project, key='derived-assertion')['context']['entity']
        self.assertEqual(current['scope']['derivation']['parent_version_id'], 'unknown-metadata')

    def test_history_preserves_old_assertions_and_withdrawn_citations(self):
        self.write(self.derivation())
        first = self.f.store.read_v1('finding-context', self.project, key='derived-assertion')['context']['entity']
        self.write({'op': 'revise_finding', 'finding_id': first['id'], 'expected_record_revision': first['record_revision'],
                    'fields': {'claim': 'A narrower recorded derivation assertion'}, 'rationale': 'Keep the former statement in history'})
        current = self.f.store.read_v1('finding-context', self.project, key=first['id'])['context']['entity']
        links = self.f.store.read_v1('evidence-links', self.project, subject_type='finding_revision',
                                    subject_id=current['evidence_origin_id'])['items']
        parent_link = next(x for x in links if x['artifact_version_id'] == self.parent)
        self.write({'op': 'set_evidence_state', 'evidence_link_id': parent_link['id'], 'expected_state': 'available',
                    'state': 'retracted', 'reason': 'Withdraw a synthetic citation without hiding the assertion'})
        current = self.f.store.read_v1('finding-context', self.project, key=first['id'])['context']['entity']
        self.write({'op': 'archive_finding', 'finding_id': current['id'],
                    'expected_record_revision': current['record_revision'], 'rationale': 'Retain archived derivation history'})
        code, page = self.read('source-derivations', subject_id=self.parent, limit=1)
        self.assertEqual(code, 200, page)
        self.assertEqual(len(page['items']), 1)
        self.assertTrue(page['next_cursor'])
        code, second = self.read('source-derivations', subject_id=self.parent, limit=1, cursor=page['next_cursor'])
        self.assertEqual(code, 200, second)
        rows = page['items'] + second['items']
        self.assertEqual(len({x['id'] for x in rows}), 2)
        self.assertEqual(sum(x['is_current'] for x in rows), 1)
        self.assertTrue(all(x['archived'] for x in rows))
        self.assertEqual(next(x for x in rows if x['is_current'])['support_counts']['retracted'], 1)
        self.assertEqual(next(x for x in rows if not x['is_current'])['support_counts']['retracted'], 0,
                         'withdrawing an exact new citation does not rewrite older distinct citation identities')
        self.assertEqual(self.read('source-derivations', subject_id=self.child, limit=1, cursor=page['next_cursor'])[0], 400)
        self.write({'op': 'record_negative_search', 'id': 'advance', 'artifact_version_id': self.parent,
                    'query': 'new search', 'method': 'Synthetic scan', 'searched_range': 'Line 1',
                    'coverage': {'complete': True}, 'outcome': 'Not found in that range'})
        code, stale = self.read('source-derivations', subject_id=self.parent, limit=1, cursor=page['next_cursor'])
        self.assertEqual(code, 409, stale)
        self.assertTrue(stale['restart_required'])

    def test_portable_finding_validation_uses_supplied_source_references(self):
        self.write(self.derivation())
        from tools import workspace_findings
        from tools.roadmap_store import StoreError
        records = self.f.store.exchange_preview(self.project, portable=True)['bundle']['records']
        with self.f.store._connection() as db:
            workspace_findings.validate_bundle(db, self.project, records)
            forged = copy.deepcopy(records)
            for revision in forged['finding_revisions']:
                scope = json.loads(revision['scope_json'])
                scope['derivation']['parent_version_id'] = str(uuid.uuid4())
                revision['scope_json'] = json.dumps(scope, sort_keys=True)
            with self.assertRaises(StoreError):
                workspace_findings.validate_bundle(db, self.project, forged)


if __name__ == '__main__':
    unittest.main()
