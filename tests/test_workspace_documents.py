"""Document contracts use temporary stores and caller-owned transactions."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools import roadmap_documents
from tools.roadmap_store import RoadmapStore, StoreError


class WorkspaceDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RoadmapStore(self.root / 'documents.sqlite3')
        self.project = self.store.read_v1('projects')['items'][0]['id']
        self.principal = self.store.read_v1('capabilities', self.project)['capabilities']['local_principal_id']
        with self.store._connection() as db:
            roadmap_documents.create_schema(db)
        self.seq = 0

    def apply(self, operation, *, project=None, principal=None):
        self.seq += 1
        request_id = f'doc-test-{self.seq}'
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO transactions VALUES(?,?,?)', (request_id, f'digest-{self.seq}', '{}'))
            result = roadmap_documents.apply_operation(db, project or self.project,
                                                       principal or self.principal, operation, request_id)
            db.commit()
            return result

    def read(self, resource, **options):
        with self.store._connection() as db:
            return roadmap_documents.read(db, resource, self.project, **options)

    def service_doc(self, body='## Heading\nOriginal source text.'):
        result = self.apply({'op':'create_document','id':'doc-main','kind':'guide','title':'Guide',
                             'authority_kind':'service_body','body':body})
        return result['id'], result['current_version_id']

    def test_create_response_matches_persisted_document_revision(self):
        for authority in ('service_body', 'registered_file'):
            operation = {'op': 'create_document', 'id': authority, 'kind': 'guide',
                         'title': 'Revision contract', 'authority_kind': authority}
            if authority == 'service_body':
                operation['body'] = 'Initial version'
            result = self.apply(operation)
            document = next(item for item in self.read('documents')['items']
                            if item['id'] == result['id'])
            self.assertEqual(result['record_revision'], document['revision'])
            self.assertEqual(result.get('current_version_id'), document['current_version_id'])

    def test_service_versions_proposals_pinned_comments_and_feedback(self):
        doc, first = self.service_doc()
        quote = 'Original source text.'
        self.apply({'op':'add_comment','id':'comment-xss','document_version_id':first,
                    'anchor':{'heading':'## Heading','start':len('## Heading\n'),
                              'end':len('## Heading\n')+len(quote),'quote':quote,
                              'sha256':hashlib.sha256(quote.encode()).hexdigest()},
                    'body':'<img src=x onerror=alert(1)>'})
        prop = self.apply({'op':'propose_edit','id':'proposal-1','document_id':doc,
                           'base_version_id':first,'proposed_body':'## Heading\nRevised text.'})
        accepted = self.apply({'op':'accept_proposal','proposal_id':prop['id'],'reason':'Reviewed against evidence'})
        second = accepted['version']['id']
        discussion = self.read('discussions', document_id=doc)['items'][0]
        self.assertTrue(discussion['outdated'])
        self.assertEqual(discussion['body'], '<img src=x onerror=alert(1)>', 'untrusted text stays plain data')
        self.assertEqual(discussion['document_version_id'], first, 'comments remain pinned to original version')
        self.assertEqual(self.read('document-versions', document_id=doc)['items'][0]['source_created_at'], None)
        self.apply({'op':'set_feedback','document_version_id':first,'value':'helpful','reason':'Useful context'})
        with self.store._connection() as db:
            db.execute('INSERT INTO agent_sessions(id,principal_id,created_at) VALUES(?,?,?)',
                       ('session-one',self.principal,'2030-01-01T00:00:00Z'))
            db.execute('INSERT INTO agent_sessions(id,principal_id,created_at) VALUES(?,?,?)',
                       ('session-two',self.principal,'2030-01-02T00:00:00Z'))
        self.apply({'op':'set_feedback','document_version_id':first,'value':'unhelpful','reason':'Needs a caveat'})
        self.apply({'op':'set_feedback','document_version_id':second,'value':'helpful','reason':'Clearer now'})
        rows=self.read('feedback', document_id=doc)['items']
        first_vote=next(x for x in rows if x['document_version_id']==first)
        self.assertEqual(first_vote['sample_size'],1, 'same principal changing feedback does not multiply votes')
        self.assertEqual(self.store.read_v1('events',self.project)['items'][0]['actor_id'], self.principal)
        self.assertEqual(first_vote['unhelpful_count'],1)
        self.assertEqual(len(rows),2, 'feedback remains version specific')
        with self.store._connection() as db:
            feedback_events=db.execute("SELECT before_json,after_json FROM workspace_events WHERE project_id=? AND kind='set_feedback' ORDER BY sequence",(self.project,)).fetchall()
        self.assertEqual(len(feedback_events),3, 'each feedback change appends an attributed event')
        self.assertIsNotNone(feedback_events[1]['before_json'], 'changing a principal vote preserves its prior value in the event')
        self.assertEqual(self.read('documents')['items'][0]['current_version_id'], second)

    def test_stale_proposal_requires_explicit_reconciliation(self):
        doc, base = self.service_doc('Version one')
        stale=self.apply({'op':'propose_edit','id':'stale-proposal','document_id':doc,
                          'base_version_id':base,'proposed_body':'Stale body'})
        current=self.apply({'op':'propose_edit','id':'current-proposal','document_id':doc,
                            'base_version_id':base,'proposed_body':'Version two'})
        self.apply({'op':'accept_proposal','proposal_id':current['id'],'reason':'Current edit'})
        with self.assertRaises(StoreError) as exc:
            self.apply({'op':'accept_proposal','proposal_id':stale['id'],'reason':'Attempt stale edit'})
        self.assertEqual(exc.exception.status,409)
        proposal=next(x for x in self.read('proposals')['items'] if x['id']==stale['id'])
        self.assertEqual(proposal['state'],'pending', 'reconciliation conflict does not silently mutate proposal state')

    def test_registered_file_snapshot_hash_guard_authority_and_unknown_dates(self):
        file_path=self.root/'diary.md'
        file_path.write_text('Registered diary body',encoding='utf-8')
        def v1(*ops):
            self.seq += 1
            return self.store.transact_v1({'project_id':self.project,'actor':{'principal_id':self.principal},
                'request_id':f'research-{self.seq}-{len(ops)}','expected_revision':self.store.snapshot()['revision'],
                'operations':list(ops)})
        v1({'op':'register_root','id':'docs-root','path':str(self.root)})
        v1({'op':'register_artifact','id':'diary-source','root_id':'docs-root','relative_path':'diary.md','kind':'document'})
        version=v1({'op':'register_artifact_version','artifact_id':'diary-source'})['changed_records'][0]
        source_id=version['id']
        doc=self.apply({'op':'create_document','id':'registered-diary','kind':'diary','title':'Diary','authority_kind':'registered_file'})['id']
        with self.assertRaises(StoreError) as exc:
            self.apply({'op':'register_document_version','document_id':doc,
                        'source':{'artifact_version_id':source_id,'expected_sha256':'0'*64}})
        self.assertEqual(exc.exception.status,409)
        artifact=self.store.read_v1('artifact-versions',self.project)['items'][0]
        registered=self.apply({'op':'register_document_version','document_id':doc,
                      'source':{'artifact_version_id':source_id,'expected_sha256':artifact['sha256']}})
        self.assertIsNone(registered['source_created_at'], 'unknown historical dates remain null')
        with self.assertRaises(StoreError):
            self.apply({'op':'propose_edit','document_id':doc,'base_version_id':registered['id'],'proposed_body':'bypass'})

    def test_project_isolation_and_guidance_capability_are_separate(self):
        doc, version=self.service_doc()
        foreign='00000000-0000-4000-8000-000000009999'
        with self.store._connection() as db:
            db.execute("INSERT INTO projects(id,slug,goal) VALUES(?, 'foreign-docs', '')", (foreign,))
        with self.assertRaises(StoreError) as exc:
            self.apply({'op':'add_comment','document_version_id':version,'body':'foreign'},project=foreign)
        self.assertEqual(exc.exception.status,404)
        with self.assertRaises(StoreError) as exc:
            self.apply({'op':'approve_guidance','document_version_id':version,'task_id':'unknown'})
        self.assertEqual(exc.exception.status,403, 'contribute does not imply approve_guidance')
        with self.store._connection() as db:
            db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',
                       (json.dumps(['contribute','approve_guidance']),self.project,self.principal))
        with self.assertRaises(StoreError) as exc:
            self.apply({'op':'approve_guidance','document_version_id':version,'area_id':'unknown'})
        self.assertEqual(exc.exception.status,404, 'capability grant still requires a scoped target')

    def test_schema_and_non_commit_transaction_boundary(self):
        with self.store._connection() as db:
            before=db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            db.execute('BEGIN')
            roadmap_documents.apply_operation(db,self.project,self.principal,
                {'op':'create_document','id':'rolled-back','kind':'guide','title':'Rollback',
                 'authority_kind':'service_body','body':'body'},'rolled-back-request')
            db.rollback()
        self.assertEqual(self.read('documents')['items'],[])
        self.assertEqual(before,0)


if __name__ == '__main__':
    unittest.main()
