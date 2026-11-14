"""Private, bounded RPC staging. Cache writes never advance domain revisions."""
import base64
import binascii
import hashlib
import json
import time
import uuid
try:
    from . import workspace_bundle_chunks as codec
    from .roadmap_store import StoreError
except ImportError:
    import workspace_bundle_chunks as codec
    from roadmap_store import StoreError

TARGETS = frozenset({'/v1/transactions', '/v1/exchange-preview', '/v1/live-join', '/v1/live-resolve', '/v1/git-sync-apply'})
TTL = 24 * 60 * 60
MAX_JOBS = 8
MAX_STAGED_BYTES = 1024 * 1024 * 1024

def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS transfer_jobs(project_id TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,principal_id TEXT NOT NULL,target TEXT NOT NULL,manifest_json TEXT NOT NULL,expires_at REAL NOT NULL,PRIMARY KEY(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS transfer_chunks(project_id TEXT NOT NULL,transfer_id TEXT NOT NULL,sha256 TEXT NOT NULL,body BLOB NOT NULL,PRIMARY KEY(project_id,transfer_id,sha256),FOREIGN KEY(project_id,transfer_id) REFERENCES transfer_jobs(project_id,id) ON DELETE CASCADE)')

def _identity(payload):
    project, identity = payload.get('project_id'), payload.get('transfer_id')
    if not isinstance(project,str) or not project or len(project)>200:
        raise StoreError(400,'Invalid transfer project')
    try:
        if str(uuid.UUID(identity)) != identity: raise ValueError()
    except (ValueError,TypeError,AttributeError):
        raise StoreError(400,'Transfer identity must be a canonical UUID') from None
    return project, identity

def _job(db,project,identity,principal):
    row=db.execute('SELECT * FROM transfer_jobs WHERE project_id=? AND id=?',(project,identity)).fetchone()
    if row is None: raise StoreError(404,'Transfer staging is unavailable; begin again')
    if row['principal_id']!=principal: raise StoreError(403,'Transfer belongs to another principal')
    return row

def stage(store,action,payload,principal,*,download=False):
    if not isinstance(payload,dict):raise StoreError(400,'Transfer requires an object')
    fields={'begin':{'target','manifest'},'chunk':{'sha256','data'},'finish':set(),'release':set(),'download':{'sha256'}}
    if action not in fields or set(payload)!={'project_id','transfer_id'}|fields[action]:
        raise StoreError(400,'Invalid transfer request fields')
    project,identity=_identity(payload)
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('DELETE FROM transfer_jobs WHERE expires_at<?',(time.time(),))
            if not db.execute('SELECT 1 FROM projects WHERE id=? AND archived_at IS NULL',(project,)).fetchone():
                raise StoreError(404,'Active project not found')
            if action=='begin':
                codec.validate_manifest(payload['manifest'])
                if payload['target'] not in TARGETS and not (download and payload['target']=='@download'):raise StoreError(400,'Unsupported staged target')
                encoded=json.dumps(payload['manifest'],sort_keys=True,separators=(',',':'))
                old=db.execute('SELECT * FROM transfer_jobs WHERE project_id=? AND id=?',(project,identity)).fetchone()
                if old:
                    old=_job(db,project,identity,principal)
                    if old['target']!=payload['target'] or old['manifest_json']!=encoded:
                        raise StoreError(409,'Transfer identity already binds different content')
                else:
                    if db.execute('SELECT count(*) FROM transfer_jobs').fetchone()[0]>=64:
                        raise StoreError(413,'Temporary transfer job limit reached')
                    if db.execute('SELECT count(*) FROM transfer_jobs WHERE principal_id=?',(principal,)).fetchone()[0]>=MAX_JOBS:
                        raise StoreError(413,'Too many staged transfers; release old staging')
                    if db.execute('SELECT coalesce(sum(length(body)),0) FROM transfer_chunks').fetchone()[0]+payload['manifest']['payload_bytes']>MAX_STAGED_BYTES:
                        raise StoreError(413,'Temporary transfer storage limit reached')
                    db.execute('INSERT INTO transfer_jobs VALUES(?,?,?,?,?,?)',(project,identity,principal,payload['target'],encoded,time.time()+TTL))
                result={'transfer_id':identity,'received':[r[0] for r in db.execute('SELECT sha256 FROM transfer_chunks WHERE project_id=? AND transfer_id=?',(project,identity))]}
            else:
                job=_job(db,project,identity,principal)
                manifest=json.loads(job['manifest_json'])
                codec.validate_manifest(manifest)
                if action=='download':
                    digest=payload['sha256']
                    if job['target']!='@download' or not isinstance(digest,str) or digest not in {r['sha256'] for r in manifest['chunks']}:
                        raise StoreError(400,'Unknown response chunk')
                    row=db.execute('SELECT body FROM transfer_chunks WHERE project_id=? AND transfer_id=? AND sha256=?',(project,identity,digest)).fetchone()
                    if row is None:raise StoreError(404,'Response chunk is unavailable')
                    result={'sha256':digest,'data':base64.b64encode(row[0]).decode('ascii')}
                elif action=='chunk':
                    digest=payload['sha256'];expected={r['sha256']:r['byte_length'] for r in manifest['chunks']}
                    if not isinstance(digest,str) or digest not in expected:raise StoreError(400,'Chunk is not declared')
                    data=payload['data']
                    if not isinstance(data,str) or len(data)>((codec.DEFAULT_CHUNK_BYTES+2)//3)*4:raise StoreError(413,'Chunk exceeds wire bound')
                    try:raw=base64.b64decode(data,validate=True)
                    except (ValueError,binascii.Error):raise StoreError(400,'Invalid chunk encoding') from None
                    if len(raw)!=expected[digest] or hashlib.sha256(raw).hexdigest()!=digest:
                        raise StoreError(400,'Chunk integrity check failed')
                    used=db.execute('SELECT coalesce(sum(length(body)),0) FROM transfer_chunks').fetchone()[0]
                    exists=db.execute('SELECT 1 FROM transfer_chunks WHERE project_id=? AND transfer_id=? AND sha256=?',(project,identity,digest)).fetchone()
                    if not exists and used+len(raw)>MAX_STAGED_BYTES:raise StoreError(413,'Temporary transfer storage limit reached')
                    db.execute('INSERT OR IGNORE INTO transfer_chunks VALUES(?,?,?,?)',(project,identity,digest,raw))
                    result={'transfer_id':identity,'sha256':digest}
                elif action=='release':
                    db.execute('DELETE FROM transfer_jobs WHERE project_id=? AND id=?',(project,identity));result={'released':True}
                else:
                    if job['target'] not in TARGETS:raise StoreError(400,'Response cache is not an executable request')
                    def read(digest):
                        row=db.execute('SELECT body FROM transfer_chunks WHERE project_id=? AND transfer_id=? AND sha256=?',(project,identity,digest)).fetchone()
                        if row is None:raise codec.ChunkError('Transfer is incomplete')
                        return bytes(row[0])
                    decoded=codec.unpack(manifest,read)
                    if decoded.get('project_id')!=project:raise StoreError(403,'Staged project does not match payload')
                    result=(job['target'],decoded)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def cache_response(store,project,principal,value):
    manifest,chunks=codec.pack(value)
    identity=str(uuid.uuid5(uuid.NAMESPACE_URL,'workbench-response:'+project+':'+principal+':'+manifest['payload_sha256']))
    body={'project_id':project,'transfer_id':identity}
    staged=stage(store,'begin',{**body,'target':'@download','manifest':manifest},principal,download=True)
    received=set(staged['received'])
    for digest,raw in chunks.items():
        if digest not in received:
            stage(store,'chunk',{**body,'sha256':digest,'data':base64.b64encode(raw).decode('ascii')},principal)
    return {'format':'workbench-chunked-response','version':1,**body,'manifest':manifest}
