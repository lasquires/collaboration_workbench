"""Loopback API for the SQLite-backed roadmap and read-only variable registry."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import uuid
import threading
import urllib.parse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from roadmap_store import RoadmapStore, StoreError
from roadmap_schema import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "docs" / "BOARD.md"  # historical source; never read for task state
BOARD_HISTORY = ROOT / "docs" / "journal" / "BOARD-HISTORY-2026-11-12.md"
START = "<!-- ROADMAP_TASK_RECORDS:START -->"  # migration compatibility constant only
END = "<!-- ROADMAP_TASK_RECORDS:END -->"
STORE_PATH = Path(os.environ.get("SSG2_ROADMAP_STORE", ROOT / "analysis" / "roadmap.sqlite3"))
VARIABLE_REGISTRY = ROOT / "analysis" / "variable-occurrences.json"
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
_STATUSES = {"ready", "active", "blocked", "review", "done"}
_LOCK_HANDLE = None


class RoadmapError(StoreError):
    pass


def _store(path: Path | None = None) -> RoadmapStore:
    path = Path(path or STORE_PATH)
    if not path.is_file():
        raise RoadmapError(503, "Authoritative task database is missing")
    try:
        return RoadmapStore(path)
    except StoreError as exc:
        raise RoadmapError(exc.status, str(exc)) from exc


def _legacy_tasks(snapshot: dict) -> list[dict]:
    return RoadmapStore._legacy_from_snapshot(snapshot)


def snapshot() -> dict:
    """Return the stable database snapshot plus compatibility fields."""
    snap = _store().snapshot()
    return {**snap, "source": "sqlite", "claims_available": True,
            "message": "Task state is read from the authoritative SQLite database.",
            "legacy_tasks": _legacy_tasks(snap)}


def variable_occurrences(query: object = "", exact: object = False) -> dict:
    from variable_registry import search_occurrences, validate_registry
    if not isinstance(query, str) or len(query) > 500 or "\x00" in query:
        raise RoadmapError(400, "Query must be a string of at most 500 characters")
    if not isinstance(exact, bool):
        raise RoadmapError(400, "Exact must be a boolean")
    document, task_snapshot = _store().variable_registry_snapshot()
    registry_source = "SQLite research registry (immutable metadata import)"
    if document is None:
        registry_source = "analysis/variable-occurrences.json"
        try:
            raw = VARIABLE_REGISTRY.read_bytes()
            document = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
            raise RoadmapError(503, "Variable registry is unavailable or malformed") from exc
    else:
        raw = json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    task_ids = {task["id"] for task in task_snapshot["tasks"]}
    old_board_reference = ""
    for reference_path in (BOARD_HISTORY, BOARD):
        try:
            if reference_path.exists():
                old_board_reference += reference_path.read_text(encoding="utf-8")
        except OSError:
            continue
    errors = validate_registry(document, repo_root=ROOT, board_path=BOARD,
                              task_ids=task_ids, board_reference_text=old_board_reference)
    if errors:
        raise RoadmapError(503, "Variable registry failed validation: " + "; ".join(errors))
    rows = search_occurrences(document, query, exact=exact) if query else list(document["occurrences"])
    all_rows = {row["id"]: row for row in document["occurrences"]}
    wish_ids = {wid for row in rows for wid in row["wish_ids"]}
    wishes = [wish for wish in document["task_wishes"] if wish["id"] in wish_ids]
    leads = [{"source_id": row["id"], "target_id": match["occurrence_id"], "reason": match["reason"]}
             for row in rows for match in row["possible_matches"]
             if match["status"] == "lead" and match["occurrence_id"] in all_rows]
    return {"source": registry_source, "revision": hashlib.sha256(raw).hexdigest(),
            "task_revision": task_snapshot["revision"], "query": query, "exact": exact,
            "occurrences": rows, "task_wishes": wishes, "leads": leads}


def _claim_ops(task_id, claimant, release):
    if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        raise RoadmapError(400, "Invalid task id")
    if not release and (not isinstance(claimant, str) or not claimant.strip() or len(claimant) > 120):
        raise RoadmapError(400, "Claimant must be 1–120 characters")
    return [{"op": "release", "task_id": task_id, **({"claimant": claimant.strip()} if claimant else {})}]


def _compat_request_id(expected_revision, actor, operations):
    """Stable retry key for legacy wrappers when clients omit request_id."""
    try:
        encoded = json.dumps({"expected_revision": expected_revision, "actor": actor,
                              "operations": operations}, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RoadmapError(400, "Compatibility request is not valid JSON") from exc
    return "compat:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def update_claim(task_id, claimant, expected_revision, release=False, request_id=None, actor=None):
    if not isinstance(release, bool):
        raise RoadmapError(400, "Invalid release value")
    operations = [{"op": "release" if release else "claim", "task_id": task_id,
                   **({"claimant": claimant.strip()} if isinstance(claimant, str) and claimant.strip() else {})}]
    actor = actor or claimant or "roadmap-api"
    return transact({"expected_revision": expected_revision,
                     "request_id": request_id if request_id is not None else _compat_request_id(expected_revision, actor, operations),
                     "actor": actor, "operations": operations})


def update_task(task_id, expected_revision, fields, subtask_id=None, new_subtask=None,
                request_id=None, actor="roadmap-api"):
    if not isinstance(fields, dict) or (not fields and new_subtask is None):
        raise RoadmapError(400, "Update fields must be a non-empty object")
    if subtask_id is not None:
        task_id = subtask_id  # stable IDs address arbitrary-depth descendants directly
    operations = []
    if new_subtask is not None:
        operations.append({"op": "create", "parent_id": task_id, "record": new_subtask})
    if fields:
        operations.append({"op": "update", "task_id": task_id, "fields": fields})
    request_id = request_id if request_id is not None else _compat_request_id(expected_revision, actor, operations)
    return transact({"expected_revision": expected_revision,
                     "request_id": request_id,
                     "actor": actor, "operations": operations})


def transact(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"expected_revision", "request_id", "actor", "operations"}:
        raise RoadmapError(400, "Transaction requires expected_revision, request_id, actor, and operations")
    try:
        return _store().transact(payload["expected_revision"], payload["request_id"],
                                 payload["actor"], payload["operations"])
    except StoreError as exc:
        raise RoadmapError(exc.status, str(exc)) from exc


def workspace_read(parsed):
    """Strict versioned read adapter; project selection is never implicit."""
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    allowed = {"project_id", "limit", "cursor", "depth", "after", "principal_id", "document_id", "task_id", "ancestor_id", "order", "state", "survivor_id", "absorbed_id", "merge_id"}
    if set(params) - allowed or any(len(values) != 1 for values in params.values()):
        raise RoadmapError(400, "Unknown or duplicate workspace parameter")
    values = {key: item[0] for key, item in params.items()}
    path = parsed.path.strip('/').split('/')
    project_id = values.pop("project_id", None)
    options = {}
    for key, value in values.items():
        if key in {"limit", "depth"}:
            if not value.isascii() or not value.isdigit():
                raise RoadmapError(400, "Invalid integer workspace parameter")
            value = int(value)
        options[key] = value
    if path == ["v1", "projects"]:
        if params:
            raise RoadmapError(400, "Project listing accepts no parameters")
        return _store().read_v1("projects")
    if len(path) == 4 and path[1] == "projects" and path[3] == "capabilities":
        if params:
            raise RoadmapError(400, "Capabilities project is selected by path")
        return _store().read_v1("capabilities", urllib.parse.unquote(path[2]))
    if path == ['v1','tasks','merge-reversal-preview']:
        if set(options)!={'merge_id'}:
            raise RoadmapError(400,'Merge reversal preview requires merge_id')
        return _store().merge_reversal_preview(project_id,options['merge_id'])
    if path == ['v1','tasks','merge-preview']:
        if set(options)!={'survivor_id','absorbed_id'}:
            raise RoadmapError(400,'Merge preview requires survivor_id and absorbed_id')
        return _store().merge_preview(project_id,options['survivor_id'],options['absorbed_id'])
    if path == ['v1','exchange-export']:
        if set(options)-{'ancestor_id'}:
            raise RoadmapError(400,'Unsupported exchange export parameter')
        return _store().exchange_preview(project_id,ancestor_id=options.get('ancestor_id'))
    if len(path)==2 and path[1] in {'attempts','validations','blockers','claims'}:
        if set(options)-{'task_id','limit','cursor','order','state'}:
            raise RoadmapError(400,'Unsupported collaboration read parameter')
        return _store().read_v1(path[1],project_id,**options)
    if len(path) == 2 and path[1] in {"tasks", "events", "artifacts", "artifact-versions", "index-runs", "occurrences", "observations", "questions", "negative-searches", "findings", "hints", "subscriptions", "inbox", "processing-status", "replay-jobs", "documents", "document-versions", "discussions", "proposals", "feedback"}:
        resource = path[1]
    elif len(path) == 4 and path[1] == "tasks" and path[3] in {"context", "neighborhood", "history"}:
        resource = path[3]
        options["key"] = urllib.parse.unquote(path[2])
    else:
        raise RoadmapError(404, "Unknown workspace route")
    permitted = {"tasks": {"limit", "cursor"}, "events": {"limit", "after"},
                 "context": set(), "history": {"limit", "cursor", "key"},
                 "neighborhood": {"limit", "depth", "cursor", "key"}}.get(resource, {"limit", "cursor"}) | {"key"}
    if resource in {"hints", "subscriptions", "inbox", "processing-status", "replay-jobs"}:
        permitted.add("principal_id")
    if resource in {"documents", "document-versions", "discussions", "proposals", "feedback"}:
        permitted.add("document_id")
    if set(options) - permitted:
        raise RoadmapError(400, "Parameter unsupported on this resource")
    return _store().read_v1(resource, project_id, **options)


@contextmanager
def service_lock(path: Path):
    """Exclusive nonblocking OS lock keyed to canonical DB path; never removes stale files."""
    global _LOCK_HANDLE
    canonical = Path(path).resolve()
    lock_path = canonical.with_name(canonical.name + ".service.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0"); handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RoadmapError(503, "Another roadmap service holds the database lock") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RoadmapError(503, "Another roadmap service holds the database lock") from exc
        _LOCK_HANDLE = handle
        yield
    finally:
        if _LOCK_HANDLE is handle:
            _LOCK_HANDLE = None
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "SSG2Roadmap/2"

    def _json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 8 * 1024 * 1024:
                raise RoadmapError(413 if self.path.startswith('/v1/') else 400, "Request body exceeds 8 MB")
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RoadmapError(400, "Malformed JSON request") from exc

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/v1/"):
                self._json(200, workspace_read(parsed))
            elif parsed.path == "/api/tasks" and not parsed.query:
                self._json(200, snapshot())
            elif parsed.path == "/api/graph" and not parsed.query:
                from task_graph import project_tasks
                snap = snapshot()
                # Project flat nodes directly while preserving the v1 graph shape.
                graph = project_tasks(_legacy_tasks(snap))
                graph.update({"source": "sqlite", "revision": snap["revision"],
                              "partial_coverage": True, "claims_available": True,
                              "events": snap["events"], "links": snap["links"]})
                # Replace projected legacy IDs/relationships with canonical flat data.
                graph["nodes"] = [{"id": t["id"], "parent_id": t["parent_id"], "fields": t["record"],
                                   "provenance": {"source": "SQLite task store", "path": t["source_path"]}}
                                  for t in snap["tasks"]]
                graph["edges"] = ([{"type": "contains", "source": t["parent_id"], "target": t["id"]}
                                   for t in snap["tasks"] if t["parent_id"] is not None] +
                                  [{"type": "prerequisite", "source": l["target_id"], "target": l["source_id"]}
                                   for l in snap["links"] if l["kind"] == "depends_on"] +
                                  [{"type": "related", "source": l["source_id"], "target": l["target_id"]}
                                   for l in snap["links"] if l["kind"] == "related"])
                graph["coverage"] = {"source": "SQLite task store", "top_level_records": sum(t["parent_id"] is None for t in snap["tasks"]),
                                     "node_count": len(snap["tasks"])}
                self._json(200, graph)
            elif parsed.path == "/api/variable-occurrences":
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                if set(params) - {"query", "exact"} or any(len(v) != 1 for v in params.values()):
                    raise RoadmapError(400, "Expected at most one query and exact parameter")
                exact = params.get("exact", ["false"])[0]
                if exact not in {"true", "false"}:
                    raise RoadmapError(400, "Exact must be true or false")
                self._json(200, variable_occurrences(params.get("query", [""])[0], exact == "true"))
            elif parsed.path in {"/", "/index.html", "/graph"} and not parsed.query:
                page = ROOT / "docs" / ("task-graph.html" if parsed.path == "/graph" else "roadmap-dashboard.html")
                body = page.read_bytes(); self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif self.path.startswith("/repo/"):
                self._serve_repo_file()
            else:
                self._json(404, {"error": "Not found"})
        except StoreError as exc:
            self._store_error(exc)
        except (ValueError, TypeError) as exc:
            self._store_error(StoreError(400, str(exc)))
        except OSError:
            self._json(404, {"error": "Requested resource is unavailable"})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self._body()
            if parsed.path == "/v1/transactions" and not parsed.query:
                result = _store().transact_v1(payload)
            elif parsed.path == "/v1/variable-import-preview" and not parsed.query:
                result = _store().preview_variable_registry(payload)
            elif parsed.path == '/v1/exchange-preview' and not parsed.query:
                if not isinstance(payload,dict) or set(payload)!={'project_id','bundle'}:
                    raise RoadmapError(400,'Expected project_id and bundle')
                result = _store().exchange_preview(payload['project_id'],bundle=payload['bundle'])
            elif parsed.path == "/api/transaction":
                result = transact(payload)
            elif parsed.path == "/api/claim":
                result = update_claim(payload.get("task_id"), payload.get("claimant"),
                                      payload.get("revision"), payload.get("release", False),
                                      payload.get("request_id"), payload.get("actor"))
            elif parsed.path == "/api/task-update":
                result = update_task(payload.get("task_id"), payload.get("revision"), payload.get("fields"),
                                     payload.get("subtask_id"), payload.get("new_subtask"),
                                     payload.get("request_id"), payload.get("actor", "roadmap-api"))
            else:
                self._json(404, {"error": "Not found"}); return
            self._json(200, result)
        except StoreError as exc:
            self._store_error(exc)
        except (ValueError, TypeError) as exc:
            self._store_error(StoreError(400, str(exc)))

    def _store_error(self, exc):
        if not self.path.startswith("/v1/"):
            self._json(exc.status, {"error": str(exc)})
            return
        codes = {400: "validation", 403: "capability", 404: "not_found", 409: "conflict", 413: "size", 503: "unavailable"}
        try:
            latest = _store().snapshot()["revision"]
        except StoreError:
            latest = None
        result = {"error": str(exc), "code": codes.get(exc.status, "error"),
                  "conflicts": getattr(exc, "conflicts", []), "latest_revision": latest}
        for name in ("restart_required", "limits"):
            if hasattr(exc, name):
                result[name] = getattr(exc, name)
        self._json(exc.status, result)

    def _serve_repo_file(self):
        relative = urllib.parse.unquote(self.path[len("/repo/"):])
        candidate = (ROOT / relative).resolve()
        try:
            candidate.relative_to(ROOT.resolve())
            if candidate.suffix.lower() not in {".md", ".json", ".py", ".js", ".html", ".txt", ".c", ".h"}:
                raise ValueError()
            candidate.relative_to(ROOT / "docs")
        except ValueError:
            try:
                candidate.relative_to((ROOT / "analysis").resolve())
            except ValueError:
                try:
                    candidate.relative_to((ROOT / "tests").resolve())
                except ValueError:
                    try: candidate.relative_to((ROOT / "tools").resolve())
                    except ValueError:
                        self._json(403, {"error": "Path is not served"}); return
        body = candidate.read_bytes(); self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def checkpoint_before_upgrade(path):
    """Called only under the service's exclusive startup lock, before migration."""
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        version = db.execute('SELECT schema_version FROM store_meta WHERE singleton=1').fetchone()[0]
    if version == SCHEMA_VERSION:
        return None
    if version not in (1, 2, 3):
        raise RoadmapError(503, 'Unsupported task database schema; no migration attempted')
    from migrate_roadmap import backup_database
    def file_hash(target):
        value = hashlib.sha256()
        with target.open('rb') as source:
            for block in iter(lambda: source.read(1024*1024), b''):
                value.update(block)
        return value.hexdigest()
    source_hash = file_hash(path)
    checkpoint = path.with_name(path.stem + '.before-v' + str(SCHEMA_VERSION) + '.' + uuid.uuid4().hex + '.sqlite3')
    backup_database(path, checkpoint)
    manifest = {'source':str(path),'source_sha256':source_hash,'source_schema':version,
                'target_schema':SCHEMA_VERSION,'checkpoint':str(checkpoint),
                'checkpoint_sha256':file_hash(checkpoint),'created_at':datetime.now(timezone.utc).isoformat(),
                'method':'SQLite backup API under exclusive service startup lock; integrity and foreign keys checked'}
    checkpoint.with_suffix(checkpoint.suffix+'.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return checkpoint


def start_matcher_worker(path, interval=1.0):
    """One bounded durable-job consumer; publication never waits on matching."""
    stop = threading.Event()
    def consume():
        while not stop.is_set():
            try:
                store = RoadmapStore(path)
                for project in store.read_v1('projects')['items']:
                    if stop.is_set():
                        break
                    store.process_jobs(project['id'],limit=100)
            except (StoreError, OSError, ValueError) as exc:
                # The durable outbox/status endpoint is the recovery authority.
                print(f'Matcher retry: {exc}', flush=True)
            stop.wait(interval)
    worker = threading.Thread(target=consume,name='research-matcher',daemon=True)
    worker.start()
    return stop, worker


def main(argv=None):
    global STORE_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--store", type=Path, default=STORE_PATH)
    args = parser.parse_args(argv)
    selected_store = args.store.resolve()
    if not selected_store.is_file():
        raise SystemExit(f"Authoritative task database is missing: {selected_store}")
    try:
        with service_lock(selected_store):
            checkpoint = checkpoint_before_upgrade(selected_store)
            if checkpoint:
                print(f'Pre-migration checkpoint: {checkpoint}', flush=True)
            STORE_PATH = selected_store
            _store(selected_store).snapshot()
            server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
            stop, worker = start_matcher_worker(selected_store)
            print(f"SSG2 roadmap at http://127.0.0.1:{args.port}/ (store: {selected_store})")
            try: server.serve_forever()
            except KeyboardInterrupt: pass
            finally:
                stop.set()
                worker.join(timeout=6)
                server.server_close()
    except RoadmapError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
