import copy
import unittest

from tools.roadmap_match_rules import match_question


QUESTION = {
    "id": "q1", "project_id": "p1", "criteria_revision": 1,
    "criteria": {"any_of": [{"identifier": "spritemap", "namespace": "plist",
                              "scope_key": "hero", "artifact_version_id": "av1",
                              "binary_build_id": "build1"}]},
}
CANDIDATE = {
    "id": "o1", "project_id": "p1", "candidate_type": "observation",
    "evidence_revision": 1, "identifier": "spritemap", "namespace": "plist",
    "scope_key": "hero", "artifact_version_id": "av1", "binary_build_id": "build1",
    "locator_key": "sprite/1", "support_hashes": ["a" * 64],
}
RULE = {"id": "exact-scoped", "version": "1"}


class MatchRulesTests(unittest.TestCase):
    def match(self, q=QUESTION, c=CANDIDATE, r=RULE):
        return match_question(q, c, r)

    def test_pins_all_explicit_scope_and_rejects_foreign_project(self):
        self.assertIsNotNone(self.match())
        for key, value in (("project_id", "p2"), ("scope_key", "other"),
                           ("binary_build_id", "build2")):
            c = copy.deepcopy(CANDIDATE); c[key] = value
            self.assertIsNone(self.match(c=c), key)

    def test_identifier_only_requires_opt_in_and_is_labeled(self):
        q = copy.deepcopy(QUESTION)
        q["criteria"] = {"any_of": [{"identifier": "spritemap"}]}
        self.assertIsNone(self.match(q=q))
        q["criteria"]["allow_low_specificity"] = True
        result = self.match(q=q)
        self.assertTrue(result["low_specificity"])

    def test_unopted_broad_or_branch_cannot_bypass_scoped_branch(self):
        q = copy.deepcopy(QUESTION)
        q["criteria"]["any_of"].append({"identifier": "spritemap"})
        c = {**CANDIDATE, "scope_key": "other"}
        self.assertIsNone(self.match(q=q, c=c))
        q["criteria"]["allow_low_specificity"] = True
        result = self.match(q=q, c=c)
        self.assertTrue(result["low_specificity"])

    def test_groups_or_and_fields_within_group(self):
        q = copy.deepcopy(QUESTION)
        q["criteria"]["any_of"].append({"identifier": "other", "namespace": "plist"})
        self.assertIsNotNone(self.match(q=q))
        q["criteria"]["any_of"] = [{"identifier": "spritemap", "namespace": "wrong"}]
        self.assertIsNone(self.match(q=q))

    def test_semantic_fingerprint_stability_and_identity(self):
        q = copy.deepcopy(QUESTION)
        q["criteria"]["any_of"].append({"identifier": "spritemap", "namespace": "plist"})
        a = self.match(q=q)
        q["criteria"]["any_of"].reverse()
        self.assertEqual(a["fingerprint"], self.match(q=q)["fingerprint"])
        q["presentation_revision"] = 99
        self.assertEqual(a["fingerprint"], self.match(q=q)["fingerprint"])
        self.assertNotEqual(a["fingerprint"], self.match(q={**q, "id": "q2"})["fingerprint"])
        self.assertNotEqual(a["fingerprint"], self.match(c={**CANDIDATE, "id": "o2"})["fingerprint"])
        changed = {**CANDIDATE, "support_hashes": ["b" * 64]}
        self.assertNotEqual(a["fingerprint"], self.match(c=changed)["fingerprint"])
        self.assertEqual(a["fingerprint"], self.match(q=q, r={"id": "exact-scoped", "version": "2"})["fingerprint"])

    def test_malformed_criteria_is_rejected(self):
        with self.assertRaises(ValueError):
            self.match(q={**QUESTION, "criteria": {"any_of": "bad"}})
        q = copy.deepcopy(QUESTION); q["criteria"]["any_of"][0]["offset"] = float("nan")
        with self.assertRaises(ValueError):
            self.match(q=q)
        with self.assertRaises(ValueError):
            self.match(c={**CANDIDATE, "support_hashes": ["not-a-hash"]})


if __name__ == "__main__":
    unittest.main()
