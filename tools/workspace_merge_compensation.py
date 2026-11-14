"""Reviewed, append-only compensation for a merge with intervening edits."""
import json
import uuid
from datetime import datetime,timezone
try:
    from . import roadmap_research as research, workspace_task_namespace as task_namespace
except ImportError:
    import roadmap_research as research, workspace_task_namespace as task_namespace


def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS merge_compensations(id TEXT NOT NULL,project_id TEXT NOT NULL,merge_id TEXT NOT NULL,request_id TEXT NOT NULL,preview_json TEXT NOT NULL,choices_json TEXT NOT NULL,applied_json TEXT NOT NULL,source_references_json TEXT NOT NULL,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,merge_id) REFERENCES merge_records(project_id,id))')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS merge_compensations_{action.lower()} BEFORE {action} ON merge_compensations BEGIN SELECT RAISE(ABORT,'merge compensation is immutable'); END")


def _structure():
    try:
        from . import roadmap_structure
    except ImportError:
        import roadmap_structure
    return roadmap_structure


def _presence(mapping,key):return {'present':key in mapping,'value':mapping.get(key)}


def _history(db,project,merge):
    structure=_structure()
    try:
        snapshots=[task_namespace.snapshot(db,project,json.loads(merge[field]),to_local=True) for field in ('before_json','after_json')]
        sets=[]
        for snapshot in snapshots:
            if not isinstance(snapshot,dict) or any(not isinstance(snapshot.get(key),list) for key in ('tasks','links','containment')):raise ValueError()
            identities=set()
            for row in snapshot['tasks']:
                required={'uid','project_id','id','record_json','original_json','parent_id','sibling_order','area_id','decomposition_complete','archive_reason','archived_at'}
                if not isinstance(row,dict) or not required<=set(row) or row['project_id']!=project or not isinstance(row['uid'],str) or row['uid'] in identities:raise ValueError()
                actual=structure.task_row(db,project,row['uid'])
                if row['id']!=actual['id']:raise ValueError()
                record=json.loads(row['record_json']);original=json.loads(row['original_json'])
                if not isinstance(record,dict) or not isinstance(original,dict) or record.get('id')!=row['id']:raise ValueError()
                if type(row['sibling_order']) is not int or row['sibling_order']<0 or type(row['decomposition_complete']) is not int or row['decomposition_complete'] not in (0,1):raise ValueError()
                if row['parent_id'] is not None:structure.task_row(db,project,row['parent_id'])
                if row['area_id'] is not None:research.scoped(db,'areas',project,row['area_id'])
                if any(row[key] is not None and not isinstance(row[key],str) for key in ('archive_reason','archived_at')):raise ValueError()
                identities.add(row['uid'])
            for link in snapshot['links']:
                if not isinstance(link,dict) or set(link)!={'source_id','target_id','kind','rationale','review_state'} or link['kind'] not in ('related','depends_on') or not isinstance(link['rationale'],str) or link['review_state'] not in ('unreviewed','lead','confirmed','legacy'):raise ValueError()
                for key in ('source_id','target_id'):structure.task_row(db,project,link[key])
            for edge in snapshot['containment']:
                if not isinstance(edge,dict) or edge.get('project_id')!=project or edge.get('child_id') not in identities or type(edge.get('required_for_parent')) is not int or edge['required_for_parent'] not in (0,1):raise ValueError()
                if edge.get('parent_id') is not None:structure.task_row(db,project,edge['parent_id'])
            sets.append(identities)
        if sets[0]!=sets[1] or not {merge['survivor_id'],merge['absorbed_id']}<=sets[0]:raise ValueError()
        return snapshots
    except (ValueError,TypeError,KeyError):
        research.error(409,'Merge history has incompatible scoped snapshots; review its original source')


