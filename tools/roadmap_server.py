"""Local read/write dashboard backed by an explicit task block in docs/BOARD.md.

Run from the repository root: python tools/roadmap_server.py
The service binds only to loopback. Until the coordinator migrates records into
the marked JSON block, claims are deliberately unavailable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "docs" / "BOARD.md"
START = "<!-- ROADMAP_TASK_RECORDS:START -->"
END = "<!-- ROADMAP_TASK_RECORDS:END -->"
_WRITE_LOCK = threading.Lock()
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
_STATUSES = {"ready", "active", "blocked", "review", "done"}


class RoadmapError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _load_board() -> tuple[str, str, list[dict], bool]:
    try:
        # Decode exact bytes instead of universal-newline translation so the
        # revision check matches the on-disk file on both CRLF and LF systems.
        raw = BOARD.read_bytes().decode("utf-8")
    except OSError as exc:
        raise RoadmapError(500, "Authoritative board is unavailable") from exc
    revision = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if raw.count(START) != 1 or raw.count(END) != 1 or raw.index(START) > raw.index(END):
        return raw, revision, [], False
    a, b = raw.index(START) + len(START), raw.index(END)
    try:
        value = json.loads(raw[a:b].strip())
    except (ValueError, TypeError):
        return raw, revision, [], False
    if not isinstance(value, list) or any(not _valid_record(x) for x in value):
        return raw, revision, [], False
    if not _unique_ids(value):
        return raw, revision, [], False
    return raw, revision, value, True


def _valid_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("id"), str) or not _ID.fullmatch(record["id"]):
        return False
    if not all(isinstance(record.get(k), str) and record[k].strip() for k in ("title", "status", "summary")):
        return False
    if record["status"] not in _STATUSES:
        return False
    for key in ("owner", "parent_gate", "stage", "prior_work", "next_step"):
        if key in record and not isinstance(record[key], str):
            return False
    for key in ("dependencies", "acceptance"):
        if key in record and (not isinstance(record[key], list) or not all(isinstance(x, str) for x in record[key])):
            return False
    if "acceptance_gate_passed" in record and not isinstance(record["acceptance_gate_passed"], bool):
        return False
    claim = record.get("claim")
    if claim is not None and (not isinstance(claim, dict) or not isinstance(claim.get("by"), str) or not claim["by"].strip()):
        return False
    subtasks = record.get("subtasks", [])
    if not isinstance(subtasks, list):
        return False
    for item in subtasks:
        if not _valid_subtask(item):
            return False
    for key in ("evidence",):
        if not isinstance(record.get(key, []), list) or not all(_safe_link(x) for x in record.get(key, [])):
            return False
    return True


def _valid_subtask(item: object) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _ID.fullmatch(item["id"]):
        return False
    if not all(isinstance(item.get(k), str) and item[k].strip() for k in ("title", "status")):
        return False
    if item["status"] not in _STATUSES:
        return False
    if "summary" in item and not isinstance(item["summary"], str):
        return False
    if "subtasks" in item:  # Function-level children are leaves.
        return False
    if not isinstance(item.get("evidence", []), list) or not all(_safe_link(x) for x in item.get("evidence", [])):
        return False
    return isinstance(item.get("attempts", []), list) and all(isinstance(x, (str, dict)) for x in item.get("attempts", []))


def _unique_ids(records: list[dict]) -> bool:
    ids = [item["id"] for item in records]
    ids += [sub["id"] for item in records for sub in item.get("subtasks", [])]
    return len(ids) == len(set(ids))


def _safe_link(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme or parsed.netloc or value.startswith("/"):
        return False
    try:
        (ROOT / value).resolve().relative_to(ROOT.resolve())
        return True
    except (ValueError, OSError):
        return False


def snapshot() -> dict:
    raw, revision, tasks, available = _load_board()
    return {
        "source": "docs/BOARD.md", "revision": revision,
        "claims_available": available,
        "message": "Claims use the shared board task records." if available else
                   "Shared task records are not migrated. Showing board source only; claims are disabled.",
        "tasks": tasks, "board": raw,
    }


def update_claim(task_id: object, claimant: object, expected_revision: object, release: object = False) -> dict:
    if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        raise RoadmapError(400, "Invalid task id")
    if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
        raise RoadmapError(400, "A valid board revision is required")
    if not isinstance(release, bool):
        raise RoadmapError(400, "Invalid release value")
    if not release and (not isinstance(claimant, str) or not claimant.strip() or len(claimant) > 120):
        raise RoadmapError(400, "Claimant must be 1–120 characters")
    with _WRITE_LOCK:
        raw, revision, records, available = _load_board()
        if revision != expected_revision:
            raise RoadmapError(409, "Board changed; refresh before updating")
        if not available:
            raise RoadmapError(409, "Shared task records are not migrated")
        record = next((x for x in records if x["id"] == task_id), None)
        if record is None:
            raise RoadmapError(404, "Task record not found")
        current_claim = record.get("claim")
        if release:
            if not current_claim:
                raise RoadmapError(409, "Task has no active claim")
            if not isinstance(claimant, str) or claimant.strip() != current_claim["by"]:
                raise RoadmapError(409, "Only the current claimant can release this claim")
        elif current_claim and current_claim["by"] != claimant.strip():
            raise RoadmapError(409, "Task is already claimed by another person")
        record["claim"] = None if release else {"by": claimant.strip()}
        a, b = raw.index(START) + len(START), raw.index(END)
        replacement = "\n" + json.dumps(records, ensure_ascii=False, indent=2) + "\n"
        updated = raw[:a] + replacement + raw[b:]
        # Atomic replacement preserves every byte outside the marked block.
        fd, temp_name = tempfile.mkstemp(prefix=".roadmap-board-", suffix=".tmp", dir=BOARD.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            # Recheck immediately before replace to reject stale cooperative writers.
            if hashlib.sha256(BOARD.read_bytes()).hexdigest() != revision:
                raise RoadmapError(409, "Board changed during update; refresh before updating")
            os.replace(temp_name, BOARD)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return snapshot()


def update_task(task_id: object, expected_revision: object, fields: object,
                subtask_id: object = None, new_subtask: object = None) -> dict:
    """Optimistically update a task or nested subtask; all agents use this API."""
    if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        raise RoadmapError(400, "Invalid task id")
    if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
        raise RoadmapError(400, "A valid board revision is required")
    if not isinstance(fields, dict) or (not fields and new_subtask is None):
        raise RoadmapError(400, "Update fields must be a non-empty object")
    if subtask_id is not None and (not isinstance(subtask_id, str) or not _ID.fullmatch(subtask_id)):
        raise RoadmapError(400, "Invalid subtask id")
    if new_subtask is not None and (subtask_id is not None or not _valid_subtask(new_subtask)):
        raise RoadmapError(400, "Invalid function-level subtask")
    allowed = {"status", "owner", "summary", "dependencies", "evidence", "acceptance", "parent_gate", "acceptance_gate_passed", "stage", "prior_work", "next_step"} if subtask_id is None else {"status", "title", "summary", "evidence", "attempts"}
    if not set(fields).issubset(allowed):
        raise RoadmapError(400, "Unsupported update field")
    if "status" in fields and (not isinstance(fields["status"], str) or fields["status"] not in _STATUSES):
        raise RoadmapError(400, "Unsupported task status")
    if "acceptance_gate_passed" in fields and not isinstance(fields["acceptance_gate_passed"], bool):
        raise RoadmapError(400, "acceptance_gate_passed must be boolean")
    for key in ("evidence", "dependencies", "acceptance"):
        if key in fields and (not isinstance(fields[key], list) or not all(isinstance(x, str) for x in fields[key])):
            raise RoadmapError(400, f"{key} must be a list of strings")
        if key == "evidence" and key in fields and not all(_safe_link(x) for x in fields[key]):
            raise RoadmapError(400, "Evidence links must resolve inside the repository")
    if "attempts" in fields and (not isinstance(fields["attempts"], list) or not all(isinstance(x, (str, dict)) for x in fields["attempts"])):
        raise RoadmapError(400, "attempts must be a list of strings or objects")
    if any(not isinstance(v, str) or len(v) > 2000 for k, v in fields.items() if k in {"title", "owner", "summary", "parent_gate", "stage", "prior_work", "next_step"}):
        raise RoadmapError(400, "Text update is invalid or too long")
    with _WRITE_LOCK:
        raw, revision, records, available = _load_board()
        if revision != expected_revision:
            raise RoadmapError(409, "Board changed; refresh before updating")
        if not available:
            raise RoadmapError(409, "Shared task records are not migrated")
        record = next((x for x in records if x["id"] == task_id), None)
        if record is None:
            raise RoadmapError(404, "Task record not found")
        target = record
        if subtask_id is not None:
            if record["status"] == "done":
                raise RoadmapError(409, "Reopen the parent task before updating its subtask")
            target = next((x for x in record.get("subtasks", []) if x["id"] == subtask_id), None)
            if target is None:
                raise RoadmapError(404, "Nested subtask not found")
        if new_subtask is not None:
            if record["status"] == "done":
                raise RoadmapError(409, "Reopen the parent task before adding a subtask")
            if any(item["id"] == new_subtask["id"] for item in records) or any(
                    sub["id"] == new_subtask["id"] for item in records for sub in item.get("subtasks", [])):
                raise RoadmapError(409, "Task/subtask ID already exists")
            record.setdefault("subtasks", []).append(new_subtask)
        if subtask_id is None:
            gate_passed = fields.get("acceptance_gate_passed", record.get("acceptance_gate_passed", False))
            resulting_status = fields.get("status", record["status"])
            resulting_evidence = fields.get("evidence", record.get("evidence", []))
            if resulting_status == "done" and (gate_passed is not True or not resulting_evidence):
                raise RoadmapError(409, "Parent cannot be done until its acceptance gate is marked passed with evidence")
            if gate_passed is False and resulting_status == "done":
                raise RoadmapError(409, "A completed parent must retain its passed acceptance gate")
        target.update(fields)
        if any(not _valid_record(item) for item in records) or not _unique_ids(records):
            raise RoadmapError(400, "Update would create an invalid or duplicate task record")
        a, b = raw.index(START) + len(START), raw.index(END)
        updated = raw[:a] + "\n" + json.dumps(records, ensure_ascii=False, indent=2) + "\n" + raw[b:]
        fd, temp_name = tempfile.mkstemp(prefix=".roadmap-board-", suffix=".tmp", dir=BOARD.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            if hashlib.sha256(BOARD.read_bytes()).hexdigest() != revision:
                raise RoadmapError(409, "Board changed during update; refresh before updating")
            os.replace(temp_name, BOARD)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return snapshot()


class Handler(BaseHTTPRequestHandler):
    server_version = "SSG2Roadmap/1"

    def _json(self, status: int, value: dict) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/tasks":
            try:
                self._json(200, snapshot())
            except RoadmapError as exc:
                self._json(exc.status, {"error": str(exc)})
        elif self.path == "/" or self.path == "/index.html":
            page = ROOT / "docs" / "roadmap-dashboard.html"
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/repo/"):
            self._serve_repo_file()
        else:
            self._json(404, {"error": "Not found"})

    def _serve_repo_file(self) -> None:
        try:
            relative = urllib.parse.unquote(urlparse(self.path).path[len("/repo/"):])
            if not relative or "\\" in relative:
                raise ValueError()
            target = (ROOT / relative).resolve()
            target.relative_to(ROOT.resolve())
            allowed_top = {"docs", "analysis", "tests", "tools", "web"}
            if relative.split("/", 1)[0] not in allowed_top or target.suffix.lower() not in {".md", ".json", ".py", ".js", ".html", ".cjs"}:
                raise ValueError()
            body = target.read_bytes()
        except (ValueError, OSError):
            self._json(404, {"error": "Evidence file not found"})
            return
        mime = "text/plain; charset=utf-8" if target.suffix.lower() in {".md", ".py", ".js", ".cjs", ".json"} else "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path not in {"/api/claim", "/api/task-update"}:
            self._json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 8192:
                raise RoadmapError(400, "Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise RoadmapError(400, "Invalid request")
            if self.path == "/api/claim":
                result = update_claim(payload.get("task_id"), payload.get("claimant"),
                                      payload.get("revision"), payload.get("release", False))
            else:
                result = update_task(payload.get("task_id"), payload.get("revision"),
                                     payload.get("fields", {}), payload.get("subtask_id"),
                                     payload.get("new_subtask"))
            self._json(200, result)
        except RoadmapError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "Invalid JSON"})

    def log_message(self, fmt: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"SSG2 roadmap at http://127.0.0.1:{args.port}/ (board: {BOARD})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
