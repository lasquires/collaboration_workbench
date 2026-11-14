"""Service-owned project registration and local repository connections.

Portable exchange deliberately excludes this table: roots are machine-local.
"""
import json
import uuid
from pathlib import Path
from datetime import datetime,timezone
try:
    from . import roadmap_research as research,roadmap_schema,roadmap_git
except ImportError:
    import roadmap_research as research,roadmap_schema,roadmap_git

OPS={'create_project','register_repository'}

def create_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS repository_connections(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),root TEXT NOT NULL UNIQUE,head_commit TEXT,branch TEXT,dirty INTEGER NOT NULL CHECK(dirty IN (0,1)),sync_state TEXT NOT NULL DEFAULT 'pending',updated_at TEXT NOT NULL)")

def apply_operation(db,project,principal,op,request_id):
    caps=db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(project,principal)).fetchone()
    if caps is None or 'manage_projects' not in json.loads(caps[0]): research.error(403,'Project administration capability required')
    now=datetime.now(timezone.utc).isoformat()
    if op['op']=='create_project':
        identity=research.text(op.get('id'),'project UUID',200)
        try:
            if str(uuid.UUID(identity))!=identity: raise ValueError()
        except ValueError: research.error(400,'Project identity must be a canonical UUID')
        slug=research.text(op.get('slug'),'project slug',80)
        existing=db.execute('SELECT * FROM projects WHERE id=?',(identity,)).fetchone()
        if existing:
            if existing['slug']!=slug: research.error(409,'Project identity is already registered with different metadata')
            result={'type':'project','id':identity,'already_registered':True}
        else:
            if db.execute('SELECT 1 FROM projects WHERE slug=?',(slug,)).fetchone(): research.error(409,'Project slug is already registered')
            db.execute('INSERT INTO projects(id,slug,goal,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)',(identity,slug,research.text(op.get('goal','Research project'),'project goal'),principal,now,now))
            local=roadmap_schema.principal_for_label(db,identity,'local')
            db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',(research.canonical(['contribute','review','manage_claims','manage_projects']),identity,local))
            # Keep the authenticated creator able to administer the new project.
            db.execute('INSERT INTO project_members VALUES(?,?,?) ON CONFLICT(project_id,principal_id) DO NOTHING',
                       (identity,principal,research.canonical(['contribute','review','manage_claims','manage_projects'])))
            result={'type':'project','id':identity,'record_revision':1}
        if op.get('repository_root') is not None:
            local=roadmap_schema.principal_for_label(db,identity,'local')
            connection=apply_operation(db,identity,local,{'op':'register_repository','root':op['repository_root']},request_id)
            result['repository_id']=connection['id']
    elif op['op']=='register_repository':
        try: metadata=roadmap_git.inspect_repository(research.text(op.get('root'),'repository root',4000))
        except roadmap_git.GitAdapterError as exc: research.error(400,str(exc))
        root=str(Path(metadata.root).resolve())
        existing=db.execute('SELECT * FROM repository_connections WHERE root=?',(root,)).fetchone()
        if existing:
            if existing['project_id']!=project: research.error(409,'Repository is already connected to another project')
            result={'type':'repository','id':existing['id'],'already_registered':True}
        else:
            identity=str(uuid.uuid4())
            db.execute('INSERT INTO repository_connections(id,project_id,root,head_commit,branch,dirty,updated_at) VALUES(?,?,?,?,?,?,?)',(identity,project,root,metadata.head_commit,metadata.branch,int(metadata.dirty),now))
            result={'type':'repository','id':identity,'sync_state':'pending'}
    else: research.error(400,'Unknown project operation')
    db.execute('INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),project,request_id,principal,result['type'],result['id'],op['op'],research.canonical(result),now))
    return result

def read(db,project):
    if not db.execute('SELECT 1 FROM projects WHERE id=?',(project,)).fetchone():research.error(404,'Scoped project not found')
    meta=db.execute('SELECT * FROM store_meta').fetchone()
    return {'api_version':'research-workspace/v1','project_id':project,'revision':f"{meta['store_id']}:{meta['revision']}",
            'items':[dict(row) for row in db.execute('SELECT * FROM repository_connections WHERE project_id=? ORDER BY root LIMIT 100',(project,))],
            'next_cursor':None,'coverage':{'complete':False,'source':'local repository connections'}}
