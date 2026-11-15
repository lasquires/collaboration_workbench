"""Evidence persistence on the caller's transaction; never a second writer.

Source identities and extracted observations are immutable. Publication selects
search visibility, never a preferred interpretation of conflicting observations.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


OPS = {'review_task_sharing', 'review_journal_sharing', 'register_root', 'bind_root', 'register_artifact', 'register_artifact_version',
       'create_index_run', 'stage_batch', 'publish_run', 'create_question',
       'record_negative_search', 'import_variable_registry', 'revise_question', 'create_finding', 'revise_finding', 'archive_finding', 'restore_finding', 'set_evidence_state','create_answer','review_answer','resolve_question','set_question_state'}
RESOURCES = {'task-sharing-original', 'task-sharing-review', 'journal-sharing-review', 'artifacts', 'artifact-versions', 'index-runs', 'occurrences',
             'observations', 'questions', 'negative-searches', 'finding-context', 'question-context', 'answer-context', 'answer-support', 'findings', 'finding-revisions', 'evidence-links', 'evidence-changes', 'research-review-notices','answers','answer-reviews','question-resolutions','source-manifests'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def error(status, message):
    try:
        from .roadmap_store import StoreError
    except ImportError:
        from roadmap_store import StoreError
    raise StoreError(status, message)


def text(value, name, limit=20000):
    if not isinstance(value, str) or not value.strip() or '\0' in value or len(value)>limit:
        error(400, 'Invalid '+name)
    return value


def optional_text(value,name,*,allow_none=True,limit=20000):
    if value is None and allow_none:
        return value
    if not isinstance(value,str) or '\0' in value or len(value)>limit:
        error(400,'Invalid '+name)
    return value


def sha(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        error(400,'Expected lowercase SHA-256')
    return value


def scoped(db, table, project, identity):
    # All callers use constant table names. Values are always bound parameters.
    text(identity,'scoped ID',200)
    row=db.execute(f'SELECT * FROM {table} WHERE project_id=? AND id=?',(project,identity)).fetchone()
    if row is None:
        error(404,'Scoped research record not found')
    return row


def create_schema(db):
    statements = [
        "CREATE TABLE artifact_roots(id TEXT NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),path TEXT NOT NULL,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id))",
        "CREATE TABLE artifact_blobs(project_id TEXT NOT NULL REFERENCES projects(id),sha256 TEXT NOT NULL,body BLOB NOT NULL,PRIMARY KEY(project_id,sha256))",
        "CREATE TABLE artifacts(id TEXT NOT NULL,project_id TEXT NOT NULL,root_id TEXT NOT NULL,relative_path TEXT NOT NULL,kind TEXT NOT NULL,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(project_id,id),UNIQUE(project_id,root_id,relative_path),FOREIGN KEY(project_id,root_id) REFERENCES artifact_roots(project_id,id))",
        "CREATE TABLE artifact_versions(id TEXT NOT NULL,project_id TEXT NOT NULL,artifact_id TEXT NOT NULL,sha256 TEXT NOT NULL,byte_length INTEGER NOT NULL CHECK(byte_length>=0),repository_revision TEXT,binary_build_id TEXT,availability TEXT NOT NULL,source_created_at TEXT,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),UNIQUE(project_id,artifact_id,sha256),FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id))",
        "CREATE TABLE index_runs(id TEXT NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),extractor_id TEXT NOT NULL,extractor_version TEXT NOT NULL,config_hash TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('staging','published','rejected')),coverage_json TEXT NOT NULL,manifest_hash TEXT,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(project_id,id))",
        "CREATE TABLE index_run_inputs(project_id TEXT NOT NULL,run_id TEXT NOT NULL,artifact_version_id TEXT NOT NULL,PRIMARY KEY(project_id,run_id,artifact_version_id),FOREIGN KEY(project_id,run_id) REFERENCES index_runs(project_id,id),FOREIGN KEY(project_id,artifact_version_id) REFERENCES artifact_versions(project_id,id))",
        "CREATE TABLE index_batches(project_id TEXT NOT NULL,run_id TEXT NOT NULL,batch_key TEXT NOT NULL,payload_hash TEXT NOT NULL,records_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(project_id,run_id,batch_key),FOREIGN KEY(project_id,run_id) REFERENCES index_runs(project_id,id))",
        "CREATE TABLE index_errors(id TEXT NOT NULL,project_id TEXT NOT NULL,run_id TEXT NOT NULL,input_json TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,run_id) REFERENCES index_runs(project_id,id))",
        "CREATE TABLE occurrences(id TEXT NOT NULL,project_id TEXT NOT NULL,artifact_version_id TEXT NOT NULL,namespace TEXT NOT NULL,scope_key TEXT NOT NULL,locator_key TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),UNIQUE(project_id,artifact_version_id,namespace,scope_key,locator_key),FOREIGN KEY(project_id,artifact_version_id) REFERENCES artifact_versions(project_id,id))",
        "CREATE TABLE observations(id TEXT NOT NULL,project_id TEXT NOT NULL,occurrence_id TEXT NOT NULL,identifier TEXT NOT NULL,extractor_id TEXT NOT NULL,extractor_version TEXT NOT NULL,config_hash TEXT NOT NULL,value_json TEXT NOT NULL,provenance_json TEXT NOT NULL,content_hash TEXT NOT NULL,evidence_revision INTEGER NOT NULL DEFAULT 1,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),UNIQUE(project_id,occurrence_id,extractor_id,extractor_version,config_hash,content_hash),FOREIGN KEY(project_id,occurrence_id) REFERENCES occurrences(project_id,id))",
        "CREATE TABLE run_observations(project_id TEXT NOT NULL,run_id TEXT NOT NULL,observation_id TEXT NOT NULL,PRIMARY KEY(project_id,run_id,observation_id),FOREIGN KEY(project_id,run_id) REFERENCES index_runs(project_id,id),FOREIGN KEY(project_id,observation_id) REFERENCES observations(project_id,id))",
        "CREATE TABLE observation_reconciliations(id TEXT NOT NULL,project_id TEXT NOT NULL,occurrence_id TEXT NOT NULL,selected_observation_ids_json TEXT NOT NULL,rationale TEXT NOT NULL,evidence_json TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,occurrence_id) REFERENCES occurrences(project_id,id))",
        "CREATE TABLE occurrence_edges(id TEXT NOT NULL,project_id TEXT NOT NULL,source_id TEXT NOT NULL,target_id TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('successor','approved_alias','candidate')),rationale TEXT NOT NULL,review_state TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,source_id) REFERENCES occurrences(project_id,id),FOREIGN KEY(project_id,target_id) REFERENCES occurrences(project_id,id))",
        "CREATE TABLE questions(id TEXT NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),owner_task_id TEXT,area_id TEXT,state TEXT NOT NULL CHECK(state IN ('open','investigating','resolved','withdrawn')),current_revision_id TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,archived_at TEXT,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,owner_task_id) REFERENCES tasks(project_id,uid),FOREIGN KEY(project_id,area_id) REFERENCES areas(project_id,id))",
        "CREATE TABLE question_revisions(id TEXT NOT NULL,project_id TEXT NOT NULL,question_id TEXT NOT NULL,text TEXT NOT NULL,expected_evidence TEXT NOT NULL,criteria_json TEXT NOT NULL,criteria_revision INTEGER NOT NULL,presentation_revision INTEGER NOT NULL,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,question_id) REFERENCES questions(project_id,id) DEFERRABLE INITIALLY DEFERRED)",
        "CREATE TABLE negative_searches(id TEXT NOT NULL,project_id TEXT NOT NULL,artifact_version_id TEXT NOT NULL,query TEXT NOT NULL,method TEXT NOT NULL,searched_range TEXT NOT NULL,coverage_json TEXT NOT NULL,outcome TEXT NOT NULL,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,artifact_version_id) REFERENCES artifact_versions(project_id,id))",
        "CREATE TABLE research_outbox(id TEXT NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),event_sequence INTEGER NOT NULL REFERENCES workspace_events(sequence),kind TEXT NOT NULL,payload_json TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempt_count INTEGER NOT NULL DEFAULT 0,last_error TEXT,next_attempt_at TEXT,created_at TEXT NOT NULL,UNIQUE(project_id,event_sequence,kind))",
        "CREATE TABLE registry_imports(project_id TEXT PRIMARY KEY REFERENCES projects(id),source_hash TEXT NOT NULL,document_json TEXT NOT NULL,created_at TEXT NOT NULL)",
        "CREATE TABLE registry_records(project_id TEXT NOT NULL,alias TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,raw_json TEXT NOT NULL,PRIMARY KEY(project_id,alias),FOREIGN KEY(project_id) REFERENCES registry_imports(project_id))",
        "CREATE TABLE findings(id TEXT NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),current_revision_id TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,archived_at TEXT,PRIMARY KEY(project_id,id))",
        "CREATE TABLE finding_revisions(id TEXT NOT NULL,project_id TEXT NOT NULL,finding_id TEXT NOT NULL,claim TEXT NOT NULL,scope_json TEXT NOT NULL,classification TEXT NOT NULL,uncertainty TEXT NOT NULL,evidence_revision INTEGER NOT NULL DEFAULT 1,presentation_revision INTEGER NOT NULL DEFAULT 1,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,finding_id) REFERENCES findings(project_id,id) DEFERRABLE INITIALLY DEFERRED)",
        "CREATE TABLE evidence_links(id TEXT NOT NULL,project_id TEXT NOT NULL,subject_type TEXT NOT NULL,subject_id TEXT NOT NULL,artifact_version_id TEXT NOT NULL,locator_json TEXT NOT NULL,method TEXT NOT NULL,limitations TEXT NOT NULL,support_hash TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('available','retracted','superseded')),created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,artifact_version_id) REFERENCES artifact_versions(project_id,id))",
        "CREATE TABLE answers(id TEXT NOT NULL,project_id TEXT NOT NULL,question_id TEXT NOT NULL,claim TEXT NOT NULL,evidence_json TEXT NOT NULL,review_gate TEXT NOT NULL,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,question_id) REFERENCES questions(project_id,id))",
        "CREATE INDEX observations_identifier ON observations(project_id,identifier,occurrence_id)",
        "CREATE INDEX index_runs_state ON index_runs(project_id,state,id)",
        "CREATE INDEX research_outbox_state ON research_outbox(project_id,state,event_sequence)",
    ]
    for statement in statements:
        db.execute(statement)
    for table in ('artifact_blobs','artifact_versions','occurrences','observations','question_revisions','negative_searches','registry_imports','registry_records','finding_revisions'):
        for verb in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER immutable_{table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'research source records are immutable'); END")


def _path(root, relative):
    text(relative,'relative source path',2000)
    pure=PurePosixPath(relative)
    if '\\' in relative or ':' in relative or pure.is_absolute() or any(p in ('','..','.') for p in relative.split('/')):
        error(400,'Source path must remain inside a registered root')
    root=Path(root).resolve()
    path=(root/relative).resolve()
    if not path.is_relative_to(root):
        error(400,'Source path escapes registered root')
    return path


def _record(record, inputs):
    if not isinstance(record,dict) or set(record)-{'artifact_version_id','namespace','scope_key','locator_key','identifier','value','provenance'}:
        error(400,'Invalid indexed observation fields')
    for name in ('artifact_version_id','namespace','scope_key','identifier'):
        text(record.get(name),name,2000)
    if record['artifact_version_id'] not in inputs:
        error(400,'Observation source is not an input to this run')
    locator=record.get('locator_key')
    if not isinstance(locator,(str,dict)) or not locator:
        error(400,'Exact nonempty locator with member discriminator is required')
    if not isinstance(record.get('provenance'),dict) or not record['provenance'] or 'value' not in record:
        error(400,'Observation value and extraction provenance required')
    canonical(record)
    return record


def stage_batch(db,project_id,run_id,batch_key,records,payload_hash=None):
    run=scoped(db,'index_runs',project_id,run_id)
    text(batch_key,'batch key',200)
    if not isinstance(records,list) or not 1<=len(records)<=500:
        error(400,'Batch requires 1–500 records')
    actual=digest(records)
    if payload_hash is not None and sha(payload_hash)!=actual:
        error(409,'Batch payload hash does not match records')
    old=db.execute('SELECT * FROM index_batches WHERE project_id=? AND run_id=? AND batch_key=?',(project_id,run_id,batch_key)).fetchone()
    if old:
        if old['payload_hash']!=actual:
            error(409,'Batch key reused with changed content')
        return {'type':'index_batch','id':run_id+':'+batch_key,'payload_hash':actual}
    if run['state']!='staging':
        error(409,'Published or rejected run cannot accept new batches')
    inputs={r[0] for r in db.execute('SELECT artifact_version_id FROM index_run_inputs WHERE project_id=? AND run_id=?',(project_id,run_id))}
    for record in records:
        _record(record,inputs)
    db.execute('INSERT INTO index_batches VALUES(?,?,?,?,?,?)',(project_id,run_id,batch_key,actual,canonical(records),datetime.now(timezone.utc).isoformat()))
    return {'type':'index_batch','id':run_id+':'+batch_key,'payload_hash':actual}


def publish_run(db,project_id,run_id,manifest_hash=None,*,manifest=None):
    run=scoped(db,'index_runs',project_id,run_id)
    if not isinstance(manifest,dict) or set(manifest)!={'batches','record_count'}:
        error(400,'Publication requires exact batch manifest and record_count')
    actual=digest(manifest)
    if manifest_hash is not None and sha(manifest_hash)!=actual:
        error(409,'Manifest hash mismatch')
    if run['state']=='published':
        if run['manifest_hash']!=actual: error(409,'Published manifest is immutable')
        return {'type':'index_run','id':run_id,'record_revision':run['revision']}
    if run['state']!='staging': error(409,'Run is not publishable')
    batches=list(db.execute('SELECT * FROM index_batches WHERE project_id=? AND run_id=? ORDER BY batch_key',(project_id,run_id)))
    expected=[{'key':r['batch_key'],'sha256':r['payload_hash']} for r in batches]
    if manifest['batches']!=expected or type(manifest['record_count']) is not int or manifest['record_count']!=sum(len(json.loads(r['records_json'])) for r in batches):
        error(409,'Manifest differs from staged batches')
    if not batches: error(400,'Cannot publish an empty run')
    inputs={r[0] for r in db.execute('SELECT artifact_version_id FROM index_run_inputs WHERE project_id=? AND run_id=?',(project_id,run_id))}
    now=datetime.now(timezone.utc).isoformat()
    for batch in batches:
        records=json.loads(batch['records_json'])
        if digest(records)!=batch['payload_hash']: error(409,'Staged batch integrity failure')
        for record in records:
            _record(record,inputs)
            identity=[project_id,record['artifact_version_id'],record['namespace'],record['scope_key'],canonical(record['locator_key'])]
            occurrence=str(uuid.uuid5(uuid.NAMESPACE_URL,'occurrence:'+canonical(identity)))
            db.execute('INSERT OR IGNORE INTO occurrences VALUES(?,?,?,?,?,?,?)',(occurrence,*identity,now))
            content=digest({'identifier':record['identifier'],'value':record['value'],'provenance':record['provenance']})
            oid=str(uuid.uuid5(uuid.NAMESPACE_URL,'observation:'+canonical([project_id,occurrence,run['extractor_id'],run['extractor_version'],run['config_hash'],content])))
            db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,project_id,occurrence,record['identifier'],run['extractor_id'],run['extractor_version'],run['config_hash'],canonical(record['value']),canonical(record['provenance']),content,1,run['created_by'],now))
            db.execute('INSERT OR IGNORE INTO run_observations VALUES(?,?,?)',(project_id,run_id,oid))
    db.execute("UPDATE index_runs SET state='published',manifest_hash=?,revision=revision+1 WHERE project_id=? AND id=?",(actual,project_id,run_id))
    return {'type':'index_run','id':run_id,'record_revision':run['revision']+1}


def apply_operation(db,project,principal,op,request_id):
    kind=op['op']; now=datetime.now(timezone.utc).isoformat()
    try:
        from . import workspace_findings
    except ImportError:
        import workspace_findings
    try:
        from . import workspace_answers
    except ImportError:
        import workspace_answers
    if kind=='review_task_sharing':
        try:
            from . import workspace_task_disclosure
        except ImportError:
            import workspace_task_disclosure
        return workspace_task_disclosure.apply_operation(db,project,principal,op,request_id)
    if kind=='review_journal_sharing':
        try:
            from . import workspace_journal_disclosure
        except ImportError:
            import workspace_journal_disclosure
        return workspace_journal_disclosure.apply_operation(db,project,principal,op,request_id)
    if kind in workspace_answers.OPS:return workspace_answers.apply_operation(db,project,principal,op,request_id)
    if kind in workspace_findings.OPS:return workspace_findings.apply_operation(db,project,principal,op,request_id)
    identity=op.get('id') or str(uuid.uuid4())
    text(identity,'record ID',200)
    if kind=='register_root':
        path=Path(text(op.get('path'),'root path',4000)).resolve()
        if not path.is_dir(): error(400,'Registered source root must be an existing directory')
        db.execute('INSERT INTO artifact_roots VALUES(?,?,?,?,?)',(identity,project,str(path),principal,now))
        result={'type':'artifact_root','id':identity}
    elif kind=='bind_root':
        root=scoped(db,'artifact_roots',project,op.get('root_id'))
        if not root['path'].startswith('workspace-root:'):
            error(409,'Source root is already bound on this host')
        path=Path(text(op.get('path'),'root path',4000)).resolve()
        if not path.is_dir(): error(400,'Bound source root must be an existing directory')
        db.execute('UPDATE artifact_roots SET path=? WHERE project_id=? AND id=?',(str(path),project,root['id']))
        result={'type':'artifact_root','id':root['id']}
    elif kind=='register_artifact':
        root=scoped(db,'artifact_roots',project,op.get('root_id'))
        if root['path'].startswith('workspace-root:'): error(409,'Source root is unbound on this host')
        _path(root['path'],op.get('relative_path'))
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,1)',(identity,project,root['id'],op['relative_path'],text(op.get('kind'),'artifact kind',100),principal,now))
        result={'type':'artifact','id':identity,'record_revision':1}
    elif kind=='register_artifact_version':
        for field in ('repository_revision','binary_build_id','source_created_at'):
            optional_text(op.get(field),field,limit=2000)
        artifact=scoped(db,'artifacts',project,op.get('artifact_id'))
        root=scoped(db,'artifact_roots',project,artifact['root_id'])
        if root['path'].startswith('workspace-root:'): error(409,'Source root is unbound on this host')
        path=_path(root['path'],artifact['relative_path'])
        try:
            with path.open('rb') as source:
                body=source.read(64*1024*1024+1)
        except OSError:
            error(400,'Source bytes unavailable; cannot authenticate version')
        if len(body)>64*1024*1024:
            error(413,'Local artifact snapshot limit is 64 MiB; split the source into scoped inputs')
        source_hash=hashlib.sha256(body).hexdigest()
        if op.get('expected_sha256') is not None and sha(op['expected_sha256'])!=source_hash:
            error(409,'Source changed since preview')
        identity=str(uuid.uuid5(uuid.NAMESPACE_URL,canonical([project,artifact['id'],source_hash])))
        old=db.execute('SELECT * FROM artifact_versions WHERE project_id=? AND id=?',(project,identity)).fetchone()
        if old and any(op.get(k) is not None and op[k]!=old[k] for k in ('repository_revision','binary_build_id','source_created_at')):
            error(409,'Existing immutable source metadata differs')
        if old and (old['sha256']!=source_hash or old['artifact_id']!=artifact['id'] or old['byte_length']!=len(body)):
            error(409,'Existing immutable source identity differs')
        db.execute('INSERT OR IGNORE INTO artifact_blobs VALUES(?,?,?)',(project,source_hash,body))
        if not old:
            db.execute('INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identity,project,artifact['id'],source_hash,len(body),op.get('repository_revision'),op.get('binary_build_id'),'available',op.get('source_created_at'),principal,now))
        result={'type':'artifact_version','id':identity,'sha256':source_hash}
    elif kind=='create_index_run':
        inputs=op.get('input_version_ids')
        coverage=op.get('coverage')
        if not isinstance(inputs,list) or not inputs or len(inputs)>500 or not isinstance(coverage,dict) or type(coverage.get('complete')) is not bool:
            error(400,'Explicit source inputs and coverage required')
        if not coverage['complete'] and not coverage.get('excluded'):
            error(400,'Partial index must name excluded coverage')
        for version in inputs: scoped(db,'artifact_versions',project,version)
        db.execute('INSERT INTO index_runs VALUES(?,?,?,?,?,?,?,NULL,?,?,1)',(identity,project,text(op.get('extractor_id'),'extractor ID',200),text(op.get('extractor_version'),'extractor version',200),sha(op.get('config_hash')),'staging',canonical(coverage),principal,now))
        for version in sorted(set(inputs)): db.execute('INSERT INTO index_run_inputs VALUES(?,?,?)',(project,identity,version))
        result={'type':'index_run','id':identity,'record_revision':1}
    elif kind=='stage_batch':
        result=stage_batch(db,project,op.get('run_id'),op.get('batch_key'),op.get('records'),op.get('payload_hash'))
    elif kind=='publish_run':
        result=publish_run(db,project,op.get('run_id'),op.get('manifest_hash'),manifest=op.get('manifest'))
    elif kind=='create_question':
        criteria=op.get('criteria',{})
        try:
            try:
                from .roadmap_match_rules import validate_criteria
            except ImportError:
                from roadmap_match_rules import validate_criteria
            validate_criteria(criteria,allow_empty=True)
        except ValueError as exc:
            error(400,str(exc))
        expected=optional_text(op.get('expected_evidence',''),'expected evidence',allow_none=False)
        for field,table in [('owner_task_id','tasks'),('area_id','areas')]:
            if op.get(field) is not None:
                if table=='tasks':
                    if not db.execute('SELECT 1 FROM tasks WHERE project_id=? AND uid=?',(project,text(op[field],field,200))).fetchone(): error(404,'Scoped task not found')
                else: scoped(db,table,project,op[field])
        qrev=str(uuid.uuid4())
        db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?,1,?,?,?,NULL)',(identity,project,op.get('owner_task_id'),op.get('area_id'),'open',qrev,principal,now,now))
        db.execute('INSERT INTO question_revisions VALUES(?,?,?,?,?,?,1,1,?,?)',(qrev,project,identity,text(op.get('text'),'question text'),expected,canonical(criteria),principal,now))
        result={'type':'question','id':identity,'record_revision':1}
    elif kind=='revise_question':
        question=scoped(db,'questions',project,op.get('question_id'))
        expected_revision=op.get('expected_record_revision')
        if type(expected_revision) is not int: error(400,'Question record revision is required')
        if expected_revision!=question['revision']: error(409,'Question changed; reread and reconcile explicitly')
        if question['state'] not in ('open','investigating'):
            error(409,'Reopen the question before revising a resolved or withdrawn question')
        rationale=text(op.get('rationale'),'revision rationale')
        previous=scoped(db,'question_revisions',project,question['current_revision_id'])
        body=text(op.get('text',previous['text']),'question text')
        expected=optional_text(op.get('expected_evidence',previous['expected_evidence']),'expected evidence',allow_none=False)
        criteria=op.get('criteria',json.loads(previous['criteria_json']))
        try:
            try:
                from .roadmap_match_rules import validate_criteria
            except ImportError:
                from roadmap_match_rules import validate_criteria
            validate_criteria(criteria,allow_empty=True)
        except ValueError as exc: error(400,str(exc))
        criteria_json=canonical(criteria)
        matching_changed=criteria_json!=previous['criteria_json']
        editorial_changed=body!=previous['text'] or expected!=previous['expected_evidence']
        if not matching_changed and not editorial_changed: error(400,'Question revision contains no changes')
        qrev=str(uuid.uuid4())
        db.execute('INSERT INTO question_revisions VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (qrev,project,question['id'],body,expected,criteria_json,
                    previous['criteria_revision']+int(matching_changed),
                    previous['presentation_revision']+int(editorial_changed),principal,now))
        db.execute('UPDATE questions SET current_revision_id=?,revision=revision+1,updated_at=? WHERE project_id=? AND id=?',
                   (qrev,now,project,question['id']))
        if matching_changed:
            db.execute("UPDATE hints SET state='stale' WHERE project_id=? AND question_id=? AND state!='stale'",
                       (project,question['id']))
        result={'type':'question','id':question['id'],'record_revision':question['revision']+1,
                'revision_id':qrev,'previous_revision_id':previous['id'],
                'matching_changed':matching_changed,'presentation_changed':editorial_changed,'rationale':rationale}
    elif kind=='record_negative_search':
        version=scoped(db,'artifact_versions',project,op.get('artifact_version_id'))
        if not isinstance(op.get('coverage'),dict) or type(op['coverage'].get('complete')) is not bool: error(400,'Search coverage is required')
        db.execute('INSERT INTO negative_searches VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,project,version['id'],text(op.get('query'),'search query'),text(op.get('method'),'search method'),text(op.get('searched_range'),'searched range'),canonical(op['coverage']),text(op.get('outcome'),'search outcome'),principal,now))
        result={'type':'negative_search','id':identity}
    elif kind=='import_variable_registry':
        result=import_variable_registry(db,project,op.get('document'),op.get('source_hash'),dry_run=False,principal=principal)
    else:
        error(400,'Unknown research operation')
    event_id=str(uuid.uuid4())
    event_kind='hydrate_artifact_version' if kind=='register_artifact_version' and old else kind
    cur=db.execute("INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",(event_id,project,request_id,principal,result['type'],result['id'],event_kind,canonical(result),now))
    if kind in ('publish_run','create_question') or (kind=='revise_question' and result['matching_changed']):
        db.execute('INSERT INTO research_outbox(id,project_id,event_sequence,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),project,cur.lastrowid,kind,canonical(result),now))
    if kind=='import_variable_registry' and not result.get('already_imported'):
        publications=[('publish_run',{'type':'index_run','id':result['run_id']})]
        publications.extend(('create_question',{'type':'question','id':qid}) for qid in result['question_ids'])
        for event_kind,record in publications:
            event=db.execute("INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),project,request_id,principal,record['type'],record['id'],event_kind,canonical(record),now))
            db.execute('INSERT INTO research_outbox(id,project_id,event_sequence,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),project,event.lastrowid,event_kind,canonical(record),now))
    return result


def export_variable_registry(db,project_id):
    row=db.execute('SELECT document_json FROM registry_imports WHERE project_id=?',(project_id,)).fetchone()
    if not row:
        return None
    original=json.loads(row[0])
    return {'schema_version':original['schema_version'],
            'occurrences':[json.loads(r[0]) for r in db.execute("SELECT raw_json FROM registry_records WHERE project_id=? AND entity_type='occurrence' ORDER BY rowid",(project_id,))],
            'task_wishes':[json.loads(r[0]) for r in db.execute("SELECT raw_json FROM registry_records WHERE project_id=? AND entity_type='question' ORDER BY rowid",(project_id,))]}


def import_variable_registry(db,project_id,document,source_hash,dry_run,*,principal=None):
    """Pin a canonical metadata witness; never authenticate its cited binaries.

    The original JSON remains immutable evidence. The SQL projection is lossless;
    extracted metadata claims remain distinct from original native observations.
    """
    sha(source_hash)
    if not isinstance(document,dict) or set(document)!={'schema_version','occurrences','task_wishes'} or type(document['schema_version']) is not int or document['schema_version']!=1:
        error(400,'Unsupported variable registry document')
    if digest(document)!=source_hash:
        error(409,'Registry document differs from preview hash')
    for key in ('occurrences','task_wishes'):
        if not isinstance(document[key],list) or len(document[key])>500:
            error(400,'Registry migration requires bounded arrays')
        aliases=[]
        for record in document[key]:
            if not isinstance(record,dict): error(400,'Invalid registry record')
            aliases.append(text(record.get('id'),'registry alias',200))
            if key=='occurrences':
                text(record.get('name'),'occurrence name')
                text(record.get('kind'),'occurrence namespace')
                if not isinstance(record.get('source'),dict) or not isinstance(record.get('meaning'),dict):
                    error(400,'Registry occurrence requires source and meaning objects')
                for field in ('summary','status','confidence'):
                    text(record['meaning'].get(field),'meaning '+field)
            else:
                text(record.get('question'),'legacy question')
                if not isinstance(record.get('occurrence_ids'),list) or any(not isinstance(alias,str) for alias in record['occurrence_ids']):
                    error(400,'Wish occurrence references must be strings')
        if len(set(aliases))!=len(aliases): error(400,'Duplicate registry aliases')
    all_aliases=[r['id'] for key in ('occurrences','task_wishes') for r in document[key]]
    if len(set(all_aliases))!=len(all_aliases): error(400,'Registry aliases overlap')
    unresolved=[]
    for wish in document['task_wishes']:
        target=wish.get('task_id') or wish.get('board_reference')
        if target and not db.execute('SELECT 1 FROM tasks WHERE project_id=? AND id=?',(project_id,target)).fetchone():
            unresolved.append({'alias':wish['id'],'target':target})
    projected={'schema_version':document['schema_version'],
               'occurrences':[json.loads(canonical(record)) for record in document['occurrences']],
               'task_wishes':[json.loads(canonical(record)) for record in document['task_wishes']]}
    result={'type':'variable_registry','id':project_id,'source_hash':source_hash,
            'field_differences':json_pointer_differences(document,projected),'occurrence_count':len(document['occurrences']),
            'question_count':len(document['task_wishes']),'unresolved_task_references':unresolved,
            'source_identity':'canonical JSON metadata witness; cited native bytes unverified'}
    previous=db.execute('SELECT * FROM registry_imports WHERE project_id=?',(project_id,)).fetchone()
    if previous:
        if previous['source_hash']!=source_hash or json.loads(previous['document_json'])!=document:
            error(409,'Registry authority already cut over; changed source requires reviewed migration')
        return {**result,'already_imported':True}
    if dry_run:
        return {**result,'dry_run':True}
    if principal is None: error(400,'Migration requires attributed principal')
    now=datetime.now(timezone.utc).isoformat()
    root_id=str(uuid.uuid5(uuid.NAMESPACE_URL,project_id+':metadata-registry-root'))
    # No client path is used: canonical source bytes are captured in SQL itself.
    db.execute('INSERT INTO artifact_roots VALUES(?,?,?,?,?)',(root_id,project_id,'service-owned:registry',principal,now))
    artifact_id=str(uuid.uuid5(uuid.NAMESPACE_URL,project_id+':variable-registry'))
    version_id=str(uuid.uuid5(uuid.NAMESPACE_URL,artifact_id+':'+source_hash))
    db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,1)',(artifact_id,project_id,root_id,'variable-occurrences.json','metadata-snapshot',principal,now))
    db.execute('INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(version_id,project_id,artifact_id,source_hash,len(canonical(document).encode()),None,None,'metadata_snapshot',None,principal,now))
    db.execute('INSERT INTO artifact_blobs VALUES(?,?,?)',(project_id,source_hash,canonical(document).encode('utf-8')))
    db.execute('INSERT INTO registry_imports VALUES(?,?,?,?)',(project_id,source_hash,canonical(document),now))
    run_id=str(uuid.uuid5(uuid.NAMESPACE_URL,version_id+':legacy-variable-import'))
    config=digest({'format':'legacy-variable-registry/v1'})
    db.execute('INSERT INTO index_runs VALUES(?,?,?,?,?,?,?,NULL,?,?,1)',(run_id,project_id,'legacy-variable-registry','1',config,'staging',canonical({'complete':False,'indexed':['supplied variable registry'],'excluded':['unlisted symbols','original binary authentication']}),principal,now))
    db.execute('INSERT INTO index_run_inputs VALUES(?,?,?)',(project_id,run_id,version_id))
    indexed=[]
    for record in document['occurrences']:
        source=record.get('source')
        if not isinstance(source,dict): error(400,'Registry occurrence requires source selector')
        name=text(record.get('name'),'occurrence name')
        namespace=text(record.get('kind'),'occurrence namespace')
        scope=canonical({'artifact':source.get('artifact'),'scope':source.get('scope')})
        locator={'selector':source.get('selector'),'member':record['id']}
        indexed.append({'artifact_version_id':version_id,'namespace':namespace,'scope_key':scope,'locator_key':locator,
                        'identifier':name,'value':record.get('meaning'),
                        'provenance':{'method':'legacy metadata import','native_source_authenticated':False,
                            'original':record,'limitations':'Cited native artifacts have not been authenticated by this import.'}})
        occurrence=str(uuid.uuid5(uuid.NAMESPACE_URL,'occurrence:'+canonical([project_id,version_id,namespace,scope,canonical(locator)])))
        db.execute('INSERT INTO registry_records VALUES(?,?,?,?,?)',(project_id,record['id'],'occurrence',occurrence,canonical(record)))
        db.execute('INSERT INTO entity_aliases VALUES(?,?,?,?)',(project_id,'occurrence',record['id'],occurrence))
        meaning=record.get('meaning') or {}
        fid=str(uuid.uuid5(uuid.NAMESPACE_URL,occurrence+':legacy-meaning')); rev=str(uuid.uuid5(uuid.NAMESPACE_URL,fid+':1'))
        db.execute('INSERT INTO findings VALUES(?,?,?,1,?,?,NULL)',(fid,project_id,rev,principal,now))
        db.execute('INSERT INTO finding_revisions VALUES(?,?,?,?,?,?,?,1,1,?,?)',(rev,project_id,fid,meaning.get('summary','Uninterpreted legacy occurrence'),canonical({'occurrence_id':occurrence,'metadata_version_id':version_id,'identifier':name,'namespace':namespace,'scope_key':scope,'artifact_version_id':version_id}),meaning.get('status','unknown'),canonical({'confidence':meaning.get('confidence'),'source_created_at':record.get('discovered_at'),'native_authentication':'unverified'}),principal,now))
        db.execute('INSERT INTO evidence_links VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),project_id,'finding_revision',rev,version_id,canonical(locator),'legacy metadata transcription','This authenticates metadata, not its cited native claims.',source_hash,'available',principal,now))
    if indexed:
        batch=stage_batch(db,project_id,run_id,'registry',indexed)
        publish_run(db,project_id,run_id,manifest={'batches':[{'key':'registry','sha256':batch['payload_hash']}],'record_count':len(indexed)})
    question_ids=[]
    for record in document['task_wishes']:
        qid=str(uuid.uuid5(uuid.NAMESPACE_URL,project_id+':question:'+record['id'])); qrev=str(uuid.uuid5(uuid.NAMESPACE_URL,qid+':1'))
        question_ids.append(qid)
        target=record.get('task_id')
        task=db.execute('SELECT uid FROM tasks WHERE project_id=? AND id=?',(project_id,target)).fetchone() if target else None
        groups=[]
        for alias in record.get('occurrence_ids',[]):
            original=next((r for r in document['occurrences'] if r['id']==alias),None)
            if original:
                groups.append({'identifier':original['name'],'namespace':original['kind'],
                               'scope_key':canonical({'artifact':original['source'].get('artifact'),'scope':original['source'].get('scope')})})
        state={'unresolved':'open','parked':'withdrawn','resolved':'investigating'}.get(record.get('status'),'open')
        db.execute('INSERT INTO questions VALUES(?,?,?,NULL,?,?,1,?,?,?,NULL)',(qid,project_id,task['uid'] if task else None,state,qrev,principal,now,now))
        concern='Legacy resolution lacks accepted answer; independent evidence review required.' if state=='investigating' else 'Original native/source evidence is required.'
        db.execute('INSERT INTO question_revisions VALUES(?,?,?,?,?,?,1,1,?,?)',(qrev,project_id,qid,text(record.get('question'),'legacy question'),concern,canonical({'any_of':groups} if groups else {}),principal,now))
        db.execute('INSERT INTO registry_records VALUES(?,?,?,?,?)',(project_id,record['id'],'question',qid,canonical(record)))
        db.execute('INSERT INTO entity_aliases VALUES(?,?,?,?)',(project_id,'question',record['id'],qid))
    differences=json_pointer_differences(document,export_variable_registry(db,project_id))
    if differences:
        error(409,'Registry compatibility projection differs from input: '+canonical(differences[:20]))
    return {**result,'field_differences':differences,'dry_run':False,'run_id':run_id,'question_ids':question_ids}


def json_pointer_differences(expected,actual,path=''):
    """Lossless migration diagnostic, including unknown keys and explicit nulls."""
    if type(expected) is not type(actual):
        return [{'pointer':path,'expected':expected,'actual':actual}]
    if isinstance(expected,dict):
        differences=[]
        for key in sorted(set(expected)|set(actual)):
            pointer=path+'/'+str(key).replace('~','~0').replace('/','~1')
            if key not in expected or key not in actual:
                differences.append({'pointer':pointer,'missing_from':'expected' if key not in expected else 'actual'})
            else:
                differences.extend(json_pointer_differences(expected[key],actual[key],pointer))
        return differences
    if isinstance(expected,list):
        if len(expected)!=len(actual): return [{'pointer':path,'expected_length':len(expected),'actual_length':len(actual)}]
        return [difference for i,(left,right) in enumerate(zip(expected,actual)) for difference in json_pointer_differences(left,right,path+'/'+str(i))]
    return [] if expected==actual else [{'pointer':path,'expected':expected,'actual':actual}]


def read(db,resource,project,*,limit=None,cursor=None,**unused):
    try:
        from .roadmap_projection import API_VERSION, _coverage, _encode, _decode
    except ImportError:
        from roadmap_projection import API_VERSION, _coverage, _encode, _decode
    try:
        from . import workspace_findings
    except ImportError:
        import workspace_findings
    try:
        from . import workspace_answers
    except ImportError:
        import workspace_answers
    if resource=='task-sharing-original':
        if limit is not None or cursor is not None or set(unused)-{'key','source_sha256','disclosure_id'}:error(400,'Unsupported task original filter')
        try:
            from . import workspace_task_disclosure
        except ImportError:
            import workspace_task_disclosure
        return workspace_task_disclosure.read_original(db,project,**unused)
    if resource=='task-sharing-review':
        if unused:error(400,'Unsupported task sharing review filter')
        try:
            from . import workspace_task_disclosure
        except ImportError:
            import workspace_task_disclosure
        return workspace_task_disclosure.preview(db,project,limit=limit,cursor=cursor)
    if resource in ('finding-context','question-context','answer-context','answer-support'):
        try:
            from . import workspace_research_context
        except ImportError:
            import workspace_research_context
        return workspace_research_context.read(db,resource,project,limit=limit,cursor=cursor,**unused)
    if resource=='journal-sharing-review':
        if unused:error(400,'Unsupported sharing review filter')
        try:
            from . import workspace_journal_disclosure
        except ImportError:
            import workspace_journal_disclosure
        return workspace_journal_disclosure.preview(db,project,limit=limit,cursor=cursor)
    if resource=='source-manifests':
        try:
            from . import workspace_source_index
        except ImportError:
            import workspace_source_index
        return workspace_source_index.read(db,project,limit=limit,cursor=cursor,**unused)
    if resource in workspace_answers.RESOURCES:return workspace_answers.read(db,resource,project,limit=limit,cursor=cursor,**unused)
    if resource in workspace_findings.RESOURCES:return workspace_findings.read(db,resource,project,limit=limit,cursor=cursor,**unused)
    coverage=_coverage(db,project)
    meta=db.execute('SELECT * FROM store_meta WHERE singleton=1').fetchone()
    revision=f"{meta['store_id']}:{meta['revision']}"
    limit=100 if limit is None else limit
    if type(limit) is not int or not 1<=limit<=100: error(400,'Page limit must be 1–100')
    binding={'resource':resource,'project_id':project,'revision':revision,'limit':limit}
    offset=0
    if cursor:
        page=_decode(cursor)
        if page.get('revision')!=revision:
            exc=None
            try: error(409,'Snapshot cursor expired; restart required')
            except Exception as raised: exc=raised
            exc.restart_required=True
            raise exc
        if set(page)!=set(binding)|{'offset'} or any(page.get(k)!=v for k,v in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:
            error(400,'Cursor does not match project/filter')
        offset=page['offset']
    tables={'artifact-versions':'artifact_versions','index-runs':'index_runs','negative-searches':'negative_searches'}
    table=tables.get(resource,resource)
    if resource=='questions':
        query='SELECT q.*,r.text,r.expected_evidence,r.criteria_json,r.criteria_revision,r.presentation_revision FROM questions q JOIN question_revisions r ON r.project_id=q.project_id AND r.id=q.current_revision_id WHERE q.project_id=? ORDER BY q.created_at,q.id LIMIT ? OFFSET ?'
    elif resource=='findings':
        query='SELECT f.*,r.claim,r.scope_json,r.classification,r.uncertainty,r.evidence_revision,r.presentation_revision FROM findings f JOIN finding_revisions r ON r.project_id=f.project_id AND r.id=f.current_revision_id WHERE f.project_id=? ORDER BY f.created_at,f.id LIMIT ? OFFSET ?'
    elif resource=='observations':
        query="SELECT o.*,c.artifact_version_id,c.namespace,c.scope_key,c.locator_key,v.binary_build_id,v.sha256 AS source_sha256 FROM observations o JOIN occurrences c ON c.project_id=o.project_id AND c.id=o.occurrence_id JOIN artifact_versions v ON v.project_id=c.project_id AND v.id=c.artifact_version_id WHERE o.project_id=? AND EXISTS(SELECT 1 FROM run_observations ro JOIN index_runs r ON r.project_id=ro.project_id AND r.id=ro.run_id WHERE ro.project_id=o.project_id AND ro.observation_id=o.id AND r.state='published') ORDER BY o.created_at,o.id LIMIT ? OFFSET ?"
    else:
        query=f'SELECT * FROM {table} WHERE project_id=? ORDER BY created_at,id LIMIT ? OFFSET ?'
    items=[]
    rows=db.execute(query,(project,limit+1,offset)).fetchall()
    for row in rows[:limit]:
        item=dict(row)
        for key in list(item):
            if key.endswith('_json'): item[key[:-5]]=json.loads(item.pop(key))
        if 'revision' in item: item['record_revision']=item.pop('revision')
        if resource=='artifact-versions':
            item['local_bytes_available']=db.execute('SELECT 1 FROM artifact_blobs WHERE project_id=? AND sha256=?',(project,item['sha256'])).fetchone() is not None
        if resource=='observations':
            item['locator_key']=json.loads(item['locator_key'])
            item['candidate_type']='observation'
            item['support_hashes']=[item['source_sha256'],item['content_hash']]
        if resource=='findings':item=workspace_findings.describe(db,project,item)
        if resource=='questions':item=workspace_answers.describe_question(db,project,item)
        items.append(item)
    runs=[{'id':r['id'],'coverage':json.loads(r['coverage_json'])} for r in db.execute("SELECT id,coverage_json FROM index_runs WHERE project_id=? AND state='published' ORDER BY id",(project,))]
    coverage={**coverage,'source':'published research indexes','published_runs':runs,'complete':False}
    return {'api_version':API_VERSION,'project_id':project,'revision':revision,'items':items,'coverage':coverage,
            'next_cursor':_encode({**binding,'offset':offset+limit}) if len(rows)>limit else None}
