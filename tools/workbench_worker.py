"""Worker entry point for connected project checkouts. All writes use the Workbench API."""
import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workspace


def brief(repo, ctx):
    fields, task = ctx["fields"], ctx["task"]
    rules = list(ctx.get("instructions", []))
    for name in ("AGENTS.md", ".research-workspace/AGENTS.md"):
        path = (repo / name).resolve()
        if path.is_relative_to(repo) and path.is_file():
            rules.append({"source_path": name, "body": path.read_text(encoding="utf-8-sig")})
    history = [e.get("details", {}).get("text") for e in ctx.get("history", [])]
    last_handoff=next((text for text in history if text and text.startswith(("Pending worker handoff ","Verified handoff "))),None)
    return {"handoff":last_handoff,"review_leads":ctx.get("variable_leads",[]),"task": task["id"], "title": task["title"], "status": task["status"],
            "work_type": "historical report" if task["alias"].startswith("report-") else
                         "bounded comparison" if ctx.get("acceptance") else "needs a pass condition",
            "open_question": fields.get("discovery_wait") or fields.get("summary"),
            "established": fields.get("prior_work"), "latest_notes": [t for t in history if t][:5],
            "attempts": ctx.get("structured_attempts", []), "next_comparison": fields.get("next_step"),
            "evidence": fields.get("evidence", []), "pass_condition": ctx.get("acceptance"),
            "rules": rules}


