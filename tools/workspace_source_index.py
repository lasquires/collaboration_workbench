"""Publish committed Git objects as citable source versions and lexical evidence."""
import hashlib
import copy
import json
import re
import uuid
import unicodedata
from datetime import datetime,timezone
try:
    from . import roadmap_research as research
except ImportError:
    import roadmap_research as research

EXTRACTOR='workbench.git.lexical'
CONFIG={'namespace':'source-token','tokenizer':'unicode-identifiers/v1','unicode_version':unicodedata.unidata_version,'source':'pinned-git-blob','path_encoding':'portable-git-path/v1','meaning':'lexical only; no native interpretation'}
CONFIG_HASH=research.digest(CONFIG)
PREFIX={'root':'git-source-root-','artifact':'git-source-artifact-','version':'git-source-version-','run':'git-source-run-','manifest':'git-source-manifest-'}


def identity(kind,*parts):return PREFIX[kind]+str(uuid.uuid5(uuid.NAMESPACE_URL,research.canonical(parts)))


def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS git_source_manifests(id TEXT NOT NULL,project_id TEXT NOT NULL,root_id TEXT NOT NULL,commit_id TEXT NOT NULL,files_json TEXT NOT NULL,coverage_json TEXT NOT NULL,run_id TEXT,config_hash TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,root_id) REFERENCES artifact_roots(project_id,id),FOREIGN KEY(project_id,run_id) REFERENCES index_runs(project_id,id))')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS git_source_manifests_{action.lower()} BEFORE {action} ON git_source_manifests BEGIN SELECT RAISE(ABORT,'source manifests are immutable'); END")


def generated_first_seen(table,row):
    """Only generated records may differ in local first-observed time."""
    if row is None:return True
    if row.get('created_by') is not None:return False
    if table in ('artifact_roots','artifacts','artifact_versions','index_runs','git_source_manifests'):
        kind={'artifact_roots':'root','artifacts':'artifact','artifact_versions':'version','index_runs':'run','git_source_manifests':'manifest'}[table]
        return str(row.get('id','')).startswith(PREFIX[kind]) and (table!='index_runs' or row.get('extractor_id')==EXTRACTOR)
    if table=='index_batches':return str(row.get('run_id','')).startswith(PREFIX['run'])
    if table=='occurrences':return str(row.get('artifact_version_id','')).startswith(PREFIX['version'])
    return table=='observations' and row.get('extractor_id')==EXTRACTOR



def logical_path_bytes(raw):
    if not isinstance(raw,bytes) or not raw or b'\0' in raw or raw.startswith(b'/') or any(part in (b'',b'.',b'..') for part in raw.split(b'/')):research.error(400,'Git source path must remain repository-relative')
    result=''
    for char in raw.decode('utf-8','surrogateescape'):
        if 0xDC80<=ord(char)<=0xDCFF:result+='%'+format(ord(char)-0xDC00,'02X')
        elif char.isalnum() or char in '/-._~' or ord(char)>127 and not char.isspace():result+=char
        else:result+=''.join('%'+format(byte,'02X') for byte in char.encode('utf-8'))
    return result


def logical_path(path):
    if not isinstance(path,str):research.error(400,'Invalid Git source path')
    return logical_path_bytes(path.encode('utf-8'))


def _coverage_paths(coverage):
    result=copy.deepcopy(coverage)
    for item in result.get('excluded',[]):
        for key in ('path','first_path','last_path'):
            if isinstance(item.get(key),str):
                raw=bytes.fromhex(item[key+'_git_hex']) if key+'_git_hex' in item else item[key].encode('utf-8')
                item[key]=logical_path_bytes(raw)
                if item[key].encode('utf-8')!=raw:item[key+'_git_hex']=raw.hex()
    return result


