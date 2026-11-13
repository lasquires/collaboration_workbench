#!/usr/bin/env python3
"""Validate and search the versioned, evidence-linked variable occurrence index."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = REPO_ROOT / "analysis" / "variable-occurrences.json"
# This file now documents the workflow. Keep legacy variable wishes resolvable
# against the byte-preserved task-reference text after task state moves to SQLite.
DEFAULT_BOARD = REPO_ROOT / "docs" / "journal" / "BOARD-HISTORY-2026-11-12.md"
DEFAULT_NATIVE_MAP = REPO_ROOT / "analysis" / "native-map.json"
KINDS = {"plist_key", "native_property", "native_ivar", "local_or_parameter", "serialized_field", "other"}
EVIDENCE_STATUSES = {"executed", "instruction-derived", "source-key-match", "located", "names-only", "inferred", "unknown", "disputed"}
CONFIDENCES = {"high", "medium", "low", "unknown"}
MEANING_STATUSES = {"partial", "names-only", "unknown", "disputed", "established"}
WISH_STATUSES = {"unresolved", "resolved", "parked"}


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if "\x00" in value:
        return False
    posix = PurePosixPath(value.replace("\\", "/"))
    windows = PureWindowsPath(value)
    return not posix.is_absolute() and not windows.is_absolute() and not windows.drive and ".." not in posix.parts


def _board_task_ids(board: str) -> set[str]:
    start = "<!-- ROADMAP_TASK_RECORDS:START -->"
    end = "<!-- ROADMAP_TASK_RECORDS:END -->"
    if start not in board or end not in board:
        return set()
    try:
        records = json.loads(board.split(start, 1)[1].split(end, 1)[0].strip())
    except (ValueError, TypeError):
        return set()
    ids: set[str] = set()
    if not isinstance(records, list):
        return ids
    for record in records:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("id"), str):
            ids.add(record["id"])
        for child in record.get("subtasks", []):
            if isinstance(child, dict) and isinstance(child.get("id"), str):
                ids.add(child["id"])
    return ids


def validate_registry(
    document: Any,
    *,
    repo_root: Path = REPO_ROOT,
    board_path: Path = DEFAULT_BOARD,
    native_map_path: Path = DEFAULT_NATIVE_MAP,
    task_ids: set[str] | None = None,
    board_reference_text: str | None = None,
) -> list[str]:
    """Return actionable structural/reference errors; never assess native truth."""
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["registry: expected a JSON object"]
    if set(document) != {"schema_version", "occurrences", "task_wishes"}:
        errors.append("registry: fields must be exactly schema_version, occurrences, task_wishes")
    if document.get("schema_version") != 1 or isinstance(document.get("schema_version"), bool):
        errors.append("schema_version: expected integer 1")
    occurrences = document.get("occurrences")
    wishes = document.get("task_wishes")
    if not isinstance(occurrences, list):
        errors.append("occurrences: expected an array")
        occurrences = []
    if not isinstance(wishes, list):
        errors.append("task_wishes: expected an array")
        wishes = []

    board_text = (board_reference_text if board_reference_text is not None else
                  board_path.read_text(encoding="utf-8") if board_path.exists() else "")
    task_ids = set(task_ids) if task_ids is not None else _board_task_ids(board_text)
    try:
        native_map = _read_json(native_map_path)
        mapping_ids = {m.get("id") for m in native_map.get("mappings", []) if isinstance(m, dict)}
    except (OSError, ValueError, AttributeError, TypeError):
        mapping_ids = set()
        errors.append(f"native map: could not load {native_map_path}")

    occurrence_ids: set[str] = set()
    occurrence_by_id: dict[str, dict[str, Any]] = {}
    occurrence_fields = {
        "id", "name", "kind", "source", "discovered_at", "date_basis", "evidence",
        "meaning", "native_mapping_ids", "wish_ids", "possible_matches", "review_state",
    }
    evidence_fields = {"path", "claim", "status", "confidence"}
    for index, occurrence in enumerate(occurrences):
        label = f"occurrences[{index}]"
        if not isinstance(occurrence, dict):
            errors.append(f"{label}: expected an object")
            continue
        if set(occurrence) != occurrence_fields:
            errors.append(f"{label}: fields must be exactly {', '.join(sorted(occurrence_fields))}")
        oid = occurrence.get("id")
        if not isinstance(oid, str) or not re.fullmatch(r"VO-\d{6}", oid):
            errors.append(f"{label}.id: expected VO- followed by six digits")
        elif oid in occurrence_ids:
            errors.append(f"{label}.id: duplicate occurrence identifier {oid}")
        else:
            occurrence_ids.add(oid)
            occurrence_by_id[oid] = occurrence
        if not isinstance(occurrence.get("name"), str) or not occurrence.get("name", "").strip():
            errors.append(f"{label}.name: expected a non-empty string")
        if occurrence.get("kind") not in KINDS:
            errors.append(f"{label}.kind: unsupported occurrence kind")

        source = occurrence.get("source")
        if not isinstance(source, dict) or set(source) != {"artifact", "selector", "scope"}:
            errors.append(f"{label}.source: expected artifact, selector, and scope")
        else:
            if not _safe_relative_path(source.get("artifact")):
                errors.append(f"{label}.source.artifact: expected a safe repository-relative or excluded-source path")
            if not isinstance(source.get("selector"), str) or not source.get("selector", "").strip():
                errors.append(f"{label}.source.selector: expected a precise non-empty selector")
            if not isinstance(source.get("scope"), dict):
                errors.append(f"{label}.source.scope: expected an object of known scope dimensions")

        discovered = occurrence.get("discovered_at")
        date_basis = occurrence.get("date_basis")
        if discovered is None:
            if date_basis != "unknown":
                errors.append(f"{label}.date_basis: must be 'unknown' when discovered_at is null")
        elif not isinstance(discovered, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", discovered) or date_basis in (None, "unknown", ""):
            errors.append(f"{label}.discovered_at: expected YYYY-MM-DD with a non-unknown date_basis")

        evidence = occurrence.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{label}.evidence: expected at least one cited item")
            evidence = []
        for eidx, item in enumerate(evidence):
            elabel = f"{label}.evidence[{eidx}]"
            if not isinstance(item, dict) or set(item) != evidence_fields:
                errors.append(f"{elabel}: expected path, claim, status, and confidence")
                continue
            path = item.get("path")
            if not _safe_relative_path(path):
                errors.append(f"{elabel}.path: expected a safe repository-relative path")
            elif not (repo_root / Path(path)).is_file():
                errors.append(f"{elabel}.path: evidence file does not exist: {path}")
            if not isinstance(item.get("claim"), str) or not item.get("claim", "").strip():
                errors.append(f"{elabel}.claim: expected a bounded claim")
            if item.get("status") not in EVIDENCE_STATUSES:
                errors.append(f"{elabel}.status: unsupported evidence status")
            if item.get("confidence") not in CONFIDENCES:
                errors.append(f"{elabel}.confidence: unsupported confidence")

        meaning = occurrence.get("meaning")
        if not isinstance(meaning, dict) or set(meaning) != {"summary", "status", "confidence"}:
            errors.append(f"{label}.meaning: expected summary, status, and confidence")
        else:
            if not isinstance(meaning.get("summary"), str) or not meaning.get("summary", "").strip():
                errors.append(f"{label}.meaning.summary: expected a bounded summary")
            if meaning.get("status") not in MEANING_STATUSES:
                errors.append(f"{label}.meaning.status: unsupported meaning status")
            if meaning.get("confidence") not in CONFIDENCES:
                errors.append(f"{label}.meaning.confidence: unsupported confidence")

        native_ids = occurrence.get("native_mapping_ids")
        if not isinstance(native_ids, list) or any(not isinstance(mid, str) for mid in native_ids):
            errors.append(f"{label}.native_mapping_ids: expected an array of mapping IDs")
        else:
            for mid in native_ids:
                if mid not in mapping_ids:
                    errors.append(f"{label}.native_mapping_ids: unknown native-map mapping ID {mid}")
        wish_ids = occurrence.get("wish_ids")
        if not isinstance(wish_ids, list) or any(not isinstance(wid, str) for wid in wish_ids):
            errors.append(f"{label}.wish_ids: expected an array of wish IDs")
        if occurrence.get("review_state") not in {"unreviewed", "reviewed", "disputed"}:
            errors.append(f"{label}.review_state: unsupported review state")
        matches = occurrence.get("possible_matches")
        if not isinstance(matches, list):
            errors.append(f"{label}.possible_matches: expected an array")
        else:
            seen_match_ids: set[str] = set()
            for midx, match in enumerate(matches):
                mlabel = f"{label}.possible_matches[{midx}]"
                if not isinstance(match, dict) or set(match) != {"occurrence_id", "reason", "status"}:
                    errors.append(f"{mlabel}: expected occurrence_id, reason, and status")
                    continue
                target = match.get("occurrence_id")
                if target == oid or not isinstance(target, str):
                    errors.append(f"{mlabel}.occurrence_id: must reference a different occurrence")
                if target in seen_match_ids:
                    errors.append(f"{mlabel}.occurrence_id: duplicate candidate reference {target}")
                seen_match_ids.add(target)
                if match.get("status") not in {"lead", "confirmed_same_concept", "distinct", "unresolved"}:
                    errors.append(f"{mlabel}.status: unsupported match status")
                if not isinstance(match.get("reason"), str) or not match.get("reason", "").strip():
                    errors.append(f"{mlabel}.reason: expected an explanation")

    wish_ids_seen: set[str] = set()
    wishes_by_id: dict[str, dict[str, Any]] = {}
    wish_fields = {"id", "question", "status", "occurrence_ids", "task_id", "board_reference"}
    for index, wish in enumerate(wishes):
        label = f"task_wishes[{index}]"
        if not isinstance(wish, dict):
            errors.append(f"{label}: expected an object")
            continue
        if set(wish) != wish_fields:
            errors.append(f"{label}: fields must be exactly {', '.join(sorted(wish_fields))}")
        wid = wish.get("id")
        if not isinstance(wid, str) or not re.fullmatch(r"VW-\d{6}", wid):
            errors.append(f"{label}.id: expected VW- followed by six digits")
        elif wid in wish_ids_seen:
            errors.append(f"{label}.id: duplicate wish identifier {wid}")
        else:
            wish_ids_seen.add(wid)
            wishes_by_id[wid] = wish
        if not isinstance(wish.get("question"), str) or not wish.get("question", "").strip():
            errors.append(f"{label}.question: expected a concrete unresolved question")
        if wish.get("status") not in WISH_STATUSES:
            errors.append(f"{label}.status: unsupported wish status")
        refs = wish.get("occurrence_ids")
        if not isinstance(refs, list) or not refs:
            errors.append(f"{label}.occurrence_ids: expected at least one occurrence reference")
        else:
            for ref in refs:
                if ref not in occurrence_ids:
                    errors.append(f"{label}.occurrence_ids: unknown occurrence ID {ref}")
        task_id = wish.get("task_id")
        if task_id is not None and task_id not in task_ids:
            errors.append(f"{label}.task_id: must reference a real migrated board task/subtask ID")
        board_ref = wish.get("board_reference")
        if not isinstance(board_ref, str) or not board_ref.strip() or board_ref not in board_text:
            errors.append(f"{label}.board_reference: expected an existing board heading/reference")
        if task_id is None and wish.get("status") == "unresolved" and not board_ref:
            errors.append(f"{label}: unassigned unresolved wish needs a board reference for coordinator routing")

    for oid, occurrence in occurrence_by_id.items():
        refs = occurrence.get("wish_ids")
        if isinstance(refs, list):
            if len(refs) != len(set(refs)):
                errors.append(f"occurrence {oid}.wish_ids: duplicate wish reference")
            for wid in refs:
                if wid not in wish_ids_seen:
                    errors.append(f"occurrence {oid}.wish_ids: unknown wish ID {wid}")
                elif oid not in wishes_by_id[wid].get("occurrence_ids", []):
                    errors.append(f"occurrence {oid}.wish_ids: wish {wid} does not reference this occurrence")
        for match in occurrence.get("possible_matches", []) if isinstance(occurrence.get("possible_matches"), list) else []:
            if not isinstance(match, dict):
                continue
            target = match.get("occurrence_id")
            if target not in occurrence_ids:
                errors.append(f"occurrence {oid}.possible_matches: unknown occurrence ID {target}")
            elif isinstance(target, str) and target in occurrence_by_id:
                left = re.sub(r"[^a-z0-9]+", "", str(occurrence.get("name", "")).casefold())
                right = re.sub(r"[^a-z0-9]+", "", str(occurrence_by_id[target].get("name", "")).casefold())
                if match.get("status") == "lead" and left != right:
                    errors.append(f"occurrence {oid}.possible_matches: a lead must have the same normalized spelling")
    for wid, wish in wishes_by_id.items():
        for oid in wish.get("occurrence_ids", []) if isinstance(wish.get("occurrence_ids"), list) else []:
            if oid in occurrence_by_id and wid not in occurrence_by_id[oid].get("wish_ids", []):
                errors.append(f"wish {wid}.occurrence_ids: occurrence {oid} does not link back to this wish")
    return errors


def search_occurrences(document: dict[str, Any], query: str, *, exact: bool = False) -> list[dict[str, Any]]:
    """Search occurrences by name, location, scope, evidence, status, or wish text."""
    needle = query.casefold()
    wishes = {w.get("id"): w for w in document.get("task_wishes", []) if isinstance(w, dict)}
    output = []
    for occurrence in document.get("occurrences", []):
        if not isinstance(occurrence, dict):
            continue
        name_match = str(occurrence.get("name", "")).casefold() == needle if exact else needle in str(occurrence.get("name", "")).casefold()
        haystack = json.dumps(occurrence, ensure_ascii=False, sort_keys=True).casefold()
        wish_text = " ".join(str(wishes.get(wid, {}).get("question", "")) for wid in occurrence.get("wish_ids", []))
        if name_match or (not exact and (needle in haystack or needle in wish_text.casefold())):
            output.append(occurrence)
    return output


def _display(occurrence: dict[str, Any], all_occurrences: list[dict[str, Any]], wishes: list[dict[str, Any]]) -> str:
    same_name = [o for o in all_occurrences if o.get("id") != occurrence.get("id") and o.get("name", "").casefold() == occurrence.get("name", "").casefold()]
    lines = [
        f"{occurrence.get('id')}  {occurrence.get('name')}  [{occurrence.get('kind')}]",
        f"  source: {occurrence.get('source', {}).get('artifact')} :: {occurrence.get('source', {}).get('selector')}",
        f"  scope: {json.dumps(occurrence.get('source', {}).get('scope', {}), ensure_ascii=False, sort_keys=True)}",
        f"  meaning: {occurrence.get('meaning', {}).get('status')} / {occurrence.get('meaning', {}).get('confidence')} — {occurrence.get('meaning', {}).get('summary')}",
        f"  discovery: {occurrence.get('discovered_at') or 'unknown'} ({occurrence.get('date_basis')})",
    ]
    for match in occurrence.get("possible_matches", []):
        lines.append(f"  candidate lead: {match.get('occurrence_id')} — {match.get('reason')}")
    configured = {m.get("occurrence_id") for m in occurrence.get("possible_matches", []) if isinstance(m, dict) and m.get("status") == "lead"}
    for candidate in same_name:
        if candidate.get("id") not in configured:
            lines.append(f"  same-spelling candidate (unreviewed lead): {candidate.get('id')} — meanings remain separate")
    for wid in occurrence.get("wish_ids", []):
        wish = next((w for w in wishes if w.get("id") == wid), None)
        if wish:
            lines.append(f"  wish {wid} [{wish.get('status')}]: {wish.get('question')}")
    for item in occurrence.get("evidence", []):
        lines.append(f"  evidence: {item.get('path')} [{item.get('status')}/{item.get('confidence')}] {item.get('claim')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate", help="validate schema and evidence references")
    validate_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    validate_parser.add_argument("--board", type=Path, default=DEFAULT_BOARD)
    validate_parser.add_argument("--native-map", type=Path, default=DEFAULT_NATIVE_MAP)
    search_parser = sub.add_parser("search", help="search and list occurrences without merging them")
    search_parser.add_argument("query")
    search_parser.add_argument("--exact", action="store_true", help="match occurrence names exactly")
    search_parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    args = parser.parse_args(argv)
    try:
        document = _read_json(args.registry)
    except (OSError, ValueError) as exc:
        print(f"Cannot read registry {args.registry}: {exc}", file=sys.stderr)
        return 2
    if args.command == "validate":
        errors = validate_registry(document, repo_root=args.registry.resolve().parents[1], board_path=args.board, native_map_path=args.native_map)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print(f"OK: {len(document['occurrences'])} occurrences and {len(document['task_wishes'])} task wishes; structural/reference validation only.")
        return 0
    results = search_occurrences(document, args.query, exact=args.exact)
    if not results:
        print("No matching occurrences.")
        return 0
    for i, occurrence in enumerate(results):
        if i:
            print()
        print(_display(occurrence, document.get("occurrences", []), document.get("task_wishes", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