def run(args):
    repo = args.repo.resolve()
    project = workspace.load_manifest(repo)["project_id"]
    url = workspace.local_url(args.url)
    def get(key):
        return workspace.request(url, "/v1/tasks/"+quote(key,safe="")+"/context?project_id="+project)["context"]
    def write(*ops):
        return workspace.transaction(url, project, list(ops))
    ctx = get(args.task)
    uid = ctx["task"]["id"]
    if args.action == "brief":
        return brief(repo,ctx)
    if args.action == "claim":
        info = brief(repo,ctx)
        if info["work_type"] != "bounded comparison" and not args.research:
            raise ValueError(info["work_type"] + ". Inspect the brief or select --research for deliberate investigation.")
        write(dict(op="claim",task_id=uid,claimant=args.worker,reserved_paths=args.paths))
        return brief(repo,get(uid))
    if args.action == "watch":
        group={"identifier":args.variable,"scope_key":args.owner,"namespace":args.namespace}
        if args.build: group["binary_build_id"]=args.build
        q=str(uuid.uuid4())
        return write(dict(op="create_question",id=q,text=args.question,owner_task_id=uid,criteria={"any_of":[group]}),
                     dict(op="subscribe",target_type="question",target_id=q,selector={}))
    if args.action == "discover":
        scope={"task_id":uid,"identifier":args.variable,"namespace":args.namespace,
               "scope_key":args.owner,"artifact_version_id":args.version}
        note={"variables":[args.variable],"claim":args.claim,"source":args.version,"locator":args.locator}
        signature=json.dumps([project,scope,args.claim,args.locator,args.method,args.limits],sort_keys=True)
        finding_id=str(uuid.uuid5(uuid.NAMESPACE_URL,signature))
        note["finding_id"]=finding_id
        marker="Audit index v1\n"+json.dumps(note)
        if any(e.get("details",{}).get("text")==marker for e in ctx.get("history",[])):
            return {"finding_id":finding_id,"already_recorded":True}
        return write(dict(op="create_finding",id=finding_id,claim=args.claim,scope=scope,classification="source observation",
                          evidence=[dict(artifact_version_id=args.version,locator=args.locator,method=args.method,limitations=args.limits)]),
                     dict(op="journal",task_id=uid,kind="observation",text=marker),
                     dict(op="process_jobs",limit=100))
    if (ctx["fields"].get("claim") or {}).get("by")!=args.worker:
        raise ValueError("Finish requires the worker's current claim.")
    previous=None
    if args.action=="resume":
        latest=next((e.get("details",{}).get("text","") for e in ctx.get("history",[])
                     if e.get("details",{}).get("text","").startswith(("Verified handoff ","Pending worker handoff "))),"")
        if not latest.startswith("Pending worker handoff "):
            raise ValueError("No pending handoff on this task")
        previous=json.loads(latest.split("\n",1)[1])
        args.text,args.boundary,args.outcome,args.evidence=(previous[k] for k in ("result","boundary","outcome","evidence"))
    # Validate deterministic local fields before persisting a pending intent.
    # A malformed boundary cannot be recovered by replaying the same handoff.
    from roadmap_research import text as validate_text
    validate_text(args.boundary, 'attempt boundary', 200)
    follow=json.loads(args.follow.read_text(encoding="utf-8-sig")) if args.follow else (previous or {}).get("follow_up")
    if args.child_title:
        follow={"title":args.child_title,"alias":args.child_alias or ("behavior-"+uuid.uuid4().hex),"next_step":args.next_step,"acceptance":args.pass_condition}
    if follow and not all(isinstance(follow.get(k),str) and follow[k].strip() for k in ("next_step","acceptance")):
        raise ValueError("Follow-up requires next_step and acceptance.")
    alias=None
    ops=[]
    if follow:
        alias=follow.get("existing") or follow["alias"]
        if follow.get("existing"):
            child=get(alias)["task"]
            if child["id"]==uid:raise ValueError("Choose a distinct follow-up child")
            alias=child["id"]
            if uid not in {child.get("parent_id"),*(e["parent_id"] for e in child.get("contains",[]))}:
                ops.append(dict(op="add_containment",task_id=alias,parent_id=uid,required_for_parent=False))
        else:
            ops.append(dict(op="create",alias=alias,parent_id=uid,record=dict(title=follow["title"],status="ready",
                        summary=follow["next_step"],prior_work=args.text,next_step=follow["next_step"],
                        acceptance=[follow["acceptance"]],evidence=args.evidence)))
    intent=dict(result=args.text,boundary=args.boundary,outcome=args.outcome,evidence=args.evidence,follow_up=follow)
    token=hashlib.sha256(json.dumps([uid,args.worker,intent],sort_keys=True).encode()).hexdigest()[:24]
    pending="Pending worker handoff "+token+"\n"+json.dumps(intent)
    committed="Measured worker result "+token+"\n"+args.text
    def event_for(context, text):
        return next((event for event in context["history"] if event.get("details",{}).get("text")==text), None)
    def guarded_write(ops):
        caps=workspace.request(url,"/v1/projects/"+project+"/capabilities")
        snapshot=workspace.request(url,"/v1/tasks/"+quote(uid,safe="")+"/context?project_id="+project)
        if (snapshot["context"]["fields"].get("claim") or {}).get("by")!=args.worker:
            raise ValueError("Task claim changed during handoff")
        return workspace.request(url,"/v1/transactions",{"project_id":project,"actor":{"principal_id":caps["capabilities"]["local_principal_id"]},
            "expected_revision":snapshot["revision"],"request_id":str(uuid.uuid4()),"operations":ops})
    if not event_for(ctx,pending):
        guarded_write([dict(op="journal",task_id=uid,kind="observation",text=pending)])
    already=event_for(ctx,committed)
    if not already:
        if follow and follow.get("existing"):
            ops.append(dict(op="journal",task_id=alias,kind="observation",text="Follow-up from "+uid+"\n"+json.dumps(intent),evidence=args.evidence))
        ops.extend([dict(op="record_attempt",task_id=uid,expected_record_revision=get(uid)["task"]["record_revision"],
                         rationale=args.text,boundary_id=args.boundary,outcome=args.outcome,evidence=args.evidence),
                    dict(op="journal",task_id=uid,kind="observation",text=committed,evidence=args.evidence)])
        if ctx["task"]["status"] in ("ready","active"):
            ops.append(dict(op="update",task_id=uid,fields={"status":"review"}))
        receipt=guarded_write(ops)
    else:
        receipt={"request_id":already["request_id"]}
    latest=get(uid)
    if not event_for(latest,committed) or not any(a["boundary_id"]==args.boundary and a["outcome"]==args.outcome for a in latest["structured_attempts"]):
        raise RuntimeError("Attempt readback failed; handoff remains pending.")
    child_id=None
    if alias:
        child=get(alias)["task"]
        if uid not in {child.get("parent_id"),*(e["parent_id"] for e in child.get("contains",[]))}:
            raise RuntimeError("Follow-up readback failed; handoff remains pending.")
        child_id=child["id"]
    guarded_write([dict(op="journal",task_id=uid,kind="observation",text="Verified handoff "+token+" "+json.dumps({"result_request":receipt["request_id"],"child":child_id})),
                   dict(op="release",task_id=uid,claimant=args.worker)])
    return {"result_request":receipt["request_id"],"child":child_id,"claim":get(uid)["fields"].get("claim")}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo",type=Path,default=Path.cwd())
    parser.add_argument("--url",default=workspace.DEFAULT_URL)
    sub=parser.add_subparsers(dest="action",required=True)
    for action in ("brief","claim","finish","resume","watch","discover"):
        cmd=sub.add_parser(action)
        cmd.add_argument("task")
        if action in ("claim","finish","resume"):cmd.add_argument("worker")
        if action=="claim":
            cmd.add_argument("paths",nargs="*")
            cmd.add_argument("--research",action="store_true")
        if action=="finish":
            cmd.add_argument("text")
            cmd.add_argument("--boundary",required=True)
            cmd.add_argument("--outcome",required=True,choices=["passed","failed","blocked","inconclusive"])
            cmd.add_argument("--evidence",nargs="+",required=True)
        if action in ("finish","resume"):
            choice=cmd.add_mutually_exclusive_group()
            choice.add_argument("--follow",type=Path)
            choice.add_argument("--child-title")
            cmd.add_argument("--child-alias")
            cmd.add_argument("--next-step")
            cmd.add_argument("--pass-condition")
        if action in ("watch","discover"):
            for field in ("variable","owner"):cmd.add_argument("--"+field,required=True)
            cmd.add_argument("--namespace",default="variable")
        if action=="watch":
            cmd.add_argument("--build")
            cmd.add_argument("--question",required=True)
        if action=="discover":
            for field in ("version","locator","method","limits","claim"):cmd.add_argument("--"+field,required=True)
    print(json.dumps(run(parser.parse_args()),indent=2,ensure_ascii=False))


if __name__=="__main__":
    main()
