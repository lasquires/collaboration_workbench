"""Independent local Research Workspace launcher and project setup CLI."""
from pathlib import Path
import argparse
import json
import os
import re
import sys
import uuid
from urllib.parse import urlsplit,urlencode

APP_ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(APP_ROOT/'tools'))
from roadmap_store import RoadmapStore,StoreError
from roadmap_cli import request,ApiError
from roadmap_git import inspect_repository,GitAdapterError
from workspace_project_contract import validate_manifest,resolve_project_remote,build_contract
from roadmap_schema import SCHEMA_VERSION
from roadmap_exchange import TABLES

PROTOCOL='research-workspace/v1'
DEFAULT_URL='http://127.0.0.1:8771'
MANIFEST=Path('.research-workspace/project.json')


def default_home():
    if os.environ.get('RESEARCH_WORKSPACE_HOME'):
        return Path(os.environ['RESEARCH_WORKSPACE_HOME']).expanduser().resolve()
    if os.environ.get('LOCALAPPDATA'):
        return Path(os.environ['LOCALAPPDATA'])/'ResearchWorkspace'
    return Path.home()/'.local/share/research-workspace'


def initialize_home(home):
    home=Path(home).expanduser().resolve();home.mkdir(parents=True,exist_ok=True)
    path=home/'workspace.sqlite3'
    if not path.exists(): RoadmapStore(path,bootstrap_slug='workspace')
    return path


def bootstrap_host(home):
    """Issue one owner credential offline under the service's exclusive lock."""
    import roadmap_server
    import workspace_auth
    home=Path(home).expanduser().resolve();home.mkdir(parents=True,exist_ok=True)
    path=home/'workspace.sqlite3';credential=home/'owner.token'
    with roadmap_server.service_lock(path):
        if credential.exists():raise FileExistsError('Owner credential already exists; it was not overwritten')
        if path.exists():roadmap_server.checkpoint_before_upgrade(path)
        store=RoadmapStore(path,bootstrap_slug='workspace')
        project=store.read_v1('projects')['items'][0]['id']
        principal=store.read_v1('capabilities',project)['capabilities']['local_principal_id']
        created=False
        try:
            with store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                # Explicit host ownership includes administration of connected projects.
                for row in db.execute('SELECT id FROM projects WHERE archived_at IS NULL').fetchall():
                    member=db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(row[0],principal)).fetchone()
                    caps=set(json.loads(member[0])) if member else set()
                    caps.update(('contribute','review','manage_claims','manage_projects'))
                    db.execute('INSERT INTO project_members VALUES(?,?,?) ON CONFLICT(project_id,principal_id) DO UPDATE SET capabilities_json=excluded.capabilities_json',
                               (row[0],principal,json.dumps(sorted(caps))))
                grant=workspace_auth.issue_owner_token(db,principal)
                fd=os.open(credential,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);created=True
                with os.fdopen(fd,'w',encoding='utf-8') as output:
                    output.write(grant['token']+'\n');output.flush();os.fsync(output.fileno())
                db.commit()
        except Exception:
            if created:credential.unlink(missing_ok=True)
            raise
    return {'credential_file':str(credential),'credential_id':grant['id'],'expires_at':grant['expires_at'],
            'next':'Keep this file private. Shared hosting requires TLS and explicit serve flags.'}


def _offline_owner(home,operation):
    import roadmap_server
    import workspace_auth
    home=Path(home).expanduser().resolve();path=home/'workspace.sqlite3'
    if not path.is_file():raise ValueError('Initialize the host before managing credentials')
    with roadmap_server.service_lock(path):
        roadmap_server.checkpoint_before_upgrade(path)
        store=RoadmapStore(path)
        with (home/'owner.token').open('r',encoding='utf-8') as source:secret=source.read(258).strip()
        with store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            owner=workspace_auth.authenticate(db,secret)
            if not owner['is_owner']:raise ValueError('Host owner credential required')
            result=operation(db,owner['principal_id'],home)
            db.commit();return result


def provision_member(home,project,label,capabilities=None):
    """Create a scoped collaborator credential while the host is stopped."""
    import workspace_auth
    created=[]
    def provision(db,owner,root):
        invitation=workspace_auth.issue_invite(db,project,owner,label,capabilities)
        grant=workspace_auth.redeem_invite(db,invitation['secret'])
        directory=root/'credentials'
        if not directory.resolve().is_relative_to(root):raise ValueError('Credentials must remain in the private host directory')
        directory.mkdir(mode=0o700,exist_ok=True)
        output_path=directory/(grant['id']+'.token')
        fd=os.open(output_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);created.append(output_path)
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            output.write(grant['token']+'\n');output.flush();os.fsync(output.fileno())
        return {'credential_file':str(output_path),'credential_id':grant['id'],'principal_id':grant['principal_id'],
                'project_id':project,'expires_at':grant['expires_at'],'capabilities':capabilities or ['contribute']}
    try:return _offline_owner(home,provision)
    except Exception:
        for path in created:path.unlink(missing_ok=True)
        raise


