"""Reviewed task prose projections; raw fields and local consent never travel."""
import json
import re
import uuid
from datetime import datetime, timezone
try:
    from . import roadmap_research as research, workspace_journal_projection as projection
    from .workspace_journal_disclosure import roots, _required
except ImportError:
    import roadmap_research as research, workspace_journal_projection as projection
    from workspace_journal_disclosure import roots, _required

COLUMNS=('id','project_id','task_id','slot','field','source_sha256','public_json','policy_version','origin_store_id')
SLOTS={'record_json','original_json'}
FIELDS={'summary','attempts','subtasks'}
POLICY=1

def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS task_disclosures(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),task_id TEXT NOT NULL,slot TEXT NOT NULL,field TEXT NOT NULL,source_sha256 TEXT NOT NULL,public_json TEXT NOT NULL,policy_version INTEGER NOT NULL,origin_store_id TEXT NOT NULL,FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid))')
    db.execute('CREATE INDEX IF NOT EXISTS task_disclosure_context ON task_disclosures(project_id,task_id)')
    db.execute('CREATE TABLE IF NOT EXISTS task_private_fields(disclosure_id TEXT PRIMARY KEY REFERENCES task_disclosures(id),raw_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS task_share_choices(project_id TEXT NOT NULL,task_id TEXT NOT NULL,slot TEXT NOT NULL,field TEXT NOT NULL,source_sha256 TEXT NOT NULL,disclosure_id TEXT NOT NULL REFERENCES task_disclosures(id),reviewed_by TEXT NOT NULL REFERENCES principals(id),reviewed_at TEXT NOT NULL,PRIMARY KEY(project_id,task_id,slot,field),FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid))')
    for table in ('task_disclosures','task_private_fields'):
        for action in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'task disclosure history is immutable'); END")

def _key(task,slot,field):return research.canonical([task,slot,field])

def _redact_prose(value,known):
    if isinstance(value,dict):
        result={}
        for key,child in value.items():
            if projection._path_check(key,known):raise ValueError('Unsafe object key')
            if projection._path_check(child,known) and key not in {'text','summary','note','message','attempts'}:raise ValueError('Unknown or authority field cannot be redacted')
            result[key]=_redact_prose(child,known)
        return result
    if isinstance(value,list):return [_redact_prose(child,known) for child in value]
    if not isinstance(value,str) or not projection._path_check(value,known):return value
    if value.startswith(('{','[')):
        try:nested=json.loads(value)
        except ValueError:pass
        else:return research.canonical(_redact_prose(nested,known))
    # Recognized historical API route references are retained explicitly as routes.
    result=value
    for route in ('/api/tasks','/api/transaction'):
        result=re.sub(re.escape(route)+r'(?![\w/-])',lambda m:'[root-relative API route: '+route[1:]+']',result)
    for root in sorted((r for r in known if r),key=len,reverse=True):
        result=result.replace(root,'[local workspace]').replace(root.replace('\\','/'),'[local workspace]')
    # Preserve surrounding prose and measurements. These are proposals, never
    # silently approved; quoted or space-bearing paths still need human review.
    result=re.sub(r'''(?i)(?:[a-z]:[\\/]|\\\\)[^\s"<>|]+''',lambda m:projection._REDACT,result)
    result=re.sub(r'''(?:(?<=\s)|(?<=[(=])|^)/(?!/)[^\s"<>|]+''',lambda m:projection._REDACT,result)
    return projection._redact(result,())

def _redact_field(slot,field,value,known):
    if slot not in SLOTS or field not in FIELDS or (field=='subtasks' and slot!='original_json'):raise ValueError('Unsupported authority field')
    if field=='subtasks':
        if not isinstance(value,list):raise ValueError('Invalid historical subtasks')
        result=[]
        for child in value:
            if not isinstance(child,dict):raise ValueError('Invalid historical subtask')
            record={}
            for key,item in child.items():
                if projection._path_check(key,known):raise ValueError('Unsafe historical key')
                if projection._path_check(item,known):record[key]=_redact_field(slot,key,item,known)
                else:record[key]=item
            result.append(record)
        return result
    return _redact_prose(value,known)