def _context(db,project,merge):
    structure=_structure();before,after=_history(db,project,merge)
    identities={row['uid'] for snapshot in (before,after) for row in snapshot['tasks']}
    for row in before['tasks']:
        if row['parent_id']:identities.add(structure.task_row(db,project,row['parent_id'])['uid'])
    pair=(merge['survivor_id'],merge['absorbed_id'])
    legacy=[structure.task_row(db,project,uid)['id'] for uid in pair]
    for link in db.execute('SELECT * FROM task_links WHERE source_id IN (?,?) OR target_id IN (?,?)',(*legacy,*legacy)):
        for field in ('source_id','target_id'):identities.add(structure.task_row(db,project,link[field])['uid'])
    for uid in list(identities):
        for parent in structure.ancestors(db,project,uid):identities.add(parent['uid'])
    current=structure._merge_snapshot(db,project,identities,pair)
    redirects=[dict(row) for row in db.execute('SELECT * FROM task_redirects WHERE project_id=? ORDER BY absorbed_id',(project,)) if row['absorbed_id'] in identities or row['survivor_id'] in identities]
    current['redirects']=redirects
    return before,after,current


def preview_merge_compensation(db,project,merge_id):
    merge=research.scoped(db,'merge_records',project,merge_id)
    before,after,current=_context(db,project,merge)
    original={row['uid']:row for row in before['tasks']};merged={row['uid']:row for row in after['tasks']};latest={row['uid']:row for row in current['tasks']}
    actions=[]
    def action(kind,identity,field,old,new,present):
        if old==new:return
        actions.append({'key':research.canonical([kind,identity,field]),'kind':kind,'task_id':identity,'field':field,
            'original':old,'after_merge':new,'current':present,'conflict':present!=new,
            'recommended':'current' if present!=new else 'original'})
    for uid,old in original.items():
        new=merged[uid];present=latest[uid]
        old_record=json.loads(old['record_json']);new_record=json.loads(new['record_json']);current_record=json.loads(present['record_json'])
        excluded={'id','claim','reserved_paths','session_id','lease','status','acceptance_gate_passed','dependencies'}
        for key in sorted((set(old_record)|set(new_record))-excluded):
            action('task_field',uid,key,_presence(old_record,key),_presence(new_record,key),_presence(current_record,key))
        columns=['parent_id','sibling_order','area_id','decomposition_complete']
        if uid!=merge['absorbed_id']:columns+=['archive_reason','archived_at']
        for key in columns:action('task_column',uid,key,_presence(old,key),_presence(new,key),_presence(present,key))
    old_edges={row['child_id']:row for row in before['containment']};new_edges={row['child_id']:row for row in after['containment']};current_edges={row['child_id']:row for row in current['containment']}
    for uid in sorted(set(old_edges)&set(new_edges)):
        field='required_for_parent'
        action('containment',uid,field,_presence(old_edges[uid],field),_presence(new_edges[uid],field),_presence(current_edges[uid],field))
    def link_index(snapshot):return {research.canonical([row['source_id'],row['target_id'],row['kind']]):row for row in snapshot['links']}
    old_links=link_index(before);new_links=link_index(after);current_links=link_index(current)
    for key in sorted(set(old_links)|set(new_links)):
        action('link',key,'row',_presence(old_links,key),_presence(new_links,key),_presence(current_links,key))
    problems=[]
    if merge['reversed_by'] is not None:problems.append({'kind':'already_reversed','request_id':merge['reversed_by']})
    own=[row for row in current['redirects'] if row['merge_id']==merge_id and row['active']]
    if not own:problems.append({'kind':'missing_active_redirect'})
    if any(row['active'] and row['merge_id']!=merge_id for row in current['redirects']):problems.append({'kind':'overlapping_merge','reason':'Reconcile later overlapping merges before compensating this scope'})
    guard={'merge_id':merge_id,'reversed_by':merge['reversed_by'],'current':current}
    return {'merge_id':merge_id,'survivor_id':merge['survivor_id'],'absorbed_id':merge['absorbed_id'],
        'guard_digest':research.digest(guard),'guards':{uid:{'record_revision':row['revision'],'structure_revision':row['structure_revision']} for uid,row in sorted(latest.items())},
        'actions':actions,'snapshots':{'original':before,'after_merge':after,'current':current},
        'claims':[row for row in current['claims'] if row['released_at'] is None],
        'problems':problems,'apply_available':not problems,'choice_checks_required':True,
        'required_effects':{'restore_separate_identity':merge['absorbed_id'],'unarchive_absorbed':True,'pair_acceptance_requires_review':True}}