def revoke_credential(home,identity):
    import workspace_auth
    def revoke(db,owner,root):
        workspace_auth.revoke_token(db,identity,owner)
        return {'credential_id':identity,'revoked':True}
    return _offline_owner(home,revoke)


def local_url(value):
    parsed=urlsplit(value)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Service URL must be a loopback HTTP address without credentials or a path')
    return value.rstrip('/')


def load_manifest(repo):
    path=Path(repo)/MANIFEST
    if not path.resolve().is_relative_to(Path(repo).resolve()): raise ValueError('Project manifest must remain inside the repository')
    raw=path.read_bytes()
    if len(raw)>65536: raise ValueError('Project manifest exceeds 64 KB')
    document=json.loads(raw.decode('utf-8-sig'))
    return validate_manifest(document)


def transaction(url,project,operations,*,request_fn=None):
    client=request_fn or request
    caps=client(url,'/v1/projects/'+project+'/capabilities')
    return client(url,'/v1/transactions',{'project_id':project,
        'actor':{'principal_id':caps['capabilities']['local_principal_id']},
        'request_id':str(uuid.uuid4()),'expected_revision':caps['revision'],'operations':operations})


def setup_project(directory,url=DEFAULT_URL,*,apply=False,name=None,proposed_manifest=None,request_fn=None):
    client=request_fn or request
    url=local_url(url);metadata=inspect_repository(directory);repo=Path(metadata.root)
    if Path(directory).resolve()!=repo: raise ValueError('Choose the repository root, not a nested directory')
    manifest_path=repo/MANIFEST
    existing_manifest=load_manifest(repo) if manifest_path.exists() else None
    if proposed_manifest is not None:
        validate_manifest(proposed_manifest)
        if existing_manifest is not None and proposed_manifest!=existing_manifest:
            raise ApiError(409,'Project manifest changed since preview; reread and reconcile')
    manifest=existing_manifest or proposed_manifest or {'protocol':PROTOCOL,'project_id':str(uuid.uuid4()),'name':name or repo.name,'synchronization':resolve_project_remote(repo)}
    manifest=validate_manifest(manifest)
    if 'synchronization' in manifest:
        configured=manifest['synchronization']
        actual=resolve_project_remote(repo,configured['remote_name'])
        if configured['repository']!=actual['repository']:
            raise ApiError(409,'Project remote changed since setup; reread and reconcile')
    instruction_path=repo/'.research-workspace/AGENTS.md'
    if not instruction_path.resolve().is_relative_to(repo) or not manifest_path.resolve().is_relative_to(repo):
        raise ValueError('Integration files must remain inside the repository')
    instructions="""# Research Workspace project integration

This project connects to the independently installed Research Workspace app.
Use `research-workspace doctor --repo .` to verify the local service and project
identity. If the command is not installed, use `python <app-install>/workspace.py`
with the same arguments. Ask the local operator for the app installation path;
do not copy application source or create a second SQLite writer in this repo.

The portable project identity is in `.research-workspace/project.json`. Keep it
when cloning or moving the project. Local clone paths, databases and leases are
not portable project files.

When evidence reveals unfinished behavior that can be investigated and tested independently, add it to the project map before handing off. Search existing tasks by behavior, aliases and relevant variables. Reuse the matching UUID and link it under each relevant parent. If no match exists, create a child under the most specific parent. Record the source evidence, open question and observable acceptance test in its Workbench record. Read back the parent link and journal the finding through the scoped API. A worker with write access owns this step. A read-only worker names the intended child and parent in its handoff so the coordinator can persist and verify them.

Use the platform's scoped CLI/API for tasks, claims, research and journals.
Refresh indexed sources after Git changes. Shared research changes travel through
Git text exports; review conflicts explicitly. A hint or workflow status does not
verify a forensic claim. Never edit the app database directly or merge SQLite
files. See the platform's project-agent setup guide for the supported commands.
"""
    contract=build_contract(SCHEMA_VERSION,
        {'module':'tools/roadmap_schema.py','entry_point':'RoadmapStore (service-owned migrations)'},
        {'format':'workbench-exchange','version':2,'manual_legacy_format':'ssg2-exchange/v1','tables':list(TABLES),
         'schema_policy':'Each bundle carries exact column definitions; the service validates project, schema, content hashes and revisions before applying.'})
    files={'.research-workspace/schema.json':json.dumps(contract,ensure_ascii=False,sort_keys=True,indent=2)+'\n',
           MANIFEST.as_posix():json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n',
           '.research-workspace/AGENTS.md':instructions}
    if not apply: return {'project_id':manifest['project_id'],'repository':str(repo),'apply':False,'files':files}
    # Existing integration prose belongs to the project. Never overwrite it.
    if instruction_path.exists() and instruction_path.read_text(encoding='utf-8')!=instructions:
        raise ValueError('Existing integration instructions differ; reconcile the preview before setup')
    contract_path=repo/'.research-workspace/schema.json'
    if not contract_path.resolve().is_relative_to(repo):
        raise ValueError('Project schema must remain inside the repository')
    if contract_path.exists() and contract_path.read_text(encoding='utf-8')!=files['.research-workspace/schema.json']:
        raise ValueError('Existing project schema differs; reconcile the preview before setup')
    projects=client(url,'/v1/projects')['items']
    if not projects: raise ValueError('Service has no administration workspace')
    admin=next((p for p in projects if p['slug']=='workspace'),projects[0])
    existing=next((p for p in projects if p['id']==manifest['project_id']),None)
    if existing is None:
        slug=re.sub('[^a-z0-9.-]+','-',manifest['name'].lower()).strip('-.')[:60] or 'project'
        transaction(url,admin['id'],[{'op':'create_project','id':manifest['project_id'],
            'slug':slug+'-'+manifest['project_id'][:8],'goal':'Research for '+manifest['name'],'repository_root':str(repo)}],request_fn=client)
    # Register through the service before writing portable setup files, so a
    # rejected connection cannot silently change this project's instructions.
    connections=client(url,'/v1/repositories?'+urlencode({'project_id':manifest['project_id']}))['items']
    if not any(Path(connection['root']).resolve()==repo for connection in connections):
        transaction(url,manifest['project_id'],[{'op':'register_repository','root':str(repo)}],request_fn=client)
    manifest_path.parent.mkdir(parents=True,exist_ok=True)
    if not manifest_path.exists():
        with manifest_path.open('x',encoding='utf-8',newline='\n') as output: output.write(files[MANIFEST.as_posix()])
    if not contract_path.exists():
        with contract_path.open('x',encoding='utf-8',newline='\n') as output: output.write(files['.research-workspace/schema.json'])
    if not instruction_path.exists():
        with instruction_path.open('x',encoding='utf-8',newline='\n') as output: output.write(instructions)
    return {'project_id':manifest['project_id'],'repository':str(repo),'apply':True,
            'agent_instructions':str(instruction_path),'next':'Add a reference to .research-workspace/AGENTS.md in your project agent instructions.'}


