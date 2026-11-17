"""Durable matcher behavior against isolated temporary roadmap stores."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from tools.roadmap_matcher import apply_operation, create_schema, process_jobs, read
from tools.roadmap_research import canonical
from tools.roadmap_store import RoadmapStore
from tools.roadmap_schema import principal_for_label


class MatcherTests(unittest.TestCase):
    def test_question_scope_revision_stales_old_hint_and_retains_decision(self):
        self.publish(self.source())
        self.question()
        self.process()
        old=self.read_matcher('hints')['items'][0]
        self.apply({'op':'decide_hint','hint_id':old['id'],'state':'dismissed','reason':'Already checked'})
        self.write({'op':'revise_question','question_id':'q','expected_record_revision':1,
                    'text':'Editorial clarification','rationale':'Clarify'})
        self.assertEqual(self.process()['new_hints'],0)
        self.assertEqual(len(self.read_matcher('hints')['items']),1)
        for revision,scope in ((2,'Ball'),(3,'Player')):
            self.write({'op':'revise_question','question_id':'q','expected_record_revision':revision,
                        'criteria':{'any_of':[{'identifier':'speed','namespace':'objc-field','scope_key':scope}]},
                        'rationale':'Review scope '+scope})
            self.process()
        hints=self.read_matcher('hints')['items']
        self.assertEqual(len(hints),2)
        self.assertEqual(next(item for item in hints if item['id']==old['id'])['state'],'stale')
        successor=next(item for item in hints if item['id']!=old['id'])
        self.assertEqual(successor['state'],'dismissed')
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT state FROM hint_decisions WHERE hint_id=?',(old['id'],)).fetchone()[0],'dismissed')
            self.assertIsNotNone(db.execute('SELECT carried_from_id FROM hint_decisions WHERE hint_id=?',(successor['id'],)).fetchone()[0])

    def test_unhandled_outbox_event_is_acknowledged(self):
        from tools.roadmap_matcher import _process_event_page
        self.question()
        with self.store._connection() as db:
            event=dict(db.execute("SELECT * FROM research_outbox ORDER BY event_sequence DESC LIMIT 1").fetchone())
            event_id=event["id"]
            event["kind"]="non_matching_metadata"
            self.assertEqual(_process_event_page(db,self.project,event,1),(0,0,True))
            self.assertEqual(db.execute("SELECT state FROM research_outbox WHERE id=?",(event_id,)).fetchone()[0],"done")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RoadmapStore(self.root / "store.sqlite3")
        self.project = self.store.read_v1("projects")["items"][0]["id"]
        self.principal = self.store.read_v1("capabilities", self.project)["capabilities"]["local_principal_id"]
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            create_schema(db)
            db.commit()
        self.seq = 0

    def write(self, *ops):
        self.seq += 1
        return self.store.transact_v1({"project_id": self.project,
            "actor": {"principal_id": self.principal}, "request_id": f"m-{self.seq}",
            "expected_revision": self.store.snapshot()["revision"], "operations": list(ops)})

    def source(self):
        (self.root / "witness.bin").write_bytes(b"native witness bytes")
        self.write({"op": "register_root", "id": "root", "path": str(self.root)})
        self.write({"op": "register_artifact", "id": "artifact", "root_id": "root",
                    "relative_path": "witness.bin", "kind": "binary"})
        return self.write({"op": "register_artifact_version", "artifact_id": "artifact",
                           "binary_build_id": "build-A"})["changed_records"][0]["id"]

    def publish(self, version, identifier="speed", scope="Player", *, member="field-a", value=None, locator=None):
        self.seq += 1; run = f"run-{self.seq}"
        self.write({"op": "create_index_run", "id": run, "extractor_id": "probe",
                    "extractor_version": "1", "config_hash": "a" * 64,
                    "input_version_ids": [version], "coverage": {"complete": True}})
        record = {"artifact_version_id": version, "namespace": "objc-field", "scope_key": scope,
                  "locator_key": locator if locator is not None else {"member": member, "offset": 12},
                  "identifier": identifier, "value": {"type": "float"} if value is None else value,
                  "provenance": {"method": "binary probe"}}
        batch = self.write({"op": "stage_batch", "run_id": run, "batch_key": "0", "records": [record]})["changed_records"][0]
        self.write({"op": "publish_run", "run_id": run, "manifest": {"batches": [{"key": "0", "sha256": batch["payload_hash"]}], "record_count": 1}})
        return run

    def question(self, criteria=None, question_id="q"):
        return self.write({"op": "create_question", "id": question_id, "text": "Where is speed used?",
                           "criteria": criteria or {"any_of": [{"identifier": "speed", "namespace": "objc-field", "scope_key": "Player"}]}})

    def process(self, limit=100):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = process_jobs(db, self.project, limit)
            db.commit()
            return result

    def apply(self, op, request="extra"):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = apply_operation(db, self.project, self.principal, op, request)
            db.commit()
            return result

    def add_finding(self, version, *, finding_id="f", revision_id="fr",
                    scope=None, support_hash=None):
        scope = scope if scope is not None else {
            "identifier": "speed", "namespace": "objc-field", "scope_key": "Player",
            "artifact_version_id": version, "offset": 12,
        }
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = "2026-11-12T00:00:00+00:00"
            db.execute("INSERT INTO findings VALUES(?,?,?,1,?,?,NULL)",
                       (finding_id, self.project, revision_id, self.principal, now))
            db.execute("INSERT INTO finding_revisions VALUES(?,?,?,?,?,?,?,1,1,?,?)",
                       (revision_id, self.project, finding_id, "Exact scoped finding", canonical(scope),
                        "unknown", "unverified", self.principal, now))
            db.execute("INSERT INTO evidence_links VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("el-" + revision_id, self.project, "finding_revision", revision_id, version,
                        canonical({"member": "field-a", "offset": 12}), "test evidence", "fixture",
                        support_hash or ("c" * 64), "available", self.principal, now))
            db.commit()
        return revision_id

    def emit_finding(self, revision_id):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            request_id = db.execute("SELECT request_id FROM transactions ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            event_id = str(uuid.uuid4()); now = "2026-11-12T00:00:01+00:00"
            cur = db.execute("INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (event_id, self.project, request_id, self.principal, "finding_revision", revision_id,
                              "publish_finding", canonical({"id": revision_id}), now))
            db.execute("INSERT INTO research_outbox(id,project_id,event_sequence,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                       (str(uuid.uuid4()), self.project, cur.lastrowid, "publish_finding",
                        canonical({"id": revision_id}), now))
            db.commit()

    def read_matcher(self, resource, **kwargs):
        with self.store._connection() as db:
            db.execute("BEGIN")
            return read(db, resource, self.project, **kwargs)

    def test_forward_and_reverse_matching_are_pinned_and_retry_safe(self):
        version = self.source(); self.question()
        self.publish(version)
        result = self.process()
        self.assertEqual(result["new_hints"], 1)
        hint = self.read_matcher("hints")["items"][0]
        self.assertEqual(hint["candidate_type"], "observation")
        self.assertEqual(hint["rule_version"], "1")
        self.assertIn("namespace", hint["compared"])
        self.assertEqual(self.process()["new_hints"], 0)
        # New question event scans already published observations in reverse.
        self.question(question_id="q2")
        self.assertEqual(self.process()["new_hints"], 1)
        self.assertEqual(len(self.read_matcher("hints")["items"]), 2)

    def test_durable_fanout_cursor_survives_restart_and_dedupes(self):
        version = self.source(); self.question()
        self.publish(version, member="a"); self.publish(version, member="b")
        first = self.process(limit=1)
        self.assertEqual(first["processed_pairs"], 1)
        pending = self.read_matcher('processing-status')['items'][0]['pending_jobs']
        self.assertTrue(pending)
        self.assertTrue(any(job['fanout_offset'] > 0 for job in pending))
        self.store = RoadmapStore(self.store.path)
        self.assertEqual(self.read_matcher('processing-status')['items'][0]['pending_jobs'], pending)
        history = []
        for _ in range(20):
            result = self.process(limit=1); history.append(result)
            if not result["pending_outbox"]:
                break
        else:
            self.fail(f"outbox cursor did not finish within bounded retries: {history}")
        self.assertEqual(len(self.read_matcher("hints")["items"]), 2)
        self.assertEqual(self.read_matcher('processing-status')['items'][0]['pending_jobs'], [])
        self.process()
        self.assertEqual(len(self.read_matcher("hints")["items"]), 2)

    def test_sparse_watch_skips_unrelated_history_in_one_page(self):
        version = self.source()
        self.publish(version, identifier="position", member="other")
        self.publish(version)
        self.process()
        self.question()
        result = self.process(limit=1)
        self.assertEqual(result["processed_pairs"], 1)
        self.assertEqual(result["pending_outbox"], 0)
        self.assertEqual(result["new_hints"], 1)

    def test_sparse_watch_resumes_legacy_offset_without_duplicate_hints(self):
        version=self.source()
        self.publish(version,identifier="position",member="other")
        self.publish(version)
        self.process()
        self.question()
        with self.store._connection() as db:
            db.execute("UPDATE research_outbox SET fanout_offset=1 WHERE kind='create_question' AND state='pending'")
        result=self.process(limit=1)
        self.assertEqual(result["pending_outbox"],0)
        self.assertEqual(result["new_hints"],1)
        self.assertEqual(self.process()["new_hints"],0)

    def test_scope_isolation_empty_criteria_and_membership_scoped_inbox(self):
        version = self.source(); self.question({"any_of": [{"identifier": "speed", "namespace": "objc-field", "scope_key": "Other"}]})
        self.publish(version)
        self.assertEqual(self.process()["new_hints"], 0)
        self.question({}, question_id="empty")
        self.assertEqual(self.process()["failed_jobs"], 0)
        self.assertEqual(self.read_matcher("inbox", principal_id=self.principal)["items"], [])

    def test_decision_carries_on_rule_replay_and_read_state_is_private(self):
        version = self.source(); self.question(); self.publish(version); self.process()
        hint = self.read_matcher("hints")["items"][0]
        self.apply({"op": "decide_hint", "hint_id": hint["id"], "state": "dismissed", "reason": "Checked source"})
        self.apply({"op": "replay_rule", "rule_version": "2"}, request="replay-2")
        self.process()
        hints = self.read_matcher("hints")["items"]
        self.assertEqual(len(hints), 2)
        with self.store._connection() as db:
            carried = db.execute("SELECT state,carried_from_id FROM hint_decisions WHERE hint_id=?", (hints[1]["id"],)).fetchone()
            self.assertEqual(carried["state"], "dismissed")
            self.assertTrue(carried["carried_from_id"])
        self.assertEqual(hints[1]["state"], "dismissed")
        inbox = self.read_matcher("inbox", principal_id=self.principal)["items"]
        self.assertEqual(inbox, []) # no subscriptions means no deliveries

    def test_scoped_subscription_digest_and_read_state(self):
        version = self.source(); self.question()
        self.apply({"op": "subscribe", "target_type": "question", "target_id": "q"}, request="sub-q")
        self.publish(version); self.process()
        inbox = self.read_matcher("inbox", principal_id=self.principal)["items"]
        self.assertEqual(len(inbox), 1)
        self.assertEqual(len(inbox[0]["hint_ids"]), 1)
        self.apply({"op": "mark_inbox_read", "inbox_id": inbox[0]["id"]}, request="read-q")
        self.assertIsNotNone(self.read_matcher("inbox", principal_id=self.principal)["items"][0]["read_at"])

    def test_scalar_value_and_string_locator_candidate_are_supported(self):
        version = self.source(); self.question()
        self.publish(version, value=4, locator="sprite/12")
        self.assertEqual(self.process()["new_hints"], 1)
        compared = self.read_matcher("hints")["items"][0]["compared"]
        self.assertEqual(compared["identifier"], "speed")

    def test_existing_legacy_claimant_is_added_as_inbox_recipient(self):
        label = "known-legacy-worker"
        self.write({"op": "create", "alias": "task-owner", "record": {
            "title": "Owned task", "status": "active", "owner": label}})
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            principal_for_label(db, self.project, label)
            task = next(r for r in db.execute("SELECT uid,record_json FROM tasks WHERE project_id=?", (self.project,))
                        if '"known-legacy-worker"' in r["record_json"])
            db.commit()
        self.write({"op": "create_question", "id": "owned-q", "text": "Where is speed?",
                    "owner_task_id": task["uid"], "criteria": {"any_of": [{"identifier": "speed", "namespace": "objc-field", "scope_key": "Player"}]}})
        version = self.source(); self.publish(version); self.process()
        self.assertEqual(self.read_matcher("inbox", principal_id=self.principal)["items"], [])
        with self.store._connection() as db:
            owner = db.execute("SELECT id FROM principals WHERE label=?", (label,)).fetchone()
        self.assertIsNotNone(owner)
        self.assertEqual(len(self.read_matcher("inbox", principal_id=owner["id"])["items"]), 1)

    def test_explicit_finding_scope_matches_forward_and_reverse(self):
        version = self.source(); self.question()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE research_outbox SET state='done' WHERE kind='create_question'")
            db.commit()
        revision = self.add_finding(version)
        self.emit_finding(revision)
        result = self.process()
        self.assertEqual(result["new_hints"], 1)
        hint = next(h for h in self.read_matcher("hints")["items"] if h["candidate_id"] == revision)
        self.assertEqual(hint["candidate_type"], "finding_revision")
        self.question(question_id="q2")
        self.assertEqual(self.process()["new_hints"], 1)
        q2hint = next(h for h in self.read_matcher("hints")["items"]
                      if h["question_id"] == "q2" and h["candidate_id"] == revision)
        self.assertEqual(q2hint["candidate_type"], "finding_revision")

    def test_finding_without_explicit_scope_is_skipped_and_retraction_stales_hints(self):
        version = self.source(); self.question()
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE research_outbox SET state='done' WHERE kind='create_question'")
            db.commit()
        revision = self.add_finding(version)
        bad_revision = self.add_finding(version, finding_id="f-bad", revision_id="fr-bad",
                                        scope={"occurrence_id": "o-legacy", "identifier": "speed"},
                                        support_hash="d" * 64)
        self.emit_finding(revision)
        self.emit_finding(bad_revision)
        self.process()
        hints = self.read_matcher("hints")["items"]
        self.assertEqual([h["candidate_id"] for h in hints], [revision])
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE evidence_links SET state='retracted' WHERE subject_id=?", (revision,))
            db.commit()
        stale = self.process()
        self.assertGreaterEqual(stale["stale_hints"], 1)
        self.assertEqual(self.read_matcher("hints")["items"][0]["state"], "stale")

    def test_partial_support_retraction_stales_old_decision_on_same_rule(self):
        version=self.source(); self.question()
        revision=self.add_finding(version)
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            original=dict(db.execute('SELECT * FROM evidence_links WHERE subject_id=?',(revision,)).fetchone())
            columns=list(original)
            original['id']='second-support'; original['support_hash']='d'*64
            db.execute('INSERT INTO evidence_links('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[original[k] for k in columns])
            db.commit()
        self.emit_finding(revision); self.process()
        hint=next(h for h in self.read_matcher('hints')['items'] if h['candidate_id']==revision)
        self.apply({'op':'decide_hint','hint_id':hint['id'],'state':'useful','reason':'Both source witnesses supported it.'},request='useful-before-retraction')
        with self.store._connection() as db:
            db.execute("UPDATE evidence_links SET state='retracted' WHERE project_id=? AND subject_id=? AND support_hash=?",(self.project,revision,'c'*64))
        self.assertGreaterEqual(self.process()['stale_hints'],1)
        self.apply({'op':'replay_rule','rule_version':'1'},request='same-rule-replay')
        self.process()
        old=next(h for h in self.read_matcher('hints')['items'] if h['id']==hint['id'])
        self.assertEqual(old['state'],'stale')
        self.assertIn('c'*64,old['support_hashes'])
        notices=[row for row in self.read_matcher('inbox',principal_id=self.principal)['items']
                 if row['transition_key']=='evidence-review']
        self.assertEqual(len(notices),1)
        self.assertEqual(notices[0]['hint_ids'],[hint['id']])
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT state FROM hint_decisions WHERE hint_id=?',(hint['id'],)).fetchone()[0],'useful')


if __name__ == "__main__":
    unittest.main()
