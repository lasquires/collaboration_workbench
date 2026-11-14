"""HTTP-only agent client for the loopback SSG2 roadmap service."""
from __future__ import annotations

import argparse
import os
import re
import json
import sys
import urllib.error
import urllib.request
import uuid
import hashlib
from pathlib import Path
from urllib.parse import urlencode,urlsplit


class ApiError(Exception):
    def __init__(self, status, message, *, details=None):
        self.status = status
        self.message = message
        self.details = details if isinstance(details,dict) else {}
        super().__init__(message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_wire(base_url, path, payload=None, *, token_file=None, chunked=False, timeout=5):
    parsed=urlsplit(base_url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ApiError(0,'Service URL must be HTTP(S) without embedded credentials, query or fragment')
    if not path.startswith('/') or path.startswith('//'):
        raise ApiError(0,'Expected an absolute API path')
    headers={"X-Workspace-Chunked":"v1"} if chunked else {}
    token_file=token_file or os.environ.get('RESEARCH_WORKSPACE_TOKEN_FILE')
    if token_file:
        if parsed.scheme!='https' and parsed.hostname not in ('127.0.0.1','localhost','::1'):
            raise ApiError(0,'Authenticated remote requests require HTTPS')
        try:
            with Path(token_file).open('r',encoding='utf-8') as credential:
                token=credential.read(258).strip()
        except (OSError,UnicodeError):
            raise ApiError(0,'Cannot read credential file') from None
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',token):
            raise ApiError(0,'Malformed credential file')
        headers['Authorization']='Bearer '+token
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data,
                                 headers={**headers,**({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            raw=response.read(8*1024*1024+1) if chunked else response.read()
            if chunked and len(raw)>8*1024*1024:raise ApiError(0,'Response exceeds bounded transfer protocol')
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(8*1024*1024+1).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = {}
        message = body.get("error", f"HTTP {exc.code}")
        if exc.code == 409:
            message = f"STALE CONFLICT (409): {message}; reread context and reconcile explicitly"
        raise ApiError(exc.code, message, details=body) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(0, f"Roadmap API unavailable or returned invalid JSON: {exc}") from exc



def request(base_url,path,payload=None,*,token_file=None,chunked=False,timeout=5):
    """Opt-in staged RPC preserves exact payload identities and never rebases conflicts."""
    if not chunked:return _request_wire(base_url,path,payload,token_file=token_file,timeout=timeout)
    import base64
    try:
        from . import workspace_bundle_chunks as codec
    except ImportError:
        import workspace_bundle_chunks as codec
    from urllib.parse import parse_qs
    project=payload.get('project_id') if isinstance(payload,dict) else parse_qs(urlsplit(path).query).get('project_id',[None])[0]
    def wire(target,body=None):
        return _request_wire(base_url,target,body,token_file=token_file,chunked=True,timeout=timeout)
    def release(body):
        try:wire('/v1/transfer-release',body)
        except ApiError:pass  # Expiry cleans lost acknowledgments; domain receipts remain authoritative.
    def decode(result):
        if not isinstance(result,dict) or result.get('format')!='workbench-chunked-response':return result
        if (set(result)!={'format','version','project_id','transfer_id','manifest'} or type(result['version']) is not int or result['version']!=1 or not project or result['project_id']!=project):
            raise codec.ChunkError('Chunked response project or format mismatch')
        if not isinstance(result['transfer_id'],str) or str(uuid.UUID(result['transfer_id']))!=result['transfer_id']:raise codec.ChunkError('Invalid response identity')
        codec.validate_manifest(result['manifest'])
        body={'project_id':project,'transfer_id':result['transfer_id']}
        wire_errors=[]
        def read(digest):
            try:reply=wire('/v1/transfer-download',{**body,'sha256':digest})
            except ApiError as error:
                wire_errors.append(error);raise
            if not isinstance(reply,dict) or set(reply)!={'sha256','data'} or reply['sha256']!=digest or not isinstance(reply['data'],str):
                raise codec.ChunkError('Invalid response chunk envelope')
            return base64.b64decode(reply['data'],validate=True)
        try:decoded=codec.unpack(result['manifest'],read)
        except codec.ChunkError:
            if wire_errors:raise wire_errors[0]
            raise
        release(body)
        return decoded
    try:
        if payload is not None and len(codec.canonical(payload))>codec.DEFAULT_CHUNK_BYTES:
            if not project:raise codec.ChunkError('Large requests require project_id')
            manifest,chunks=codec.pack(payload)
            identity=str(uuid.uuid5(uuid.NAMESPACE_URL,'workbench-upload:'+project+':'+path+':'+manifest['payload_sha256']))
            body={'project_id':project,'transfer_id':identity}
            begun=wire('/v1/transfer-begin',{**body,'target':path,'manifest':manifest})
            received=begun.get('received') if isinstance(begun,dict) else None
            if not isinstance(received,list) or any(digest not in chunks for digest in received):raise codec.ChunkError('Invalid staged resume acknowledgment')
            for digest,raw in chunks.items():
                if digest not in received:wire('/v1/transfer-chunk',{**body,'sha256':digest,'data':base64.b64encode(raw).decode('ascii')})
            result=decode(wire('/v1/transfer-finish',body))
            release(body)
            return result
        return decode(wire(path,payload))
    except (ValueError,TypeError,KeyError) as error:
        raise ApiError(0,'Chunked transfer validation failed: '+str(error)) from None


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
    snap = request(args.url, "/api/tasks", token_file=args.token_file)
    request_id = args.request_id or str(uuid.uuid4())
    payload = {"expected_revision": snap["revision"], "request_id": request_id,
               "actor": args.actor, "operations": [operation]}
    print(f"Request ID: {request_id}", file=sys.stderr)
    result = request(args.url, "/api/transaction", payload, token_file=args.token_file)
    print(f"Updated at revision {result.get('revision', 'unknown')} (request {request_id})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8769", help="workspace API base URL (HTTPS for remote credentials)")
    parser.add_argument("--token-file",type=Path,help="private bearer credential file; also RESEARCH_WORKSPACE_TOKEN_FILE")
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("v1-read", help="read a project-scoped workspace resource")
    read.add_argument("resource")
    read.add_argument("--project")
    read.add_argument("--limit", type=int)
    read.add_argument("--cursor")
    read.add_argument("--repository",help="repository connection UUID for source-update history")
    transaction = sub.add_parser("v1-transaction", help="submit an exact v1 envelope from a UTF-8 JSON file")
    transaction.add_argument("--file", type=Path, required=True)
    exchange_export = sub.add_parser('exchange-export', help='save a read-only Git exchange bundle to a new JSON file')
    exchange_export.add_argument('--project', required=True)
    exchange_export.add_argument('--ancestor', help='previously registered common ancestor snapshot ID')
    exchange_export.add_argument('--output', type=Path, required=True)
    exchange_preview = sub.add_parser('exchange-preview', help='preview a bundle without applying changes')
    exchange_preview.add_argument('--project', required=True)
    exchange_preview.add_argument('--file', type=Path, required=True)
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
    client=lambda path,payload=None: request(args.url,path,payload,token_file=args.token_file)
    try:
        if args.command == 'exchange-export':
            params = {'project_id': args.project}
            if args.ancestor is not None:
                params['ancestor_id'] = args.ancestor
            result = client( '/v1/exchange-export?' + urlencode(params))
            # Serialize before opening so a bad response cannot leave a partial file.
            body = json.dumps(result['bundle'], ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
            with args.output.open('x', encoding='utf-8', newline='\n') as output:
                output.write(body)
            print(f'Exported bundle to {args.output}')
            return 0
        if args.command == 'exchange-preview':
            bundle = json.loads(args.file.read_text(encoding='utf-8-sig'))
            if not isinstance(bundle, dict):
                raise ValueError('Exchange file must contain a bundle object')
            result = client( '/v1/exchange-preview', {'project_id': args.project, 'bundle': bundle})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "process-jobs":
            caps = client( '/v1/projects/'+args.project+'/capabilities')
            request_id = args.request_id or str(uuid.uuid4())
            print(f'Request ID: {request_id}',file=sys.stderr)
            result = client('/v1/transactions',{'project_id':args.project,
                'actor':{'principal_id':caps['capabilities']['local_principal_id']},
                'request_id':request_id,'expected_revision':caps['revision'],
                'operations':[{'op':'process_jobs','limit':args.limit}]})
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        if args.command == "import-variables":
            document = json.loads(args.file.read_text(encoding="utf-8-sig"))
            source_hash = hashlib.sha256(json.dumps(document,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
            preview = client( "/v1/variable-import-preview", {"project_id":args.project,"document":document,"source_hash":source_hash})
            if args.dry_run:
                print(json.dumps(preview,ensure_ascii=False,indent=2))
                return 0
            if args.expected_source_sha256 != source_hash or not args.principal:
                raise ValueError("Apply requires --principal and --expected-source-sha256 from the dry run")
            request_id = args.request_id or str(uuid.uuid4())
            print(f"Request ID: {request_id}",file=sys.stderr)
            result = client( "/v1/transactions", {"project_id":args.project,
                "actor":{"principal_id":args.principal},"request_id":request_id,"expected_revision":preview['revision'],
                "operations":[{"op":"import_variable_registry","document":document,"source_hash":source_hash}]})
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        if args.command == "v1-read":
            if not args.resource or any(c not in 'abcdefghijklmnopqrstuvwxyz-' for c in args.resource):
                raise ValueError("Expected a workspace resource name")
            params = {k: v for k, v in {"project_id":args.project,"limit":args.limit,"cursor":args.cursor,"repository_id":args.repository}.items() if v is not None}
            result = client( "/v1/" + args.resource + ('?' + urlencode(params) if params else ''))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "v1-transaction":
            payload = json.loads(args.file.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("Transaction file must contain a v1 envelope object")
            result = client( "/v1/transactions", payload)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "context":
            print(format_context(client( "/api/tasks"), args.task_id))
            return 0
        if args.command == "list":
            print(format_task_list(client( "/api/tasks"), args.status))
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
