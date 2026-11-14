"""Service-owned local repository scans, published with source-revision CAS.

Git inspection runs outside database transactions. These records describe source
updates, not indexed evidence, shared synchronization or forensic acceptance.
"""
import hashlib
import json
import uuid
from datetime import datetime,timezone
from pathlib import Path
try:
    from . import roadmap_research as research,workspace_git_watch as watcher,roadmap_schema
except ImportError:
    import roadmap_research as research,workspace_git_watch as watcher,roadmap_schema


def create_schema(db):
    columns={row[1] for row in db.execute('PRAGMA table_info(repository_connections)')}
    for definition in ("source_state TEXT NOT NULL DEFAULT 'pending'",'source_revision INTEGER NOT NULL DEFAULT 0',
                       'snapshot_id TEXT','last_error TEXT'):
        if definition.split()[0] not in columns:db.execute('ALTER TABLE repository_connections ADD COLUMN '+definition)
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS repository_project_identity ON repository_connections(project_id,id)')
    db.execute('CREATE TABLE IF NOT EXISTS repository_snapshots(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,repository_id TEXT NOT NULL,source_revision INTEGER NOT NULL,fingerprint TEXT NOT NULL,snapshot_json TEXT NOT NULL,changes_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(project_id,repository_id,source_revision),FOREIGN KEY(project_id,repository_id) REFERENCES repository_connections(project_id,id))')
    db.execute('CREATE INDEX IF NOT EXISTS repository_snapshot_order ON repository_snapshots(project_id,repository_id,source_revision)')
    for verb in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_repository_snapshots_{verb.lower()} BEFORE {verb} ON repository_snapshots BEGIN SELECT RAISE(ABORT,'repository snapshots are immutable'); END")


def _connection(db,project,identity):
    row=db.execute('SELECT * FROM repository_connections WHERE project_id=? AND id=?',(project,identity)).fetchone()
    if row is None:research.error(404,'Scoped repository connection not found')
    return row


