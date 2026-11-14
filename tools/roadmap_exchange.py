"""Deterministic Git-transport data, never SQLite files or live ownership.

All writes belong to the caller's revision-checked transaction. Historical remote
events and alternatives are retained in immutable snapshots, not replayed as local
transactions. Membership, capabilities, sessions and delivery queues stay local.
"""
import base64
import hashlib
import json
import re

try:
    from . import roadmap_research as research, workspace_task_namespace as task_namespace
except ImportError:
    import roadmap_research as research, workspace_task_namespace as task_namespace

OPS = {'register_exchange_snapshot', 'import_bundle'}
RESOURCES = {'exchange'}
TABLES = tuple('projects principals areas area_paths tasks task_links containment entity_aliases attempts validation_records task_blockers merge_records task_redirects guidance_links artifact_roots artifact_blobs artifacts artifact_versions index_runs index_run_inputs index_batches index_errors occurrences observations run_observations observation_reconciliations occurrence_edges questions question_revisions negative_searches registry_imports registry_records findings finding_revisions evidence_links answers documents document_versions discussion_comments edit_proposals task_journal_entries merge_compensations finding_revision_lineage evidence_change_events research_review_notices answer_contexts answer_reviews question_answer_selections question_resolution_events git_source_manifests'.split())
IMMUTABLE = set('answers principals entity_aliases validation_records artifact_blobs artifact_versions occurrences observations question_revisions negative_searches registry_imports registry_records finding_revisions document_versions discussion_comments index_batches task_journal_entries merge_compensations finding_revision_lineage evidence_change_events research_review_notices answer_contexts answer_reviews question_resolution_events git_source_manifests'.split())
# Only mutable relationships have row-removal semantics. Evidence/history rows
# remain append-only; unknown-base omissions never establish a deletion.
PRESENCE_TABLES = {'task_links'}
DECLARED_ANCESTOR = object()
canonical = research.canonical
_PORTABLE_FORMAT = 'workbench-exchange'
_PORTABLE_HISTORY = {'id','project_id','request_id','actor_id','entity_type','entity_id','kind','recorded_at','payload_sha256'}
_HOST_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\|(?:^|[\s(=])/(?!/))")


def _portable_path_check(value, known_roots):
    if isinstance(value, dict):
        for key,item in value.items():
            _portable_path_check(key, known_roots)
            _portable_path_check(item, known_roots)
    elif isinstance(value, list):
        for item in value: _portable_path_check(item, known_roots)
    elif isinstance(value, str):
        # Inspect decoded JSON before path syntax: nested canonical action keys
        # escape quotes with doubled backslashes, which are not UNC paths.
        if value.startswith(('{','[')):
            try: nested=json.loads(value)
            except ValueError: pass
            else:
                _portable_path_check(nested, known_roots)
                return
        if any(root and (root in value or root.replace('\\','/') in value) for root in known_roots) or _HOST_PATH.search(value):
            research.error(409, 'Portable exchange contains a host path; review source text before sharing')



def _retain_portable_history(db,project,events):
    for event in events:
        if not isinstance(event,dict) or set(event)!=_PORTABLE_HISTORY or event['project_id']!=project:
            research.error(409,'Portable history metadata is incompatible')
        if event['kind']=='import_bundle':continue
        body=canonical(event)
        existing=db.execute('SELECT body_json FROM portable_event_history WHERE project_id=? AND id=?',(project,event['id'])).fetchone()
        if existing and existing[0]!=body:research.error(409,'Immutable portable history identity collision')
        db.execute('INSERT OR IGNORE INTO portable_event_history VALUES(?,?,?)',(project,event['id'],body))


JOURNAL_KINDS = {'observation','attempt','blocker','review','handoff'}


