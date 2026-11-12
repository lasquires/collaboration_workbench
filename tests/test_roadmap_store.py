"""Behavioral fixtures for the local SQLite task authority; never opens project DB."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import closing

from tools.roadmap_store import RoadmapStore, StoreError


def records():
    return [
        {"id": "A", "title": "Parent", "summary": "scope", "status": "ready",
         "owner": None, "claim": {"by": "old", "extra": 1}, "unknown": {"null": None},
         "subtasks": [
             {"id": "A-child", "title": "Child", "status": "done",
              "attempts": ["unknown author", {"result": "blocked", "custom": None}]},
             {"id": "A-child-2", "title": "Second", "status": "ready", "summary": None}]},
        {"id": "B", "title": "Island", "summary": "other", "status": "ready",
         "dependencies": ["A"], "subtasks": []}]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "roadmap.sqlite3"
        self.store = RoadmapStore(self.path)
        self.store.import_v1(records(), "a" * 64)
        self.counter = 0

    def tx(self, *ops, actor="agent", revision=None, request_id=None):
        self.counter += 1
        return self.store.transact(revision or self.store.snapshot()["revision"],
                                   request_id or f"r{self.counter}", actor, list(ops))

    def assert_error(self, status, fn):
        with self.assertRaises(StoreError) as error:
            fn()
        self.assertEqual(error.exception.status, status)

    def test_import_is_lossless_and_restart_preserves_identity_order(self):
        self.assertEqual(self.store.export_legacy_records(), records())
        snap = self.store.snapshot()
        self.assertEqual([t["id"] for t in snap["tasks"]], ["A", "A-child", "A-child-2", "B"])
        self.assertEqual([(t["parent_id"], t["sibling_order"]) for t in snap["tasks"]],
                         [(None, 0), ("A", 0), ("A", 1), (None, 1)])
        self.assertEqual(RoadmapStore(self.path).snapshot(), snap)
        legacy = [e for e in snap["events"] if e["kind"] == "legacy_attempt"]
        self.assertEqual(len(legacy), 2)
        self.assertTrue(all(e["actor"] is None and e["historical_at"] is None for e in legacy))
        self.assertEqual(snap["tasks"][1]["record"]["status"], "done")
        self.assertNotIn("acceptance_gate_passed", snap["tasks"][1]["record"])

    def test_repeat_import_checks_hash_and_payload_without_new_events(self):
        before = self.store.snapshot()
        self.store.import_v1(records(), "a" * 64)
        self.assertEqual(before, self.store.snapshot())
        self.assert_error(409, lambda: self.store.import_v1(records(), "b" * 64))
        changed = records(); changed[0]["title"] = "changed"
        self.assert_error(409, lambda: self.store.import_v1(changed, "a" * 64))

    def test_atomic_failure_leaves_state_revision_and_events_unchanged(self):
        before = self.store.snapshot()
        self.assert_error(400, lambda: self.tx(
            {"op": "update", "task_id": "B", "fields": {"summary": "new"}},
            {"op": "set_parent", "task_id": "A", "parent_id": "missing"}))
        self.assertEqual(before, self.store.snapshot())

    def test_revision_conflict_and_request_replay_after_lost_response(self):
        revision = self.store.snapshot()["revision"]
        ops = [{"op": "journal", "task_id": "B", "kind": "attempt", "text": "boundary unresolved"}]
        result = self.store.transact(revision, "lost-response", "agent", ops)
        later = self.tx({"op": "update", "task_id": "B", "fields": {"summary": "later"}})
        self.assertEqual(self.store.transact(revision, "lost-response", "agent", ops), result)
        self.assertEqual(self.store.snapshot(), later)
        self.assert_error(409, lambda: self.store.transact(revision, "new-request", "agent", ops))
        self.assert_error(409, lambda: self.store.transact(revision, "lost-response", "other", ops))

    def test_claim_release_transfer_requires_identity_and_records_history(self):
        self.assert_error(409, lambda: self.tx({"op": "release", "task_id": "A", "claimant": "other"}))
        self.tx({"op": "transfer", "task_id": "A", "claimant": "old", "to": "new"})
        self.tx({"op": "release", "task_id": "A", "claimant": "new"})
        snap = self.tx({"op": "claim", "task_id": "A-child", "claimant": "agent"})
        self.assertEqual(next(t for t in snap["tasks"] if t["id"] == "A-child")["record"]["claim"], {"by": "agent"})
        self.assertEqual([e["kind"] for e in snap["events"]][-3:], ["transfer", "release", "claim"])
        self.assert_error(400, lambda: self.tx({"op": "update", "task_id": "A", "fields": {"claim": {"by": "bypass"}}}))

    def test_whole_project_path_reservations_reject_directory_overlap(self):
        self.tx({"op": "claim", "task_id": "B", "claimant": "agent", "reserved_paths": [{"path": "web", "kind": "directory"}]})
        self.assert_error(409, lambda: self.tx({"op": "claim", "task_id": "A-child", "claimant": "second", "reserved_paths": ["web/game.js"]}))
        self.tx({"op": "release", "task_id": "B", "claimant": "agent"})
        self.tx({"op": "claim", "task_id": "A-child", "claimant": "second", "reserved_paths": ["web/game.js"]})

    def test_deep_graph_uses_iterative_validation_and_export(self):
        ops = []
        previous = None
        for i in range(1100):
            task_id = f"D{i}"
            ops.append({"op": "create", "record": {"id": task_id, "title": task_id, "status": "ready"}, "parent_id": previous})
            previous = task_id
        self.tx(*ops)
        before = self.store.snapshot()
        self.assert_error(400, lambda: self.tx({"op": "set_parent", "task_id": "D0", "parent_id": "D1099"}))
        self.assertEqual(before, self.store.snapshot())
        roots = self.store.export_legacy_records()
        node = roots[-1]
        for i in range(1100):
            self.assertEqual(node["id"], f"D{i}")
            if i < 1099:
                node = node["subtasks"][0]

    def test_typed_links_reject_dependency_cycles_allow_related_cycles(self):
        self.assert_error(400, lambda: self.tx({"op": "add_link", "task_id": "A", "target_id": "B", "kind": "depends_on"}))
        snap = self.tx({"op": "add_link", "task_id": "A", "target_id": "B", "kind": "related", "rationale": "lead"},
                       {"op": "add_link", "task_id": "B", "target_id": "A", "kind": "related"})
        self.assertEqual(len(snap["links"]), 3)
        self.assert_error(400, lambda: self.tx({"op": "add_link", "task_id": "A", "target_id": "missing", "kind": "related"}))
        self.assert_error(400, lambda: self.tx({"op": "add_link", "task_id": "A", "target_id": "B", "kind": "untyped"}))

    def test_gate_independent_completion_and_explicit_reopen(self):
        self.assert_error(409, lambda: self.tx({"op": "update", "task_id": "B", "fields": {"status": "done"}}))
        self.tx({"op": "update", "task_id": "B", "fields": {"status": "done", "acceptance_gate_passed": True, "evidence": ["docs/report.md"]}})
        self.assert_error(409, lambda: self.tx({"op": "create", "parent_id": "B", "record": {"id": "B-new", "title": "new", "status": "ready"}}))
        snap = self.tx({"op": "update", "task_id": "B", "fields": {"status": "active", "acceptance_gate_passed": False}},
                       {"op": "create", "parent_id": "B", "record": {"id": "B-new", "title": "new", "status": "ready"}})
        self.assertEqual(snap["tasks"][0]["record"]["status"], "ready")

    def test_archive_keeps_id_events_and_original_payload(self):
        snap = self.tx({"op": "archive", "task_id": "B", "reason": "superseded"})
        task = next(t for t in snap["tasks"] if t["id"] == "B")
        self.assertEqual(task["archive_reason"], "superseded")
        self.assertEqual(task["original_record"], records()[1])
        self.assert_error(400, lambda: self.tx({"op": "create", "record": records()[1]}))

    def test_evidence_and_reservation_paths_fail_closed(self):
        for path in ["../outside", "docs/../escape", "https://bad", "C:/bad", "/absolute", "docs\\bad", "//host/share"]:
            with self.subTest(path=path):
                self.assert_error(400, lambda: self.tx({"op": "update", "task_id": "B", "fields": {"evidence": [path]}}))

    def test_database_constraints_prevent_event_rewrite_and_dangling_parent(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            for sql in ["DELETE FROM events", "UPDATE events SET kind='changed'", "DELETE FROM transactions"]:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE tasks SET parent_id='missing' WHERE id='B'")
                db.commit()
            db.rollback()

    def test_two_connections_one_revision_only_one_winner(self):
        revision = self.store.snapshot()["revision"]
        def race(n):
            try:
                RoadmapStore(self.path).transact(revision, f"race-{n}", "agent", [{"op": "journal", "task_id": "B", "kind": "observation", "text": str(n)}])
                return 200
            except StoreError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(race, [1, 2])), [200, 409])

    def test_invalid_import_rolls_back_and_unsupported_schema_fails(self):
        other = RoadmapStore(Path(self.tmp.name) / "other.sqlite3")
        bad = records(); bad[1]["dependencies"] = ["unknown"]
        before = other.snapshot()
        self.assert_error(400, lambda: other.import_v1(bad, "b" * 64))
        self.assertEqual(before, other.snapshot())
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE store_meta SET schema_version=999")
            db.commit()
        self.assert_error(503, lambda: RoadmapStore(self.path))

    def test_storage_failure_after_state_write_rolls_back_state_and_events(self):
        before = self.store.snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER simulate_io_failure BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'injected storage failure'); END")
            db.commit()
        self.assert_error(503, lambda: self.tx({"op": "update", "task_id": "B", "fields": {"summary": "must rollback"}}))
        self.assertEqual(before, self.store.snapshot())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_malformed_operations_fail_without_uncaught_python_errors(self):
        before = self.store.snapshot()
        for operation in [
            {"op": "journal", "task_id": "B", "kind": {}, "text": "bad"},
            {"op": "add_link", "task_id": "B", "target_id": "A", "kind": []},
            {"op": "add_link", "task_id": "A", "target_id": "B", "kind": "related", "review_state": {}},
            {"op": "update", "task_id": "B", "fields": {"status": []}},
            {"op": "create", "record": {"id": "bad", "title": "bad", "status": "ready", "dependencies": None}},
            {"op": "claim", "task_id": "B", "reserved_paths": [{"path": "web", "kind": []}]},
        ]:
            with self.subTest(operation=operation):
                self.assert_error(400, lambda: self.tx(operation))
                self.assertEqual(before, self.store.snapshot())

    def test_journal_correction_references_existing_same_task_event(self):
        snap = self.tx({"op": "journal", "task_id": "B", "kind": "observation", "text": "first"})
        event_id = snap["events"][-1]["event_id"]
        self.assert_error(400, lambda: self.tx({"op": "journal", "task_id": "B", "kind": "review", "text": "bad", "supersedes": "missing"}))
        self.assert_error(400, lambda: self.tx({"op": "journal", "task_id": "A", "kind": "review", "text": "bad", "supersedes": event_id}))
        result = self.tx({"op": "journal", "task_id": "B", "kind": "review", "text": "correction", "supersedes": event_id})
        self.assertEqual(result["events"][-2]["details"]["text"], "first")
        self.assertEqual(result["events"][-1]["details"]["supersedes"], event_id)

    def test_reopening_prerequisite_requires_completed_dependents_reopened(self):
        self.tx({"op": "update", "task_id": "A", "fields": {"status": "done", "acceptance_gate_passed": True, "evidence": ["docs/a.md"]}},
                {"op": "update", "task_id": "B", "fields": {"status": "done", "acceptance_gate_passed": True, "evidence": ["docs/b.md"]}})
        self.assert_error(409, lambda: self.tx({"op": "update", "task_id": "A", "fields": {"status": "active", "acceptance_gate_passed": False}}))
        self.tx({"op": "update", "task_id": "B", "fields": {"status": "active", "acceptance_gate_passed": False}},
                {"op": "update", "task_id": "A", "fields": {"status": "active", "acceptance_gate_passed": False}})


if __name__ == "__main__":
    unittest.main()