def _public(project,task,slot,field,value,origin,known):
    redacted=_redact_field(slot,field,value,known)
    if projection._path_check(redacted,known):raise ValueError('Unsafe public field')
    content={'project_id':project,'task_id':task,'slot':slot,'field':field,'source_sha256':research.digest(value),'public_json':research.canonical(redacted),'policy_version':POLICY,'origin_store_id':origin}
    return {'id':str(uuid.uuid5(uuid.NAMESPACE_URL,'workbench-task-disclosure:'+research.digest(content))),**content}

def validate(row):
    if not isinstance(row,dict) or set(row)!=set(COLUMNS):raise ValueError('Invalid disclosure columns')
    if any(not isinstance(row[key],str) or not row[key] or len(row[key])>200 for key in ('id','project_id','task_id','slot','field','origin_store_id')):raise ValueError('Invalid identity')
    if row['slot'] not in SLOTS or row['field'] not in FIELDS or (row['field']=='subtasks' and row['slot']!='original_json') or type(row['policy_version']) is not int or row['policy_version']!=POLICY:raise ValueError('Unsupported task disclosure')
    if not isinstance(row['source_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',row['source_sha256']):raise ValueError('Invalid source digest')
    if not isinstance(row['public_json'],str):raise ValueError('Invalid public value')
    value=json.loads(row['public_json'])
    if research.canonical(value)!=row['public_json'] or projection._path_check(value,()):raise ValueError('Unsafe public value')
    if projection._path_check({key:row[key] for key in COLUMNS if key!='public_json'},()):raise ValueError('Unsafe metadata')
    content={key:row[key] for key in COLUMNS if key!='id'}
    if row['id']!=str(uuid.uuid5(uuid.NAMESPACE_URL,'workbench-task-disclosure:'+research.digest(content))):raise ValueError('Disclosure identity mismatch')

def preview(db,project,*,limit=None,cursor=None):
    try:
        from .roadmap_projection import API_VERSION,_encode,_decode
    except ImportError:
        from roadmap_projection import API_VERSION,_encode,_decode
    if not db.execute('SELECT 1 FROM projects WHERE id=? AND archived_at IS NULL',(project,)).fetchone():research.error(404,'Active project not found')
    limit=20 if limit is None else limit
    if type(limit) is not int or not 1<=limit<=100:research.error(400,'Review page limit must be1..100')
    meta=db.execute('SELECT store_id,revision FROM store_meta').fetchone();revision=f'{meta[0]}:{meta[1]}'
    binding={'project_id':project,'revision':revision,'limit':limit,'resource':'task-sharing-review'};offset=0
    if cursor is not None:
        page=_decode(cursor)
        if page.get('revision')!=revision:research.error(409,'Task sharing cursor expired; reread')
        if set(page)!=set(binding)|{'offset'} or any(page.get(k)!=v for k,v in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:research.error(400,'Task sharing cursor scope mismatch')
        offset=page['offset']
    # Page tasks rather than all nested fields; cursor remains bounded and stable.
    rows=db.execute('SELECT uid,id,record_json,original_json FROM tasks WHERE project_id=? ORDER BY uid LIMIT ? OFFSET ?',(project,limit+1,offset)).fetchall()
    known=roots(db,project);items=[]
    for task in rows[:limit]:
        for slot in sorted(SLOTS):
            for field,value in sorted(json.loads(task[slot]).items()):
                if not projection._path_check({field:value},known):continue
                proposals={}
                try:proposals['redacted']=_public(project,task['uid'],slot,field,value,meta[0],known)
                except ValueError:pass
                choice=db.execute('SELECT source_sha256,disclosure_id FROM task_share_choices WHERE project_id=? AND task_id=? AND slot=? AND field=?',(project,task['uid'],slot,field)).fetchone()
                items.append({'key':_key(task['uid'],slot,field),'task_id':task['uid'],'alias':task['id'],'slot':slot,'field':field,'source_sha256':research.digest(value),'proposals':proposals,'approved':bool(choice and choice[0]==research.digest(value) and proposals.get('redacted',{}).get('id')==choice[1])})
    body={**binding,'cursor':cursor,'items':items,'next_cursor':_encode({**binding,'offset':offset+limit}) if len(rows)>limit else None}
    return {'api_version':API_VERSION,**body,'review_digest':research.digest(body)}

def apply_operation(db,project,principal,op,request_id):
    if not db.in_transaction:research.error(409,'Task sharing review requires caller transaction')
    member=db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(project,principal)).fetchone()
    if not member or not {'contribute','review'}<=set(json.loads(member[0])):research.error(403,'Current task sharing review capability required')
    if set(op)!={'op','review_digest','limit','cursor','choices'}:research.error(400,'Invalid task sharing review fields')
    page=preview(db,project,limit=op['limit'],cursor=op['cursor'])
    if op['review_digest']!=page['review_digest']:research.error(409,'Task sharing review changed; reread explicitly')
    choices=op['choices']
    if not page['items'] or not isinstance(choices,dict) or set(choices)!={item['key'] for item in page['items']}:research.error(400,'Choose every reviewed task field explicitly')
    for item in page['items']:
        if choices[item['key']]!='redacted' or 'redacted' not in item['proposals']:research.error(400,'Unsupported task field sharing choice')
    ids=[];now=datetime.now(timezone.utc).isoformat()
    for item in page['items']:
        public=item['proposals']['redacted'];validate(public)
        db.execute('INSERT OR IGNORE INTO task_disclosures('+','.join(COLUMNS)+') VALUES('+','.join('?' for _ in COLUMNS)+')',tuple(public[key] for key in COLUMNS))
        if dict(db.execute('SELECT * FROM task_disclosures WHERE id=?',(public['id'],)).fetchone())!=public:research.error(409,'Immutable task disclosure collision')
        raw=json.loads(db.execute(f'SELECT {item["slot"]} FROM tasks WHERE project_id=? AND uid=?',(project,item['task_id'])).fetchone()[0])[item['field']]
        raw_json=research.canonical(raw)
        db.execute('INSERT OR IGNORE INTO task_private_fields VALUES(?,?)',(public['id'],raw_json))
        if db.execute('SELECT raw_json FROM task_private_fields WHERE disclosure_id=?',(public['id'],)).fetchone()[0]!=raw_json:research.error(409,'Private task field digest collision')
        db.execute('INSERT INTO task_share_choices VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id,task_id,slot,field) DO UPDATE SET source_sha256=excluded.source_sha256,disclosure_id=excluded.disclosure_id,reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at',(project,item['task_id'],item['slot'],item['field'],item['source_sha256'],public['id'],principal,now));ids.append(public['id'])
    result={'type':'task_sharing_review','id':research.digest(ids),'disclosure_ids':ids,'local_only_consent':True}
    db.execute('INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),project,request_id,principal,result['type'],result['id'],'task_sharing_review',research.canonical(result),now))
    return result

def _approved(db,project,task,slot,field,value,known):
    choice=db.execute('SELECT * FROM task_share_choices WHERE project_id=? AND task_id=? AND slot=? AND field=?',(project,task,slot,field)).fetchone()
    if not choice or choice['source_sha256']!=research.digest(value):return None
    row=db.execute('SELECT * FROM task_disclosures WHERE project_id=? AND id=?',(project,choice['disclosure_id'])).fetchone()
    if row is None:return None
    public=dict(row)
    try:
        validate(public)
        expected=_public(project,task,slot,field,value,public['origin_store_id'],known)
        if public!=expected:return None
    except (ValueError,TypeError):return None
    return public

def project_records(db,project,records,known,*,require_approval=True):
    for task in records['tasks']:
        for slot in SLOTS:
            value=json.loads(task[slot])
            for field,raw in list(value.items()):
                if not projection._path_check({field:raw},known):continue
                approved=_approved(db,project,task['uid'],slot,field,raw,known)
                if approved:value[field]=json.loads(approved['public_json'])
                elif require_approval:_required('Task contains a host path; explicit local task sharing review required')
            task[slot]=research.canonical(value)

def restore_private(db,project,row):
    """A matching reviewed public copy must never erase the local raw field."""
    local=db.execute('SELECT record_json,original_json FROM tasks WHERE project_id=? AND uid=?',(project,row['uid'])).fetchone()
    if local is None:return row
    known=roots(db,project);row=dict(row)
    for slot in SLOTS:
        merged=json.loads(row[slot])
        for field,raw in json.loads(local[slot]).items():
            approved=_approved(db,project,row['uid'],slot,field,raw,known)
            if approved and field in merged and merged[field]==json.loads(approved['public_json']):merged[field]=raw
        row[slot]=local[slot] if merged==json.loads(local[slot]) else research.canonical(merged)
    return row

def validate_bundle(project,records):
    tasks={row['uid'] for row in records['tasks']}
    for row in records.get('task_disclosures',[]):
        try:validate(row)
        except (ValueError,TypeError):research.error(400,'Task disclosure content or provenance is incompatible')
        if row['project_id']!=project or row['task_id'] not in tasks:research.error(400,'Task disclosure lacks project-scoped task context')


def context_labels(db,project,task):
    count=db.execute('SELECT count(*) FROM task_disclosures WHERE project_id=? AND task_id=?',(project,task)).fetchone()[0]
    items=[]
    for row in db.execute('SELECT d.*,v.disclosure_id IS NOT NULL AS original_available FROM task_disclosures d LEFT JOIN task_private_fields v ON v.disclosure_id=d.id WHERE d.project_id=? AND d.task_id=? ORDER BY d.id LIMIT 50',(project,task)):
        items.append({key:row[key] for key in ('id','slot','field','source_sha256','public_json','origin_store_id','original_available')})
    return {'items':items,'total':count,'omitted':max(0,count-len(items)),'meaning':'Reviewed public copies; source digests identify retained private values, not native verification.'}


def read_original(db,project,*,key=None,source_sha256=None,disclosure_id=None):
    """Host-owner HTTP access only; never embedded in review pages or exports."""
    if disclosure_id is not None:
        if key is not None or source_sha256 is not None or not isinstance(disclosure_id,str):research.error(400,'Choose one original lookup')
        row=db.execute('SELECT d.*,v.raw_json FROM task_disclosures d JOIN task_private_fields v ON v.disclosure_id=d.id WHERE d.project_id=? AND d.id=?',(project,disclosure_id)).fetchone()
        if row is None:research.error(404,'Local original not available')
        if research.digest(json.loads(row['raw_json']))!=row['source_sha256']:research.error(409,'Private original digest mismatch')
        return {'project_id':project,'task_id':row['task_id'],'slot':row['slot'],'field':row['field'],'source_sha256':row['source_sha256'],'raw_json':row['raw_json'],'disclosure_id':disclosure_id}
    try:parts=json.loads(key)
    except (ValueError,TypeError):research.error(400,'Invalid task field key')
    if not isinstance(parts,list) or len(parts)!=3 or any(not isinstance(v,str) or not v or len(v)>200 for v in parts) or parts[1] not in SLOTS:research.error(400,'Invalid task field key')
    task,slot,field=parts
    row=db.execute(f'SELECT {slot} FROM tasks WHERE project_id=? AND uid=?',(project,task)).fetchone()
    if row is None:research.error(404,'Scoped task not found')
    record=json.loads(row[0])
    if field not in record:research.error(404,'Task field not found')
    value=record[field];digest=research.digest(value)
    if digest!=source_sha256:research.error(409,'Task field changed; reread review')
    return {'project_id':project,'task_id':task,'slot':slot,'field':field,'source_sha256':digest,'raw_json':research.canonical(value)}