def _public_snapshot(snapshot):
    # Only known ownership fields are projected. Arbitrary authored values,
    # including objects with a field named "claims", remain exact.
    result={key:value for key,value in snapshot.items() if key!='claims'}
    result['tasks']=[]
    for source in snapshot['tasks']:
        row=dict(source)
        for key in ('record_json','original_json'):
            record=json.loads(row[key])
            for local in ('claim','reserved_paths','session_id','lease'):record.pop(local,None)
            row[key]=research.canonical(record)
        result['tasks'].append(row)
    return result


def _public_preview(preview):
    result={key:value for key,value in preview.items() if key!='claims'}
    result['snapshots']={key:_public_snapshot(value) for key,value in preview['snapshots'].items()}
    return result


def apply_compensation(db,project,principal,op,request_id,row,rationale):
    structure=_structure();merge=research.scoped(db,'merge_records',project,op.get('merge_id'))
    if merge['survivor_id']!=row['uid']:research.error(400,'Compensation task must be the merge survivor')
    preview=preview_merge_compensation(db,project,merge['id'])
    if preview['guard_digest']!=op.get('preview_guard'):research.error(409,'Compensation preview changed; reread and reconcile explicitly')
    if preview['problems']:research.error(409,'Resolve compensation preview problems first')
    choices=op.get('choices');keys={item['key'] for item in preview['actions']}
    if not isinstance(choices,dict) or set(choices)!=keys or any(value not in ('original','current') for value in choices.values()):research.error(400,'Choose original or current explicitly for every compensation action')
    sources=op.get('source_references')
    if not isinstance(sources,list) or not 0<len(sources)<=100:research.error(400,'Compensation requires source references')
    for source in sources:research.text(source,'compensation source reference',4000)
    context=preview['snapshots']['current'];tasks={item['uid']:dict(item) for item in context['tasks']}
    records={uid:json.loads(item['record_json']) for uid,item in tasks.items()}
    links={research.canonical([item['source_id'],item['target_id'],item['kind']]):dict(item) for item in context['links']}
    edges={item['child_id']:dict(item) for item in context['containment']}
    for item in preview['actions']:
        selected=item[choices[item['key']]];uid=item['task_id'];field=item['field']
        if item['kind']=='task_field':
            if selected['present']:records[uid][field]=selected['value']
            else:records[uid].pop(field,None)
        elif item['kind']=='task_column':tasks[uid][field]=selected['value']
        elif item['kind']=='containment':edges[uid][field]=selected['value']
        elif selected['present']:links[uid]=dict(selected['value'])
        else:links.pop(uid,None)
    pair={merge['survivor_id'],merge['absorbed_id']}
    for uid in pair:
        records[uid]['status']='review';records[uid]['acceptance_gate_passed']=False
    tasks[merge['absorbed_id']].update(archive_reason=None,archived_at=None)
    for uid in tasks:tasks[uid]['record_json']=research.canonical(records[uid])
    touched=set(pair)
    original_tasks={item['uid']:item for item in context['tasks']}
    writable=['record_json','parent_id','sibling_order','archive_reason','archived_at','area_id','decomposition_complete']
    for uid,item in tasks.items():
        if any(item[field]!=original_tasks[uid][field] for field in writable):touched.add(uid)
    original_links={research.canonical([item['source_id'],item['target_id'],item['kind']]):item for item in context['links']}
    by_legacy={item['id']:uid for uid,item in tasks.items()}
    for key in set(original_links)|set(links):
        if original_links.get(key)!=links.get(key):
            link=links.get(key) or original_links[key];touched.add(by_legacy[link['source_id']])
    for uid,edge in edges.items():
        previous=next(item for item in context['containment'] if item['child_id']==uid)
        if edge['required_for_parent']!=previous['required_for_parent']:touched.add(uid)
    affected=set(touched)
    for tree in (original_tasks,tasks):
        for uid in list(touched):
            seen=set();current=uid
            while current in tree and current not in seen:
                seen.add(current);parent=by_legacy.get(tree[current]['parent_id'])
                if parent is None:break
                affected.add(parent);current=parent
    for uid in affected:
        old=original_tasks[uid]
        if json.loads(old['record_json'])['status']=='done':research.error(409,'Reopen completed affected scope before compensation')
        if old['archive_reason'] is not None and uid!=merge['absorbed_id']:research.error(409,'Reopen archived affected scope before compensation')
    if any(item['task_id'] in affected for item in preview['claims']):research.error(409,'Release or hand off affected claims before compensation')
    current_time=datetime.now(timezone.utc).isoformat()
    for uid in touched:
        item=tasks[uid]
        db.execute('UPDATE tasks SET '+','.join(field+'=?' for field in writable)+',updated_at=? WHERE project_id=? AND uid=?',(*[item[field] for field in writable],current_time,project,uid))
    for key in set(original_links)|set(links):
        if original_links.get(key)==links.get(key):continue
        db.execute('DELETE FROM task_links WHERE source_id=? AND target_id=? AND kind=?',tuple(json.loads(key)))
        if key in links:
            link=links[key];db.execute('INSERT INTO task_links VALUES(?,?,?,?,?)',tuple(link[field] for field in ('source_id','target_id','kind','rationale','review_state')))
    for uid,edge in edges.items():db.execute('UPDATE containment SET required_for_parent=? WHERE project_id=? AND child_id=?',(edge['required_for_parent'],project,uid))
    # Dependency arrays are a compatibility mirror of the chosen final links.
    for uid in list(touched):
        item=tasks[uid];record=json.loads(db.execute('SELECT record_json FROM tasks WHERE uid=?',(uid,)).fetchone()[0])
        targets=[value[0] for value in db.execute("SELECT target_id FROM task_links WHERE source_id=? AND kind='depends_on' ORDER BY target_id",(item['id'],))]
        if 'dependencies' in record or targets:
            record['dependencies']=targets;db.execute('UPDATE tasks SET record_json=? WHERE uid=?',(research.canonical(record),uid))
    db.execute('UPDATE task_redirects SET active=0 WHERE project_id=? AND merge_id=?',(project,merge['id']))
    db.execute('UPDATE merge_records SET reversed_by=? WHERE project_id=? AND id=?',(request_id,project,merge['id']))
    for uid in affected:structure.invalidate_gate(db,uid)
    identity=str(uuid.uuid4())
    pending={'id':identity,'project_id':project,'merge_id':merge['id'],'request_id':request_id,'preview':preview,
        'choices':dict(choices),'rationale':rationale,'affected_task_ids':sorted(affected),'sources':sources,'created_by':principal,'created_at':current_time}
    return {'type':'task','id':row['uid'],'legacy_id':row['id'],'merge_id':merge['id'],'compensation_id':identity,
        'changed_task_ids':sorted(affected),'structure_task_ids':sorted(affected),'pending_compensation':pending,
        'event':{'rationale':rationale,'merge_id':merge['id'],'compensation_id':identity,'review':preview,'choices':choices,'source_references':sources,'acceptance_requires_review':True}}


