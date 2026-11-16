"""Loopback API for the SQLite-backed roadmap and read-only variable registry."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import ssl
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
import workspace_auth
from workspace_sync_service import WorkspaceSyncService
from workspace_live_service import WorkspaceLiveService
from workspace_live_ui import WorkspaceLiveUI
from workspace_live_client import LiveConflict
from roadmap_cli import ApiError as LiveApiError
from workspace_sync import SyncConflict,PrivacyBlocked
from workspace_git_transport import TransportError

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


def workspace_read(parsed,*,ui_actor_id=None):
    """Strict versioned read adapter; project selection is never implicit."""
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    allowed = {"project_id", "limit", "cursor", "depth", "after", "principal_id", "document_id", "task_id", "ancestor_id", "order", "state", "survivor_id", "absorbed_id", "merge_id", "repository_id", "portable", "compensation_id", "subject_type", "subject_id", "manifest_id", "key", "source_sha256", "disclosure_id", "offset", "review_id", "side"}
    if set(params) - allowed or any(len(values) != 1 for values in params.values()):
        raise RoadmapError(400, "Unknown or duplicate workspace parameter")
    values = {key: item[0] for key, item in params.items()}
    path = parsed.path.strip('/').split('/')
    project_id = values.pop("project_id", None)
    options = {}
    for key, value in values.items():
        if key in {"limit", "depth", "offset"}:
            if not value.isascii() or not value.isdigit():
                raise RoadmapError(400, "Invalid integer workspace parameter")
            value = int(value)
        if key=='portable':
            if value not in ('true','false'):raise RoadmapError(400,'Portable export option must be true or false')
            value=value=='true'
        options[key] = value
    if path == ["v1", "projects"]:
        if params:
            raise RoadmapError(400, "Project listing accepts no parameters")
        return _store().read_v1("projects")
    if len(path) == 4 and path[1] == "projects" and path[3] == "capabilities":
        if params:
            raise RoadmapError(400, "Capabilities project is selected by path")
        project=urllib.parse.unquote(path[2]);store=_store();result=store.read_v1('capabilities',project)
        live=WorkspaceLiveService(store).read(project)
        result['capabilities']['claim_scope']='local-only' if live['enabled'] else 'this-service'
        result['capabilities']['upstream_data_sync']=live['enabled']
        result['capabilities']['shared_claim_coordination']='explicit-host-api' if live['enabled'] else 'this-service'
        return result
    if path == ['v1','tasks','merge-compensation-preview']:
        if set(options)!={'merge_id'}:raise RoadmapError(400,'Merge compensation preview requires merge_id')
        return _store().merge_compensation_preview(project_id,options['merge_id'])
    if path == ['v1','tasks','merge-reversal-preview']:
        if set(options)!={'merge_id'}:
            raise RoadmapError(400,'Merge reversal preview requires merge_id')
        return _store().merge_reversal_preview(project_id,options['merge_id'])
    if path == ['v1','tasks','merge-preview']:
        if set(options)!={'survivor_id','absorbed_id'}:
            raise RoadmapError(400,'Merge preview requires survivor_id and absorbed_id')
        return _store().merge_preview(project_id,options['survivor_id'],options['absorbed_id'])
    if path in (['v1','live-ui-state'],['v1','live-ui-value']):
        store=_store();actor=ui_actor_id or store.read_v1('capabilities',project_id)['capabilities'].get('local_principal_id')
        ui=WorkspaceLiveUI(WorkspaceLiveService(store),project_id,actor)
        if path[-1]=='live-ui-state':
            if set(options)-{'offset'}:raise RoadmapError(400,'Unsupported live review page parameter')
            return ui.read(options.get('offset',0))
        if not {'review_id','key','side'}<=set(options) or set(options)-{'review_id','key','side','offset'}:raise RoadmapError(400,'Expected scoped live value selector')
        return ui.value(options['review_id'],options['key'],options['side'],options.get('offset',0))
    if path==['v1','live-status']:
        if options:raise RoadmapError(400,'Live status accepts only project_id')
        return WorkspaceLiveService(_store()).read(project_id)
    if path==['v1','git-sync-status']:
        if set(options)!={'repository_id'}:raise RoadmapError(400,'Git status requires repository_id')
        return WorkspaceSyncService(_store()).read(project_id,options['repository_id'])
    if path == ['v1','exchange-export']:
        if set(options)-{'ancestor_id','portable'}:
            raise RoadmapError(400,'Unsupported exchange export parameter')
        return _store().exchange_preview(project_id,ancestor_id=options.get('ancestor_id'),portable=options.get('portable',False))
    if path==['v1','task-sharing-original']:
        if set(options)-{'key','source_sha256','disclosure_id'}:raise RoadmapError(400,'Unsupported task original parameter')
        return _store().read_v1('task-sharing-original',project_id,**options)
    if path==['v1','task-sharing-review']:
        if set(options)-{'limit','cursor'}:raise RoadmapError(400,'Unsupported task sharing review parameter')
        return _store().read_v1('task-sharing-review',project_id,**options)
    if path==['v1','journal-sharing-review']:
        if set(options)-{'limit','cursor'}:raise RoadmapError(400,'Unsupported sharing review parameter')
        return _store().read_v1('journal-sharing-review',project_id,**options)
    if path==['v1','source-manifests']:
        if set(options)-{'manifest_id','limit','cursor'}:raise RoadmapError(400,'Unsupported source manifest parameter')
        return _store().read_v1('source-manifests',project_id,**options)
    if len(path)==4 and path[1] in ('findings','questions','answers','evidence-links') and path[3]=='context':
        if options:raise RoadmapError(400,'Research context route accepts only project scope')
        resource={'findings':'finding-context','questions':'question-context','answers':'answer-context','evidence-links':'evidence-context'}[path[1]]
        return _store().read_v1(resource,project_id,key=urllib.parse.unquote(path[2]))
    if len(path)==2 and path[1] in {'finding-revisions','evidence-links','evidence-changes','research-review-notices','answers','answer-reviews','question-resolutions','answer-support'}:
        if set(options)-{'subject_type','subject_id','limit','cursor'}:raise RoadmapError(400,'Unsupported finding history parameter')
        return _store().read_v1(path[1],project_id,**options)
    if path==['v1','merge-compensations']:
        if set(options)-{'task_id','merge_id','compensation_id','limit','cursor'}:raise RoadmapError(400,'Unsupported compensation audit parameter')
        return _store().read_v1('merge-compensations',project_id,**options)
    if len(path)==2 and path[1] in {'attempts','validations','blockers','claims'}:
        if set(options)-{'task_id','limit','cursor','order','state'}:
            raise RoadmapError(400,'Unsupported collaboration read parameter')
        return _store().read_v1(path[1],project_id,**options)
    if len(path) == 2 and path[1] in {"tasks", "events", "artifacts", "artifact-versions", "index-runs", "occurrences", "observations", "questions", "negative-searches", "findings", "hints", "subscriptions", "inbox", "processing-status", "replay-jobs", "documents", "document-versions", "discussions", "proposals", "feedback", "repositories", "repository-updates"}:
        resource = path[1]
    elif len(path) == 4 and path[1] == "tasks" and path[3] in {"context", "neighborhood", "history"}:
        resource = path[3]
        options["key"] = urllib.parse.unquote(path[2])
    else:
        raise RoadmapError(404, "Unknown workspace route")
    permitted = {"tasks": {"limit", "cursor"}, "events": {"limit", "after"},
                 "context": set(), "history": {"limit", "cursor", "key"},
                 "neighborhood": {"limit", "depth", "cursor", "key"}}.get(resource, {"limit", "cursor"}) | {"key"}
    if resource in {"hints", "subscriptions", "inbox", "processing-status", "replay-jobs", "feedback"}:
        permitted.add("principal_id")
    if resource=="repository-updates":
        permitted.add("repository_id")
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

    def _shared(self):
        return getattr(self.server, 'shared_mode', False) is True

    def _principal(self):
        """Authenticate each shared request against current credential state."""
        header = self.headers.get('Authorization', '')
        if not header.startswith('Bearer ') or header.count(' ') != 1:
            raise RoadmapError(401, 'Bearer credential required')
        with _store()._connection() as db:
            return workspace_auth.authenticate(db, header[7:])

    @staticmethod
    def _scope(principal, project_id):
        if not isinstance(project_id, str) or not project_id:
            raise RoadmapError(400, 'project_id is required')
        if not principal['is_owner'] and project_id != principal['project_id']:
            raise RoadmapError(403, 'Project access denied')

    def _shared_read(self, parsed, principal):
        if (parsed.path=='/v1/task-sharing-original' or parsed.path.startswith('/v1/live-ui-')) and not principal['is_owner']:
            raise RoadmapError(403,'Private review data requires host owner credential')
        if parsed.path == '/v1/projects':
            result = workspace_read(parsed)
            if not principal['is_owner']:
                result['items'] = [item for item in result['items'] if item['id'] == principal['project_id']]
            return result
        parts = parsed.path.strip('/').split('/')
        if len(parts) == 4 and parts[1] == 'projects' and parts[3] == 'capabilities':
            project_id = urllib.parse.unquote(parts[2])
        else:
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            project_id = params.get('project_id', [None])[0]
            if 'principal_id' in params and (len(params['principal_id']) != 1 or
                    params['principal_id'][0] != principal['principal_id'] and not principal['is_owner']):
                raise RoadmapError(403, 'Principal access denied')
        self._scope(principal, project_id)
        if parsed.path in ('/v1/inbox','/v1/subscriptions','/v1/feedback') and 'principal_id' not in params:
            params['principal_id']=[principal['principal_id']]
            parsed=parsed._replace(query=urllib.parse.urlencode(params,doseq=True))
        result = workspace_read(parsed,ui_actor_id=principal['principal_id']) if parsed.path.startswith('/v1/live-ui-') else workspace_read(parsed)
        if len(parts) == 4 and parts[1] == 'projects' and parts[3] == 'capabilities':
            result['capabilities'].update({'authenticated': True,'host_owner':principal['is_owner'],
                'local_principal_id': principal['principal_id'],
                'granted': ['contribute', 'review', 'manage_claims', 'manage_projects']
                           if principal['is_owner'] else principal['capabilities']})
        return result

    def _shared_write(self, parsed, payload, principal):
        if not isinstance(payload, dict):
            raise RoadmapError(400, 'Workspace mutation requires an object')
        if parsed.path == '/v1/repository-setup':
            if not principal['is_owner']:
                raise RoadmapError(403, 'Host filesystem access requires owner credential')
            return
        self._scope(principal, payload.get('project_id'))
        if (parsed.path.startswith('/v1/git-sync-') or parsed.path.startswith('/v1/live-')) and not principal['is_owner']:
            raise RoadmapError(403,'Host connection operations require owner credential')
        if parsed.path == '/v1/repository-refresh':
            if not principal['is_owner']:
                raise RoadmapError(403, 'Host filesystem access requires owner credential')
            return
        if parsed.path == '/v1/transactions':
            actor = payload.get('actor')
            if not isinstance(actor, dict) or actor.get('principal_id') != principal['principal_id']:
                raise RoadmapError(403, 'Transaction actor must match credential')
            operations = payload.get('operations')
            if not isinstance(operations, list):
                raise RoadmapError(400, 'Transaction operations are required')
            owner_ops = {'create_project', 'register_repository', 'register_root',
                         'register_artifact', 'register_artifact_version', 'bind_root',
                         'register_document_version'}
            if any(isinstance(op, dict) and op.get('op') in owner_ops for op in operations) and not principal['is_owner']:
                raise RoadmapError(403, 'Host filesystem access requires owner credential')
            required = {'contribute'}
            if any(isinstance(op, dict) and op.get('op') in {'record_validation', 'complete_task', 'approve_guidance', 'review_answer', 'review_journal_sharing', 'review_task_sharing'} for op in operations):
                required.add('review')
            if any(isinstance(op, dict) and op.get('op') in {'handoff_claim'} for op in operations):
                required.add('manage_claims')
            if any(isinstance(op, dict) and op.get('op') in {'create_project', 'register_repository'} for op in operations):
                required.add('manage_projects')
            if not principal['is_owner'] and not required <= set(principal['capabilities']):
                raise RoadmapError(403, 'Credential lacks required capability')
        elif parsed.path == '/v1/variable-import-preview' and not principal['is_owner']:
            raise RoadmapError(403, 'Host filesystem access requires owner credential')

    def _setup_request(self,base_url,path,payload=None):
        """In-process owner adapter; retain the real credential for every write."""
        principal=self._principal()
        if not principal['is_owner']:raise RoadmapError(403,'Host owner credential required')
        if payload is not None:
            if path!='/v1/transactions':raise RoadmapError(400,'Unsupported setup operation')
            return _store().transact_v1(payload,bearer_token=self.headers['Authorization'][7:])
        parsed=urlparse(path)
        result=workspace_read(parsed)
        if parsed.path=='/v1/projects':
            with _store()._connection() as db:
                administered={row['project_id'] for row in db.execute('SELECT project_id,capabilities_json FROM project_members WHERE principal_id=?',(principal['principal_id'],)) if 'manage_projects' in json.loads(row['capabilities_json'])}
            result['items']=[item for item in result['items'] if item['id'] in administered]
        if 'capabilities' in result:
            result['capabilities']['local_principal_id']=principal['principal_id']
        return result

    def _json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if status==200 and self.headers.get('X-Workspace-Chunked')=='v1' and not self.path.startswith('/v1/transfer-download') and len(data)>4*1024*1024:
            import workspace_transfer
            project=getattr(self,'_response_project',None)
            if not project:raise RoadmapError(413,'Large responses require a project scope')
            principal=self._principal() if self._shared() else None
            if principal is not None:self._scope(principal,project)
            value=workspace_transfer.cache_response(_store(),project,principal['principal_id'] if principal else 'trusted-local',value)
            data=json.dumps(value,ensure_ascii=False).encode('utf-8')
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def _check_origin(self):
        host=self.headers.get('Host','').lower()
        port=self.server.server_address[1]
        allowed=getattr(self.server,'accepted_hosts',None) or {f'127.0.0.1:{port}',f'localhost:{port}',f'[::1]:{port}'}
        if host not in allowed:
            raise RoadmapError(403,'Unrecognized service host')
        origin=self.headers.get('Origin')
        if origin is not None and origin not in ('http://'+host,'https://'+host):
            raise RoadmapError(403,'Cross-origin workspace requests are not allowed')

    def _body(self):
        self._check_origin()
        if self.headers.get('Content-Type','').split(';',1)[0].strip().lower()!='application/json':
            raise RoadmapError(415,'Workspace mutations require application/json')
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
            self._check_origin()
            if parsed.path == '/v1/service-info' and not parsed.query:
                self._json(200, {'api_version': 'research-workspace/v1', 'shared_mode': self._shared()})
            elif self._shared() and parsed.path.startswith('/api/'):
                raise RoadmapError(403, 'Legacy API is unavailable in shared mode')
            elif self._shared() and parsed.path.startswith('/repo/'):
                principal = self._principal()
                if not principal['is_owner']:
                    raise RoadmapError(403, 'Repository files require owner credential')
                self._serve_repo_file()
            elif parsed.path.startswith("/v1/"):
                self._response_project=urllib.parse.parse_qs(parsed.query).get('project_id',[None])[0]
                self._json(200, self._shared_read(parsed, self._principal()) if self._shared() else workspace_read(parsed))
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
            elif parsed.path in {'/assets/workspace-repositories.js','/assets/workspace-access.js','/assets/workspace-sharing.js','/assets/workspace-navigation.js','/assets/workspace-layout.js','/assets/workspace-live.js','/assets/workspace-research-session.js','/assets/workspace-research.js','/assets/workspace-task-session.js','/assets/workspace-task-workflows.js'} and not parsed.query:
                body=(ROOT/'docs'/parsed.path.rsplit('/',1)[-1]).read_bytes()
                self.send_response(200);self.send_header('Content-Type','text/javascript; charset=utf-8')
                self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            elif parsed.path in {"/", "/index.html"} or (parsed.path == "/graph" and not parsed.query):
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
            if self._shared() and parsed.path.startswith('/api/'):
                raise RoadmapError(403, 'Legacy API is unavailable in shared mode')
            principal = self._principal() if self._shared() and parsed.path.startswith('/v1/') else None
            payload = self._body()
            self._response_project=payload.get('project_id') if isinstance(payload,dict) else None
            if principal is not None:
                self._shared_write(parsed, payload, principal)
            if parsed.path.startswith('/v1/transfer-') and not parsed.query:
                import workspace_transfer
                if parsed.path not in ('/v1/transfer-download','/v1/transfer-release') and principal is not None and not principal['is_owner'] and 'contribute' not in principal['capabilities']:
                    raise RoadmapError(403,'Staging requires contribute capability')
                action=parsed.path.removeprefix('/v1/transfer-')
                result=workspace_transfer.stage(_store(),action,payload,principal['principal_id'] if principal else 'trusted-local')
                if action!='finish':
                    self._json(200,result);return
                target,payload=result
                parsed=urlparse(target)
                self._response_project=payload['project_id']
                if principal is not None:
                    # Reauthenticate after verification, then use the original dispatch/auth gates.
                    principal=self._principal()
                    self._shared_write(parsed,payload,principal)
            if parsed.path.startswith('/v1/live-') and not parsed.query:
                action=parsed.path.removeprefix('/v1/live-')
                extra={'configure':{'enabled'},'join-preview':set(),'join':{'review','choices'},'sync':set(),'review':{'side'},'resolve':{'side','review','choices'},'claims-read':{'task_id'},'claims-preview':{'operation'},'claims-submit':{'review_id'},'claims-review':set(),'ui-preview':{'kind'},'ui-choices':{'review_id','offset','expected_draft_revision','request_id','choices'},'ui-prepare':{'review_id','expected_draft_revision','acknowledged'},'ui-submit':{'review_id','command_id'},'ui-discard':{'review_id'}}
                if action not in extra or not isinstance(payload,dict):raise RoadmapError(400,'Invalid live connection action')
                required={'project_id'}|extra[action];allowed=required|({'url','token_file'} if action=='configure' else {'cursor'} if action=='claims-read' else set())
                if not required<=set(payload) or set(payload)-allowed:raise RoadmapError(400,'Invalid live connection request')
                project=payload['project_id'];service=WorkspaceLiveService(_store())
                try:
                    if action.startswith('ui-'):
                        actor=principal['principal_id'] if principal else _store().read_v1('capabilities',project)['capabilities'].get('local_principal_id')
                        ui=WorkspaceLiveUI(service,project,actor)
                        if action=='ui-preview':result=ui.preview(payload['kind'])
                        elif action=='ui-choices':result=ui.choose(payload['review_id'],payload['offset'],payload['expected_draft_revision'],payload['request_id'],payload['choices'])
                        elif action=='ui-prepare':result=ui.prepare(payload['review_id'],payload['expected_draft_revision'],payload['acknowledged'])
                        elif action=='ui-submit':result=ui.submit(payload['review_id'],payload['command_id'])
                        else:result=ui.discard(payload['review_id'])
                    elif action=='configure':result=service.configure(project,enabled=payload['enabled'],url=payload.get('url'),token_file=payload.get('token_file'))
                    elif action=='join-preview':result=service.preview_join(project)
                    elif action=='join':result=service.join(project,payload['review'],payload['choices'])
                    elif action=='sync':result=service.sync_now(project)
                    elif action=='claims-read':result=service.claims_read(project,payload['task_id'],payload.get('cursor'))
                    elif action=='claims-preview':result=service.claims_preview(project,payload['operation'])
                    elif action=='claims-submit':result=service.claims_confirm(project,payload['review_id'])
                    elif action=='claims-review':result=service.claims_review(project)
                    elif action=='review':result=service.review(project,payload['side'])
                    else:result=service.resolve(project,payload['side'],payload['review'],payload['choices'])
                except LiveConflict as error:
                    self._json(409,{'error':'Live reconciliation needs a fresh explicit review','code':'live_conflict','alternatives':error.alternatives});return
                except LiveApiError:
                    raise RoadmapError(503,'Live host request was not confirmed; pending work is retained') from None
            elif parsed.path.startswith('/v1/git-sync-') and not parsed.query:
                base={'project_id','repository_id'}
                extra={'configure':{'enabled','export_selected'},'now':set(),'preview':set(),
                       'apply':{'choices','remote_tip','bundle_id','expected_revision'}}
                action=parsed.path.removeprefix('/v1/git-sync-')
                if action not in extra or not isinstance(payload,dict) or set(payload)!=base|extra[action]:
                    raise RoadmapError(400,'Invalid Git sharing request')
                service=WorkspaceSyncService(_store());project=payload['project_id'];identity=payload['repository_id']
                try:
                    if action=='configure':result=service.configure(project,identity,enabled=payload['enabled'],export_selected=payload['export_selected'])
                    elif action=='now':result=service.request_sync(project,identity)
                    elif action=='preview':result=service.preview(project,identity)
                    else:result=service.apply(project,identity,payload['choices'],payload['remote_tip'],payload['bundle_id'],payload['expected_revision'])
                except SyncConflict as exc:raise RoadmapError(409,'Merge review changed; load a new preview') from None
                except (PrivacyBlocked,TransportError):raise RoadmapError(503,'Git sharing is unavailable; check portable records and project Git access') from None
            elif parsed.path == '/v1/repository-refresh' and not parsed.query:
                if not isinstance(payload,dict) or set(payload)!={'project_id','repository_id'} or any(not isinstance(value,str) or not 0<len(value)<=200 for value in payload.values()):
                    raise RoadmapError(400,'Repository refresh requires project_id and repository_id')
                result=_store().refresh_repository(payload['project_id'],payload['repository_id'])
            elif parsed.path == '/v1/repository-setup' and not parsed.query:
                if not isinstance(payload,dict) or not {'root','apply'}<=set(payload) or set(payload)-{'root','apply','manifest','name'} or type(payload['apply']) is not bool:
                    raise RoadmapError(400,'Repository setup requires root and boolean apply')
                if not isinstance(payload['root'],str) or not 0<len(payload['root'])<=4000 or '\x00' in payload['root']:
                    raise RoadmapError(400,'Invalid repository root')
                if payload['apply'] and 'manifest' not in payload:
                    raise RoadmapError(400,'Apply requires the project manifest from the preview')
                import workspace
                try:
                    result=workspace.setup_project(payload['root'],f'http://127.0.0.1:{self.server.server_address[1]}',
                        apply=payload['apply'],name=payload.get('name'),proposed_manifest=payload.get('manifest'),
                        request_fn=self._setup_request if principal is not None else None)
                except workspace.ApiError as exc:
                    raise RoadmapError(exc.status if 400<=exc.status<600 else 503,str(exc)) from exc
                except workspace.GitAdapterError as exc:
                    raise RoadmapError(400,str(exc)) from exc
            elif parsed.path == "/v1/transactions" and not parsed.query:
                result = _store().transact_v1(payload, bearer_token=self.headers['Authorization'][7:]) if principal is not None else _store().transact_v1(payload)
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
        codes = {400: "validation", 401: "authentication", 403: "capability", 404: "not_found", 409: "conflict", 413: "size", 503: "unavailable"}
        try:
            latest = _store().snapshot()["revision"]
        except StoreError:
            latest = None
        result = {"error": str(exc), "code": codes.get(exc.status, "error"),
                  "conflicts": getattr(exc, "conflicts", []), "latest_revision": latest}
        for name in ("restart_required", "limits", "sharing_review_required", "transaction_rejection"):
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
    if version not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15):
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


def start_repository_worker(path, interval=2.0):
    """Inspect local Git outside SQLite locks; publish only changed snapshots."""
    stop=threading.Event()
    def consume():
        store=RoadmapStore(path)
        while not stop.is_set():
            try:
                with store._connection() as db:
                    connections=[dict(row) for row in db.execute('SELECT id,project_id FROM repository_connections ORDER BY project_id,id')]
                for connection in connections:
                    if stop.is_set():break
                    try:store.refresh_repository(connection['project_id'],connection['id'])
                    except (StoreError,OSError,ValueError) as exc:
                        print(f'Repository refresh retry: {exc}',flush=True)
            except (StoreError,OSError,ValueError) as exc:
                print(f'Repository worker retry: {exc}',flush=True)
            stop.wait(interval)
    worker=threading.Thread(target=consume,name='repository-refresh',daemon=True);worker.start()
    return stop,worker


def start_git_sync_worker(path,interval=2.0):
    stop=threading.Event()
    def consume():
        store=RoadmapStore(path);service=WorkspaceSyncService(store);live=WorkspaceLiveService(store)
        while not stop.is_set():
            try:
                available=live.tick()
                service.tick(skip_projects=available)
            except (StoreError,OSError,ValueError):print('Git sharing worker will retry',flush=True)
            stop.wait(interval)
    worker=threading.Thread(target=consume,name='git-sharing',daemon=True);worker.start()
    return stop,worker


def accepted_service_hosts(host,port,public_host=None):
    header=f'[{host.lower()}]' if ':' in host else host.lower()
    result={f'{header}:{port}',f'127.0.0.1:{port}',f'localhost:{port}',f'[::1]:{port}'}
    if public_host is not None:
        if not isinstance(public_host,str) or not re.fullmatch(r'(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[A-Fa-f0-9:]+\])(?::[0-9]{1,5})?',public_host):
            raise RoadmapError(400,'Public host must be an explicit hostname with optional port')
        parsed=urllib.parse.urlsplit('https://'+public_host)
        try:specified_port=parsed.port
        except ValueError:raise RoadmapError(400,'Invalid public host port') from None
        if specified_port is not None and not 1<=specified_port<=65535:raise RoadmapError(400,'Invalid public host port')
        result.add(public_host.lower())
    return result


def startup_security(host, shared_mode, tls_cert, tls_key, store_path):
    """Validate remote startup before binding; return a configured TLS context."""
    if not shared_mode:
        if host not in {'127.0.0.1', 'localhost', '::1'}:
            raise RoadmapError(400, 'Non-loopback binding requires shared mode')
        if tls_cert is not None or tls_key is not None:
            raise RoadmapError(400, 'TLS options require shared mode')
        return None
    if tls_cert is None or tls_key is None:
        raise RoadmapError(400, 'Shared mode requires a TLS certificate and key')
    try:
        with RoadmapStore(store_path)._connection() as db:
            rows = db.execute('SELECT t.expires_at FROM auth_tokens t '
                              'JOIN principals p ON p.id=t.principal_id '
                              'WHERE t.is_owner=1 AND t.revoked_at IS NULL '
                              'AND p.archived_at IS NULL').fetchall()
        now = datetime.now(timezone.utc)
        if not any(datetime.fromisoformat(row[0]) > now for row in rows):
            raise RoadmapError(503, 'Shared mode requires a current owner credential')
    except sqlite3.Error as exc:
        raise RoadmapError(503, 'Credential store is unavailable') from exc
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(str(tls_cert), str(tls_key))
    except (OSError, ssl.SSLError) as exc:
        raise RoadmapError(400, 'TLS certificate or key is unusable') from exc
    return context


def main(argv=None):
    global STORE_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--store", type=Path, default=STORE_PATH)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--shared-mode', action='store_true')
    parser.add_argument('--public-host',help='explicit trusted HTTP Host header, hostname with optional port')
    parser.add_argument('--tls-cert', type=Path)
    parser.add_argument('--tls-key', type=Path)
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
            tls = startup_security(args.host, args.shared_mode, args.tls_cert, args.tls_key, selected_store)
            accepted_service_hosts(args.host,args.port,args.public_host)
            server = ThreadingHTTPServer((args.host, args.port), Handler)
            server.shared_mode = args.shared_mode
            server.accepted_hosts = accepted_service_hosts(args.host,server.server_port,args.public_host)
            if tls is not None:
                server.socket = tls.wrap_socket(server.socket, server_side=True)
            stop, worker = start_matcher_worker(selected_store)
            repository_stop, repository_worker = start_repository_worker(selected_store)
            sync_stop,sync_worker=start_git_sync_worker(selected_store)
            scheme = 'https' if tls is not None else 'http'
            print(f"Research Workspace at {scheme}://{args.host}:{server.server_port}/ (store: {selected_store})")
            try: server.serve_forever()
            except KeyboardInterrupt: pass
            finally:
                stop.set();repository_stop.set();sync_stop.set()
                worker.join(timeout=6);repository_worker.join(timeout=6);sync_worker.join(timeout=6)
                server.server_close()
    except RoadmapError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
