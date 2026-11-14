"""Locally authorized journal disclosure; immutable originals never become permissions."""
import json
import uuid
from datetime import datetime,timezone
try:
    from . import roadmap_research as research
    from . import workspace_journal_projection as projection
except ImportError:
    import roadmap_research as research
    import workspace_journal_projection as projection

OPS={'review_journal_sharing'}
RESOURCES={'journal-sharing-review'}

def create_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS journal_disclosures(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),source_event_id TEXT NOT NULL,task_id TEXT NOT NULL,source_sha256 TEXT NOT NULL,origin_store_id TEXT NOT NULL,mode TEXT NOT NULL CHECK(mode IN ('redacted','omitted')),policy_version INTEGER NOT NULL,details_json TEXT NOT NULL,source_actor_json TEXT,created_by TEXT REFERENCES principals(id),kind TEXT NOT NULL,recorded_at TEXT NOT NULL,historical_at TEXT,public_sha256 TEXT NOT NULL,FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid))")
    db.execute('CREATE INDEX IF NOT EXISTS journal_disclosure_source ON journal_disclosures(project_id,source_event_id)')
    db.execute('CREATE INDEX IF NOT EXISTS journal_disclosure_history ON journal_disclosures(project_id,task_id,source_event_id)')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS journal_disclosures_{action.lower()} BEFORE {action} ON journal_disclosures BEGIN SELECT RAISE(ABORT,'journal disclosures are immutable'); END")
    db.execute('CREATE TABLE IF NOT EXISTS journal_share_choices(project_id TEXT NOT NULL REFERENCES projects(id),event_id TEXT NOT NULL REFERENCES task_journal_entries(id),source_sha256 TEXT NOT NULL,policy_version INTEGER NOT NULL,mode TEXT NOT NULL,disclosure_id TEXT NOT NULL REFERENCES journal_disclosures(id),reviewed_by TEXT NOT NULL REFERENCES principals(id),reviewed_at TEXT NOT NULL,PRIMARY KEY(project_id,event_id))')

def roots(db,project):
    return [row[0] for row in db.execute('SELECT path FROM artifact_roots WHERE project_id=?',(project,)) if not row[0].startswith('workspace-root:')]

