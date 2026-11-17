"""Sparse paging for late scoped questions over published and authored evidence."""
import importlib.util
from pathlib import Path
from unittest import mock

from tools import roadmap_matcher


# Reuse the publication fixture without importing its TestCase into unittest's
# discovery namespace (which would run that suite a second time).
_fixture_path = Path(__file__).with_name("test_publication_sparse_matcher.py")
_fixture_spec = importlib.util.spec_from_file_location("_publication_sparse_fixture", _fixture_path)
_fixture = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture)


class QuestionSparseMatcherTests(_fixture.unittest.TestCase):
    """Compose the publication fixture so its own tests are not rediscovered."""

    def setUp(self):
        self.fixture = _fixture.SparsePublicationMatcherTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    @property
    def store(self):
        return self.fixture.store

    @store.setter
    def store(self, value):
        self.fixture.store = value

    @property
    def root(self):
        return self.fixture.root

    @property
    def project(self):
        return self.fixture.project

    def write(self, *ops):
        return self.fixture.write(*ops)

    def source(self):
        return self.fixture.source()

    def publish_many(self, version, records):
        return self.fixture.publish_many(version, records)

    def process(self, limit):
        return self.fixture.process(limit)

    def hints(self):
        return self.fixture.hints()

    def add_findings(self, version, records):
        for index, (identifier, scope) in enumerate(records):
            self.write({
                "op": "create_finding", "id": f"finding-{index}",
                "claim": f"Synthetic authored claim {identifier}",
                "scope": {"identifier": identifier, "namespace": "objc-field",
                          "scope_key": scope, "artifact_version_id": version},
                "classification": "source-supported",
                "uncertainty": "Synthetic fixture only",
                "evidence": [{"artifact_version_id": version,
                              "locator": {"member": f"Fixture.{index}"},
                              "method": "Read registered synthetic source",
                              "limitations": "No native execution"}],
            })

    def test_late_question_pages_only_identifier_possible_candidates(self):
        version = self.source()
        self.publish_many(version, [("noise", "Player") for _ in range(35)] +
                          [("speed", "Other"), ("speed", "Player")])
        self.add_findings(version, [("unrelated", "Player") for _ in range(35)] +
                          [("speed", "Other"), ("speed", "Player")])
        # Clear earlier publication/finding events with no question present.
        self.assertEqual(self.process(limit=500)["pending_outbox"], 0)
        self.write({"op": "create_question", "id": "late-q", "text": "Find speed",
                    "criteria": {"any_of": [{"identifier": "speed",
                                              "namespace": "objc-field",
                                              "scope_key": "Player"}]}})

        # Identifier filtering must cover both observations and current findings;
        # the wrong-scope candidates are possible candidates but not hints.
        with mock.patch.object(roadmap_matcher, "_observation_at",
                               side_effect=AssertionError("quadratic ordinal fallback")):
            result = self.process(limit=4)

        self.assertEqual(result["processed_pairs"], 4)
        self.assertEqual(result["new_hints"], 2)
        self.assertEqual(result["pending_outbox"], 0)
        hints = self.hints()
        self.assertEqual(len(hints), 2)
        self.assertEqual({hint["candidate_type"] for hint in hints},
                         {"observation", "finding_revision"})

    def test_legacy_global_cursor_restart_keeps_hints_and_later_sparse_matches(self):
        version = self.source()
        self.publish_many(version, [("noise", "Player") for _ in range(12)] +
                          [("speed", "Player"), ("speed", "Player")])
        self.add_findings(version, [("noise", "Player") for _ in range(12)] +
                          [("speed", "Player"), ("speed", "Player")])
        self.assertEqual(self.process(limit=500)["pending_outbox"], 0)
        self.write({"op": "create_question", "id": "late-q", "text": "Find speed",
                    "criteria": {"any_of": [{"identifier": "speed",
                                              "namespace": "objc-field",
                                              "scope_key": "Player"}]}})

        # Legacy global ordinal order is observations by ID, then current
        # findings by ID. Persist the first candidate's hint at offset one.
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            event = db.execute("SELECT id FROM research_outbox WHERE kind='create_question' AND state='pending'").fetchone()
            row = db.execute("""SELECT o.*,c.artifact_version_id,c.namespace,c.scope_key,c.locator_key,
                v.binary_build_id,v.sha256 AS source_sha256,
                (SELECT MIN(r.id) FROM index_runs r JOIN run_observations ro
                 ON ro.project_id=r.project_id AND ro.run_id=r.id
                 WHERE r.project_id=o.project_id AND r.state='published' AND ro.observation_id=o.id) AS source_run_id
                FROM observations o JOIN occurrences c ON c.project_id=o.project_id AND c.id=o.occurrence_id
                JOIN artifact_versions v ON v.project_id=c.project_id AND v.id=c.artifact_version_id
                WHERE o.project_id=? AND o.identifier='speed' ORDER BY o.id LIMIT 1""",
                (self.project,)).fetchone()
            question = roadmap_matcher._question_at(db, self.project, 0, question_id="late-q")
            self.assertTrue(roadmap_matcher._process_pair(db, self.project, question,
                                                          roadmap_matcher._candidate(row), "1"))
            db.execute("UPDATE research_outbox SET fanout_offset=1 WHERE id=?", (event["id"],))
            db.commit()

        self.store = _fixture.RoadmapStore(self.root / "store.sqlite3")
        with mock.patch.object(roadmap_matcher, "_observation_at",
                               side_effect=AssertionError("quadratic ordinal fallback")):
            result = self.process(limit=4)

        # The bounded sparse set restarts at its beginning; semantic hint
        # identity deduplication preserves the previously stored first hint.
        self.assertEqual(result["processed_pairs"], 4)
        self.assertEqual(result["new_hints"], 3)
        self.assertEqual(result["pending_outbox"], 0)
        hints = self.hints()
        self.assertEqual(len(hints), 4)
        self.assertEqual(len({(hint["candidate_type"], hint["candidate_id"])
                              for hint in hints}), 4)

    def test_large_sparse_set_resumes_from_observations_into_findings(self):
        version = self.source()
        self.publish_many(version, [("noise", "Player") for _ in range(12)] +
                          [("speed", "Player"), ("speed", "Player")])
        self.add_findings(version, [("noise", "Player") for _ in range(12)] +
                          [("speed", "Player"), ("speed", "Player")])
        self.assertEqual(self.process(limit=500)["pending_outbox"], 0)
        self.write({"op": "create_question", "id": "late-q", "text": "Find speed",
                    "criteria": {"any_of": [{"identifier": "speed",
                                              "namespace": "objc-field",
                                              "scope_key": "Player"}]}})
        with mock.patch.object(roadmap_matcher, "_observation_at",
                               side_effect=AssertionError("quadratic ordinal fallback")):
            first = self.process(limit=2)
            self.assertEqual(first["processed_pairs"], 2)
            self.assertEqual(first["new_hints"], 2)
            self.assertEqual(first["pending_outbox"], 1)
            self.store = _fixture.RoadmapStore(self.root / "store.sqlite3")
            second = self.process(limit=1)
            self.assertEqual(second["processed_pairs"], 1)
            self.assertEqual(second["new_hints"], 1)
            self.assertEqual(second["pending_outbox"], 1)
            final = self.process(limit=1)
            self.assertEqual(final["new_hints"], 1)
            self.assertEqual(final["pending_outbox"], 0)
        self.assertEqual(len(self.hints()), 4)
