"""Focused regression coverage for sparse published-run fanout."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.roadmap_matcher import create_schema, process_jobs, read
from tools.roadmap_research import canonical
from tools.roadmap_store import RoadmapStore


class SparsePublicationMatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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
            "actor": {"principal_id": self.principal}, "request_id": f"sparse-{self.seq}",
            "expected_revision": self.store.snapshot()["revision"], "operations": list(ops)})

    def source(self):
        (self.root / "witness.bin").write_bytes(b"fixture witness")
        self.write({"op": "register_root", "id": "root", "path": str(self.root)})
        self.write({"op": "register_artifact", "id": "artifact", "root_id": "root",
                    "relative_path": "witness.bin", "kind": "binary"})
        return self.write({"op": "register_artifact_version", "artifact_id": "artifact",
                           "binary_build_id": "build-A"})["changed_records"][0]["id"]

    def question(self):
        self.write({"op": "create_question", "id": "q", "text": "Find speed",
                    "criteria": {"any_of": [{"identifier": "speed", "namespace": "objc-field", "scope_key": "Player"}]}})

    def publish_many(self, version, records):
        self.seq += 1
        run = f"run-{self.seq}"
        self.write({"op": "create_index_run", "id": run, "extractor_id": "fixture",
                    "extractor_version": "1", "config_hash": "a" * 64,
                    "input_version_ids": [version], "coverage": {"complete": True}})
        batch = self.write({"op": "stage_batch", "run_id": run, "batch_key": "0", "records": [
            {"artifact_version_id": version, "namespace": "objc-field", "scope_key": scope,
             "locator_key": {"member": f"member-{i}", "offset": i}, "identifier": identifier,
             "value": {"type": "float"}, "provenance": {"method": "fixture"}}
            for i, (identifier, scope) in enumerate(records)]})["changed_records"][0]
        self.write({"op": "publish_run", "run_id": run, "manifest": {"batches": [
            {"key": "0", "sha256": batch["payload_hash"]}], "record_count": len(records)}})
        return run

    def process(self, limit):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = process_jobs(db, self.project, limit)
            db.commit()
            return result

    def hints(self):
        with self.store._connection() as db:
            db.execute("BEGIN")
            return read(db, "hints", self.project)["items"]

    def test_large_run_uses_identifier_match_budget_and_rejects_wrong_scope(self):
        version = self.source()
        self.question()
        self.assertEqual(self.process(limit=100)["pending_outbox"], 0)
        records = [("noise", "Player") for _ in range(80)]
        records += [("speed", "Other"), ("speed", "Player")]
        self.publish_many(version, records)

        result = self.process(limit=2)

        self.assertEqual(result["processed_pairs"], 2)
        self.assertEqual(result["new_hints"], 1)
        self.assertLessEqual(result["pending_outbox"], 1)
        for _ in range(5):
            if not result["pending_outbox"]:
                break
            result = self.process(limit=2)
        self.assertEqual(result["pending_outbox"], 0)
        self.assertEqual(len(self.hints()), 1)

    def test_legacy_membership_offset_resumes_without_losing_or_duplicating_hints(self):
        version = self.source()
        self.question()
        self.assertEqual(self.process(limit=100)["pending_outbox"], 0)
        run = self.publish_many(version, [("speed", "Player") for _ in range(4)])
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            event = db.execute("SELECT id FROM research_outbox WHERE kind='publish_run' AND state='pending'").fetchone()
            # Emulate a prior legacy page: one membership ordinal has been
            # processed and its corresponding hint already persisted.
            row = db.execute("""SELECT o.*,c.artifact_version_id,c.namespace,c.scope_key,c.locator_key,
                v.binary_build_id,v.sha256 AS source_sha256,ro.run_id AS source_run_id
                FROM run_observations ro JOIN observations o ON o.project_id=ro.project_id AND o.id=ro.observation_id
                JOIN occurrences c ON c.project_id=o.project_id AND c.id=o.occurrence_id
                JOIN artifact_versions v ON v.project_id=c.project_id AND v.id=c.artifact_version_id
                WHERE ro.project_id=? AND ro.run_id=? ORDER BY ro.observation_id LIMIT 1""",
                (self.project, run)).fetchone()
            from tools.roadmap_matcher import _process_pair, _question_at
            question = _question_at(db, self.project, 0)
            self.assertTrue(_process_pair(db, self.project, question, row, "1"))
            db.execute("UPDATE research_outbox SET fanout_offset=1 WHERE id=?", (event["id"],))
            db.commit()

        self.store = RoadmapStore(self.root / "store.sqlite3")
        result = self.process(limit=4)

        self.assertEqual(result["processed_pairs"], 3)
        self.assertEqual(result["new_hints"], 3)
        self.assertEqual(len(self.hints()), 4)
        self.assertEqual(result["pending_outbox"], 0)

