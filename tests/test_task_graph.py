import copy
import json
import unittest
from pathlib import Path

from tools.task_graph import project_tasks


class TaskGraphTests(unittest.TestCase):
    def test_flattens_nodes_with_original_fields_and_typed_edges(self):
        records = [{"id": "A", "title": "Alpha", "status": "ready", "summary": "s",
                   "dependencies": ["B", "missing"], "attempts": [{"note": "kept"}],
                   "custom": {"x": 1}, "subtasks": [{"id": "C", "title": "Child", "status": "done", "attempts": ["old"]}]},
                  {"id": "B", "title": "Beta", "status": "active", "summary": "b"}]
        before = copy.deepcopy(records)
        graph = project_tasks(records)
        self.assertEqual([node["id"] for node in graph["nodes"]], ["A", "C", "B"])
        self.assertEqual(graph["nodes"][0]["fields"], records[0])
        self.assertEqual(graph["nodes"][1]["fields"]["attempts"], ["old"])
        self.assertEqual([(edge["type"], edge["source"], edge["target"]) for edge in graph["edges"]],
                         [("contains", "A", "C"), ("prerequisite", "B", "A")])
        self.assertEqual(graph["unresolved_references"], [{"node_id": "A", "field": "dependencies", "target_id": "missing"}])
        self.assertEqual(records, before)

    def test_islands_and_deep_chain_are_iterative_and_stable(self):
        root = {"id": "n0", "title": "0", "status": "ready"}
        cursor = root
        for index in range(1, 1201):
            child = {"id": f"n{index}", "title": str(index), "status": "ready"}
            cursor["subtasks"] = [child]
            cursor = child
        records = [root, {"id": "island", "title": "island", "status": "blocked"}]
        first = project_tasks(records)
        second = project_tasks(records)
        self.assertEqual([node["id"] for node in first["nodes"]], [node["id"] for node in second["nodes"]])
        self.assertEqual(first["edges"], second["edges"])
        self.assertEqual(len(first["nodes"]), 1202)
        self.assertEqual(first["nodes"][-1]["id"], "island")
        self.assertEqual(sum(edge["type"] == "contains" for edge in first["edges"]), 1200)

    def test_current_board_projection_preserves_all_source_records(self):
        board = Path("docs/BOARD.md").read_text(encoding="utf-8")
        start = "<!-- ROADMAP_TASK_RECORDS:START -->"
        end = "<!-- ROADMAP_TASK_RECORDS:END -->"
        records = json.loads(board.split(start, 1)[1].split(end, 1)[0])
        graph = project_tasks(records)
        expected_nodes = sum(1 + len(record.get("subtasks", [])) for record in records)
        self.assertEqual(len(graph["nodes"]), expected_nodes)
        self.assertEqual(sum(node["id"] == "D01" for node in graph["nodes"]), 1)
        self.assertTrue(all(node["fields"]["id"] == node["id"] for node in graph["nodes"]))
        self.assertTrue(all(node["provenance"]["source"] == "ROADMAP_TASK_RECORDS/v1" for node in graph["nodes"]))


if __name__ == "__main__":
    unittest.main()
