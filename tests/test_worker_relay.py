"""Exercise two independent worker processes against a disposable HTTP workspace."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"tools")]
import workspace
from tools import roadmap_server as server
from tools.roadmap_store import RoadmapStore


class WorkerRelay(unittest.TestCase):
    def test_relay(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.addCleanup(setattr,server,"STORE_PATH",server.STORE_PATH)
            server.STORE_PATH=root/"store.sqlite3"
            store=RoadmapStore(server.STORE_PATH)
            project=store.read_v1("projects")["items"][0]["id"]
            repo=root/"example"; repo.mkdir()
            folder=repo/".research-workspace";folder.mkdir()
            (folder/"project.json").write_text(json.dumps({"protocol":workspace.PROTOCOL,"project_id":project,"name":"Example"}))
            (repo/"AGENTS.md").write_text("Use the recorded build for both comparisons.")
            http=ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
            http.shared_mode=False
            threading.Thread(target=http.serve_forever,daemon=True).start()
            self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
            url="http://127.0.0.1:"+str(http.server_port)
            def write(*ops):return workspace.transaction(url,project,list(ops))
            def worker(*args):
                r=subprocess.run([sys.executable,str(ROOT/"tools/workbench_worker.py"),"--url",url,*args],cwd=repo,capture_output=True,text=True)
                self.assertEqual(r.returncode,0,r.stderr)
                return json.loads(r.stdout)
            def context(key):return store.read_v1("context",project,key=key)["context"]
            write(dict(op="create",alias="motion",record={"title":"Motion","status":"ready","acceptance":["Compare stationary endpoints"]}))
            task=context("motion")["task"]["id"]
            first=worker("claim",task,"first")
            self.assertEqual(first["claim"]["by"], "first")
            self.assertEqual(first["status"], "ready")
            self.assertEqual(first["reserved_paths"], [])
            self.assertIn("recorded build",first["rules"][0]["body"])
            follow={"alias":"contact-motion","title":"Natural contact","next_step":"Capture the first natural contact frame","acceptance":"Compare velocity on the first contact frame"}
            (repo/"follow.json").write_text(json.dumps(follow))
            (repo/"trace.txt").write_text("stationary speed 0; contact speed 4")
            before_invalid = context(task)
            invalid = subprocess.run([sys.executable,str(ROOT/'tools/workbench_worker.py'),'--repo',str(repo),
                '--url',url,'finish',task,'first','Measured result','--boundary','x'*201,
                '--outcome','passed','--evidence','trace.txt'],capture_output=True,text=True)
            self.assertNotEqual(invalid.returncode,0)
            self.assertIn('Invalid attempt boundary',invalid.stderr)
            self.assertEqual(context(task),before_invalid)
            end=worker("finish",task,"first","Stationary speed was zero","--boundary","stationary","--outcome","passed","--evidence","trace.txt","--follow","follow.json")
            child=end["child"]
            second=worker("claim",child,"second")
            self.assertEqual(second["established"],"Stationary speed was zero")
            self.assertEqual(second["next_comparison"],follow["next_step"])
            worker("finish",child,"second","Contact speed was four","--boundary","natural contact","--outcome","passed","--evidence","trace.txt")
            write(dict(op="register_root",id="r",path=str(repo)))
            write(dict(op="register_artifact",id="a",root_id="r",relative_path="trace.txt",kind="trace"))
            version=write(dict(op="register_artifact_version",artifact_id="a",binary_build_id="test-build"))["changed_records"][0]["id"]
            write(dict(op="create",alias="old-task",record={"title":"Previously finished comparison","status":"review","acceptance":["Known baseline"],"evidence":["trace.txt"]}))
            task=context("old-task")["task"]["id"]
            from tools.roadmap_schema import principal_for_label
            with store._connection() as db:
                reviewer=principal_for_label(db,project,"fixture independent reviewer")
                db.execute("UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?",
                           (json.dumps(["contribute","review"]),project,reviewer))
            for op in [dict(op="record_validation",outcome="passed",inputs={"mode":"baseline"},outputs={"value":0},
                        evidence=[dict(artifact_version_id=version,method="compare",limitations="fixture")]),dict(op="complete_task")]:
                op.update(task_id=task,expected_record_revision=context(task)["task"]["record_revision"],rationale="Independent fixture review")
                store.transact_v1(dict(project_id=project,actor={"principal_id":reviewer},expected_revision=store.snapshot()["revision"],request_id=str(uuid.uuid4()),operations=[op]))
            worker("watch",task,"--variable","speed","--owner","Platform","--build","test-build","--question","Which contact uses speed?")
            def discover(owner):
                worker("discover",child,"--variable","speed","--owner",owner,"--version",version,"--locator","contact frame","--claim","Read speed","--method","fixture trace","--limits","one frame")
            discover("Camera")
            self.assertEqual(context(task)["progress"]["new_hint_count"],0)
            discover("Platform")
            discover("Platform")
            write(dict(op="process_jobs",limit=100))
            self.assertEqual(context(task)["progress"]["new_hint_count"],1)
            self.assertEqual(context(task)["task"]["status"],"done")
            live=workspace.request(url,"/v1/tasks/"+task+"/context?project_id="+project)["context"]
            self.assertEqual(live["variable_leads"][0]["source_task"],child)
            self.assertIn("contact frame",live["variable_leads"][0]["locator_json"])
            # An alias collision must leave the entire measured handoff recoverable.
            worker("claim",child,"third")
            collision=subprocess.run([sys.executable,str(ROOT/"tools/workbench_worker.py"),"--url",url,
                "finish",child,"third","New measured result","--boundary","repeat","--outcome","passed",
                "--evidence","trace.txt","--follow","follow.json"],cwd=repo,capture_output=True,text=True)
            self.assertNotEqual(collision.returncode,0)
            failed=context(child)
            self.assertEqual(failed["fields"]["claim"]["by"],"third")
            self.assertTrue(any("New measured result" in e["details"].get("text","") for e in failed["history"]))
            self.assertFalse(any(a["boundary_id"]=="repeat" for a in failed["structured_attempts"]))
            resumed=worker("resume",child,"third","--child-title","Repeat on new trace","--next-step","Compare frame 8","--pass-condition","Match its speed")
            self.assertIsNone(resumed["claim"])
            self.assertEqual(context(resumed["child"])["task"]["parent_id"],child)
            self.assertEqual(sum(a["boundary_id"]=="repeat" for a in context(child)["structured_attempts"]),1)
