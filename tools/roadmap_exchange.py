"""Deterministic Git-transport data, never SQLite files or live ownership.

All writes belong to the caller's revision-checked transaction. Historical remote
events and alternatives are retained in immutable snapshots, not replayed as local
transactions. Membership, capabilities, sessions and delivery queues stay local.
"""
import base64
import hashlib
import json

try:
    from . import roadmap_research as research
except ImportError:
    import roadmap_research as research

OPS = {'register_exchange_snapshot', 'import_bundle'}
RESOURCES = {'exchange'}
TABLES = tuple('projects principals areas area_paths tasks task_links containment entity_aliases attempts validation_records task_blockers merge_records task_redirects guidance_links artifact_roots artifact_blobs artifacts artifact_versions index_runs index_run_inputs index_batches index_errors occurrences observations run_observations observation_reconciliations occurrence_edges questions question_revisions negative_searches registry_imports registry_records findings finding_revisions evidence_links answers documents document_versions discussion_comments edit_proposals'.split())
IMMUTABLE = set('principals entity_aliases validation_records artifact_blobs artifact_versions occurrences observations question_revisions negative_searches registry_imports registry_records finding_revisions document_versions discussion_comments index_batches'.split())
canonical = research.canonical


def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS exchange_snapshots(project_id TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,bundle_json TEXT NOT NULL,PRIMARY KEY(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS exchange_imports(project_id TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,result_json TEXT NOT NULL,PRIMARY KEY(project_id,id))')
    for table in ('exchange_snapshots', 'exchange_imports'):
        for action in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'exchange history is immutable'); END")


def _schema(db):
    return {t: [dict(r) for r in db.execute(f'PRAGMA table_info({t})')] for t in TABLES}


def _key(schema, table, row):
    columns = sorted((c for c in schema[table] if c['pk']), key=lambda c:c['pk'])
    return canonical([row[c['name']] for c in columns])


def _encode(row):
    row = dict(row)
    for k, v in row.items():
        if isinstance(v, bytes):
            row[k] = {'base64':base64.b64encode(v).decode('ascii')}
    if 'record_json' in row:
        record = json.loads(row['record_json'])
        for key in ('claim', 'reserved_paths', 'session_id', 'lease'):
            record.pop(key, None)
        row['record_json'] = canonical(record)
    return row


def export_bundle(db, project_id, ancestor_id=None):
    if db.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone() is None:
        research.error(404, 'Unknown exchange project')
    schema = _schema(db)
    rows = {}
    for table in TABLES:
        if table == 'projects':
            query, args = 'SELECT * FROM projects WHERE id=?', (project_id,)
        elif table == 'principals':
            query, args = 'SELECT * FROM principals WHERE id IN (SELECT principal_id FROM project_members WHERE project_id=?)', (project_id,)
        elif table == 'task_links':
            query, args = 'SELECT l.* FROM task_links l JOIN tasks s ON s.id=l.source_id JOIN tasks t ON t.id=l.target_id WHERE s.project_id=? AND t.project_id=?', (project_id,project_id)
        else:
            query, args = f'SELECT * FROM {table} WHERE project_id=?', (project_id,)
        values = [_encode(r) for r in db.execute(query,args)]
        rows[table] = sorted(values,key=lambda r:_key(schema,table,r))
    history = [dict(r) for r in db.execute('SELECT * FROM workspace_events WHERE project_id=? ORDER BY id',(project_id,))]
    for event in history:
        event.pop('sequence',None)
    contributors={r.get('created_by') for records in rows.values() for r in records}
    contributors.update(r.get('reviewer_id') for records in rows.values() for r in records)
    contributors.update(e.get('actor_id') for e in history)
    contributors.discard(None)
    principals={r['id']:r for r in rows['principals']}
    for identity in contributors:
        found=db.execute('SELECT * FROM principals WHERE id=?',(identity,)).fetchone()
        if found is not None: principals[identity]=_encode(found)
    rows['principals']=sorted(principals.values(),key=lambda r:_key(schema,'principals',r))
    bundle = {'format':'ssg2-exchange','version':1,'project_id':project_id,'schema':schema,'ancestor_id':ancestor_id,'records':rows,'history':history}
    bundle['id'] = research.digest(bundle)
    return bundle


def _validate(db, project, bundle):
    if not isinstance(bundle,dict) or set(bundle) != {'format','version','project_id','schema','ancestor_id','records','history','id'}:
        research.error(400,'Invalid exchange envelope')
    content = {k:v for k,v in bundle.items() if k!='id'}
    if bundle['id'] != research.digest(content):
        research.error(400,'Exchange content hash mismatch')
    if bundle['format']!='ssg2-exchange' or bundle['version']!=1 or bundle['project_id']!=project or bundle['schema']!=_schema(db):
        research.error(409,'Exchange project or schema mismatch')
    if set(bundle['records'])!=set(TABLES):
        research.error(400,'Exchange table whitelist mismatch')
    for table, rows in bundle['records'].items():
        seen=set()
        for row in rows:
            if set(row)!={c['name'] for c in bundle['schema'][table]} or ('project_id' in row and row['project_id']!=project) or (table=='projects' and row['id']!=project):
                research.error(400,'Foreign project or unknown columns in exchange')
            key=_key(bundle['schema'],table,row)
            if key in seen: research.error(400,'Duplicate exchange identity')
            seen.add(key)
            if table=='artifact_blobs':
                try: raw=base64.b64decode(row['body']['base64'],validate=True)
                except (ValueError,KeyError,TypeError): research.error(400,'Invalid blob encoding')
                if hashlib.sha256(raw).hexdigest()!=row['sha256']: research.error(400,'Artifact blob hash mismatch')
            if table=='document_versions' and hashlib.sha256(row['body'].encode()).hexdigest()!=row['sha256']:
                research.error(400,'Document hash mismatch')
            if table=='tasks' and any(k in json.loads(row['record_json']) for k in ('claim','reserved_paths','session_id','lease')):
                research.error(400,'Live ownership cannot be imported')
    if any(e.get('project_id')!=project for e in bundle['history']): research.error(400,'Foreign project history')
    task_ids={r['id'] for r in bundle['records']['tasks']}
    if any(r['source_id'] not in task_ids or r['target_id'] not in task_ids for r in bundle['records']['task_links']):
        research.error(400,'Foreign task-link endpoint')
    area_ids={r['id'] for r in bundle['records']['areas']}
    principal_ids={r['id'] for r in bundle['records']['principals']}
    for table,rows in bundle['records'].items():
        for row in rows:
            if row.get('created_by') is not None and row['created_by'] not in principal_ids:
                research.error(400,'Unscoped contributor reference')
            if row.get('reviewer_id') is not None and row['reviewer_id'] not in principal_ids:
                research.error(400,'Unscoped reviewer reference')
            if table=='tasks' and ((row['parent_id'] is not None and row['parent_id'] not in task_ids) or (row['area_id'] is not None and row['area_id'] not in area_ids)):
                research.error(400,'Foreign task parent or area')
    validations={r['id']:r for r in bundle['records']['validation_records']}
    for row in bundle['records']['tasks']:
        local=db.execute('SELECT uid,project_id FROM tasks WHERE id=?',(row['id'],)).fetchone()
        if local is not None and (local['uid']!=row['uid'] or local['project_id']!=row['project_id']):
            research.error(409,'Task identity collision cannot alter uid or project')
        if row['acceptance_state']=='passed':
            validation=validations.get(row['validation_id'])
            if not validation or validation['task_id']!=row['uid'] or validation['outcome']!='passed' or validation['gate_revision']!=row['gate_revision']:
                research.error(400,'Accepted task lacks its matching validation gate')


def _index(bundle,table):
    return {_key(bundle['schema'],table,r):r for r in bundle['records'][table]}


def _fields(row):
    if row is None: return {}
    result=dict(row)
    if 'record_json' in result:
        result.update({'record_json.'+k:v for k,v in json.loads(result.pop('record_json')).items()})
    return result


def preview_import(db, project_id, bundle):
    _validate(db,project_id,bundle)
    local=export_bundle(db,project_id)
    ancestor=None
    if bundle['ancestor_id']:
        saved=db.execute('SELECT bundle_json FROM exchange_snapshots WHERE project_id=? AND id=?',(project_id,bundle['ancestor_id'])).fetchone()
        if not saved: research.error(409,'Unknown shared ancestor; register the common snapshot first')
        ancestor=json.loads(saved[0])
    changes=[]
    for table in TABLES:
        ours=_index(local,table); base=_index(ancestor,table) if ancestor else {}
        for key,theirs in _index(bundle,table).items():
            current=ours.get(key); original=base.get(key)
            current_fields=_fields(current); original_fields=_fields(original); remote_fields=_fields(theirs)
            fields={}
            missing=object()
            for name in sorted(set(current_fields)|set(remote_fields)|set(original_fields)):
                local_value=current_fields.get(name,missing); value=remote_fields.get(name,missing); old=original_fields.get(name,missing)
                if current is None or local_value==value: continue
                fields[name]={'base':None if old is missing else old,'local':None if local_value is missing else local_value,'remote':None if value is missing else value,
                              'base_present':old is not missing,'local_present':local_value is not missing,'remote_present':value is not missing,
                              'conflict':table in IMMUTABLE or original is None or (local_value!=old and value!=old),
                              'take_remote':original is None or value!=old}
            status='addition' if current is None else ('identical' if not fields else ('conflict' if any(f['conflict'] for f in fields.values()) else 'merge'))
            changes.append({'table':table,'key':key,'status':status,'fields':fields})
    available={r['sha256'] for r in local['records']['artifact_blobs']+bundle['records']['artifact_blobs']}
    missing=sorted({r['sha256'] for r in bundle['records']['artifact_versions'] if r['sha256'] not in available})
    return {'id':bundle['id'],'ancestor_id':bundle['ancestor_id'],'changes':changes,'missing_sources':missing}


def apply_operation(db, project, principal, op, request_id):
    if not db.in_transaction: research.error(409,'Exchange requires caller-owned transaction')
    membership=db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(project,principal)).fetchone()
    if not membership or not set(json.loads(membership[0])) & {'contribute','admin'}: research.error(403,'Contributor capability required')
    bundle=op.get('bundle'); _validate(db,project,bundle)
    name=op.get('op')
    if name not in OPS: research.error(400,'Unknown exchange operation')
    if name=='register_exchange_snapshot':
        db.execute('INSERT OR IGNORE INTO exchange_snapshots VALUES(?,?,?)',(project,bundle['id'],canonical(bundle)))
        return {'type':'exchange_snapshot','id':bundle['id']}
    prior=db.execute('SELECT result_json FROM exchange_imports WHERE project_id=? AND id=?',(project,bundle['id'])).fetchone()
    if prior: return json.loads(prior[0])
    preview=preview_import(db,project,bundle)
    choices=op.get('choices',{})
    for change in preview['changes']:
        for field, alternatives in change['fields'].items():
            token=canonical([change['table'],change['key'],field])
            if alternatives['conflict'] and choices.get(token) not in ('local','remote'):
                research.error(409,'Explicit local/remote conflict choice required: '+token)
            if change['table'] in IMMUTABLE and alternatives['conflict'] and choices.get(token)=='remote':
                research.error(409,'Immutable identity collision requires a new source identity')
    # Defer relational checks so circular current-version references can roundtrip.
    db.execute('PRAGMA defer_foreign_keys=ON')
    db.execute('SAVEPOINT exchange_apply')
    try:
        for change in preview['changes']:
            table=change['table']; incoming=_index(bundle,table)[change['key']]
            row=dict(incoming)
            if change['status']=='identical': continue
            if change['status']!='addition':
                for field,alt in change['fields'].items():
                    token=canonical([table,change['key'],field])
                    remote=choices.get(token)=='remote' if alt['conflict'] else alt['take_remote']
                    value=alt['remote'] if remote else alt['local']
                    if field.startswith('record_json.'):
                        record=json.loads(row['record_json']); key=field[len('record_json.'):]
                        if alt['remote_present' if remote else 'local_present']: record[key]=value
                        else: record.pop(key,None)
                        row['record_json']=canonical(record)
                    else: row[field]=value
                if table in IMMUTABLE: continue
            if table=='tasks' and change['status']!='addition':
                # Existing local leases survive content merges and cannot be
                # released by a sanitized foreign snapshot.
                local_record=json.loads(db.execute('SELECT record_json FROM tasks WHERE uid=? AND project_id=?',(row['uid'],project)).fetchone()[0])
                record=json.loads(row['record_json'])
                for field in ('claim','reserved_paths','session_id','lease'):
                    if field in local_record: record[field]=local_record[field]
                row['record_json']=canonical(record)
            if table=='artifact_blobs': row['body']=base64.b64decode(row['body']['base64'])
            columns=list(row); pk=[c['name'] for c in bundle['schema'][table] if c['pk']]
            if change['status']=='addition':
                db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",tuple(row.values()))
            else:
                fields=[c for c in columns if c not in pk]
                db.execute(f"UPDATE {table} SET {','.join(c+'=?' for c in fields)} WHERE {' AND '.join(c+'=?' for c in pk)}",tuple(row[c] for c in fields+pk))
        if db.execute('PRAGMA foreign_key_check').fetchone(): research.error(409,'Exchange contains missing or foreign references')
        # A field merge can alter a validation's gate or reference. Recheck the
        # resulting accepted state, rather than trusting either side in isolation.
        invalid=db.execute("SELECT 1 FROM tasks t LEFT JOIN validation_records v ON v.project_id=t.project_id AND v.id=t.validation_id WHERE t.project_id=? AND t.acceptance_state='passed' AND (v.id IS NULL OR v.task_id<>t.uid OR v.outcome<>'passed' OR v.gate_revision<>t.gate_revision)",(project,)).fetchone()
        if invalid: research.error(409,'Merged accepted task lacks its matching validation gate')
        result={'type':'exchange_import','id':bundle['id'],'preview':preview,'choices':choices}
        db.execute('INSERT OR IGNORE INTO exchange_snapshots VALUES(?,?,?)',(project,bundle['id'],canonical(bundle)))
        db.execute('INSERT INTO exchange_imports VALUES(?,?,?)',(project,bundle['id'],canonical(result)))
        research_event={'bundle_id':bundle['id'],'choices':choices,'alternatives':preview}
        try:
            from . import roadmap_documents
        except ImportError:
            import roadmap_documents
        roadmap_documents._event(db,project,principal,request_id,'exchange',bundle['id'],'import_bundle',research_event)
        db.execute('RELEASE exchange_apply')
        return result
    except Exception:
        db.execute('ROLLBACK TO exchange_apply'); db.execute('RELEASE exchange_apply')
        raise