def _summary(row,*,detail=False):
    files=json.loads(row['files_json']);coverage=json.loads(row['coverage_json'])
    if detail:
        for item in files:
            try:item['display_path']=bytes.fromhex(item['git_path_hex']).decode('utf-8') if 'git_path_hex' in item else item['path'];item['display_path_encoding']='utf8'
            except UnicodeDecodeError:item['display_path']=item['path'];item['display_path_encoding']='escaped-bytes'
    return {key:row[key] for key in ('id','project_id','root_id','commit_id','run_id','config_hash','created_at')}|{'file_count':len(files),'coverage':coverage,**({'files':files} if detail else {}),'forensic_acceptance':False}



def _existing_matches(db,table,project,identity_,expected):
    row=db.execute(f'SELECT * FROM {table} WHERE project_id=? AND id=?',(project,identity_)).fetchone()
    if row is None or any(row[key]!=value for key,value in expected.items()):research.error(409,'Generated source identity conflicts with original stored metadata')


def publish(db,project,principal,request_id,capture):
    if not db.in_transaction:research.error(409,'Source publication requires owning transaction')
    if capture.get('format')!='workbench-git-capture/v1' or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',capture.get('commit','')):research.error(400,'Invalid pinned Git capture')
    coverage=capture.get('coverage')
    if not isinstance(coverage,dict) or any(type(coverage.get(key)) is not bool for key in ('complete','pinned_complete','lexical_complete')):research.error(400,'Source capture coverage is required')
    coverage=_coverage_paths(coverage)
    root_id=identity('root',project,'main');files=[];records=[];sources=[];now=datetime.now(timezone.utc).isoformat()
    db.execute('INSERT OR IGNORE INTO artifact_roots VALUES(?,?,?,NULL,?)',(root_id,project,'workspace-root:'+root_id,now))
    _existing_matches(db,'artifact_roots',project,root_id,{'created_by':None})
    for entry in capture['files']:
        raw_path=bytes.fromhex(entry['git_path_hex']) if 'git_path_hex' in entry else entry['path'].encode('utf-8');path=logical_path_bytes(raw_path)
        item={key:entry[key] for key in ('mode','blob','size','availability')}|{'path':path}
        if path.encode('utf-8')!=raw_path:item['git_path_hex']=raw_path.hex()
        for key in ('reason','text_state'):
            if key in entry:item[key]=entry[key]
        if entry['availability']=='available':
            body=entry.get('body');sha=entry.get('sha256')
            if not isinstance(body,bytes) or len(body)!=entry['size'] or hashlib.sha256(body).hexdigest()!=sha:research.error(409,'Pinned Git source bytes differ from capture digest')
            algorithm=hashlib.sha1 if len(entry['blob'])==40 else hashlib.sha256
            if algorithm(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()!=entry['blob']:research.error(409,'Pinned Git blob object verification failed')
            artifact_id=identity('artifact',root_id,path);version_id=identity('version',project,artifact_id,sha)
            db.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,NULL,?,1)',(artifact_id,project,root_id,path,'git-blob',now))
            _existing_matches(db,'artifacts',project,artifact_id,{'root_id':root_id,'relative_path':path,'kind':'git-blob','created_by':None})
            db.execute('INSERT OR IGNORE INTO artifact_blobs VALUES(?,?,?)',(project,sha,body))
            # Commit-to-content provenance belongs to each immutable manifest;
            # identical bytes may legitimately occur in several commits/clones.
            db.execute('INSERT OR IGNORE INTO artifact_versions VALUES(?,?,?,?,?,NULL,NULL,?,NULL,NULL,?)',(version_id,project,artifact_id,sha,len(body),'available',now))
            _existing_matches(db,'artifact_versions',project,version_id,{'artifact_id':artifact_id,'sha256':sha,'byte_length':len(body),'repository_revision':None,'binary_build_id':None,'source_created_at':None,'created_by':None,'availability':'available'})
            sources.append(version_id);raw_tokens=entry.get('tokens',[])
            tokens=[token for token in raw_tokens if len(token['identifier'])<=2000] if len(path)<=2000 else []
            if len(tokens)!=len(raw_tokens):
                coverage['complete']=coverage['lexical_complete']=False
                coverage.setdefault('excluded',[]).append({'path':path,'reason':'identifier_limit' if len(path)<=2000 else 'scope_path_limit','count':len(raw_tokens)-len(tokens)})
            item.update(artifact_id=artifact_id,artifact_version_id=version_id,sha256=sha,token_count=len(tokens),token_digest=research.digest(tokens))
            for token in tokens:
                start=token['byte_offset'];length=token['byte_length'];name=token['identifier']
                if type(start) is not int or type(length) is not int or start<0 or length<=0 or body[start:start+length]!=name.encode('utf-8'):research.error(409,'Lexical token locator differs from pinned source bytes')
                records.append({'artifact_version_id':version_id,'namespace':'source-token','scope_key':path,'locator_key':{key:token[key] for key in ('byte_offset','byte_length','line','column')}|{'member':name},'identifier':name,'value':{'kind':'lexical_token'},'provenance':{'extractor':EXTRACTOR,'method':'UTF-8 lexical identifier scan','source_sha256':sha,'limitations':'Includes comments and strings; no language semantics or native verification'}})
        elif entry['availability']!='excluded':research.error(400,'Unknown Git source availability')
        files.append(item)
    coverage['token_count']=len(records)
    content={'project_id':project,'commit_id':capture['commit'],'root_id':root_id,'files':files,'coverage':coverage,'config_hash':CONFIG_HASH}
    manifest_id=identity('manifest',research.digest(content));run_id=identity('run',manifest_id) if records else None
    existing=db.execute('SELECT * FROM git_source_manifests WHERE project_id=? AND id=?',(project,manifest_id)).fetchone()
    if existing:return _summary(existing)
    if records:
        db.execute('INSERT INTO index_runs VALUES(?,?,?,?,?,?,?,NULL,NULL,?,1)',(run_id,project,EXTRACTOR,'1',CONFIG_HASH,'staging',research.canonical(coverage),now))
        for version in sorted(set(sources)):db.execute('INSERT INTO index_run_inputs VALUES(?,?,?)',(project,run_id,version))
        batches=[]
        for offset in range(0,len(records),500):
            key=f'{offset//500:06d}';result=research.stage_batch(db,project,run_id,key,records[offset:offset+500])
            batches.append({'key':key,'sha256':result['payload_hash']})
        research.publish_run(db,project,run_id,manifest={'batches':batches,'record_count':len(records)})
        payload={'type':'index_run','id':run_id}
        sequence=db.execute('INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),project,request_id,principal,'index_run',run_id,'repository_index_publication',research.canonical(payload),now)).lastrowid
        db.execute('INSERT INTO research_outbox(id,project_id,event_sequence,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),project,sequence,'publish_run',research.canonical(payload),now))
    db.execute('INSERT INTO git_source_manifests VALUES(?,?,?,?,?,?,?,?,?)',(manifest_id,project,root_id,capture['commit'],research.canonical(files),research.canonical(coverage),run_id,CONFIG_HASH,now))
    return _summary(db.execute('SELECT * FROM git_source_manifests WHERE project_id=? AND id=?',(project,manifest_id)).fetchone())


def read(db,project,*,manifest_id=None,limit=None,cursor=None,**unused):
    try:
        from .roadmap_projection import API_VERSION,_coverage,_encode,_decode
    except ImportError:
        from roadmap_projection import API_VERSION,_coverage,_encode,_decode
    coverage=_coverage(db,project);meta=db.execute('SELECT store_id,revision FROM store_meta').fetchone();revision=f'{meta[0]}:{meta[1]}'
    limit=100 if limit is None else limit
    if type(limit) is not int or not 1<=limit<=100:research.error(400,'Page limit must be 1–100')
    if manifest_id is not None:research.text(manifest_id,'source manifest ID',200)
    binding={'project_id':project,'resource':'source-manifests','manifest_id':manifest_id,'revision':revision,'limit':limit};offset=0
    if cursor:
        page=_decode(cursor)
        if page.get('revision')!=revision:research.error(409,'Source manifest cursor expired; restart required')
        if set(page)!=set(binding)|{'offset'} or any(page.get(k)!=v for k,v in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:research.error(400,'Cursor does not match source manifest filters')
        offset=page['offset']
    query='SELECT * FROM git_source_manifests WHERE project_id=?';args=[project]
    if manifest_id is not None:query+=' AND id=?';args.append(manifest_id)
    rows=db.execute(query+' ORDER BY created_at DESC,id LIMIT ? OFFSET ?',(*args,limit+1,offset)).fetchall()
    return {'api_version':API_VERSION,'project_id':project,'revision':revision,'coverage':coverage,'items':[_summary(row,detail=manifest_id is not None) for row in rows[:limit]],'next_cursor':_encode({**binding,'offset':offset+limit}) if len(rows)>limit else None}


def validate_bundle(project,records):
    manifests=records.get('git_source_manifests',[])
    roots={row['id']:row for row in records['artifact_roots']};artifacts={row['id']:row for row in records['artifacts']};versions={row['id']:row for row in records['artifact_versions']};runs={row['id']:row for row in records['index_runs']}
    observations={item['id']:item for item in records['observations']};occurrences={item['id']:item for item in records['occurrences']}
    covered_versions=set();covered_runs=set();covered_roots={row['root_id'] for row in manifests}
    for manifest in manifests:
        try:entries=json.loads(manifest['files_json'])
        except (ValueError,TypeError):research.error(400,'Invalid Git source manifest JSON')
        if not isinstance(entries,list) or any(not isinstance(entry,dict) for entry in entries):research.error(400,'Invalid Git source file metadata')
        covered_versions.update(entry['artifact_version_id'] for entry in entries if entry.get('availability')=='available' and 'artifact_version_id' in entry)
        if manifest['run_id'] is not None:covered_runs.add(manifest['run_id'])
    if (any(row['id'].startswith(PREFIX['version']) and row['id'] not in covered_versions for row in versions.values())
            or any(row['id'].startswith(PREFIX['run']) and row['id'] not in covered_runs for row in runs.values())
            or any(row['id'].startswith(PREFIX['root']) and row['id'] not in covered_roots for row in roots.values())):research.error(400,'Generated Git source provenance is missing')
    for row in manifests:
        try:files=json.loads(row['files_json']);coverage=json.loads(row['coverage_json'])
        except (ValueError,TypeError):research.error(400,'Invalid Git source manifest JSON')
        if not isinstance(files,list) or len(files)>100000 or not isinstance(coverage,dict) or any(type(coverage.get(key)) is not bool for key in ('complete','pinned_complete','lexical_complete')):research.error(400,'Invalid Git source coverage')
        if row['root_id']!=identity('root',project,'main') or row['root_id'] not in roots or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',row['commit_id']):research.error(400,'Invalid scoped Git source identity')
        paths=set();tokens=0
        for item in files:
            allowed={'path','mode','blob','size','availability','reason','text_state','artifact_id','artifact_version_id','sha256','token_count','token_digest','git_path_hex'}
            if not isinstance(item,dict) or set(item)-allowed or not {'path','mode','blob','size','availability'}<=set(item):research.error(400,'Invalid Git source file metadata')
            path=item['path']
            if not isinstance(path,str) or not path or path in paths or path.startswith('/') or '\\' in path or ':' in path or any(part in ('','.','..') for part in path.split('/')):research.error(400,'Invalid Git source path')
            paths.add(path)
            if 'git_path_hex' in item:
                try:raw_path=bytes.fromhex(item['git_path_hex'])
                except (ValueError,TypeError):research.error(400,'Invalid original Git path encoding')
                if logical_path_bytes(raw_path)!=path or raw_path==path.encode('utf-8'):research.error(400,'Git path mapping does not preserve original identity')
            elif '%' in path or logical_path(path)!=path:research.error(400,'Missing original Git path encoding')
            if not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',item['blob']):research.error(400,'Invalid Git blob identity')
            if item['availability']=='available':
                if item['mode'] not in ('100644','100755') or type(item['size']) is not int or item['size']<0:research.error(400,'Invalid regular Git source size/mode')
                version=versions.get(item.get('artifact_version_id'));artifact=artifacts.get(item.get('artifact_id'))
                if (version is None or artifact is None or artifact['root_id']!=row['root_id'] or artifact['relative_path']!=path or version['artifact_id']!=artifact['id']
                        or version['sha256']!=item.get('sha256') or version['byte_length']!=item['size'] or type(item.get('token_count')) is not int or item['token_count']<0
                        or artifact['kind']!='git-blob' or artifact['created_by'] is not None or any(version[key] is not None for key in ('repository_revision','binary_build_id','source_created_at','created_by'))
                        or artifact['id']!=identity('artifact',row['root_id'],path) or version['id']!=identity('version',project,artifact['id'],version['sha256'])):research.error(400,'Git manifest source version mismatch')
                research.sha(item.get('token_digest'))
                tokens+=item['token_count']
            elif item['availability']!='excluded':research.error(400,'Unknown Git source availability')
        if (any(type(coverage.get(key)) is not int or coverage[key]<0 for key in ('path_count','byte_count','token_count')) or type(coverage.get('path_count')) is not int or coverage['path_count']<len(files) or coverage.get('token_count')!=tokens
                or coverage.get('byte_count')!=sum(item['size'] for item in files if item['availability']=='available')
                or coverage['complete']!=(coverage['pinned_complete'] and coverage['lexical_complete'])
                or coverage['pinned_complete'] and (len(files)!=coverage['path_count'] or any(item['availability']!='available' for item in files))):research.error(400,'Git source coverage differs from pinned files')
        if row['run_id'] is not None:
            run=runs.get(row['run_id'])
            if run is None or run['extractor_id']!=EXTRACTOR or run['state']!='published' or run['config_hash']!=row['config_hash']:research.error(400,'Git source manifest lacks its published lexical run')
            if sum(1 for item in records['run_observations'] if item['run_id']==row['run_id'])!=tokens:research.error(400,'Git lexical observation count differs from pinned manifest')
        elif tokens:research.error(400,'Git source tokens lack a published index run')
        linked={item['observation_id'] for item in records['run_observations'] if item['run_id']==row['run_id']} if row['run_id'] is not None else set()
        for item in files:
            if item['availability']!='available':continue
            captured_tokens=[]
            for identity_ in linked:
                observation=observations.get(identity_)
                occurrence=occurrences.get(observation['occurrence_id']) if observation is not None else None
                if occurrence is None:research.error(400,'Git source observation lacks scoped occurrence')
                if occurrence['artifact_version_id']!=item['artifact_version_id']:continue
                if occurrence['namespace']!='source-token' or occurrence['scope_key']!=item['path'] or observation['extractor_id']!=EXTRACTOR or observation['config_hash']!=row['config_hash']:research.error(400,'Git source observation scope differs from manifest')
                try:
                    locator=json.loads(occurrence['locator_key']);token={key:locator[key] for key in ('byte_offset','byte_length','line','column')}|{'identifier':observation['identifier']}
                except (ValueError,TypeError,KeyError):research.error(400,'Git source token locator is incompatible')
                captured_tokens.append(token)
            captured_tokens.sort(key=lambda token:token['byte_offset'])
            if len(captured_tokens)!=item['token_count'] or research.digest(captured_tokens)!=item['token_digest']:research.error(400,'Git source token digest differs from published index')
        content={'project_id':project,'commit_id':row['commit_id'],'root_id':row['root_id'],'files':files,'coverage':coverage,'config_hash':row['config_hash']}
        if row['id']!=identity('manifest',research.digest(content)):research.error(400,'Git source manifest content identity mismatch')
