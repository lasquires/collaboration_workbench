import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import roadmap_server as roadmap


class RoadmapServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.board_path = Path(self.temp.name) / "BOARD.md"
        self.original = "# Existing prose\nold status mentions are historical.\n\n" + roadmap.START + "\n" + json.dumps([
            {"id": "p03", "title": "Dashboard", "status": "active", "summary": "Shared source", "owner": "worker",
             "dependencies": ["p02"], "evidence": ["analysis/native-map.json"], "acceptance": ["Preserve gate"],
             "parent_gate": "Parent remains active until acceptance passes.", "claim": None,
             "subtasks": [{"id": "p03-api", "title": "API update", "status": "active",
                           "evidence": ["analysis/native-map.json"], "attempts": ["run 1: conflict handled"]}]}
        ], indent=2) + "\n" + roadmap.END + "\n# trailing unrelated content\n"
        self.board_path.write_text(self.original, encoding="utf-8")
        self.old_board = roadmap.BOARD
        roadmap.BOARD = self.board_path

    def tearDown(self):
        roadmap.BOARD = self.old_board
        self.temp.cleanup()

    def test_read_exposes_current_records_and_nested_subtasks(self):
        result = roadmap.snapshot()
        self.assertTrue(result["claims_available"])
        self.assertEqual(result["source"], "docs/BOARD.md")
        self.assertEqual(result["tasks"][0]["subtasks"][0]["attempts"], ["run 1: conflict handled"])

    def test_claim_and_release_preserve_unrelated_board_bytes(self):
        before, revision, _, _ = roadmap._load_board()
        updated = roadmap.update_claim("p03", "Luke", revision)
        self.assertEqual(updated["tasks"][0]["claim"], {"by": "Luke"})
        after_claim = self.board_path.read_bytes().decode("utf-8")
        self.assertTrue(after_claim.startswith(before[:before.index(roadmap.START)]))
        self.assertTrue(after_claim.endswith(before[before.index(roadmap.END):]))
        updated = roadmap.update_claim("p03", "Luke", updated["revision"], release=True)
        self.assertIsNone(updated["tasks"][0]["claim"])
        final = self.board_path.read_bytes().decode("utf-8")
        self.assertTrue(final.startswith(before[:before.index(roadmap.START)]))
        self.assertTrue(final.endswith(before[before.index(roadmap.END):]))

    def test_claim_is_single_owner_and_release_requires_identity(self):
        _, revision, _, _ = roadmap._load_board()
        first = roadmap.update_claim("p03", "Alice", revision)
        with self.assertRaises(roadmap.RoadmapError) as ctx:
            roadmap.update_claim("p03", "Bob", first["revision"])
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(roadmap.RoadmapError) as ctx:
            roadmap.update_claim("p03", "Bob", first["revision"], release=True)
        self.assertEqual(ctx.exception.status, 409)
        released = roadmap.update_claim("p03", "Alice", first["revision"], release=True)
        self.assertIsNone(released["tasks"][0]["claim"])

    def test_stale_revision_conflicts_without_mutating(self):
        _, rev, _, _ = roadmap._load_board()
        self.board_path.write_text(self.original + "external edit\n", encoding="utf-8")
        latest = self.board_path.read_text(encoding="utf-8")
        with self.assertRaises(roadmap.RoadmapError) as ctx:
            roadmap.update_claim("p03", "Luke", rev)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.board_path.read_text(encoding="utf-8"), latest)

    def test_invalid_input_and_missing_records_fail_closed(self):
        _, rev, _, _ = roadmap._load_board()
        for task_id, claimant, expected, release in [
            ("../BOARD.md", "x", rev, False), ("p03", "x", "bad", False), ("p03", "", rev, False),
            ("missing", "x", rev, False), ("p03", "", rev, "yes"),
        ]:
            with self.assertRaises(roadmap.RoadmapError):
                roadmap.update_claim(task_id, claimant, expected, release)
        self.assertEqual(self.board_path.read_text(encoding="utf-8"), self.original)

    def test_unsafe_links_invalidate_record_block(self):
        bad = self.original.replace("analysis/native-map.json", "javascript:alert(1)")
        self.board_path.write_text(bad, encoding="utf-8")
        snap = roadmap.snapshot()
        self.assertFalse(snap["claims_available"])
        self.assertEqual(snap["tasks"], [])

    def test_malformed_optional_fields_duplicate_ids_and_grandchildren_fail_closed(self):
        baseline = roadmap._load_board()[2]
        cases = []
        invalid = copy.deepcopy(baseline); invalid[0]["dependencies"] = "p02"; cases.append(invalid)
        invalid = copy.deepcopy(baseline); invalid[0]["claim"] = []; cases.append(invalid)
        invalid = copy.deepcopy(baseline); invalid[0]["status"] = []; cases.append(invalid)
        invalid = copy.deepcopy(baseline); invalid[0]["subtasks"][0]["id"] = "p03"; cases.append(invalid)
        invalid = copy.deepcopy(baseline); invalid[0]["subtasks"][0]["subtasks"] = []; cases.append(invalid)
        invalid = copy.deepcopy(baseline); invalid[0]["status"] = "imaginary"; cases.append(invalid)
        invalid = copy.deepcopy(baseline); invalid[0]["evidence"] = ["//["]; cases.append(invalid)
        for records in cases:
            prefix = self.original[:self.original.index(roadmap.START) + len(roadmap.START)]
            suffix = self.original[self.original.index(roadmap.END):]
            changed = prefix + "\n" + json.dumps(records, indent=2) + "\n" + suffix
            self.board_path.write_text(changed, encoding="utf-8")
            self.assertFalse(roadmap.snapshot()["claims_available"])

    def test_agent_can_update_nested_subtask_without_completing_parent(self):
        _, revision, _, _ = roadmap._load_board()
        updated = roadmap.update_task("p03", revision,
                                      {"status": "done", "attempts": ["run 1", "run 2"]},
                                      subtask_id="p03-api")
        parent = updated["tasks"][0]
        self.assertEqual(parent["status"], "active")
        self.assertEqual(parent["subtasks"][0]["status"], "done")
        self.assertEqual(parent["subtasks"][0]["attempts"], ["run 1", "run 2"])

    def test_agent_can_create_function_level_subtask_with_fresh_revision(self):
        _, revision, _, _ = roadmap._load_board()
        result = roadmap.update_task("p03", revision, {}, new_subtask={
            "id": "p03-copy", "title": "Handoff copy", "status": "ready",
            "evidence": ["docs/BOARD.md"], "attempts": []
        })
        self.assertEqual([x["id"] for x in result["tasks"][0]["subtasks"]], ["p03-api", "p03-copy"])

    def test_update_cannot_write_record_that_reader_would_reject(self):
        _, revision, _, _ = roadmap._load_board()
        with self.assertRaises(roadmap.RoadmapError) as ctx:
            roadmap.update_task("p03", revision, {"summary": "   "})
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(roadmap.snapshot()["tasks"][0]["summary"], "Shared source")
        _, revision, _, _ = roadmap._load_board()
        with self.assertRaises(roadmap.RoadmapError) as ctx:
            roadmap.update_task("p03", revision, {}, new_subtask={
                "id": "p03-empty", "title": " ", "status": "ready"
            })
        self.assertEqual(ctx.exception.status, 400)

    def test_parent_done_requires_explicit_gate_and_evidence(self):
        _, revision, _, _ = roadmap._load_board()
        with self.assertRaises(roadmap.RoadmapError) as ctx:
            roadmap.update_task("p03", revision, {"status": "done"})
        self.assertEqual(ctx.exception.status, 409)
        _, revision, _, _ = roadmap._load_board()
        updated = roadmap.update_task("p03", revision,
                                      {"status": "done", "acceptance_gate_passed": True,
                                       "evidence": ["analysis/native-map.json"]})
        self.assertEqual(updated["tasks"][0]["status"], "done")

    def test_agent_update_stale_revision_and_invalid_field_are_rejected(self):
        _, revision, _, _ = roadmap._load_board()
        self.board_path.write_text(self.original + "concurrent edit\n", encoding="utf-8")
        with self.assertRaises(roadmap.RoadmapError) as ctx:
            roadmap.update_task("p03", revision, {"status": "done"})
        self.assertEqual(ctx.exception.status, 409)
        _, new_revision, _, _ = roadmap._load_board()
        with self.assertRaises(roadmap.RoadmapError):
            roadmap.update_task("p03", new_revision, {"claim": {"by": "bypass"}})
        self.assertEqual(roadmap.snapshot()["tasks"][0]["status"], "active")

    def test_unhashable_status_update_returns_validation_error(self):
        _, revision, _, _ = roadmap._load_board()
        with self.assertRaises(roadmap.RoadmapError) as ctx:
            roadmap.update_task("p03", revision, {"status": []})
        self.assertEqual(ctx.exception.status, 400)

    def test_unmigrated_board_disables_updates(self):
        self.board_path.write_text("# board with historical prose\n", encoding="utf-8")
        snap = roadmap.snapshot()
        self.assertFalse(snap["claims_available"])
        with self.assertRaises(roadmap.RoadmapError) as ctx:
            roadmap.update_claim("p03", "Luke", snap["revision"])
        self.assertEqual(ctx.exception.status, 409)


