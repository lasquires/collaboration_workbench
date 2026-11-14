"""Keep portable project task keys independent of installation-local SQLite keys.

Mappings are local metadata, never shared rows or user aliases. Original exchange
bundles and historical prose remain unchanged; recovery translates scoped views.
"""
import copy
import json
import uuid
try:
    from . import roadmap_research as research
except ImportError:
    import roadmap_research as research


def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS task_exchange_keys(project_id TEXT NOT NULL,task_id TEXT NOT NULL,exchange_id TEXT NOT NULL,PRIMARY KEY(project_id,task_id),UNIQUE(project_id,exchange_id),FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid))')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS task_exchange_keys_{action.lower()} BEFORE {action} ON task_exchange_keys BEGIN SELECT RAISE(ABORT,'task exchange bindings are immutable'); END")


def maps(db,project):
    rows=db.execute('SELECT t.id,t.uid,COALESCE(k.exchange_id,t.id) AS exchange_id FROM tasks t LEFT JOIN task_exchange_keys k ON k.project_id=t.project_id AND k.task_id=t.uid WHERE t.project_id=?',(project,)).fetchall()
    forward={row['id']:row['exchange_id'] for row in rows}
    if len(set(forward.values()))!=len(forward):research.error(409,'Ambiguous project task exchange keys')
    return forward,{value:key for key,value in forward.items()}


def task_record(row,translation):
    row=dict(row)
    old=row['id'];row['id']=translation.get(old,old)
    if row['parent_id'] is not None:row['parent_id']=translation.get(row['parent_id'],row['parent_id'])
    record=json.loads(row['record_json']);original=copy.deepcopy(record)
    if record.get('id')==old:record['id']=row['id']
    if isinstance(record.get('dependencies'),list):record['dependencies']=[translation.get(key,key) for key in record['dependencies']]
    if record!=original:row['record_json']=research.canonical(record)
    return row


def link_record(row,translation):
    return {**row,'source_id':translation.get(row['source_id'],row['source_id']),'target_id':translation.get(row['target_id'],row['target_id'])}


def export_records(db,project,records):
    forward,_=maps(db,project)
    records['tasks']=[task_record(row,forward) for row in records['tasks']]
    records['task_links']=[link_record(row,forward) for row in records['task_links']]
    # Key ordering is part of the content hash and must survive remapping.
    records['tasks'].sort(key=lambda row:research.canonical([row['id']]))
    records['task_links'].sort(key=lambda row:research.canonical([row['source_id'],row['target_id'],row['kind']]))


def validate_tasks(db,project,rows):
    forward,reverse=maps(db,project)
    seen=set()
    for row in rows:
        if not isinstance(row['uid'],str) or not row['uid'] or len(row['uid'])>200:research.error(400,'Invalid logical task identity')
        if row['uid'] in seen:research.error(400,'Duplicate logical task identity')
        seen.add(row['uid'])
        local=db.execute('SELECT id,project_id FROM tasks WHERE uid=?',(row['uid'],)).fetchone()
        if local is not None and (local['project_id']!=project or forward[local['id']]!=row['id']):
            research.error(409,'Task identity collision cannot alter UUID, project or exchange key')
        if row['id'] in reverse:
            existing=db.execute('SELECT uid FROM tasks WHERE id=?',(reverse[row['id']],)).fetchone()
            if existing[0]!=row['uid']:research.error(409,'Task identity collision: project exchange key is already bound')


def prepare_import(db,project,rows):
    """Allocate keys without writes; caller persists bindings in its savepoint."""
    _,translation=maps(db,project)
    occupied={row[0] for row in db.execute('SELECT id FROM tasks')}
    source_keys={row['id'] for row in rows}
    bindings=[]
    for row in rows:
        local=db.execute('SELECT id FROM tasks WHERE uid=? AND project_id=?',(row['uid'],project)).fetchone()
        if local is not None:identity=local[0]
        else:
            identity=row['id']
            if identity in occupied:
                identity='task-'+str(uuid.uuid5(uuid.NAMESPACE_URL,project+':'+row['uid']))
                while identity in occupied or identity in source_keys:identity='task-'+str(uuid.uuid4())
        occupied.add(identity);translation[row['id']]=identity
        bindings.append((project,row['uid'],row['id']))
    return translation,bindings


def snapshot(db,project,value,*,to_local=False):
    """Translate storage fields in a copy, validating UID binding on recovery."""
    if not isinstance(value,dict) or any(not isinstance(value.get(key),list) for key in ('tasks','links','containment')):
        research.error(409,'Merge history has incompatible scoped snapshots')
    for row in value['tasks']:
        if (not isinstance(row,dict) or not {'id','uid','project_id','parent_id','record_json'}<=set(row)
                or any(not isinstance(row[key],str) for key in ('id','uid','project_id','record_json'))):
            research.error(409,'Merge history has incompatible task row')
        try:record=json.loads(row['record_json'])
        except ValueError:research.error(409,'Merge history has incompatible task record')
        if not isinstance(record,dict):research.error(409,'Merge history has incompatible task record')
    for row in value['links']:
        if not isinstance(row,dict) or any(not isinstance(row.get(key),str) for key in ('source_id','target_id','kind')):
            research.error(409,'Merge history has incompatible link row')
    result=copy.deepcopy(value);forward,reverse=maps(db,project)
    translation=reverse if to_local else forward
    if to_local:
        for row in result.get('tasks',[]):
            if not isinstance(row,dict):research.error(409,'Merge history has incompatible task row')
            actual=db.execute('SELECT id FROM tasks WHERE project_id=? AND uid=?',(project,row.get('uid'))).fetchone()
            if actual is None or row.get('project_id')!=project or forward[actual[0]]!=row.get('id'):
                research.error(409,'Merge history task exchange binding is incompatible')
    result['tasks']=[task_record(row,translation) for row in result['tasks']]
    result['links']=[link_record(row,translation) for row in result['links']]
    result['links'].sort(key=lambda row:(row['source_id'],row['target_id'],row['kind']))
    return result
