"""Read-only projection of the roadmap's legacy v1 task array."""
from __future__ import annotations

from typing import Any


SOURCE_VERSION = "ROADMAP_TASK_RECORDS/v1"


def _clone_iterative(value: Any) -> Any:
    """Clone JSON-shaped data without Python's recursion-limited deepcopy."""
    if isinstance(value, dict):
        root: Any = {}
    elif isinstance(value, list):
        root = []
    else:
        return value
    memo = {id(value): root}
    pending = [(value, root)]
    while pending:
        source, target = pending.pop()
        items = source.items() if isinstance(source, dict) else enumerate(source)
        for key, child in items:
            if isinstance(child, dict):
                clone = memo.get(id(child))
                if clone is None:
                    clone = {}
                    memo[id(child)] = clone
                    pending.append((child, clone))
            elif isinstance(child, list):
                clone = memo.get(id(child))
                if clone is None:
                    clone = []
                    memo[id(child)] = clone
                    pending.append((child, clone))
            else:
                clone = child
            if isinstance(target, dict):
                target[key] = clone
            else:
                target.append(clone)
    return root


def project_tasks(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a stable flat graph while retaining each source record verbatim.

    Node fields are deep copies of the full original objects. Provenance paths
    identify the record's position in the original top-level/subtasks arrays.
    Dependencies point prerequisite -> dependent. Missing targets are explicit
    unresolved references and never become synthetic graph nodes.
    """
    if not isinstance(records, list):
        raise TypeError("records must be a list")

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    source_ids: set[int] = set()
    ids: set[str] = set()
    source_copy = _clone_iterative(records)
    pending: list[tuple[dict[str, Any], str | None, list[int]]] = []
    for index in range(len(records) - 1, -1, -1):
        pending.append((source_copy[index], None, [index]))

    # Reverse-push children to retain source-order preorder without recursion.
    while pending:
        record, parent_id, path = pending.pop()
        if not isinstance(record, dict):
            raise TypeError(f"task at {path!r} must be an object")
        identity = id(record)
        if identity in source_ids:
            raise ValueError(f"task object is repeated or cyclic at {path!r}")
        source_ids.add(identity)
        task_id = record.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"task at {path!r} has no non-empty string id")
        if task_id in ids:
            raise ValueError(f"duplicate task id: {task_id}")
        ids.add(task_id)
        nodes.append({
            "id": task_id,
            "parent_id": parent_id,
            "fields": record,
            "provenance": {"source": SOURCE_VERSION, "path": path},
        })
        if parent_id is not None:
            edges.append({"type": "contains", "source": parent_id, "target": task_id})
        children = record.get("subtasks", [])
        if not isinstance(children, list):
            raise TypeError(f"subtasks for {task_id} must be a list")
        for index in range(len(children) - 1, -1, -1):
            pending.append((children[index], task_id, path + ["subtasks", index]))

    # IDs are now known, allowing forward references between top-level tasks.
    unresolved: list[dict[str, str]] = []
    for node in nodes:
        dependencies = node["fields"].get("dependencies", [])
        if not isinstance(dependencies, list):
            raise TypeError(f"dependencies for {node['id']} must be a list")
        for target_id in dependencies:
            if not isinstance(target_id, str):
                unresolved.append({"node_id": node["id"], "field": "dependencies", "target_id": repr(target_id)})
            elif target_id in ids:
                edges.append({"type": "prerequisite", "source": target_id, "target": node["id"]})
            else:
                unresolved.append({"node_id": node["id"], "field": "dependencies", "target_id": target_id})

    return {
        "schema": "task-graph/v1",
        "nodes": nodes,
        "edges": edges,
        "unresolved_references": unresolved,
        "coverage": {"source": SOURCE_VERSION, "top_level_records": len(records), "node_count": len(nodes)},
    }
