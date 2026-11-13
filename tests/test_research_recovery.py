"""Crash/retry invariants for the durable matcher page transaction."""
import unittest

from tools.roadmap_matcher import read
from tools.roadmap_store import RoadmapStore


class MatcherRecoveryTests(unittest.TestCase):
    def setUp(self):
        from tests.test_research_matcher import MatcherTests
        self.h = MatcherTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_interrupted_fanout_resumes_after_store_restart_without_duplicates(self):
        version = self.h.source()
        self.h.question()
        self.h.publish(version, member="a")
        self.h.publish(version, member="b")
        first = self.h.process(limit=1)
        self.assertEqual(first["processed_pairs"], 1)
        self.h.store = RoadmapStore(self.h.store.path)
        for _ in range(10):
            result = self.h.process(limit=1)
            if not result["pending_outbox"]:
                break
        self.assertEqual(result["pending_outbox"], 0)
        self.h.process()
        self.assertEqual(len(self.h.read_matcher("hints")["items"]), 2)

    def test_failed_page_rolls_back_partial_effects_and_keeps_source(self):
        version = self.h.source()
        self.h.question()
        self.h.publish(version, member="a")
        self.h.publish(version, member="b")
        with self.h.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Corrupt the second canonical candidate hash to trigger a rule validation error.
            rows = db.execute("SELECT o.id FROM observations o JOIN run_observations ro ON ro.project_id=o.project_id AND ro.observation_id=o.id JOIN index_runs r ON r.project_id=ro.project_id AND r.id=ro.run_id WHERE o.project_id=? AND r.state='published' ORDER BY o.id", (self.h.project,)).fetchall()
            self.assertEqual(len(rows), 2)
            db.execute("DROP TRIGGER immutable_observations_update")
            old_hash = db.execute("SELECT content_hash FROM observations WHERE id=?", (rows[1]["id"],)).fetchone()[0]
            db.execute("UPDATE observations SET content_hash='invalid' WHERE id=?", (rows[1]["id"],))
            db.commit()
        result = self.h.process(limit=10)
        self.assertGreaterEqual(result["failed_jobs"], 1)
        hints = self.h.read_matcher("hints")["items"]
        self.assertTrue(all(h["candidate_id"] != rows[1]["id"] for h in hints))
        observations = self.h.store.read_v1("observations", self.h.project)["items"]
        self.assertEqual(len(observations), 2)
        status = self.h.read_matcher("processing-status")["items"][0]
        self.assertGreaterEqual(status["failed_outbox"], 1)
        self.assertTrue(status["errors"])
        with self.h.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE observations SET content_hash=? WHERE id=?", (old_hash, rows[1]["id"]))
            db.commit()
        for i, err in enumerate(status["errors"]):
            if "kind" in err:
                self.h.apply({"op": "retry_failed_job", "target_type": "outbox", "job_id": err["id"]}, request=f"retry-{i}")
        self.h.process(limit=10)
        self.assertEqual(len(self.h.read_matcher("hints")["items"]), 2)


if __name__ == "__main__":
    unittest.main()
