"""HTTP-only agent client for the loopback SSG2 roadmap service."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
import hashlib
from pathlib import Path
from urllib.parse import urlencode


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def request(base_url, path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = {}
        message = body.get("error", f"HTTP {exc.code}")
        if exc.code == 409:
            message = f"STALE CONFLICT (409): {message}; reread context and reconcile explicitly"
        raise ApiError(exc.code, message) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(0, f"Roadmap API unavailable or returned invalid JSON: {exc}") from exc


def _record(task):
    return task.get("record", {})


def format_task_list(snapshot, status=None):
    """Render tasks in API snapshot order, with compact ownership context."""
    lines = []
    if snapshot.get("partial_coverage"):
        lines.append("WARNING: task coverage is partial; check linked history and open candidate tasks for review.")
    lines.append("ID | STATUS | TITLE | PARENT | CLAIM / OWNER")
    for task in snapshot.get("tasks", []):
        record = _record(task)
        task_status = record.get("status", "unknown")
        if status and task_status != status:
            continue
        claim = record.get("claim")
        claimant = claim.get("by", "") if isinstance(claim, dict) else ""
        owner = record.get("owner", "")
        lines.append(" | ".join((task.get("id", "?"), task_status,
                                 str(record.get("title", "(untitled)")),
                                 str(task.get("parent_id") or "-"),
                                 ", ".join(part for part in (f"claim={claimant}" if claimant else "",
                                                              f"owner={owner}" if owner else "") if part) or "-")))
    return "\n".join(lines)


def format_context(snapshot, task_id):
    tasks = {t["id"]: t for t in snapshot.get("tasks", [])}
    task = tasks.get(task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")
    record = _record(task)
    lines = [f"{task_id}: {record.get('title', '(untitled)')} [{record.get('status', 'unknown')}]"]
    lines += [f"Revision: {snapshot.get('revision', 'unknown')}", f"Summary: {record.get('summary', '')}"]
    if task.get("parent_id") in tasks:
        parent = tasks[task["parent_id"]]
        lines.append(f"Parent: {parent['id']} — {_record(parent).get('title', '(untitled)')}")
    children = [t for t in tasks.values() if t.get("parent_id") == task_id]
    if children:
        lines.append("Children: " + "; ".join(f"{c['id']} ({_record(c).get('status', 'unknown')})" for c in children))
    prereqs = [l["target_id"] for l in snapshot.get("links", []) if l.get("source_id") == task_id and l.get("kind") == "depends_on"]
    if prereqs:
        lines.append("Prerequisites: " + ", ".join(f"{p} [{_record(tasks[p]).get('status', 'unknown')}]" if p in tasks else p for p in prereqs))
    for label in ("owner", "reserved_paths", "acceptance", "acceptance_gate_passed", "evidence", "prior_work", "next_work"):
        if label in record:
            lines.append(f"{label.replace('_', ' ').title()}: {json.dumps(record[label], ensure_ascii=False)}")
    events = [e for e in snapshot.get("events", []) if e.get("task_id") == task_id]
    if events:
        lines.append("Journal / events:")
        for event in events:
            details = event.get("details", {})
            text = details.get("text", details.get("outcome", details.get("boundary", ""))) if isinstance(details, dict) else ""
            lines.append(f"- {event.get('sequence', '?')} {event.get('kind', 'event')}: {text}")
    return "\n".join(lines)


def _write(args, operation):
    snap = request(args.url, "/api/tasks")
    request_id = args.request_id or str(uuid.uuid4())
    payload = {"expected_revision": snap["revision"], "request_id": request_id,
               "actor": args.actor, "operations": [operation]}
    print(f"Request ID: {request_id}", file=sys.stderr)
    result = request(args.url, "/api/transaction", payload)
    print(f"Updated at revision {result.get('revision', 'unknown')} (request {request_id})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8769", help="loopback roadmap API base URL")
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("v1-read", help="read a project-scoped workspace resource")
    read.add_argument("resource")
    read.add_argument("--project")
    read.add_argument("--limit", type=int)
    read.add_argument("--cursor")
    transaction = sub.add_parser("v1-transaction", help="submit an exact v1 envelope from a UTF-8 JSON file")
    transaction.add_argument("--file", type=Path, required=True)
    variable_import = sub.add_parser("import-variables", help="preview or apply one hash-pinned metadata registry cutover")
    variable_import.add_argument("--file", type=Path, required=True)
    variable_import.add_argument("--project", required=True)
    variable_import.add_argument("--principal")
    variable_import.add_argument("--expected-source-sha256")
    variable_import.add_argument("--dry-run", action="store_true")
    variable_import.add_argument("--request-id")
    jobs = sub.add_parser("process-jobs", help="process one bounded durable matching page")
    jobs.add_argument("--project", required=True)
    jobs.add_argument("--limit", type=int, default=100)
    jobs.add_argument("--request-id")
    listing = sub.add_parser("list", help="list task IDs, status, parent, and ownership")
    listing.add_argument("--status", choices=("ready", "active", "blocked", "review", "done"))
    create = sub.add_parser("create", help="create one task or subtask from a JSON record")
    create.add_argument("--record", required=True, help="flat task record as a JSON object")
    create.add_argument("--parent", help="optional existing parent task ID")
    create.add_argument("--actor", required=True)
    create.add_argument("--request-id")
    context = sub.add_parser("context", help="print task handoff context and event history")
    context.add_argument("task_id")
    for name in ("claim", "release"):
        p = sub.add_parser(name)
        p.add_argument("task_id")
        p.add_argument("actor")
        p.add_argument("--request-id")
    update = sub.add_parser("update")
    update.add_argument("task_id"); update.add_argument("--actor", required=True)
    update.add_argument("--field", action="append", default=[], metavar="NAME=JSON")
    update.add_argument("--request-id")
    journal = sub.add_parser("journal")
    journal.add_argument("task_id"); journal.add_argument("--actor", required=True)
    journal.add_argument("--kind", required=True); journal.add_argument("--text", required=True)
    journal.add_argument("--request-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "process-jobs":
            caps = request(args.url, '/v1/projects/'+args.project+'/capabilities')
            request_id = args.request_id or str(uuid.uuid4())
            print(f'Request ID: {request_id}',file=sys.stderr)
            result = request(args.url,'/v1/transactions',{'project_id':args.project,
                'actor':{'principal_id':caps['capabilities']['local_principal_id']},
                'request_id':request_id,'expected_revision':caps['revision'],
                'operations':[{'op':'process_jobs','limit':args.limit}]})
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        if args.command == "import-variables":
            document = json.loads(args.file.read_text(encoding="utf-8-sig"))
            source_hash = hashlib.sha256(json.dumps(document,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
            preview = request(args.url, "/v1/variable-import-preview", {"project_id":args.project,"document":document,"source_hash":source_hash})
            if args.dry_run:
                print(json.dumps(preview,ensure_ascii=False,indent=2))
                return 0
            if args.expected_source_sha256 != source_hash or not args.principal:
                raise ValueError("Apply requires --principal and --expected-source-sha256 from the dry run")
            request_id = args.request_id or str(uuid.uuid4())
            print(f"Request ID: {request_id}",file=sys.stderr)
            result = request(args.url, "/v1/transactions", {"project_id":args.project,
                "actor":{"principal_id":args.principal},"request_id":request_id,"expected_revision":preview['revision'],
                "operations":[{"op":"import_variable_registry","document":document,"source_hash":source_hash}]})
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        if args.command == "v1-read":
            if not args.resource or any(c not in 'abcdefghijklmnopqrstuvwxyz-' for c in args.resource):
                raise ValueError("Expected a workspace resource name")
            params = {k: v for k, v in {"project_id":args.project,"limit":args.limit,"cursor":args.cursor}.items() if v is not None}
            result = request(args.url, "/v1/" + args.resource + ('?' + urlencode(params) if params else ''))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "v1-transaction":
            payload = json.loads(args.file.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("Transaction file must contain a v1 envelope object")
            result = request(args.url, "/v1/transactions", payload)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "context":
            print(format_context(request(args.url, "/api/tasks"), args.task_id))
            return 0
        if args.command == "list":
            print(format_task_list(request(args.url, "/api/tasks"), args.status))
            return 0
        if args.command == "create":
            record = json.loads(args.record)
            if not isinstance(record, dict) or not all(key in record for key in ("id", "title", "status")):
                raise ValueError("--record must be a JSON object with id, title, and status")
            if "subtasks" in record or record.get("claim") is not None:
                raise ValueError("Create one flat record; claims use the claim command")
            op = {"op": "create", "record": record, "parent_id": args.parent}
            _write(args, op)
            return 0
        if args.command in {"claim", "release"}:
            op = {"op": args.command, "task_id": args.task_id, "claimant": args.actor}
            args.actor = args.actor
        elif args.command == "journal":
            op = {"op": "journal", "task_id": args.task_id, "kind": args.kind, "text": args.text}
        else:
            fields = {}
            for item in args.field:
                name, sep, value = item.partition("=")
                if not sep or not name or name in fields:
                    raise ValueError("Each --field must be a unique NAME=JSON")
                fields[name] = json.loads(value)
            if not fields:
                raise ValueError("Provide at least one --field NAME=JSON")
            op = {"op": "update", "task_id": args.task_id, "fields": fields}
        _write(args, op)
        return 0
    except (ApiError, ValueError, KeyError, OSError) as exc:
        print(f"roadmap-cli: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