class RoadmapDashboardTests(unittest.TestCase):
    def run_refresh_case(self, case):
        node = shutil.which("node") or str(Path(os.environ.get("USERPROFILE", "")) /
            ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")
        page = (roadmap.ROOT / "docs/roadmap-dashboard.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
        # Exercise the page's refresh code without opening a browser. Rendering
        # is a boundary stub: it replaces controls with the fetched record.
        script = script[:script.rindex("document.querySelector('#refresh').onclick")]
        setup = r"""
const assert=require('node:assert/strict');
const controls={taskstatus:{id:'taskstatus',value:'active'},taskevidence:{id:'taskevidence',value:'docs/A.md'}};
const elements={notice:{textContent:''},updated:{textContent:''},main:{innerHTML:'task'},discardDrafts:{hidden:true}};
const document={querySelector:s=>elements[s.slice(1)]||controls[s.slice(1)]||null,
querySelectorAll:s=>s==='#main [id]'?Object.values(controls):[],getElementById:id=>controls[id]||elements[id]};
let incoming={source:'docs/BOARD.md',revision:'0'.repeat(64),message:'live',tasks:[{id:'x',stage:'Stage',status:'active'}]},fail=false,renders=0;
const fetch=async()=>{if(fail)throw Error('outage');return {ok:true,json:async()=>incoming}};
"""
        probe = r"""
data=incoming;selected='x';selectedStage='Stage';
if(typeof formBaseline!=='undefined')formBaseline=JSON.stringify(captureDrafts());
render=()=>{renders++;controls.taskstatus.value=data.tasks[0].status;controls.taskevidence.value='docs/B.md';if(typeof formBaseline!=='undefined')formBaseline=JSON.stringify(captureDrafts());};
""" + case
        result = subprocess.run([node, "-"], input=setup+script+probe,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_poll_keeps_draft_bound_to_original_revision(self):
        self.run_refresh_case(r"""
(async()=>{controls.taskevidence.value='docs/DRAFT.md';
incoming={...incoming,revision:'1'.repeat(64),tasks:[{id:'x',stage:'Stage',status:'blocked'}]};
await refresh();assert.equal(data.revision,'0'.repeat(64));assert.equal(controls.taskevidence.value,'docs/DRAFT.md');
assert.match(elements.notice.textContent,/changed|newer/i);assert.equal(elements.discardDrafts.hidden,false);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_poll_updates_untouched_controls(self):
        self.run_refresh_case(r"""
(async()=>{incoming={...incoming,revision:'1'.repeat(64),tasks:[{id:'x',stage:'Stage',status:'blocked'}]};
await refresh();assert.equal(data.revision,'1'.repeat(64));assert.equal(controls.taskstatus.value,'blocked');
assert.equal(controls.taskevidence.value,'docs/B.md');})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_same_revision_recovery_clears_unavailable_notice(self):
        self.run_refresh_case(r"""
(async()=>{fail=true;await refresh();assert.match(elements.notice.textContent,/unavailable/);
fail=false;await refresh();assert.equal(elements.notice.textContent,'live');assert.equal(elements.main.innerHTML,'task');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


if __name__ == "__main__":
    unittest.main()
