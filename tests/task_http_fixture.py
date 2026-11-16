"""Short-lived real shared HTTP fixture for task workflow browser integration."""
import json, sys, tempfile, threading
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from tools.roadmap_store import RoadmapStore
from tools import roadmap_server as server
from tools import workspace_auth as auth

tmp = tempfile.TemporaryDirectory(prefix='task-http-')
server.STORE_PATH = Path(tmp.name) / 'workspace.sqlite3'
store = RoadmapStore(server.STORE_PATH)
with store._connection() as db:
    project = db.execute('SELECT id FROM projects ORDER BY id LIMIT 1').fetchone()[0]
    owner = db.execute('SELECT principal_id FROM project_members WHERE project_id=? LIMIT 1', (project,)).fetchone()[0]
    tokens = {}
    for name in ('contributor', 'reviewer', 'alternate'):
        caps = ['contribute','review','manage_claims'] if name == 'reviewer' else ['contribute']
        invite = auth.issue_invite(db, project, owner, 'member', caps)
        tokens[name] = auth.redeem_invite(db, invite['secret'])['token']
        # Token identity is fixed by invitation role; use explicit test principals for API actor.
    db.commit()

def write(op, principal=owner):
    with store._connection() as db:
        revision = store._revision(store._meta(db))
    return store.transact_v1({'project_id': project, 'actor': {'principal_id': principal},
        'expected_revision': revision, 'request_id': __import__('uuid').uuid4().hex,
        'operations': [op]})

parent = write({'op':'create','alias':'http-parent','record':{'title':'HTTP parent','status':'active','acceptance':['Parent gate']}})['changed_records'][0]['id']
child = write({'op':'create','alias':'http-child','parent_id':parent,'record':{'title':'HTTP child','status':'active','acceptance':['Child gate']}})['changed_records'][0]['id']
claim_task = write({'op':'create','alias':'http-claim','record':{'title':'HTTP claim fixture','status':'active','acceptance':['Claim gate']}})['changed_records'][0]['id']
overlap_task = write({'op':'create','alias':'http-overlap','record':{'title':'HTTP overlap fixture','status':'active','acceptance':['Overlap gate']}})['changed_records'][0]['id']
source = Path(tmp.name) / 'evidence.bin'; source.write_bytes(b'synthetic workflow integration evidence')
write({'op':'register_root','id':'http-root','path':tmp.name})
write({'op':'register_artifact','id':'http-evidence','root_id':'http-root','relative_path':'evidence.bin','kind':'binary'})
evidence = write({'op':'register_artifact_version','artifact_id':'http-evidence','binary_build_id':'fixture-build'})['changed_records'][0]['id']
with store._connection() as db:
    # Bind the bearer principals represented in the tokens to the fixture labels.
    token_principals = {name: auth.authenticate(db, token)['principal_id'] for name, token in tokens.items()}
    for name, principal in token_principals.items():
        if name == 'reviewer':
            db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?', (json.dumps(['contribute','review','manage_claims']),project,principal))
    db.commit()

http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
http.shared_mode = True
thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
print(json.dumps({'base':f'http://127.0.0.1:{http.server_port}','project':project,'parent':parent,'child':child,
    'claim_task':claim_task,'overlap_task':overlap_task,'evidence':evidence,'tokens':tokens,'principals':token_principals}), flush=True)
try:
    sys.stdin.readline()
finally:
    http.shutdown(); thread.join(); http.server_close(); tmp.cleanup()
