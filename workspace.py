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
from roadmap_store import RoadmapStore
from roadmap_cli import request,ApiError
from roadmap_git import inspect_repository,GitAdapterError

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


def validate_manifest(document):
    if not isinstance(document,dict) or set(document)!={'protocol','project_id','name'} or document['protocol']!=PROTOCOL:
        raise ValueError('Unsupported or malformed project manifest')
    if not isinstance(document['project_id'],str) or not isinstance(document['name'],str) or not document['name'].strip() or len(document['name'])>200:
        raise ValueError('Manifest requires a project UUID and a name of at most 200 characters')
    if str(uuid.UUID(document['project_id']))!=document['project_id']:
        raise ValueError('Manifest requires a canonical project UUID and a name')
    return document


def transaction(url,project,operations):
    caps=request(url,'/v1/projects/'+project+'/capabilities')
    return request(url,'/v1/transactions',{'project_id':project,
        'actor':{'principal_id':caps['capabilities']['local_principal_id']},
        'request_id':str(uuid.uuid4()),'expected_revision':caps['revision'],'operations':operations})


def setup_project(directory,url=DEFAULT_URL,*,apply=False,name=None,proposed_manifest=None):
    url=local_url(url);metadata=inspect_repository(directory);repo=Path(metadata.root)
    if Path(directory).resolve()!=repo: raise ValueError('Choose the repository root, not a nested directory')
    manifest_path=repo/MANIFEST
    existing_manifest=load_manifest(repo) if manifest_path.exists() else None
    if proposed_manifest is not None:
        validate_manifest(proposed_manifest)
        if existing_manifest is not None and proposed_manifest!=existing_manifest:
            raise ApiError(409,'Project manifest changed since preview; reread and reconcile')
    manifest=existing_manifest or proposed_manifest or {'protocol':PROTOCOL,'project_id':str(uuid.uuid4()),'name':name or repo.name}
    validate_manifest(manifest)
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

Use the platform's scoped CLI/API for tasks, claims, research and journals.
Refresh indexed sources after Git changes. Shared research changes travel through
Git text exports; review conflicts explicitly. A hint or workflow status does not
verify a forensic claim. Never edit the app database directly or merge SQLite
files. See the platform's project-agent setup guide for the supported commands.
"""
    files={MANIFEST.as_posix():json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n',
           '.research-workspace/AGENTS.md':instructions}
    if not apply: return {'project_id':manifest['project_id'],'repository':str(repo),'apply':False,'files':files}
    # Existing integration prose belongs to the project. Never overwrite it.
    if instruction_path.exists() and instruction_path.read_text(encoding='utf-8')!=instructions:
        raise ValueError('Existing integration instructions differ; reconcile the preview before setup')
    projects=request(url,'/v1/projects')['items']
    if not projects: raise ValueError('Service has no administration workspace')
    admin=next((p for p in projects if p['slug']=='workspace'),projects[0])
    existing=next((p for p in projects if p['id']==manifest['project_id']),None)
    if existing is None:
        slug=re.sub('[^a-z0-9.-]+','-',manifest['name'].lower()).strip('-.')[:60] or 'project'
        transaction(url,admin['id'],[{'op':'create_project','id':manifest['project_id'],
            'slug':slug+'-'+manifest['project_id'][:8],'goal':'Research for '+manifest['name'],'repository_root':str(repo)}])
    # Register through the service before writing portable setup files, so a
    # rejected connection cannot silently change this project's instructions.
    connections=request(url,'/v1/repositories?'+urlencode({'project_id':manifest['project_id']}))['items']
    if not any(Path(connection['root']).resolve()==repo for connection in connections):
        transaction(url,manifest['project_id'],[{'op':'register_repository','root':str(repo)}])
    manifest_path.parent.mkdir(parents=True,exist_ok=True)
    if not manifest_path.exists():
        with manifest_path.open('x',encoding='utf-8',newline='\n') as output: output.write(files[MANIFEST.as_posix()])
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
    return {'connected':connection is not None,'protocol':projects['api_version'],'project_id':manifest['project_id'],
            'repository':str(repo),'head_commit':metadata.head_commit,'branch':metadata.branch,'dirty':metadata.dirty,
            'service':url,'service_revision':projects['revision'],
            'sync_state':connection['sync_state'] if connection else 'not connected',
            'next_action':'Git synchronization is not yet implemented' if connection else 'Run setup --apply against this local service'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    serve=commands.add_parser('serve',help='start the independent local app')
    serve.add_argument('--home',type=Path,default=default_home());serve.add_argument('--port',type=int,default=8771)
    for command in ('setup','doctor'):
        part=commands.add_parser(command);part.add_argument('--repo',type=Path,required=True);part.add_argument('--url',default=DEFAULT_URL)
        if command=='setup':part.add_argument('--apply',action='store_true');part.add_argument('--name')
    args=parser.parse_args(argv)
    try:
        if args.command=='serve':
            if not 1<=args.port<=65535: raise ValueError('Port must be between 1 and 65535')
            store=initialize_home(args.home)
            import roadmap_server
            return roadmap_server.main(['--store',str(store),'--port',str(args.port)])
        result=setup_project(args.repo,args.url,apply=args.apply,name=args.name) if args.command=='setup' else diagnose(args.repo,args.url)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if args.command!='doctor' or result['connected'] else 2
    except (ValueError,OSError,ApiError,GitAdapterError) as exc:
        print('research-workspace: '+str(exc),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
