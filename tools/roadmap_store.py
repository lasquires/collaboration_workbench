"""Transactional local task store. HTTP/migration orchestration belongs to callers.

Only explicit ``import_v1`` imports legacy records; opening an existing unsupported
or malformed database fails closed. See sqlite-store-implementation.md for the
public operation contract. Actor labels are coordination labels, not identities.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    from . import roadmap_schema, roadmap_projection, roadmap_research, roadmap_matcher, roadmap_documents, roadmap_structure
except ImportError:
    import roadmap_schema, roadmap_projection, roadmap_research, roadmap_matcher, roadmap_documents, roadmap_structure

SCHEMA_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_STATUSES = {"ready", "active", "blocked", "review", "done"}
_KINDS = {"observation", "attempt", "blocker", "review", "handoff"}
_ROOT = Path(__file__).resolve().parents[1]


class StoreError(Exception):
    """HTTP-compatible status plus stable human-readable message."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise StoreError(400, "Expected finite JSON data within supported payload size") from exc


def _copy(value):
    return json.loads(_json(value))


def _text(value, label, limit=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise StoreError(400, f"Invalid {label}")
    return value


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise StoreError(400, "Invalid task ID")
    return value


def _path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise StoreError(400, "Expected repository-relative path")
    try:
        url = urlsplit(value)
    except ValueError as exc:
        raise StoreError(400, "Invalid repository-relative path") from exc
    parts = value.split("/")
    if url.scheme or url.netloc or url.query or url.fragment or value.startswith("/") or any(p in {"", ".", ".."} for p in parts) or ":" in value:
        raise StoreError(400, "Unsafe repository-relative path")
    try:
        (_ROOT / value).resolve().relative_to(_ROOT)
    except (ValueError, OSError) as exc:
        raise StoreError(400, "Path escapes repository") from exc
    return value


def _acyclic(ids, pairs, label):
    """Kahn traversal avoids Python recursion limits on deep task trees."""
    outgoing = {i: [] for i in ids}
    degree = dict.fromkeys(ids, 0)
    for a, b in pairs:
        if a not in ids or b not in ids or a == b:
            raise StoreError(400, f"Invalid {label} target")
        outgoing[a].append(b)
        degree[b] += 1
    ready = [i for i in ids if not degree[i]]
    count = 0
    while ready:
        node = ready.pop()
        count += 1
        for other in outgoing[node]:
            degree[other] -= 1
            if not degree[other]:
                ready.append(other)
    if count != len(ids):
        raise StoreError(400, f"Cyclic {label} graph")


_SCHEMA = """
CREATE TABLE store_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
 store_id TEXT NOT NULL, revision INTEGER NOT NULL, source_sha256 TEXT,
 import_digest TEXT, import_records_json TEXT);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, parent_id TEXT REFERENCES tasks(id) DEFERRABLE INITIALLY DEFERRED,
 sibling_order INTEGER NOT NULL CHECK(sibling_order>=0), record_json TEXT NOT NULL,
 original_json TEXT NOT NULL, source_path_json TEXT, archive_reason TEXT);
CREATE TABLE task_links (
 source_id TEXT NOT NULL REFERENCES tasks(id), target_id TEXT NOT NULL REFERENCES tasks(id),
 kind TEXT NOT NULL CHECK(kind IN ('depends_on','related')),
 rationale TEXT NOT NULL, review_state TEXT NOT NULL,
 PRIMARY KEY(source_id,target_id,kind), CHECK(source_id<>target_id));
CREATE TABLE transactions (
 request_id TEXT PRIMARY KEY, payload_digest TEXT NOT NULL, result_json TEXT NOT NULL);
CREATE TABLE events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
 request_id TEXT NOT NULL REFERENCES transactions(request_id) DEFERRABLE INITIALLY DEFERRED,
 task_id TEXT REFERENCES tasks(id), actor_json TEXT, recorded_at TEXT NOT NULL,
 historical_at TEXT, kind TEXT NOT NULL, details_json TEXT NOT NULL);
CREATE TRIGGER immutable_event_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER immutable_event_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER immutable_transaction_update BEFORE UPDATE ON transactions BEGIN SELECT RAISE(ABORT,'transactions are immutable'); END;
CREATE TRIGGER immutable_transaction_delete BEFORE DELETE ON transactions BEGIN SELECT RAISE(ABORT,'transactions are immutable'); END;
"""


class RoadmapStore:
    def __init__(self, path):
        self.path = Path(path)
        if str(path) == ":memory:":
            raise StoreError(400, "Use a filesystem database; connections are per operation")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not names:
                # executescript commits implicitly; explicitly bracket initial DDL.
                try:
                    db.executescript("BEGIN IMMEDIATE;" + _SCHEMA)
                    db.execute("INSERT INTO store_meta VALUES(1,?,?,0,NULL,NULL,NULL)",
                               (SCHEMA_VERSION, str(uuid.uuid4())))
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
            try:
                roadmap_schema.migrate(db, fresh=not names)
            except ValueError as exc:
                raise StoreError(503, str(exc)) from exc
            self._meta(db)

    @contextmanager
    def _connection(self):
        try:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            try:
                yield db
            finally:
                db.close()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StoreError(503, "Task database busy; retry after reconciliation") from exc
            raise StoreError(503, "Task database unavailable or incompatible") from exc
        except sqlite3.DatabaseError as exc:
            raise StoreError(503, "Task database failed integrity/constraint operation") from exc

    def _meta(self, db):
        row = db.execute("SELECT * FROM store_meta WHERE singleton=1").fetchone()
        if row is None or row["schema_version"] != roadmap_schema.SCHEMA_VERSION:
            raise StoreError(503, "Unsupported task database schema")
        return row

    @staticmethod
    def _revision(meta):
        return f"{meta['store_id']}:{meta['revision']}"

    def _state(self, db):
        tasks = {}
        for row in db.execute("SELECT * FROM tasks ORDER BY rowid"):
            tasks[row["id"]] = {
                "id": row["id"], "parent_id": row["parent_id"],
                "sibling_order": row["sibling_order"], "record": json.loads(row["record_json"]),
                "original_record": json.loads(row["original_json"]),
                "source_path": json.loads(row["source_path_json"]) if row["source_path_json"] else None,
                "archive_reason": row["archive_reason"]}
        links = [dict(row) for row in db.execute("SELECT * FROM task_links ORDER BY source_id,target_id,kind")]
        return tasks, links

    def _snapshot(self, db):
        meta = self._meta(db)
        tasks, links = self._state(db)
        # Stable depth-first ordering without depending on ID spelling or recursion.
        children = {}
        for task in tasks.values():
            children.setdefault(task["parent_id"], []).append(task)
        for group in children.values():
            group.sort(key=lambda t: (t["sibling_order"], t["id"]))
        stack = list(reversed(children.get(None, [])))
        ordered = []
        while stack:
            task = stack.pop()
            ordered.append(task)
            stack.extend(reversed(children.get(task["id"], [])))
        events = []
        for row in db.execute("SELECT * FROM events ORDER BY sequence"):
            item = dict(row)
            item["actor"] = json.loads(item.pop("actor_json")) if row["actor_json"] else None
            item["details"] = json.loads(item.pop("details_json"))
            events.append(item)
        return {"schema_version": SCHEMA_VERSION, "store_id": meta["store_id"],
                "revision": self._revision(meta), "source_sha256": meta["source_sha256"],
                "partial_coverage": True, "tasks": ordered, "links": links, "events": events}

    @staticmethod
    def _legacy_from_snapshot(snapshot):
        """Reconstruct v1 records iteratively for legacy graph/UI clients."""
        nodes = {t["id"]: _copy(t["record"]) for t in snapshot["tasks"]}
        roots = []
        for task in snapshot["tasks"]:
            node = nodes[task["id"]]
            if "subtasks" in task["original_record"]:
                node["subtasks"] = []
            if task["parent_id"] is None:
                roots.append(node)
            else:
                nodes[task["parent_id"]].setdefault("subtasks", []).append(node)
        return roots

    def snapshot(self):
        with self._connection() as db:
            db.execute("BEGIN")
            try:
                return self._snapshot(db)
            finally:
                db.rollback()

    def export_legacy_records(self):
        """Reconstruct current nested records, preserving original absent/empty keys."""
        snapshot = self.snapshot()
        nodes = {t["id"]: _copy(t["record"]) for t in snapshot["tasks"]}
        roots = []
        for task in snapshot["tasks"]:
            node = nodes[task["id"]]
            if "subtasks" in task["original_record"]:
                node["subtasks"] = []
            if task["parent_id"] is None:
                roots.append(node)
            else:
                nodes[task["parent_id"]].setdefault("subtasks", []).append(node)
        return roots

    def _event(self, db, request_id, task_id, actor, kind, details, historical_at=None, principal_id=None):
        db.execute("INSERT INTO events(event_id,request_id,task_id,actor_json,recorded_at,historical_at,kind,details_json) VALUES(?,?,?,?,?,?,?,?)",
                   (str(uuid.uuid4()), request_id, task_id, _json(actor) if actor else None,
                    datetime.now(timezone.utc).isoformat(), historical_at, kind, _json(details)))
        if task_id is not None and kind != "legacy_attempt":
            task = db.execute("SELECT uid,project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            principal_id = principal_id or roadmap_schema.principal_for_label(db, task["project_id"], actor["name"])
            db.execute("INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,before_json,after_json,rationale,recorded_at) VALUES(?,?,?,?,'task',?,?,?,?,?,?)",
                       (str(uuid.uuid4()), task["project_id"], request_id, principal_id, task["uid"], kind,
                        _json(details.get("before")) if "before" in details else None,
                        _json(details.get("after")) if "after" in details else None,
                        details.get("text"), datetime.now(timezone.utc).isoformat()))

    def import_v1(self, records, source_sha256):
        if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise StoreError(400, "Expected exact source SHA-256")
        if not isinstance(records, list):
            raise StoreError(400, "Legacy records must be an array")
        encoded = _json(records)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                meta = self._meta(db)
                if meta["source_sha256"]:
                    if meta["source_sha256"] == source_sha256 and meta["import_digest"] == digest:
                        return self._snapshot(db)
                    raise StoreError(409, "Database already imported from different source")
                if db.execute("SELECT 1 FROM tasks LIMIT 1").fetchone() or meta["revision"]:
                    raise StoreError(409, "Cannot import into populated store")
                tasks, links = {}, []
                stack = [(record, None, i, [i]) for i, record in reversed(list(enumerate(records)))]
                while stack:
                    record, parent_id, order, location = stack.pop()
                    if not isinstance(record, dict):
                        raise StoreError(400, "Legacy record must be an object")
                    task_id = _id(record.get("id"))
                    if task_id in tasks:
                        raise StoreError(400, "Duplicate imported task ID")
                    children = record.get("subtasks", [])
                    if not isinstance(children, list):
                        raise StoreError(400, "Legacy subtasks must be an array")
                    current = _copy({k: v for k, v in record.items() if k != "subtasks"})
                    tasks[task_id] = {"id": task_id, "parent_id": parent_id, "sibling_order": order,
                                      "record": current, "original_record": _copy(record),
                                      "source_path": location, "archive_reason": None}
                    dependencies = record.get("dependencies", [])
                    if not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies):
                        raise StoreError(400, "Legacy dependencies must be task IDs")
                    for dep in dependencies:
                        links.append({"source_id": task_id, "target_id": dep, "kind": "depends_on", "rationale": "", "review_state": "legacy"})
                    stack.extend((child, task_id, i, location + ["subtasks", i])
                                 for i, child in reversed(list(enumerate(children))))
                self._validate(tasks, links)
                self._write_state(db, tasks, links)
                roadmap_structure.sync_legacy_claims(db,historical=True)
                request_id = "migration:" + source_sha256
                self._event(db, request_id, None, None, "migration", {"source_sha256": source_sha256, "ids": list(tasks)})
                for task in tasks.values():
                    for attempt in task["record"].get("attempts", []):
                        self._event(db, request_id, task["id"], None, "legacy_attempt",
                                    {"attempt": attempt, "source_path": task["source_path"], "date_basis": "unknown"})
                db.execute("UPDATE store_meta SET revision=1,source_sha256=?,import_digest=?,import_records_json=?",
                           (source_sha256, digest, encoded))
                result = self._snapshot(db)
                db.execute("INSERT INTO transactions VALUES(?,?,?)", (request_id, digest, _json(result)))
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    def _validate(self, tasks, links):
        for task in tasks.values():
            record = task["record"]
            _id(task["id"])
            if record.get("id") != task["id"]:
                raise StoreError(400, "Record ID mismatch")
            _text(record.get("title"), "title")
            if not isinstance(record.get("status"), str) or record["status"] not in _STATUSES:
                raise StoreError(400, "Unsupported workflow status")
            for key in ("summary", "owner", "stage", "prior_work", "next_step", "parent_gate"):
                if key in record and record[key] is not None and not isinstance(record[key], str):
                    raise StoreError(400, f"Invalid {key}")
            for key in ("dependencies", "acceptance", "evidence", "attempts"):
                if key in record and not isinstance(record[key], list):
                    raise StoreError(400, f"Invalid {key}")
            for path in record.get("evidence", []):
                _path(path)
            if any(not isinstance(v, str) for v in record.get("acceptance", [])):
                raise StoreError(400, "Invalid acceptance criteria")
            if any(not isinstance(v, (str, dict)) for v in record.get("attempts", [])):
                raise StoreError(400, "Invalid legacy attempt")
            if "acceptance_gate_passed" in record and not isinstance(record["acceptance_gate_passed"], bool):
                raise StoreError(400, "Invalid gate state")
            claim = record.get("claim")
            if claim is not None:
                if not isinstance(claim, dict):
                    raise StoreError(400, "Invalid claim")
                _text(claim.get("by"), "claimant", 120)
            self._reservations(record.get("reserved_paths", []))
        ids = set(tasks)
        _acyclic(ids, [(t["id"], t["parent_id"]) for t in tasks.values() if t["parent_id"] is not None], "parent")
        seen = set()
        for link in links:
            key = (link["source_id"], link["target_id"], link["kind"])
            if key in seen or key[0] not in ids or key[1] not in ids or key[0] == key[1] or key[2] not in {"depends_on", "related"}:
                raise StoreError(400, "Invalid or duplicate task link")
            seen.add(key)
        _acyclic(ids, [(l["source_id"], l["target_id"]) for l in links if l["kind"] == "depends_on"], "dependency")
        active = []
        for task in tasks.values():
            if task["record"].get("claim"):
                for path, directory in self._reservations(task["record"].get("reserved_paths", [])):
                    for other_id, other, other_dir in active:
                        if other_id != task["id"] and (path == other or (directory and other.startswith(path + "/")) or (other_dir and path.startswith(other + "/"))):
                            raise StoreError(409, "Active reserved paths overlap")
                    active.append((task["id"], path, directory))

    @staticmethod
    def _reservations(values):
        if not isinstance(values, list):
            raise StoreError(400, "Reserved paths must be an array")
        result = []
        for value in values:
            if isinstance(value, str):
                path, directory = value, False
            elif isinstance(value, dict) and set(value) == {"path", "kind"} and value["kind"] in ("file", "directory"):
                path, directory = value["path"], value["kind"] == "directory"
            else:
                raise StoreError(400, "Invalid reservation")
            result.append((_path(path).casefold(), directory))
        return result

    def _write_state(self, db, tasks, links, project_id=None, mirror_containment=True):
        for t in tasks.values():
            db.execute("INSERT INTO tasks(id,parent_id,sibling_order,record_json,original_json,source_path_json,archive_reason) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id,sibling_order=excluded.sibling_order,record_json=excluded.record_json,archive_reason=excluded.archive_reason",
                       (t["id"], t["parent_id"], t["sibling_order"], _json(t["record"]),
                        _json(t["original_record"]), _json(t["source_path"]) if t["source_path"] is not None else None, t["archive_reason"]))
        db.execute("DELETE FROM task_links")
        db.executemany("INSERT INTO task_links VALUES(:source_id,:target_id,:kind,:rationale,:review_state)", links)
        roadmap_schema.sync_tasks(db, project_id=project_id, mirror_containment=mirror_containment)

    @staticmethod
    def _actor(actor):
        if isinstance(actor, str):
            return {"name": _text(actor, "actor", 120), "kind": "agent"}
        if isinstance(actor, dict) and set(actor) == {"name", "kind"} and actor["kind"] in ("human", "agent"):
            return {"name": _text(actor["name"], "actor", 120), "kind": actor["kind"]}
        raise StoreError(400, "Actor requires name and human/agent kind")

    def read_v1(self, resource, project_id=None, **options):
        with self._connection() as db:
            db.execute("BEGIN")
            try:
                if resource in roadmap_research.RESOURCES:
                    return roadmap_research.read(db, resource, project_id, **options)
                if resource in roadmap_matcher.RESOURCES:
                    return roadmap_matcher.read(db, resource, project_id, **options)
                if resource in roadmap_documents.RESOURCES:
                    return roadmap_documents.read(db, resource, project_id, **options)
                if resource in roadmap_structure.RESOURCES:
                    return roadmap_structure.read(db, resource, project_id, **options)
                return roadmap_projection.read_projection(db, resource, project_id, **options)
            finally:
                db.rollback()

    def variable_registry_snapshot(self):
        """Compatibility document and task revision from the same SQL snapshot."""
        with self._connection() as db:
            db.execute("BEGIN")
            try:
                project = roadmap_schema.default_project(db)
                return roadmap_research.export_variable_registry(db, project), self._snapshot(db)
            finally:
                db.rollback()

    def process_jobs(self, project_id, limit=100):
        """Service-owned atomic matcher page. Cursor-only acks do not stale drafts."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                roadmap_projection._coverage(db, project_id)
                result = roadmap_matcher.process_jobs(db, project_id, limit)
                if result['new_hints'] or result.get('stale_hints'):
                    request_id = 'matcher:' + str(uuid.uuid4())
                    principal = roadmap_schema.principal_for_label(db, project_id, 'workspace matcher')
                    db.execute("UPDATE store_meta SET revision=revision+1")
                    db.execute("INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()),project_id,request_id,principal,'matcher_jobs',project_id,'matcher_results',_json(result),datetime.now(timezone.utc).isoformat()))
                    db.execute("INSERT INTO transactions VALUES(?,?,?)",(request_id,hashlib.sha256(_json(result).encode()).hexdigest(),_json(result)))
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    def preview_variable_registry(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"project_id", "document", "source_hash"}:
            raise StoreError(400, "Expected project_id, document and source_hash")
        with self._connection() as db:
            db.execute("BEGIN")
            try:
                roadmap_projection._coverage(db, payload["project_id"])
                preview = roadmap_research.import_variable_registry(db, payload["project_id"],
                    payload["document"], payload["source_hash"], dry_run=True)
                return {"api_version":roadmap_projection.API_VERSION,"project_id":payload["project_id"],
                        "revision":self._revision(self._meta(db)),"preview":preview}
            finally:
                db.rollback()

    def merge_preview(self, project_id, survivor_id, absorbed_id):
        with self._connection() as db:
            db.execute('BEGIN')
            try:
                roadmap_projection._coverage(db,project_id)
                return {'api_version':roadmap_projection.API_VERSION,'project_id':project_id,
                        'revision':self._revision(self._meta(db)),
                        'preview':roadmap_structure.preview_merge(db,project_id,survivor_id,absorbed_id)}
            finally:
                db.rollback()

    def exchange_preview(self, project_id, bundle=None, ancestor_id=None):
        try:
            from . import roadmap_exchange
        except ImportError:
            import roadmap_exchange
        with self._connection() as db:
            db.execute('BEGIN')
            try:
                roadmap_projection._coverage(db,project_id)
                envelope={'api_version':roadmap_projection.API_VERSION,'project_id':project_id,
                          'revision':self._revision(self._meta(db))}
                if bundle is None:
                    return {**envelope,'bundle':roadmap_exchange.export_bundle(db,project_id,ancestor_id)}
                return {**envelope,'preview':roadmap_exchange.preview_import(db,project_id,bundle)}
            finally:
                db.rollback()

    def transact_v1(self, payload):
        """Scoped adapter, retaining the existing single global CAS transaction."""
        required = {"project_id", "actor", "request_id", "expected_revision", "operations"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise StoreError(400, "Expected project_id, actor, request_id, expected_revision, operations")
        _text(payload["request_id"], "request ID", 200)
        if payload["request_id"].startswith("migration:"):
            raise StoreError(400, "Reserved request ID")
        if not isinstance(payload["expected_revision"], str):
            raise StoreError(400, "Expected revision required")
        actor = payload["actor"]
        if not isinstance(actor, dict) or set(actor) - {"principal_id", "session_id"} or not isinstance(actor.get("principal_id"), str):
            raise StoreError(400, "Actor requires stable principal_id and optional session_id")
        if actor.get("session_id") is not None:
            _text(actor["session_id"], "session ID", 200)
        if not isinstance(payload["operations"], list) or not 1 <= len(payload["operations"]) <= 500:
            raise StoreError(400, "Expected 1–500 operations")
        with self._connection() as db:
            project = payload["project_id"]
            if not isinstance(project, str) or not db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
                raise StoreError(404, "Scoped project not found")
            principal = db.execute("SELECT p.*,m.capabilities_json FROM principals p JOIN project_members m ON m.principal_id=p.id WHERE m.project_id=? AND p.id=? AND p.archived_at IS NULL", (project, actor["principal_id"])).fetchone()
            if principal is None:
                raise StoreError(403, "Principal lacks project membership")
            if "contribute" not in json.loads(principal["capabilities_json"]):
                raise StoreError(403, "Principal lacks contribute capability")
            if actor.get("session_id") is not None and not db.execute("SELECT 1 FROM agent_sessions WHERE id=? AND principal_id=?", (actor["session_id"], actor["principal_id"])).fetchone():
                raise StoreError(403, "Session does not belong to principal")
            prior = db.execute("SELECT * FROM transactions WHERE request_id=?", (payload["request_id"],)).fetchone()
            if prior:
                digest = hashlib.sha256(_json({"v1": payload}).encode()).hexdigest()
                if digest != prior["payload_digest"]:
                    raise StoreError(409, "Request ID reused with different payload")
                return json.loads(prior["result_json"])
            operations = _copy(payload["operations"])
            local_creates = {}
            for op in operations:
                if not isinstance(op, dict):
                    raise StoreError(400, "Invalid operation")
                if op.get('op') == 'create_task':
                    op['op'] = 'create'
                if op.get('op') in roadmap_structure.OPS:
                    for field in ('task_id','parent_id'):
                        if isinstance(op.get(field),str) and op[field] in local_creates:
                            op[field] = local_creates[op[field]]
                    continue
                if op.get("op") in roadmap_research.OPS | roadmap_matcher.OPS | roadmap_documents.OPS | {"process_jobs"}:
                    continue
                if op.get("op") == "create":
                    record = op.get("record")
                    if not isinstance(record, dict) or "id" in record:
                        raise StoreError(400, "v1 create requires record without id and optional alias")
                    alias = op.pop("alias", None) or "task-" + uuid.uuid4().hex
                    _id(alias)
                    if db.execute("SELECT 1 FROM entity_aliases WHERE project_id=? AND entity_type='task' AND alias=?", (project, alias)).fetchone():
                        raise StoreError(409, "Task alias is never reused")
                    # Legacy storage keys remain global; public aliases are scoped.
                    legacy = alias if not db.execute("SELECT 1 FROM tasks WHERE id=?", (alias,)).fetchone() else "task-" + uuid.uuid4().hex
                    record["id"] = legacy
                    local_creates[alias] = legacy
                if op.get("op") == "update" and isinstance(op.get("fields"), dict) and set(op["fields"]) & {"id", "acceptance_gate_passed"}:
                    raise StoreError(400, "Identity and validated acceptance are not editable v1 fields")
                for field in ("task_id", "parent_id", "target_id"):
                    if op.get(field) is not None:
                        key = op[field]
                        _text(key, "task reference", 200)
                        if key in local_creates:
                            op[field] = local_creates[key]
                        else:
                            uid = roadmap_projection.resolve_entity(db, project, "task", key)
                            op[field] = db.execute("SELECT id FROM tasks WHERE project_id=? AND uid=?", (project, uid)).fetchone()[0]
                record_fields = op.get("fields") if op.get("op") == "update" else op.get("record")
                if isinstance(record_fields, dict) and "dependencies" in record_fields:
                    if not isinstance(record_fields["dependencies"], list):
                        raise StoreError(400, "Dependencies must be task IDs")
                    converted = []
                    for key in record_fields["dependencies"]:
                        _text(key, "dependency reference", 200)
                        uid = roadmap_projection.resolve_entity(db, project, "task", key)
                        converted.append(db.execute("SELECT id FROM tasks WHERE project_id=? AND uid=?", (project, uid)).fetchone()[0])
                    record_fields["dependencies"] = converted
        return self.transact(payload["expected_revision"], payload["request_id"],
                             {"name": principal["label"], "kind": "agent"}, operations,
                             _workspace={"payload": payload, "aliases": local_creates})

    def transact(self, expected_revision, request_id, actor, operations, *, _workspace=None):
        actor = self._actor(actor)
        _text(request_id, "request ID", 200)
        if request_id.startswith("migration:"):
            raise StoreError(400, "Reserved request ID")
        if not isinstance(expected_revision, str):
            raise StoreError(400, "Expected revision required")
        if not isinstance(operations, list) or not operations or len(operations) > 10000:
            raise StoreError(400, "Expected 1–10000 operations")
        payload = _json({"v1": _workspace["payload"]} if _workspace else {"expected_revision": expected_revision, "actor": actor, "operations": operations})
        if len(payload.encode("utf-8")) > 8_000_000:
            raise StoreError(400, "Transaction exceeds 8 MB")
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._meta(db)
                prior = db.execute("SELECT * FROM transactions WHERE request_id=?", (request_id,)).fetchone()
                if prior:
                    if prior["payload_digest"] != digest:
                        raise StoreError(409, "Request ID reused with different payload")
                    return json.loads(prior["result_json"])
                if expected_revision != self._revision(self._meta(db)):
                    raise StoreError(409, "Task revision changed; reread and reconcile")
                tasks, links = self._state(db)
                before = _copy(tasks)
                metadata_before = {row['uid']:dict(row) for row in db.execute('SELECT * FROM tasks')}
                structure_ids = set()
                validated_tasks = set()
                event_data = []
                research_changes = []
                task_operations = []
                for operation in operations:
                    if _workspace and isinstance(operation,dict) and operation.get('op') in roadmap_structure.OPS:
                        project = _workspace['payload']['project_id']
                        principal = _workspace['payload']['actor']['principal_id']
                        self._write_state(db,tasks,links,project_id=project,mirror_containment=False)
                        roadmap_structure.sync_legacy_claims(db)
                        change = roadmap_structure.apply_operation(db,project,principal,operation,request_id)
                        tasks,links = self._state(db)
                        structure_ids.update(change.pop('structure_task_ids',[]))
                        change.pop('changed_task_ids',None)
                        event = change.pop('event')
                        event_data.append((change['legacy_id'],operation['op'],event))
                        if operation['op']=='complete_task':
                            validated_tasks.add(change['legacy_id'])
                        research_changes.append(change)
                        continue
                    if _workspace and isinstance(operation, dict) and operation.get("op") in roadmap_research.OPS | roadmap_matcher.OPS | roadmap_documents.OPS | {"process_jobs"}:
                        project = _workspace["payload"]["project_id"]
                        principal = _workspace["payload"]["actor"]["principal_id"]
                        try:
                            if operation["op"] in roadmap_research.OPS:
                                change = roadmap_research.apply_operation(db, project, principal, operation, request_id)
                            elif operation["op"] in roadmap_documents.OPS:
                                change = roadmap_documents.apply_operation(db, project, principal, operation, request_id)
                            else:
                                change = ({"type":"matcher_jobs","id":project,**roadmap_matcher.process_jobs(db, project, operation.get("limit",100))}
                                          if operation["op"] == "process_jobs" else
                                          roadmap_matcher.apply_operation(db, project, principal, operation, request_id))
                                db.execute("INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,rationale,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                    (str(uuid.uuid4()),project,request_id,principal,change["type"],change["id"],operation["op"],_json(change),operation.get("reason"),datetime.now(timezone.utc).isoformat()))
                        except sqlite3.IntegrityError as exc:
                            raise StoreError(409,"Research identity or relationship conflicts with existing history") from exc
                        research_changes.append(change)
                        continue
                    task_operations.append(operation)
                    if isinstance(operation, dict) and operation.get("op") == "journal" and "supersedes" in operation:
                        supersedes = _text(operation["supersedes"], "superseded event ID", 100)
                        previous = db.execute("SELECT task_id FROM events WHERE event_id=?", (supersedes,)).fetchone()
                        if previous is None or previous["task_id"] != operation.get("task_id"):
                            raise StoreError(400, "Correction must reference an existing event on the same task")
                    event_data.append(self._operate(tasks, links, operation, actor))
                self._validate(tasks, links)
                self._gates(before, tasks, links, task_operations, validated_tasks=validated_tasks)
                self._write_state(db, tasks, links, project_id=_workspace["payload"]["project_id"] if _workspace else None)
                if _workspace:
                    project = _workspace["payload"]["project_id"]
                    for alias, legacy in _workspace["aliases"].items():
                        row = db.execute("SELECT uid FROM tasks WHERE id=?", (legacy,)).fetchone()
                        if alias != legacy:
                            db.execute("INSERT INTO entity_aliases VALUES(?,'task',?,?)", (project, alias, row["uid"]))
                roadmap_schema.sync_tasks(db, before=before)
                # Parent guards cover aggregate scope, not only edits to the
                # parent's own text. Detect legacy and typed containment edits.
                for key,task in tasks.items():
                    old=before.get(key)
                    if old is None or old['parent_id']!=task['parent_id'] or old['archive_reason']!=task['archive_reason']:
                        parents={task['parent_id'],old['parent_id'] if old else None}
                        for parent in parents:
                            seen=set()
                            while parent is not None and parent not in seen:
                                seen.add(parent)
                                uid=db.execute('SELECT uid FROM tasks WHERE id=?',(parent,)).fetchone()[0]
                                structure_ids.add(uid)
                                parent=tasks[parent]['parent_id']
                    if old and any(old['record'].get(field)!=task['record'].get(field) for field in ('acceptance','parent_gate')):
                        uid=db.execute('SELECT uid FROM tasks WHERE id=?',(key,)).fetchone()[0]
                        roadmap_structure.invalidate_gate(db,uid)
                for uid in structure_ids:
                    old=metadata_before.get(uid)
                    if old:
                        db.execute('UPDATE tasks SET structure_revision=? WHERE uid=?',(old['structure_revision']+1,uid))
                        row=db.execute('SELECT gate_revision FROM tasks WHERE uid=?',(uid,)).fetchone()
                        if row['gate_revision']==old['gate_revision']:
                            roadmap_structure.invalidate_gate(db,uid)
                for row in db.execute('SELECT * FROM tasks').fetchall():
                    old=metadata_before.get(row['uid'])
                    if old and row['revision']==old['revision'] and any(row[field]!=old[field] for field in ('gate_revision','acceptance_state','validation_id','decomposition_complete','area_id','structure_revision','archived_at')):
                        db.execute('UPDATE tasks SET revision=revision+1 WHERE uid=?',(row['uid'],))
                roadmap_structure.sync_legacy_claims(db)
                recorded_at = datetime.now(timezone.utc).isoformat()
                for task_id in set(tasks) - set(before):
                    row = db.execute("SELECT project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
                    principal_id = (_workspace["payload"]["actor"]["principal_id"] if _workspace else
                                    roadmap_schema.principal_for_label(db, row["project_id"], actor["name"]))
                    db.execute("UPDATE tasks SET created_by=?,created_at=?,updated_at=? WHERE id=?",
                               (principal_id, recorded_at, recorded_at, task_id))
                for task_id, kind, details in event_data:
                    self._event(db, request_id, task_id, actor, kind, details,
                                principal_id=_workspace["payload"]["actor"]["principal_id"] if _workspace else None)
                db.execute("UPDATE store_meta SET revision=revision+1")
                result = self._snapshot(db)
                if _workspace:
                    changed = list(dict.fromkeys(item[0] for item in event_data if item[0]))
                    result = {"revision": result["revision"], "event_cursor": roadmap_projection._event_cursor(db, project),
                              "changed_records": [{"type": "task", "id": row["uid"], "record_revision": row["revision"]}
                                  for task_id in changed for row in db.execute("SELECT uid,revision FROM tasks WHERE id=?", (task_id,))] + research_changes,
                              "request_id": request_id}
                db.execute("INSERT INTO transactions VALUES(?,?,?)", (request_id, digest, _json(result)))
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    def _operate(self, tasks, links, operation, actor):
        if not isinstance(operation, dict) or not isinstance(operation.get("op"), str):
            raise StoreError(400, "Invalid operation")
        op = operation["op"]
        keys = {"create": {"record", "parent_id"}, "update": {"task_id", "fields"},
                "set_parent": {"task_id", "parent_id"},
                "add_link": {"task_id", "target_id", "kind", "rationale", "review_state"},
                "remove_link": {"task_id", "target_id", "kind"},
                "claim": {"task_id", "claimant", "reserved_paths"},
                "release": {"task_id", "claimant"}, "transfer": {"task_id", "claimant", "to"},
                "journal": {"task_id", "kind", "text", "evidence", "supersedes", "boundary", "outcome"},
                "archive": {"task_id", "reason"}}
        if op not in keys or set(operation) - keys[op] - {"op"}:
            raise StoreError(400, "Unsupported operation or fields")
        if op == "create":
            record = operation.get("record")
            if not isinstance(record, dict) or "subtasks" in record:
                raise StoreError(400, "Create one flat record at a time")
            record = _copy(record)
            task_id = _id(record.get("id"))
            if task_id in tasks:
                raise StoreError(400, "Task ID already exists")
            if record.get("claim") is not None:
                raise StoreError(400, "Use explicit claim operation")
            parent_id = operation.get("parent_id")
            if parent_id is not None:
                _id(parent_id)
            task = {"id": task_id, "parent_id": parent_id,
                    "sibling_order": 1 + max((t["sibling_order"] for t in tasks.values() if t["parent_id"] == parent_id), default=-1),
                    "record": record, "original_record": _copy(record), "source_path": None, "archive_reason": None}
            tasks[task_id] = task
            self._sync_dependencies(task_id, record.get("dependencies", []), links)
            return task_id, op, {"after": _copy(task)}
        task_id = _id(operation.get("task_id"))
        if task_id not in tasks:
            raise StoreError(404, "Task not found")
        task, before = tasks[task_id], _copy(tasks[task_id])
        record = task["record"]
        if op == "update":
            fields = operation.get("fields")
            allowed = {"title", "summary", "status", "owner", "stage", "prior_work", "next_step", "acceptance", "parent_gate", "acceptance_gate_passed", "evidence", "dependencies"}
            if not isinstance(fields, dict) or not fields or set(fields) - allowed:
                raise StoreError(400, "Unsupported task fields; claims/journals require explicit operations")
            record.update(_copy(fields))
            if "dependencies" in fields:
                self._sync_dependencies(task_id, fields["dependencies"], links)
        elif op == "set_parent":
            parent = operation.get("parent_id")
            if parent is not None:
                _id(parent)
            task["parent_id"] = parent
            task["sibling_order"] = 1 + max((t["sibling_order"] for t in tasks.values() if t["parent_id"] == parent and t["id"] != task_id), default=-1)
        elif op in {"add_link", "remove_link"}:
            target, kind = _id(operation.get("target_id")), operation.get("kind")
            if kind not in ("depends_on", "related"):
                raise StoreError(400, "Invalid relationship kind")
            matching = [l for l in links if (l["source_id"], l["target_id"], l["kind"]) == (task_id, target, kind)]
            if op == "add_link":
                if matching:
                    raise StoreError(400, "Relationship already exists")
                rationale, state = operation.get("rationale", ""), operation.get("review_state", "unreviewed")
                if not isinstance(rationale, str) or state not in ("unreviewed", "lead", "confirmed", "legacy"):
                    raise StoreError(400, "Invalid relationship review metadata")
                links.append({"source_id": task_id, "target_id": target, "kind": kind, "rationale": rationale, "review_state": state})
            elif not matching:
                raise StoreError(404, "Relationship not found")
            else:
                links.remove(matching[0])
            if kind == "depends_on":
                record["dependencies"] = [l["target_id"] for l in links if l["source_id"] == task_id and l["kind"] == kind]
        elif op in {"claim", "release", "transfer"}:
            claimant = _text(operation.get("claimant", actor["name"]), "claimant", 120)
            current = record.get("claim")
            if op == "claim":
                if current and current["by"] != claimant:
                    raise StoreError(409, "Task already claimed")
                record["claim"] = current if current else {"by": claimant}
                if "reserved_paths" in operation:
                    self._reservations(operation["reserved_paths"])
                    record["reserved_paths"] = _copy(operation["reserved_paths"])
            else:
                if not current or current["by"] != claimant:
                    raise StoreError(409, "Only current claimant may release or transfer")
                record["claim"] = None if op == "release" else {"by": _text(operation.get("to"), "new claimant", 120)}
        elif op == "journal":
            if not isinstance(operation.get("kind"), str) or operation["kind"] not in _KINDS:
                raise StoreError(400, "Invalid journal kind")
            _text(operation.get("text"), "journal text")
            evidence = operation.get("evidence", [])
            if not isinstance(evidence, list):
                raise StoreError(400, "Invalid journal evidence")
            for path in evidence:
                _path(path)
            for key in ("boundary", "outcome"):
                if key in operation:
                    _text(operation[key], key)
            return task_id, operation["kind"], {k: _copy(v) for k, v in operation.items() if k not in {"op", "task_id", "kind"}}
        elif op == "archive":
            task["archive_reason"] = _text(operation.get("reason"), "archive reason")
        return task_id, op, {"operation": _copy(operation), "before": before, "after": _copy(task)}

    @staticmethod
    def _sync_dependencies(task_id, dependencies, links):
        if not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies):
            raise StoreError(400, "Dependencies must be task IDs")
        links[:] = [l for l in links if not (l["source_id"] == task_id and l["kind"] == "depends_on")]
        links.extend({"source_id": task_id, "target_id": d, "kind": "depends_on", "rationale": "", "review_state": "unreviewed"} for d in dependencies)

    @staticmethod
    def _gates(before, after, links, operations, *, validated_tasks=()):
        for task_id, task in after.items():
            old = before.get(task_id)
            record = task["record"]
            if record["status"] == "done" and (old is None or record != old["record"]):
                # Claims do not change the historical scope/gate of legacy done nodes.
                changed = {k for k in set(record) | set(old["record"] if old else {})
                           if record.get(k) != (old["record"].get(k) if old else None)}
                if old is None or changed - {"claim", "reserved_paths", "owner"}:
                    if task_id not in validated_tasks and (record.get("acceptance_gate_passed") is not True or not record.get("evidence")):
                        raise StoreError(409, "Completion requires this task's passed gate and evidence")
                    if old and old["record"]["status"] == "done" and changed & {"acceptance", "parent_gate"}:
                        raise StoreError(409, "Reopen task before changing completed acceptance")
            if old and old["record"]["status"] == "done" and record["status"] != "done":
                for link in links:
                    if link["kind"] == "depends_on" and link["target_id"] == task_id and after[link["source_id"]]["record"]["status"] == "done":
                        raise StoreError(409, "Reopen completed dependent when reopening prerequisite")
        for operation in operations:
            if operation["op"] in {"journal", "claim", "release", "transfer"}:
                continue
            task_id = operation.get("task_id") or operation.get("record", {}).get("id")
            old = before.get(task_id)
            new = after[task_id]
            if operation["op"] in {"add_link", "remove_link"} and operation.get("kind") == "related":
                continue
            parents = {new["parent_id"], old["parent_id"] if old else None}
            for parent in parents:
                visited = set()
                while parent is not None and parent not in visited:
                    visited.add(parent)
                    if after[parent]["record"]["status"] == "done":
                        raise StoreError(409, "Reopen completed ancestor before changing required child work")
                    parent = after[parent]["parent_id"]
            if operation["op"] in {"add_link", "remove_link", "set_parent", "archive"} and old and old["record"]["status"] == "done" and new["record"]["status"] == "done":
                raise StoreError(409, "Reopen completed task before changing its scope")
