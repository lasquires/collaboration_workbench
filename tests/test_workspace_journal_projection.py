import copy
import json
import unittest

from tools import roadmap_research as research
from tools.workspace_journal_projection import (
    COLUMNS, POLICY_VERSION, project_journal, unsafe_journal, validate_disclosure,
)


def fixture(details=None):
    details = {'text': 'safe note'} if details is None else details
    details_json = research.canonical(details)
    source = {
        'sequence': 7, 'event_id': 'evt-7', 'request_id': 'req-7', 'task_id': 'task-7',
        'actor_json': research.canonical({'name': 'Reviewer', 'kind': 'human'}),
        'recorded_at': '2026-11-14T10:00:00Z', 'historical_at': None,
        'kind': 'observation', 'details_json': details_json,
    }
    return {
        'id': 'evt-7', 'project_id': 'project-1', 'task_id': 'task-7',
        'created_by': 'principal-1', 'kind': 'observation', 'recorded_at': source['recorded_at'],
        'historical_at': None, 'details_json': details_json, 'origin_store_id': 'store-1',
        'source_event_json': research.canonical(source), 'source_sha256': research.digest(source),
    }


class JournalProjectionTests(unittest.TestCase):
    def test_fixed_contract_and_nested_redaction_preserve_safe_siblings_and_input(self):
        self.assertEqual(POLICY_VERSION, 1)
        self.assertEqual(COLUMNS, ('id','project_id','source_event_id','task_id','source_sha256','origin_store_id','mode','policy_version','details_json','source_actor_json','created_by','kind','recorded_at','historical_at','public_sha256'))
        row = fixture({'nested': {'private': 'C:\\Users\\alice\\secret.txt', 'keep': 5}, 'encoded': '{"path":"C:\\\\Users\\\\alice\\\\x"}'})
        before = copy.deepcopy(row)
        self.assertTrue(unsafe_journal(row))
        out = project_journal(row, 'redacted')
        details = json.loads(out['details_json'])
        self.assertEqual(details['nested'], {'private':'[LOCAL_PATH_REDACTED]','keep':5})
        self.assertEqual(json.loads(details['encoded']), {'path':'[LOCAL_PATH_REDACTED]'})
        self.assertEqual(row, before)
        validate_disclosure(out)
        self.assertEqual(out['source_sha256'], row['source_sha256'])
        self.assertNotEqual(out['public_sha256'], out['source_sha256'])

    def test_known_roots_and_omission(self):
        row = fixture({'note': 'secret workspace'})
        self.assertTrue(unsafe_journal(row, ('secret workspace',)))
        result = project_journal(row, 'omitted', ('secret workspace',))
        self.assertEqual(json.loads(result['details_json']), {'notice':'Original journal entry withheld after explicit local review.'})
        validate_disclosure(result)

    def test_authored_task_key_can_differ_from_resolved_journal_task_uid(self):
        row = fixture({'text':'C:\\Users\\alice\\file'})
        source = json.loads(row['source_event_json'])
        source['task_id'] = 'historical-alias'
        row['source_event_json'] = research.canonical(source)
        row['source_sha256'] = research.digest(source)
        # The journal row has the resolved stable UID, while the immutable source
        # event keeps the key authored when the journal operation was submitted.
        row['task_id'] = 'stable-task-uuid'
        self.assertTrue(unsafe_journal(row))
        self.assertEqual(project_journal(row, 'redacted')['task_id'], 'stable-task-uuid')

    def test_utf8_safe_text_is_canonical_and_preserved(self):
        row = fixture({'note':'café — 東京'})
        out = project_journal(row, 'redacted')
        self.assertIn('東京', out['details_json'])
        self.assertEqual(json.loads(out['details_json']), {'note':'café — 東京'})
        validate_disclosure(out)

    def test_reject_unsafe_attribution_and_object_keys(self):
        row = fixture({'C:\\Users\\alice\\key': 'value'})
        with self.assertRaises(ValueError): project_journal(row, 'redacted')
        row = fixture()
        source = json.loads(row['source_event_json']); source['actor_json'] = research.canonical({'name':'C:\\Users\\alice', 'kind':'human'})
        row['source_event_json'] = research.canonical(source); row['source_sha256'] = research.digest(source)
        with self.assertRaises(ValueError): project_journal(row, 'omitted')

    def test_legacy_unknown_actor_and_request_type_follow_existing_source_contract(self):
        row=fixture();source=json.loads(row['source_event_json']);source['actor_json']=''
        row['source_event_json']=research.canonical(source);row['source_sha256']=research.digest(source)
        public=project_journal(row,'redacted')
        self.assertIsNone(public['source_actor_json'])
        source['request_id']={'unexpected':'object'}
        row['source_event_json']=research.canonical(source);row['source_sha256']=research.digest(source)
        with self.assertRaises(ValueError):project_journal(row,'redacted')

    def test_invalid_source_and_tampered_public_identity_or_hash_rejected(self):
        row = fixture()
        bad = dict(row, details_json=research.canonical({'text':'forged'}))
        with self.assertRaises(ValueError): project_journal(bad, 'redacted')
        out = project_journal(row, 'redacted')
        for field, value in [('details_json','{}'), ('source_sha256','0'*64), ('mode','omitted')]:
            tampered = dict(out); tampered[field] = value
            with self.assertRaises(ValueError): validate_disclosure(tampered)
        tampered = dict(out); tampered['policy_version'] = True
        with self.assertRaises(ValueError): validate_disclosure(tampered)
        tampered = dict(out); tampered['id'] = 'other'
        with self.assertRaises(ValueError): validate_disclosure(tampered)


if __name__ == '__main__': unittest.main()
