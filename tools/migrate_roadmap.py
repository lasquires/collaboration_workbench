"""Hash-gated, lossless migration of the marked roadmap block to SQLite.

This utility never edits Markdown. Apply requires an explicit destination path,
and refuses to replace an existing database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

try:  # Support both `python tools/migrate_roadmap.py` and package imports.
    from .roadmap_store import RoadmapStore, StoreError
except ImportError:  # pragma: no cover - exercised by direct CLI invocation
    from roadmap_store import RoadmapStore, StoreError

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "docs" / "BOARD.md"
START = b"<!-- ROADMAP_TASK_RECORDS:START -->"
END = b"<!-- ROADMAP_TASK_RECORDS:END -->"


def extract_records(raw: bytes):
    if raw.count(START) != 1 or raw.count(END) != 1:
        raise ValueError("Board must contain exactly one marked task-record block")
    start = raw.index(START) + len(START)
    end = raw.index(END)
    if start > end:
        raise ValueError("Task-record block markers are reversed")
    try:
        text = raw[start:end].decode("utf-8")
        records = json.loads(text.strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Marked task block is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(records, list):
        raise ValueError("Marked task block must contain a JSON array")
    return records


def _flatten_ids(records):
    ids = []
    stack = list(reversed(records))
    while stack:
        record = stack.pop()
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("id"), str):
            ids.append(record["id"])
        children = record.get("subtasks", [])
        if isinstance(children, list):
            stack.extend(reversed(children))
    return ids


def inspect_board(board_path=BOARD):
    path = Path(board_path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    records = extract_records(raw)
    # Use precisely the production store's parser and graph validation in an
    # isolated temporary database, then prove the initial export is lossless.
    with tempfile.TemporaryDirectory(prefix="roadmap-preview-") as temp:
        store = RoadmapStore(Path(temp) / "preview.sqlite3")
        snapshot = store.import_v1(records, digest)
        exported = store.export_legacy_records()
        if exported != records:
            raise ValueError("SQLite export differs from marked source records")
    ids = _flatten_ids(records)
    id_set = set(ids)
    unresolved = []
    for record in records:
        stack = [record]
        while stack:
            current = stack.pop()
            if not isinstance(current, dict):
                continue
            task_id = current.get("id")
            for target in current.get("dependencies", []) if isinstance(current.get("dependencies", []), list) else []:
                if isinstance(target, str) and target not in id_set:
                    unresolved.append({"source_id": task_id, "target_id": target, "kind": "depends_on"})
            stack.extend(reversed(current.get("subtasks", [])) if isinstance(current.get("subtasks", []), list) else [])
    unresolved.sort(key=lambda item: (str(item["source_id"]), str(item["target_id"])))
    return {"source_sha256": digest, "record_count": len(records),
            "task_count": len(snapshot["tasks"]), "task_ids": ids,
            "event_count": len(snapshot["events"]), "unresolved_links": unresolved,
            "records": records}


def _check_database(path):
    with closing(sqlite3.connect(path)) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != [("ok",)]:
        raise ValueError(f"SQLite integrity_check failed: {integrity}")
    if foreign_keys:
        raise ValueError(f"SQLite foreign_key_check failed: {foreign_keys}")


def migrate(board_path, database_path, expected_source_sha256):
    preview = inspect_board(board_path)
    if expected_source_sha256 != preview["source_sha256"]:
        raise ValueError("Source SHA-256 does not match --expected-source-sha256")
    target = Path(database_path).expanduser().absolute()
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite selected database: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside target so final rename is same-volume atomic. The destination
    # is created only after import, integrity/FK checks and exact export pass.
    staging = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{target.name}.", suffix=".staging",
                                         dir=target.parent, delete=False) as handle:
            staging = Path(handle.name)
        staging.unlink()  # RoadmapStore requires an absent database path.
        store = RoadmapStore(staging)
        imported = store.import_v1(preview["records"], preview["source_sha256"])
        if store.export_legacy_records() != preview["records"]:
            raise ValueError("Staged SQLite export differs from source records")
        del store
        _check_database(staging)
        if imported["source_sha256"] != preview["source_sha256"]:
            raise ValueError("Staged database source hash mismatch")
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite selected database: {target}")
        os.replace(staging, target)
        staging = None
        return {key: value for key, value in preview.items() if key != "records"}
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def backup_database(source_path, destination_path):
    """Create a consistent closed-snapshot backup using SQLite's backup API."""
    source = Path(source_path).expanduser().absolute()
    destination = Path(destination_path).expanduser().absolute()
    if source == destination:
        raise ValueError("Backup source and destination must differ")
    if not source.is_file():
        raise FileNotFoundError(f"SQLite backup source does not exist: {source}")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite backup destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", suffix=".staging",
                                         dir=destination.parent, delete=False) as handle:
            staging = Path(handle.name)
        staging.unlink()
        # The source connection is opened read-only and only for the duration
        # of backup; SQLite provides a coherent snapshot even if WAL is used.
        source_uri = source.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as src, closing(sqlite3.connect(staging)) as dst:
            src.backup(dst)
        _check_database(staging)
        os.replace(staging, destination)
        staging = None
        return destination
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="validate and report without writes")
    mode.add_argument("--apply", action="store_true", help="stage and install to the explicit database path")
    mode.add_argument("--backup", action="store_true", help="SQLite-aware backup of a closed/read snapshot")
    parser.add_argument("--board", type=Path, default=BOARD, help=argparse.SUPPRESS)
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--backup-to", type=Path)
    parser.add_argument("--backup-from", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.backup:
            if not args.backup_from or not args.backup_to:
                parser.error("--backup requires --backup-from and --backup-to")
            backup_database(args.backup_from, args.backup_to)
            print(json.dumps({"backup": str(args.backup_to.absolute())}, indent=2))
            return 0
        if args.apply:
            if not args.expected_source_sha256 or not args.database:
                parser.error("--apply requires --expected-source-sha256 and --database")
            result = migrate(args.board, args.database, args.expected_source_sha256)
        else:
            result = {key: value for key, value in inspect_board(args.board).items() if key != "records"}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, StoreError) as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