def retain_task_journal(db, event_id, principal_id=None):
    event=db.execute('SELECT * FROM events WHERE event_id=?',(event_id,)).fetchone()
    if event is None or event['kind'] not in JOURNAL_KINDS:return
    task=db.execute('SELECT uid,project_id FROM tasks WHERE id=?',(event['task_id'],)).fetchone()
    if task is None:return
    raw=dict(event)
    origin=db.execute('SELECT store_id FROM store_meta').fetchone()[0]
    db.execute('INSERT OR IGNORE INTO task_journal_entries VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (event_id,task['project_id'],task['uid'],principal_id,event['kind'],event['recorded_at'],
         event['historical_at'],event['details_json'],origin,canonical(raw),research.digest(raw)))


def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS task_journal_entries(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),task_id TEXT NOT NULL,created_by TEXT REFERENCES principals(id),kind TEXT NOT NULL,recorded_at TEXT NOT NULL,historical_at TEXT,details_json TEXT NOT NULL,origin_store_id TEXT NOT NULL,source_event_json TEXT NOT NULL,source_sha256 TEXT NOT NULL,FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid))')
    db.execute('CREATE INDEX IF NOT EXISTS task_journal_history ON task_journal_entries(project_id,task_id,recorded_at,id)')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS task_journal_entries_{action.lower()} BEFORE {action} ON task_journal_entries BEGIN SELECT RAISE(ABORT,'task journals are immutable'); END")
    for event in db.execute('SELECT e.event_id,e.actor_json,e.request_id,t.project_id,t.uid FROM events e JOIN tasks t ON t.id=e.task_id WHERE e.kind IN (?,?,?,?,?)',tuple(sorted(JOURNAL_KINDS))).fetchall():
        if db.execute('SELECT 1 FROM task_journal_entries WHERE id=?',(event[0],)).fetchone():continue
        known=db.execute("SELECT actor_id FROM workspace_events WHERE project_id=? AND request_id=? AND entity_type='task' AND entity_id=? AND actor_id IS NOT NULL ORDER BY sequence LIMIT 1",(event[3],event[2],event[4])).fetchone()
        # Preserve authenticated event attribution when available. A historical
        # display label must never create membership or confer capabilities.
        retain_task_journal(db,event[0],known[0] if known else None)
    db.execute('CREATE TABLE IF NOT EXISTS exchange_snapshots(project_id TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,bundle_json TEXT NOT NULL,PRIMARY KEY(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS exchange_imports(project_id TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,result_json TEXT NOT NULL,PRIMARY KEY(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS portable_event_history(project_id TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,body_json TEXT NOT NULL,PRIMARY KEY(project_id,id))')
    for table in ('exchange_snapshots', 'exchange_imports','portable_event_history'):
        for action in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'exchange history is immutable'); END")
    # Upgrade previously applied v2 snapshots without inventing local event rows.
    for row in db.execute('SELECT s.project_id,s.bundle_json FROM exchange_snapshots s JOIN exchange_imports i ON i.project_id=s.project_id AND i.id=s.id'):
        bundle=json.loads(row[1])
        if (bundle.get('format'),bundle.get('version'))==(_PORTABLE_FORMAT,2):
            _validate(db,row[0],bundle)
            _retain_portable_history(db,row[0],bundle['history'])



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