def diagnose(directory,url=DEFAULT_URL):
    url=local_url(url);metadata=inspect_repository(directory);repo=Path(metadata.root);manifest=load_manifest(repo)
    projects=request(url,'/v1/projects')
    project=next((p for p in projects['items'] if p['id']==manifest['project_id']),None)
    connections=request(url,'/v1/repositories?'+urlencode({'project_id':manifest['project_id']})) if project else {'items':[]}
    connection=next((c for c in connections['items'] if Path(c['root']).resolve()==repo),None)
    sync=None;source_bytes=None
    if connection:
        sync=request(url,'/v1/git-sync-status?'+urlencode({'project_id':manifest['project_id'],'repository_id':connection['id']}))
        # Keep doctor bounded while checking byte availability across the
        # project's pinned versions. The API exposes no local cache paths.
        cursor=None;checked=available=0;complete=True
        for _ in range(100):
            query={'project_id':manifest['project_id'],'limit':'100'}
            if cursor:query['cursor']=cursor
            page=request(url,'/v1/artifact-versions?'+urlencode(query))
            for version in page.get('items',[]):
                checked+=1
                if version.get('local_bytes_available') is True:available+=1
            cursor=page.get('next_cursor')
            if not cursor:break
        else:complete=False
        source_bytes={'versions_checked':checked,'local_bytes_available':available,
                      'bytes_missing':checked-available,'complete':complete}
    sync_state=sync.get('state','off') if sync else 'not connected'
    actions={
        'off':'Git sharing is off; enable it explicitly and select portable data in Repositories to synchronize.',
        'pending':'Git sharing has pending changes; wait for the quiet period or choose Sync now.',
        'queued':'A Git synchronization is queued; check Repositories for its result.',
        'syncing':'Git synchronization is running; check Repositories again shortly.',
        'current':'Shared research is synchronized; check source/evidence state separately.',
        'failed':'Git synchronization failed; review the safe error below, then retry after correcting the connection or repository.',
        'review-required':'Git changes need explicit conflict review in Repositories before synchronization can continue.',
        'sharing-review-required':'Journal sharing needs review before Git export can continue.',
        'live-standby':'Live collaboration is active; Git synchronization is on standby.',
    }
    next_action=('Run setup --apply against this local service' if not connection else
                 actions.get(sync_state,'Check Repositories for the current Git synchronization state.'))
    return {'connected':connection is not None,'protocol':projects['api_version'],'project_id':manifest['project_id'],
            'repository':str(repo),'head_commit':metadata.head_commit,'branch':metadata.branch,'dirty':metadata.dirty,
            'service':url,'service_revision':projects['revision'],
            'sync_state':sync_state,
            'git_sync':({key:sync.get(key) for key in ('enabled','export_selected','requested','state','git_state',
                'error','retry_after','interval_seconds','quiet_seconds','last_attempt') if key in sync} if sync else None),
            'source_state':connection['source_state'] if connection else 'not connected',
            'source_revision':connection['source_revision'] if connection else None,
            'evidence_state':connection.get('evidence_state') if connection else 'not connected',
            'evidence_head':connection.get('evidence_head') if connection else None,
            'evidence_manifest_id':connection.get('evidence_manifest_id') if connection else None,
            'source_bytes':source_bytes,
            'next_action':next_action}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    serve=commands.add_parser('serve',help='start the independent local app')
    serve.add_argument('--home',type=Path,default=default_home());serve.add_argument('--port',type=int,default=8771)
    serve.add_argument('--host',default='127.0.0.1')
    serve.add_argument('--public-host')
    serve.add_argument('--shared-mode',action='store_true')
    serve.add_argument('--tls-cert',type=Path);serve.add_argument('--tls-key',type=Path)
    host_init=commands.add_parser('host-init',help='create a private owner credential while the service is stopped')
    host_init.add_argument('--home',type=Path,default=default_home())
    member=commands.add_parser('host-add-member',help='create a project-scoped collaborator credential while the host is stopped')
    member.add_argument('--home',type=Path,default=default_home());member.add_argument('--project',required=True)
    member.add_argument('--label',required=True);member.add_argument('--capability',action='append',choices=('contribute','review','manage_claims','manage_projects'))
    revoke=commands.add_parser('host-revoke',help='revoke a credential while the host is stopped')
    revoke.add_argument('--home',type=Path,default=default_home());revoke.add_argument('--credential-id',required=True)
    for command in ('setup','doctor'):
        part=commands.add_parser(command);part.add_argument('--repo',type=Path,required=True);part.add_argument('--url',default=DEFAULT_URL)
        if command=='setup':part.add_argument('--apply',action='store_true');part.add_argument('--name')
    args=parser.parse_args(argv)
    try:
        if args.command=='host-add-member':
            print(json.dumps(provision_member(args.home,args.project,args.label,args.capability),indent=2));return 0
        if args.command=='host-revoke':
            print(json.dumps(revoke_credential(args.home,args.credential_id),indent=2));return 0
        if args.command=='host-init':
            print(json.dumps(bootstrap_host(args.home),indent=2));return 0
        if args.command=='serve':
            if not 1<=args.port<=65535: raise ValueError('Port must be between 1 and 65535')
            store=initialize_home(args.home)
            import roadmap_server
            options=['--store',str(store),'--port',str(args.port),'--host',args.host]
            if args.shared_mode:options.append('--shared-mode')
            if args.public_host:options.extend(['--public-host',args.public_host])
            if args.tls_cert:options.extend(['--tls-cert',str(args.tls_cert)])
            if args.tls_key:options.extend(['--tls-key',str(args.tls_key)])
            return roadmap_server.main(options)
        result=setup_project(args.repo,args.url,apply=args.apply,name=args.name) if args.command=='setup' else diagnose(args.repo,args.url)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if args.command!='doctor' or result['connected'] else 2
    except (ValueError,OSError,ApiError,GitAdapterError,StoreError) as exc:
        print('research-workspace: '+str(exc),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