def refresh(store,project,identity,*,snapshotter=None):
    with store._connection() as db:
        before=dict(_connection(db,project,identity))
        previous=db.execute('SELECT snapshot_json FROM repository_snapshots WHERE id=? AND project_id=?',(before['snapshot_id'],project)).fetchone()
        previous=json.loads(previous[0]) if previous else None
    # This may take seconds; never keep the SQLite read/write lock while Git runs.
    try:
        snapshot=(snapshotter or watcher.snapshot_repository)(before['root'])
    except (watcher.GitWatchError,OSError) as exc:
        snapshot={'root':before['root'],'head':before['head_commit'],'branch':before['branch'],
                  'index_identity':None,'paths':[],'incomplete':True,'errors':[str(exc)],'scan_failed':True}
        snapshot['fingerprint']=research.digest(snapshot)
    if not isinstance(snapshot,dict) or Path(snapshot.get('root','')).resolve()!=Path(before['root']).resolve():
        research.error(400,'Repository scan returned a different root')
    fingerprint=snapshot.get('fingerprint')
    if not isinstance(fingerprint,str) or len(fingerprint)!=64 or any(c not in '0123456789abcdef' for c in fingerprint):
        research.error(400,'Repository scan lacks a valid fingerprint')
    raw=research.canonical(snapshot)
    if len(raw.encode('utf-8'))>8*1024*1024:research.error(413,'Repository snapshot exceeds publication limit')
    baseline=previous or {'root':snapshot['root'],'head':None,'branch':None,'index_identity':None,
                          'paths':[],'incomplete':False,'fingerprint':None}
    changes=watcher.compare_snapshots(baseline,snapshot)
    state='failed' if snapshot.get('scan_failed') else 'incomplete' if snapshot.get('incomplete') else 'current'
    current=datetime.now(timezone.utc).isoformat()
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row=_connection(db,project,identity)
            if row['source_revision']!=before['source_revision'] or row['root']!=before['root']:
                research.error(409,'Repository source state changed during scan; refresh again')
            if row['snapshot_id'] and previous and previous['fingerprint']==fingerprint and row['source_state']==state:
                db.rollback()
                return {'changed':False,'repository_id':identity,'source_revision':row['source_revision'],
                        'source_state':row['source_state'],'revision':store._revision(store._meta(db))}
            version=row['source_revision']+1;sid=str(uuid.uuid4());request_id='repository-scan-'+str(uuid.uuid4())
            db.execute('INSERT INTO repository_snapshots VALUES(?,?,?,?,?,?,?,?)',
                       (sid,project,identity,version,fingerprint,raw,research.canonical(changes),current))
            errors=list(snapshot.get('errors',[]))+list(changes.get('errors',[]))
            dirty=int(bool(snapshot.get('paths'))) if not snapshot.get('incomplete') else row['dirty']
            db.execute('UPDATE repository_connections SET head_commit=?,branch=?,dirty=?,source_state=?,source_revision=?,snapshot_id=?,last_error=?,updated_at=? WHERE project_id=? AND id=?',
                       (snapshot.get('head'),snapshot.get('branch'),dirty,state,version,sid,'; '.join(errors)[:8000] or None,current,project,identity))
            actor=roadmap_schema.principal_for_label(db,project,'local')
            # Portable history contains relative changes and Git identity, never
            # the local clone root or detailed OS error messages.
            summary={'source_revision':version,'head':snapshot.get('head'),'branch':snapshot.get('branch'),
                     'source_state':state,'changed_paths':changes['changed_paths'][:100],
                     'changed_path_count':len(changes['changed_paths']),'forensic_acceptance':False}
            db.execute('INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()),project,request_id,actor,'repository',identity,'repository_snapshot',research.canonical(summary),current))
            db.execute('UPDATE store_meta SET revision=revision+1')
            result={'changed':True,'repository_id':identity,'source_revision':version,'source_state':state,
                    'changes':changes,'revision':store._revision(store._meta(db))}
            db.execute('INSERT INTO transactions VALUES(?,?,?)',(request_id,hashlib.sha256(raw.encode('utf-8')).hexdigest(),research.canonical(result)))
            db.commit();return result
        except Exception:
            if db.in_transaction:db.rollback()
            raise


def _summary(row):
    snapshot=json.loads(row['snapshot_json']);changes=json.loads(row['changes_json'])
    paths=changes['changed_paths']
    return {'id':row['id'],'repository_id':row['repository_id'],'source_revision':row['source_revision'],
            'fingerprint':row['fingerprint'],'created_at':row['created_at'],'head':snapshot.get('head'),
            'branch':snapshot.get('branch'),'incomplete':snapshot.get('incomplete',True),
            'changes':{**changes,'changed_paths':paths[:50],'changed_path_count':len(paths),'omitted_paths':max(0,len(paths)-50)}}


def read(db,project,*,resource='repositories',repository_id=None,limit=100,cursor=None,**unknown):
    try:
        from .roadmap_projection import _encode,_decode
    except ImportError:
        from roadmap_projection import _encode,_decode
    if unknown:research.error(400,'Unsupported repository read parameter')
    if type(limit) is not int or not 1<=limit<=100:research.error(400,'Page limit must be 1-100')
    if not db.execute('SELECT 1 FROM projects WHERE id=?',(project,)).fetchone():research.error(404,'Scoped project not found')
    if resource=='repository-updates':_connection(db,project,repository_id)
    elif repository_id is not None:research.error(400,'Repository filtering is supported for update history')
    revision=f"{(meta:=db.execute('SELECT * FROM store_meta').fetchone())['store_id']}:{meta['revision']}"
    binding={'resource':resource,'project_id':project,'repository_id':repository_id,'limit':limit,'revision':revision};offset=0
    if cursor:
        page=_decode(cursor)
        if page.get('revision')!=revision:research.error(409,'Repository snapshot cursor expired; restart required')
        if set(page)!=set(binding)|{'offset'} or any(page.get(k)!=v for k,v in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:
            research.error(400,'Cursor does not match repository read')
        offset=page['offset']
    if resource=='repository-updates':
        rows=db.execute('SELECT * FROM repository_snapshots WHERE project_id=? AND repository_id=? ORDER BY source_revision DESC LIMIT ? OFFSET ?',(project,repository_id,limit+1,offset)).fetchall()
        items=[_summary(row) for row in rows[:limit]]
    else:
        rows=db.execute('SELECT * FROM repository_connections WHERE project_id=? ORDER BY root LIMIT ? OFFSET ?',(project,limit+1,offset)).fetchall()
        items=[]
        for row in rows[:limit]:
            item=dict(row)
            latest=db.execute('SELECT * FROM repository_snapshots WHERE id=? AND project_id=?',(row['snapshot_id'],project)).fetchone()
            item['latest_update']=_summary(latest) if latest else None
            items.append(item)
    return {'api_version':'research-workspace/v1','project_id':project,'revision':revision,'items':items,
            'next_cursor':_encode({**binding,'offset':offset+len(items)}) if len(rows)>limit else None,
            'coverage':{'complete':False,'source':'local Git repository tracking; shared research synchronization is separate'}}