def finalize_compensations(db,pending):
    for item in pending:
        structure=_structure();preview=item['preview']
        snapshot=structure._merge_snapshot(db,item['project_id'],preview['guards'],(preview['survivor_id'],preview['absorbed_id']))
        public=_public_preview(preview)
        public['format']='merge-compensation/v1'
        public['rationale']=item['rationale']
        public['affected_task_ids']=item['affected_task_ids']
        public['private_context_sha256']=research.digest(preview)
        public['omitted_local_state']=['claims','sessions','reservations']
        db.execute('INSERT INTO merge_compensations VALUES(?,?,?,?,?,?,?,?,?,?)',(item['id'],item['project_id'],item['merge_id'],item['request_id'],research.canonical(public),research.canonical(item['choices']),research.canonical(_public_snapshot(snapshot)),research.canonical(item['sources']),item['created_by'],item['created_at']))


def read(db,project,*,limit=None,cursor=None,merge_id=None,task_id=None,compensation_id=None):
    try:
        from .roadmap_projection import API_VERSION,_coverage,_encode,_decode
    except ImportError:
        from roadmap_projection import API_VERSION,_coverage,_encode,_decode
    coverage=_coverage(db,project)
    limit=50 if limit is None else limit
    if type(limit) is not int or not 1<=limit<=100:research.error(400,'Audit page limit must be 1–100')
    if merge_id is not None:research.scoped(db,'merge_records',project,merge_id)
    uid=_structure().task_row(db,project,task_id)['uid'] if task_id is not None else None
    if compensation_id is not None:research.text(compensation_id,'compensation identity',200)
    meta=db.execute('SELECT store_id,revision FROM store_meta').fetchone();revision=str(meta[0])+':'+str(meta[1])
    binding={'resource':'merge-compensations','project_id':project,'revision':revision,'limit':limit,'merge_id':merge_id,'task_id':uid,'compensation_id':compensation_id}
    offset=0
    if cursor is not None:
        page=_decode(cursor)
        if page.get('revision')!=revision:
            try:research.error(409,'Audit snapshot cursor expired; restart required')
            except Exception as error:error.restart_required=True;raise
        if set(page)!=set(binding)|{'offset'} or any(page.get(key)!=value for key,value in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:research.error(400,'Audit cursor does not match project/filter')
        offset=page['offset']
    columns='c.*' if compensation_id is not None else 'c.id,c.project_id,c.merge_id,c.request_id,c.created_by,c.created_at'
    query='SELECT '+columns+' FROM merge_compensations c WHERE c.project_id=?';args=[project]
    if merge_id is not None:query+=' AND c.merge_id=?';args.append(merge_id)
    if uid is not None:
        query+=" AND (EXISTS(SELECT 1 FROM json_each(c.preview_json,'$.affected_task_ids') t WHERE t.value=?) OR EXISTS(SELECT 1 FROM merge_records m WHERE m.project_id=c.project_id AND m.id=c.merge_id AND (m.survivor_id=? OR m.absorbed_id=?)))";args.extend([uid,uid,uid])
    if compensation_id is not None:query+=' AND c.id=?';args.append(compensation_id)
    query+=' ORDER BY c.created_at DESC,c.id DESC LIMIT ? OFFSET ?';args.extend([limit+1,offset])
    rows=db.execute(query,args).fetchall();items=[]
    for row in rows[:limit]:
        item=dict(row)
        for key in list(item):
            if key.endswith('_json'):
                try:item[key[:-5]]=json.loads(item.pop(key))
                except (ValueError,TypeError):research.error(409,'Imported compensation audit has incompatible content')
        items.append(item)
    return {'api_version':API_VERSION,'project_id':project,'revision':revision,'coverage':{**coverage,'source':'immutable merge compensation history'},'items':items,
        'next_cursor':_encode({**binding,'offset':offset+limit}) if len(rows)>limit else None}


def validate_public_audit(row,project,task_ids):
    """Reject malformed shared history before it can poison scoped reads."""
    try:
        preview=json.loads(row['preview_json']);choices=json.loads(row['choices_json']);applied=json.loads(row['applied_json']);sources=json.loads(row['source_references_json'])
        if (not isinstance(preview,dict) or preview.get('format')!='merge-compensation/v1'
                or preview.get('merge_id')!=row['merge_id'] or not isinstance(preview.get('rationale'),str)
                or not isinstance(preview.get('actions'),list) or not isinstance(preview.get('guards'),dict)
                or not isinstance(preview.get('affected_task_ids'),list) or not set(preview['affected_task_ids'])<=task_ids
                or not set(preview['guards'])<=task_ids or 'claims' in preview
                or not isinstance(choices,dict) or any(value not in ('original','current') for value in choices.values())
                or not isinstance(sources,list) or not sources or any(not isinstance(value,str) for value in sources)):
            raise ValueError()
        keys=[action['key'] for action in preview['actions']]
        if any(not isinstance(key,str) for key in keys) or len(set(keys))!=len(keys) or set(choices)!=set(keys):raise ValueError()
        snapshots=preview['snapshots']
        if not isinstance(snapshots,dict) or set(snapshots)!={'original','after_merge','current'}:raise ValueError()
        for snapshot in [*snapshots.values(),applied]:
            if not isinstance(snapshot,dict) or 'claims' in snapshot or any(not isinstance(snapshot.get(key),list) for key in ('tasks','links','containment')):raise ValueError()
            for task in snapshot['tasks']:
                if not isinstance(task,dict) or task.get('project_id')!=project or task.get('uid') not in task_ids:raise ValueError()
                for key in ('record_json','original_json'):
                    record=json.loads(task[key])
                    if not isinstance(record,dict) or set(record)&{'claim','reserved_paths','session_id','lease'}:raise ValueError()
    except (ValueError,TypeError,KeyError):research.error(400,'Invalid portable merge compensation audit')