def export_bundle(db, project_id, ancestor_id=None, *, portable=False):
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
        if portable and table == 'artifact_roots':
            for row in values: row['path'] = 'workspace-root:' + row['id']
        if portable and table == 'artifact_blobs': values = []
        rows[table] = sorted(values,key=lambda r:_key(schema,table,r))
    history = [dict(r) for r in db.execute('SELECT * FROM workspace_events WHERE project_id=? ORDER BY id',(project_id,))]
    for event in history:
        event.pop('sequence',None)
    if portable:
        history = [event for event in history if event['kind'] not in {'bind_root','hydrate_artifact_version','repository_snapshot','register_repository','import_bundle','import_publication','repository_index_publication'}]
        history = [{**{key:event[key] for key in _PORTABLE_HISTORY - {'payload_sha256'}},
                    'payload_sha256':research.digest({key:event.get(key) for key in ('before_json','after_json','rationale')})}
                   for event in history]
        retained={row['id']:json.loads(row['body_json']) for row in db.execute('SELECT id,body_json FROM portable_event_history WHERE project_id=?',(project_id,))}
        for event in history:
            if event['id'] in retained and retained[event['id']]!=event:research.error(409,'Immutable portable history identity collision')
            retained[event['id']]=event
        history=[retained[key] for key in sorted(retained)]
    contributors={r.get('created_by') for records in rows.values() for r in records}
    contributors.update(r.get('reviewer_id') for records in rows.values() for r in records)
    contributors.update(e.get('actor_id') for e in history)
    contributors.discard(None)
    principals={r['id']:r for r in rows['principals']}
    for identity in contributors:
        found=db.execute('SELECT * FROM principals WHERE id=?',(identity,)).fetchone()
        if found is not None: principals[identity]=_encode(found)
    rows['principals']=sorted(principals.values(),key=lambda r:_key(schema,'principals',r))
    task_namespace.export_records(db,project_id,rows)
    try:
        from . import workspace_findings
    except ImportError:
        import workspace_findings
    workspace_findings.export_lineage(project_id,rows)
    bundle = {'format':_PORTABLE_FORMAT if portable else 'ssg2-exchange','version':2 if portable else 1,'project_id':project_id,'schema':schema,'ancestor_id':ancestor_id,'records':rows,'history':history}
    if portable:
        roots=[row[0] for row in db.execute('SELECT path FROM artifact_roots WHERE project_id=?',(project_id,)) if not row[0].startswith('workspace-root:')]
        _portable_path_check({'records':rows,'history':history}, roots)
    bundle['id'] = research.digest(bundle)
    return bundle


def _compatible_schema(db,schema):
    current=_schema(db)
    return (set(current)-set(schema)<={'task_journal_entries','merge_compensations','finding_revision_lineage','evidence_change_events','research_review_notices','answer_contexts','answer_reviews','question_answer_selections','question_resolution_events','git_source_manifests'}
            and not set(schema)-set(current) and all(schema[key]==current[key] for key in schema))


