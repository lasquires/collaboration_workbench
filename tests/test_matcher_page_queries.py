"""Page-local matcher query reuse regression."""
import unittest

from tools import roadmap_matcher
from tests import test_research_matcher as fixtures


class MatcherPageQueryTests(unittest.TestCase):
    def test_replay_page_reuses_counts_and_candidate_rows(self):
        fixture = fixtures.MatcherTests("test_forward_and_reverse_matching_are_pinned_and_retry_safe")
        fixture.setUp()
        try:
            version = fixture.source()
            fixture.publish(version)
            fixture.publish(version, member="field-b")
            fixture.question(question_id="q1")
            fixture.question(question_id="q2")
            fixture.process()
            fixture.apply({"op": "replay_rule", "rule_version": "1"}, request="query-reuse")

            statements = []
            with fixture.store._connection() as db:
                db.set_trace_callback(statements.append)
                db.execute("BEGIN IMMEDIATE")
                roadmap_matcher.process_jobs(db, fixture.project, limit=4)
                db.commit()

            observation_counts = [s for s in statements if "SELECT count(*) FROM observations o WHERE" in s]
            observation_rows = [s for s in statements if "SELECT o.*,c.artifact_version_id" in s]
            question_rows = [s for s in statements if "SELECT q.*,r.text" in s]
            self.assertEqual(len(observation_counts), 1)
            self.assertEqual(len(observation_rows), 2)
            self.assertEqual(len(question_rows), 2)
        finally:
            fixture.tearDown()
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