def preview(db,project,*,limit=None,cursor=None):
    try:
        from .roadmap_projection import API_VERSION,_encode,_decode
    except ImportError:
        from roadmap_projection import API_VERSION,_encode,_decode
    if not db.execute('SELECT 1 FROM projects WHERE id=? AND archived_at IS NULL',(project,)).fetchone():research.error(404,'Active project not found')
    limit=100 if limit is None else limit
    if type(limit) is not int or not 1<=limit<=100:research.error(400,'Review page limit must be1..100')
    meta=db.execute('SELECT store_id,revision FROM store_meta').fetchone();revision=f"{meta[0]}:{meta[1]}"
    binding={'project_id':project,'revision':revision,'limit':limit,'resource':'journal-sharing-review'};offset=0
    if cursor is not None:
        page=_decode(cursor)
        if page.get('revision')!=revision:research.error(409,'Sharing review cursor expired; reread')
        if set(page)!=set(binding)|{'offset'} or any(page.get(k)!=v for k,v in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:research.error(400,'Sharing cursor scope mismatch')
        offset=page['offset']
    rows=db.execute('SELECT * FROM task_journal_entries WHERE project_id=? ORDER BY id LIMIT ? OFFSET ?',(project,limit+1,offset)).fetchall()
    known=roots(db,project);items=[]
    for raw in rows[:limit]:
        row=dict(raw)
        if not projection.unsafe_journal(row,known):continue
        proposals={}
        for mode in ('redacted','omitted'):
            try:proposals[mode]=projection.project_journal(row,mode,known)
            except ValueError:pass
        choice=db.execute('SELECT * FROM journal_share_choices WHERE project_id=? AND event_id=?',(project,row['id'])).fetchone()
        current=choice['mode'] if choice and choice['source_sha256']==row['source_sha256'] and choice['policy_version']==projection.POLICY_VERSION and choice['mode'] in proposals and choice['disclosure_id']==proposals[choice['mode']]['id'] else None
        items.append({'source_event_id':row['id'],'source_sha256':row['source_sha256'],'task_id':row['task_id'],'proposals':proposals,'current_choice':current,'unsupported':not bool(proposals)})
    result={'api_version':API_VERSION,**binding,'cursor':cursor,'items':items,'inspected':min(limit,len(rows)),'next_cursor':_encode({**binding,'offset':offset+limit}) if len(rows)>limit else None,'publication_warning':'Published disclosure history cannot be erased by a later local choice.'}
    result['review_digest']=research.digest(result)
    return result

def apply_operation(db,project,principal,op,request_id):
    if not db.in_transaction:research.error(409,'Sharing review requires caller transaction')
    member=db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(project,principal)).fetchone()
    if not member or not {'contribute','review'}<=set(json.loads(member[0])):research.error(403,'Current sharing review capability required')
    if set(op)!={'op','review_digest','limit','cursor','choices'}:research.error(400,'Invalid sharing review fields')
    current=preview(db,project,limit=op['limit'],cursor=op['cursor'])
    if op['review_digest']!=current['review_digest']:research.error(409,'Journal sharing review changed; reread explicitly')
    choices=op['choices'];expected={item['source_event_id'] for item in current['items']}
    if not expected or not isinstance(choices,dict) or set(choices)!=expected:research.error(400,'Choose exactly one disclosure for each reviewed entry')
    for item in current['items']:
        mode=choices[item['source_event_id']]
        if not isinstance(mode,str) or mode not in item['proposals']:research.error(400,'Unsupported journal disclosure choice')
    now=datetime.now(timezone.utc).isoformat();ids=[]
    for item in current['items']:
        mode=choices[item['source_event_id']];public=item['proposals'][mode]
        projection.validate_disclosure(public)
        db.execute('INSERT OR IGNORE INTO journal_disclosures('+','.join(projection.COLUMNS)+') VALUES('+','.join('?' for _ in projection.COLUMNS)+')',tuple(public[key] for key in projection.COLUMNS))
        existing=dict(db.execute('SELECT * FROM journal_disclosures WHERE id=?',(public['id'],)).fetchone())
        if existing!=public:research.error(409,'Immutable disclosure identity collision')
        db.execute('INSERT INTO journal_share_choices VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id,event_id) DO UPDATE SET source_sha256=excluded.source_sha256,policy_version=excluded.policy_version,mode=excluded.mode,disclosure_id=excluded.disclosure_id,reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at',(project,item['source_event_id'],item['source_sha256'],projection.POLICY_VERSION,mode,public['id'],principal,now))
        ids.append(public['id'])
    result={'type':'journal_sharing_review','id':research.digest(ids),'disclosure_ids':ids,'local_only_consent':True}
    db.execute('INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),project,request_id,principal,result['type'],result['id'],'journal_sharing_review',research.canonical(result),now))
    return result

def _required(message):
    try:
        from .roadmap_store import StoreError
    except ImportError:
        from roadmap_store import StoreError
    error=StoreError(409,message);error.sharing_review_required=True
    raise error

def project_records(db,project,records,known_roots,*,require_approval=True):
    retained=[]
    for row in records['task_journal_entries']:
        if not projection.unsafe_journal(row,known_roots):retained.append(row);continue
        choice=db.execute('SELECT * FROM journal_share_choices WHERE project_id=? AND event_id=?',(project,row['id'])).fetchone()
        if not choice or choice['source_sha256']!=row['source_sha256'] or choice['policy_version']!=projection.POLICY_VERSION:
            if not require_approval:
                retained.append(row);continue  # Private comparison only; never an outbound bundle.
            _required('Journal contains a host path; explicit local sharing review required')
        try:
            expected=projection.project_journal(row,choice['mode'],known_roots)
            actual=db.execute('SELECT * FROM journal_disclosures WHERE id=? AND project_id=?',(choice['disclosure_id'],project)).fetchone()
            if actual is None or dict(actual)!=expected:raise ValueError()
            projection.validate_disclosure(dict(actual))
        except ValueError:_required('Journal sharing projection changed; reread review')
    records['task_journal_entries']=retained

def validate_bundle(project,records,db=None):
    tasks={row['uid'] for row in records['tasks']}
    fields=('task_id','source_sha256','origin_store_id','source_actor_json','created_by','kind','recorded_at','historical_at')
    sources={};checked=set()
    for raw in records.get('task_journal_entries',[]):
        source=json.loads(raw['source_event_json']);actor=json.loads(source['actor_json']) if source.get('actor_json') else None
        basis={key:raw[key] for key in fields if key!='source_actor_json'}
        basis['source_actor_json']=research.canonical(actor) if actor is not None else None
        sources[raw['id']]=basis
    for row in records.get('journal_disclosures',[]):
        try:projection.validate_disclosure(row)
        except ValueError:research.error(400,'Journal disclosure content or provenance is incompatible')
        if row['project_id']!=project:research.error(400,'Foreign journal disclosure')
        if row['task_id'] not in tasks:research.error(400,'Journal disclosure lacks its declared task context')
        basis={key:row[key] for key in fields};identity=row['source_event_id']
        if identity in sources and sources[identity]!=basis:research.error(409,'Immutable journal source identity has conflicting disclosures')
        if db is not None and identity not in checked:
            for existing in db.execute('SELECT * FROM journal_disclosures WHERE project_id=? AND source_event_id=?',(project,identity)):
                if {key:existing[key] for key in fields}!=basis:research.error(409,'Disclosure cannot reassign a known original journal identity')
            raw=db.execute('SELECT * FROM task_journal_entries WHERE project_id=? AND id=?',(project,identity)).fetchone()
            if raw:
                source=json.loads(raw['source_event_json']);actor=json.loads(source['actor_json']) if source.get('actor_json') else None
                expected={key:raw[key] for key in fields if key!='source_actor_json'}
                expected['source_actor_json']=research.canonical(actor) if actor is not None else None
                if expected!=basis:research.error(409,'Disclosure differs from the known original journal')
        checked.add(identity)
        sources[identity]=basis