def _validate(db, project, bundle):
    if not isinstance(bundle,dict) or set(bundle) != {'format','version','project_id','schema','ancestor_id','records','history','id'}:
        research.error(400,'Invalid exchange envelope')
    if (type(bundle['version']) is not int or not isinstance(bundle['format'],str)
            or not isinstance(bundle['id'],str) or not re.fullmatch('[0-9a-f]{64}',bundle['id'])
            or not isinstance(bundle['records'],dict) or not isinstance(bundle['schema'],dict)
            or not isinstance(bundle['history'],list) or any(not isinstance(event,dict) for event in bundle['history'])
            or any(not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows) for rows in bundle['records'].values())):
        research.error(400,'Malformed exchange collections or version')
    content = {k:v for k,v in bundle.items() if k!='id'}
    if bundle['id'] != research.digest(content):
        research.error(400,'Exchange content hash mismatch')
    portable=(bundle['format'],bundle['version'])==(_PORTABLE_FORMAT,2)
    if ((not portable and (bundle['format'],bundle['version'])!=('ssg2-exchange',1))
            or bundle['project_id']!=project or not _compatible_schema(db,bundle['schema'])):
        research.error(409,'Exchange project or schema mismatch')
    if set(bundle['records'])!=set(bundle['schema']):
        research.error(400,'Exchange table whitelist mismatch')
    if portable:
        if bundle['records']['artifact_blobs'] or not isinstance(bundle['history'],list) or any(not isinstance(e,dict) or set(e)!=_PORTABLE_HISTORY or not isinstance(e['payload_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',e['payload_sha256']) for e in bundle['history']):
            research.error(400,'Portable exchange contains raw bytes or history')
        if any(not isinstance(r,dict) or not isinstance(r.get('id'),str) or r.get('path')!='workspace-root:'+r['id'] for r in bundle['records']['artifact_roots']):
            research.error(400,'Portable roots must use logical identities')
        _portable_path_check({'records':bundle['records'],'history':bundle['history']}, ())
    for table, rows in bundle['records'].items():
        seen=set()
        for row in rows:
            if set(row)!={c['name'] for c in bundle['schema'][table]} or ('project_id' in row and row['project_id']!=project) or (table=='projects' and row['id']!=project):
                research.error(400,'Foreign project or unknown columns in exchange')
            for column in bundle['schema'][table]:
                value=row[column['name']]
                if value is None:
                    if column['notnull'] or column['pk']:research.error(400,'Missing required exchange value')
                elif column['type'].upper()=='TEXT' and not isinstance(value,str):
                    research.error(400,'Invalid exchange text value')
                elif column['type'].upper()=='INTEGER' and type(value) is not int:
                    research.error(400,'Invalid exchange integer value')
            if table=='tasks':
                try:
                    if not isinstance(json.loads(row['record_json']),dict):raise ValueError()
                except (ValueError,TypeError):research.error(400,'Task exchange record must be an object')
            key=_key(bundle['schema'],table,row)
            if key in seen: research.error(400,'Duplicate exchange identity')
            seen.add(key)
            if table=='artifact_blobs':
                try: raw=base64.b64decode(row['body']['base64'],validate=True)
                except (ValueError,KeyError,TypeError): research.error(400,'Invalid blob encoding')
                if hashlib.sha256(raw).hexdigest()!=row['sha256']: research.error(400,'Artifact blob hash mismatch')
            if table=='merge_compensations':
                try:
                    from . import workspace_merge_compensation
                except ImportError:
                    import workspace_merge_compensation
                workspace_merge_compensation.validate_public_audit(row,project,{task.get('uid') for task in bundle['records']['tasks'] if isinstance(task.get('uid'),str)})
            if table=='task_journal_entries':
                try:
                    source=json.loads(row['source_event_json']);details=json.loads(row['details_json'])
                    actor=json.loads(source['actor_json']) if isinstance(source,dict) and source.get('actor_json') else None
                    if (not isinstance(source,dict) or not isinstance(details,dict)
                            or set(source)!={'sequence','event_id','request_id','task_id','actor_json','recorded_at','historical_at','kind','details_json'}
                            or type(source.get('sequence')) is not int or source['sequence']<1
                            or not isinstance(source.get('request_id'),str) or not isinstance(source.get('task_id'),str)
                            or (actor is not None and (not isinstance(actor,dict) or set(actor)!={'name','kind'} or not isinstance(actor['name'],str) or actor['kind'] not in ('human','agent')))
                            or source.get('event_id')!=row['id'] or source.get('kind')!=row['kind']
                            or row['kind'] not in JOURNAL_KINDS or source.get('recorded_at')!=row['recorded_at']
                            or source.get('historical_at')!=row['historical_at'] or source.get('details_json')!=row['details_json']
                            or research.digest(source)!=row['source_sha256']):raise ValueError()
                except (ValueError,TypeError,KeyError):research.error(400,'Journal source provenance mismatch')
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
    try:
        from . import workspace_findings
    except ImportError:
        import workspace_findings
    workspace_findings.validate_bundle(db,project,bundle['records'])
    try:
        from . import workspace_answers
    except ImportError:
        import workspace_answers
    workspace_answers.validate_bundle(project,bundle['records'])
    try:
        from . import workspace_source_index
    except ImportError:
        import workspace_source_index
    workspace_source_index.validate_bundle(project,bundle['records'])
    validations={r['id']:r for r in bundle['records']['validation_records']}
    task_namespace.validate_tasks(db,project,bundle['records']['tasks'])
    for row in bundle['records']['tasks']:
        if row['acceptance_state']=='passed':
            validation=validations.get(row['validation_id'])
            if not validation or validation['task_id']!=row['uid'] or validation['outcome']!='passed' or validation['gate_revision']!=row['gate_revision']:
                research.error(400,'Accepted task lacks its matching validation gate')


def _index(bundle,table):
    return {_key(bundle['schema'],table,r):r for r in bundle['records'].get(table,[])}


def _fields(row):
    if row is None: return {}
    result=dict(row)
    if 'record_json' in result:
        # Dependency arrays mirror task_links; row-presence review owns their
        # meaning. Do not ask for contradictory choices in a derived copy.
        result.update({'record_json.'+k:v for k,v in json.loads(result.pop('record_json')).items() if k!='dependencies'})
    return result


def preview_import(db, project_id, bundle, *, merge_base_id=DECLARED_ANCESTOR):
    _validate(db,project_id,bundle)
    portable=bundle['format']==_PORTABLE_FORMAT
    local=export_bundle(db,project_id,portable=portable)
    ancestor=None
    effective_base=bundle['ancestor_id'] if merge_base_id is DECLARED_ANCESTOR else merge_base_id
    if effective_base is not None and (not isinstance(effective_base,str) or not re.fullmatch('[0-9a-f]{64}',effective_base)):
        research.error(400,'Invalid reviewed merge base')
    if effective_base:
        saved=db.execute('SELECT bundle_json FROM exchange_snapshots WHERE project_id=? AND id=?',(project_id,effective_base)).fetchone()
        if not saved: research.error(409,'Unknown shared ancestor; register the common snapshot first')
        ancestor=json.loads(saved[0])
        if (ancestor['format'],ancestor['version']) != (bundle['format'],bundle['version']):
            research.error(409,'Shared ancestor uses another exchange format')
    changes=[]
    for table in TABLES:
        ours=_index(local,table); base=_index(ancestor,table) if ancestor else {}
        incoming=_index(bundle,table)
        keys=set(incoming)|set(base) if table in PRESENCE_TABLES else set(incoming)
        for key in sorted(keys):
            theirs=incoming.get(key);current=ours.get(key);original=base.get(key)
            if table in PRESENCE_TABLES and original is not None and (current is None or theirs is None):
                fields={}
                if current==theirs:status='identical'
                elif current is None and theirs==original:status='keep-deleted'
                else:
                    conflict=current!=original and theirs!=original
                    fields={'$presence':{'base':original,'local':current,'remote':theirs,
                            'base_present':True,'local_present':current is not None,'remote_present':theirs is not None,
                            'conflict':conflict,'take_remote':theirs!=original}}
                    status='conflict' if conflict else 'deletion'
                changes.append({'table':table,'key':key,'status':status,'fields':fields})
                continue
            current_fields=_fields(current); original_fields=_fields(original); remote_fields=_fields(theirs)
            fields={}
            missing=object()
            for name in sorted(set(current_fields)|set(remote_fields)|set(original_fields)):
                # CAS counters and last-update metadata are installation-local
                # preconditions, not competing domain values from another writer.
                if table not in IMMUTABLE and name in {'revision','structure_revision','updated_at'}:continue
                if name=='created_at':
                    try:
                        from . import workspace_source_index
                    except ImportError:
                        import workspace_source_index
                    if all(workspace_source_index.generated_first_seen(table,row) for row in (current,theirs) if row is not None):continue
                local_value=current_fields.get(name,missing); value=remote_fields.get(name,missing); old=original_fields.get(name,missing)
                if current is None or local_value==value: continue
                fields[name]={'base':None if old is missing else old,'local':None if local_value is missing else local_value,'remote':None if value is missing else value,
                              'base_present':old is not missing,'local_present':local_value is not missing,'remote_present':value is not missing,
                              'conflict':table in IMMUTABLE or original is None or (local_value!=old and value!=old),
                              'take_remote':original is None or value!=old}
            status='addition' if current is None else ('identical' if not fields else ('conflict' if any(f['conflict'] for f in fields.values()) else 'merge'))
            changes.append({'table':table,'key':key,'status':status,'fields':fields})
    available={r[0] for r in db.execute('SELECT sha256 FROM artifact_blobs WHERE project_id=?',(project_id,))}
    available.update(r['sha256'] for r in bundle['records']['artifact_blobs'])
    missing=sorted({r['sha256'] for r in bundle['records']['artifact_versions'] if r['sha256'] not in available})
    return {'id':bundle['id'],'ancestor_id':bundle['ancestor_id'],'merge_base_id':effective_base,'changes':changes,'missing_sources':missing}


def _acyclic(nodes, edges, label):
    outgoing = {node: [] for node in nodes}
    incoming = dict.fromkeys(nodes, 0)
    for source, target in edges:
        if source not in nodes or target not in nodes or source == target:
            research.error(409, 'Invalid merged ' + label + ' reference')
        outgoing[source].append(target)
        incoming[target] += 1
    pending = [node for node, count in incoming.items() if not count]
    visited = 0
    while pending:
        node = pending.pop(); visited += 1
        for target in outgoing[node]:
            incoming[target] -= 1
            if not incoming[target]: pending.append(target)
    if visited != len(nodes): research.error(409, 'Merged ' + label + ' cycle requires review')


def _review_merged_import(db, project, previous, previous_scope):
    """Imported reviews are history, never a local grant of acceptance."""
    current = {row['uid']: dict(row) for row in db.execute('SELECT * FROM tasks WHERE project_id=?', (project,))}
    for uid, row in current.items():
        old = previous.get(uid)
        if old is not None and old == row: continue
        record = json.loads(row['record_json'])
        if row['acceptance_state'] == 'passed' or record.get('status') == 'done' or record.get('acceptance_gate_passed'):
            record['status'] = 'review'
            record['acceptance_gate_passed'] = False
            db.execute("UPDATE tasks SET record_json=?,acceptance_state='invalidated',gate_revision=gate_revision+1 WHERE uid=? AND project_id=?",
                       (canonical(record), uid, project))
    tasks = {row['uid']: dict(row) for row in db.execute('SELECT * FROM tasks WHERE project_id=?', (project,))}
    current_scope = {table: {tuple(row) for row in db.execute(query, (project,))}
                     for table, query in (
                         ('containment', 'SELECT child_id,parent_id,required_for_parent FROM containment WHERE project_id=?'),
                         ('dependencies', "SELECT l.source_id,l.target_id,l.kind FROM task_links l JOIN tasks t ON t.id=l.source_id WHERE t.project_id=? AND l.kind='depends_on'"))}
    if current_scope != previous_scope:
        changed_parents = {item[1] for item in current_scope['containment'] ^ previous_scope['containment'] if item[1]}
        changed_sources = {item[0] for item in current_scope['dependencies'] ^ previous_scope['dependencies']}
        legacy = {row['id']: row for row in tasks.values()}
        for row in [tasks[uid] for uid in changed_parents if uid in tasks] + [legacy[key] for key in changed_sources if key in legacy]:
            if row['acceptance_state'] == 'passed' or json.loads(row['record_json']).get('status') == 'done':
                research.error(409, 'Reopen or revalidate accepted scope before importing relationship changes')
    by_legacy = {row['id']: row['uid'] for row in tasks.values()}
    _acyclic(set(by_legacy), [(row['id'], row['parent_id']) for row in tasks.values() if row['parent_id']], 'task parent')
    areas = {row['id']: dict(row) for row in db.execute('SELECT * FROM areas WHERE project_id=?', (project,))}
    _acyclic(set(areas), [(row['id'], row['parent_id']) for row in areas.values() if row['parent_id']], 'area parent')
    containment = list(db.execute('SELECT child_id,parent_id,required_for_parent FROM containment WHERE project_id=?', (project,)))
    _acyclic(set(tasks), [(row['child_id'], row['parent_id']) for row in containment if row['parent_id']], 'containment')
    for edge in containment:
        child = tasks.get(edge['child_id'])
        if child is None or (by_legacy.get(child['parent_id']) if child['parent_id'] else None) != edge['parent_id']:
            research.error(409, 'Merged containment disagrees with task parent')
    for row in tasks.values():
        record = json.loads(row['record_json'])
        if row['acceptance_state'] == 'passed':
            validation = db.execute('SELECT * FROM validation_records WHERE project_id=? AND id=?', (project, row['validation_id'])).fetchone()
            reviewer = validation and db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?', (project, validation['reviewer_id'])).fetchone()
            if not validation or not reviewer or 'review' not in json.loads(reviewer[0]) or validation['task_id'] != row['uid'] or validation['outcome'] != 'passed' or validation['gate_revision'] != row['gate_revision'] or row['created_by'] == validation['reviewer_id']:
                research.error(409, 'Merged acceptance lacks trusted local independent review')
        if record.get('status') == 'done':
            if row['acceptance_state'] != 'passed': research.error(409, 'Completed task lacks current local gate')
            children = [edge for edge in containment if edge['parent_id'] == row['uid']]
            if children and not row['decomposition_complete']: research.error(409, 'Completed parent has undeclared scope')
            for edge in children:
                child = tasks[edge['child_id']]
                if edge['required_for_parent'] and (child['archive_reason'] is not None or json.loads(child['record_json']).get('status') != 'done' or child['acceptance_state'] != 'passed'):
                    research.error(409, 'Completed parent has unaccepted required child')
    links = [(row['source_id'], row['target_id']) for row in db.execute("SELECT l.source_id,l.target_id FROM task_links l JOIN tasks t ON t.id=l.source_id WHERE t.project_id=? AND l.kind='depends_on'", (project,))]
    _acyclic(set(by_legacy), links, 'dependency')


def apply_operation(db, project, principal, op, request_id, *, review_import=False):
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
    try:
        from . import workspace_concurrency
    except ImportError:
        import workspace_concurrency
    heads_before=workspace_concurrency.capture_heads(db,project)
    preview=preview_import(db,project,bundle,merge_base_id=op.get('merge_base_id',DECLARED_ANCESTOR))
    previous = {row['uid']: dict(row) for row in db.execute('SELECT * FROM tasks WHERE project_id=?', (project,))} if review_import else None
    previous_scope = {table: {tuple(row) for row in db.execute(query, (project,))}
                      for table, query in (
                          ('containment', 'SELECT child_id,parent_id,required_for_parent FROM containment WHERE project_id=?'),
                          ('dependencies', "SELECT l.source_id,l.target_id,l.kind FROM task_links l JOIN tasks t ON t.id=l.source_id WHERE t.project_id=? AND l.kind='depends_on'"))} if review_import else None
    choices=op.get('choices',{})
    if not isinstance(choices, dict): research.error(400, 'Exchange choices must be an object')
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
        local_schema=_schema(db)
        task_keys,task_bindings=task_namespace.prepare_import(db,project,bundle['records']['tasks'])
        for binding in task_bindings:
            db.execute('INSERT OR IGNORE INTO task_exchange_keys VALUES(?,?,?)',binding)
        incoming_tables={table:_index(bundle,table) for table in {change['table'] for change in preview['changes']}}
        for change in preview['changes']:
            table=change['table']
            if change['status'] in ('identical','keep-deleted'):continue
            if table in PRESENCE_TABLES and '$presence' in change['fields']:
                alternatives=change['fields']['$presence']
                token=canonical([table,change['key'],'$presence'])
                remote=choices.get(token)=='remote' if alternatives['conflict'] else alternatives['take_remote']
                selected=alternatives['remote'] if remote else alternatives['local']
                primary=[column['name'] for column in local_schema[table] if column['pk']]
                if selected is None:
                    db.execute(f"DELETE FROM {table} WHERE {' AND '.join(column+'=?' for column in primary)}",tuple(task_keys.get(key,key) if table=='task_links' and column in ('source_id','target_id') else key for column,key in zip(primary,json.loads(change['key']))))
                else:
                    if table=='task_links':selected=task_namespace.link_record(selected,task_keys)
                    columns=list(selected);values=tuple(selected[column] for column in columns)
                    mutable=[column for column in columns if column not in primary]
                    db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT({','.join(primary)}) DO UPDATE SET {','.join(column+'=excluded.'+column for column in mutable)}",values)
                continue
            incoming=incoming_tables[table][change['key']]
            row=dict(incoming)
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
            if table=='artifact_roots' and bundle['format']==_PORTABLE_FORMAT and change['status']!='addition':
                row['path']=db.execute('SELECT path FROM artifact_roots WHERE project_id=? AND id=?',(project,row['id'])).fetchone()[0]
            if table=='principals' and change['status']=='addition':
                # Principal IDs are global. A principal outside this project's
                # portable scope may already exist locally; reuse only identical
                # immutable identity, without creating project membership.
                existing=db.execute('SELECT * FROM principals WHERE id=?',(row['id'],)).fetchone()
                if existing is not None:
                    if dict(existing)!=row:
                        research.error(409,'Global principal identity conflicts with local history')
                    continue
            if table=='evidence_links' and change['status']!='addition':
                previous_link=db.execute('SELECT state FROM evidence_links WHERE project_id=? AND id=?',(project,row['id'])).fetchone()
                if previous_link[0]!='available' and row['state']=='available':research.error(409,'Withdrawn evidence cannot be revived by importing an old source link')
            if table=='tasks':row=task_namespace.task_record(row,task_keys)
            elif table=='task_links':row=task_namespace.link_record(row,task_keys)
            if table=='artifact_blobs': row['body']=base64.b64decode(row['body']['base64'])
            columns=list(row); pk=[c['name'] for c in bundle['schema'][table] if c['pk']]
            if change['status']=='addition':
                db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",tuple(row.values()))
            else:
                fields=[c for c in columns if c not in pk]
                db.execute(f"UPDATE {table} SET {','.join(c+'=?' for c in fields)} WHERE {' AND '.join(c+'=?' for c in pk)}",tuple(row[c] for c in fields+pk))
        # Rebuild the compatibility mirror from the final reviewed dependency
        # graph, preserving original_json and every source snapshot unchanged.
        dependencies={}
        for link in db.execute("SELECT l.source_id,l.target_id FROM task_links l JOIN tasks t ON t.id=l.source_id WHERE t.project_id=? AND l.kind='depends_on' ORDER BY l.source_id,l.target_id",(project,)):
            dependencies.setdefault(link['source_id'],[]).append(link['target_id'])
        for task in db.execute('SELECT id,record_json FROM tasks WHERE project_id=?',(project,)).fetchall():
            record=json.loads(task['record_json']);targets=dependencies.get(task['id'],[])
            if ('dependencies' in record or targets) and record.get('dependencies')!=targets:
                record['dependencies']=targets
                db.execute('UPDATE tasks SET record_json=?,revision=revision+1 WHERE project_id=? AND id=?',(canonical(record),project,task['id']))
        if db.execute('PRAGMA foreign_key_check').fetchone(): research.error(409,'Exchange contains missing or foreign references')
        workspace_concurrency.preserve_local_revisions(db,heads_before)
        if review_import: _review_merged_import(db, project, previous, previous_scope)
        # A field merge can alter a validation's gate or reference. Recheck the
        # resulting accepted state, rather than trusting either side in isolation.
        invalid=db.execute("SELECT 1 FROM tasks t LEFT JOIN validation_records v ON v.project_id=t.project_id AND v.id=t.validation_id WHERE t.project_id=? AND t.acceptance_state='passed' AND (v.id IS NULL OR v.task_id<>t.uid OR v.outcome<>'passed' OR v.gate_revision<>t.gate_revision)",(project,)).fetchone()
        if invalid: research.error(409,'Merged accepted task lacks its matching validation gate')
        if (bundle['format'],bundle['version'])==(_PORTABLE_FORMAT,2):_retain_portable_history(db,project,bundle['history'])
        result={'type':'exchange_import','id':bundle['id'],'preview':preview,'choices':choices}
        db.execute('INSERT OR IGNORE INTO exchange_snapshots VALUES(?,?,?)',(project,bundle['id'],canonical(bundle)))
        db.execute('INSERT INTO exchange_imports VALUES(?,?,?)',(project,bundle['id'],canonical(result)))
        try:
            from . import workspace_findings
        except ImportError:
            import workspace_findings
        workspace_findings.after_import(db,project,principal,request_id,preview)
        try:
            from . import workspace_answers
        except ImportError:
            import workspace_answers
        workspace_answers.reconcile(db,project,principal,request_id)
        workspace_concurrency.preserve_local_revisions(db,heads_before)
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
