"""Deterministic bounded workspace reads; no writes or generated authority."""
import base64
import hashlib
import json
from datetime import datetime, timezone

API_VERSION = 'research-workspace/v1'
LIMITS = {'list_items':100,'neighborhood_nodes':250,'batch_records':500,'body_bytes':8*1024*1024,'history_items':50,'neighborhood_depth':20,'record_guards':10000}


def _error(status, message, **attributes):
    try:
        from .roadmap_store import StoreError
    except ImportError:
        from roadmap_store import StoreError
    error = StoreError(status,message)
    for key,value in attributes.items():
        setattr(error,key,value)
    raise error


def resolve_entity(db, project_id, entity_type, key):
    if entity_type != 'task' or not isinstance(key,str):
        _error(404,'Scoped entity not found')
    row = db.execute('SELECT uid FROM tasks WHERE project_id=? AND uid=?', (project_id,key)).fetchone()
    if not row:
        row = db.execute("SELECT t.uid FROM entity_aliases a JOIN tasks t ON t.uid=a.entity_id AND t.project_id=a.project_id WHERE a.project_id=? AND a.entity_type='task' AND a.alias=?", (project_id,key)).fetchone()
    if not row:
        _error(404,'Scoped entity not found')
    uid=row['uid'];seen=set()
    while uid not in seen:
        seen.add(uid)
        redirect=db.execute('SELECT survivor_id FROM task_redirects WHERE project_id=? AND absorbed_id=? AND active=1',(project_id,uid)).fetchone()
        if redirect is None: return uid
        uid=redirect['survivor_id']
    _error(409,'Task redirect cycle requires review')


def _encode(value):
    return base64.urlsafe_b64encode(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).decode().rstrip('=')


def _decode(value):
    if not isinstance(value,str) or len(value)>3000:
        _error(400,'Invalid cursor')
    try:
        result = json.loads(base64.b64decode(value+'='*(-len(value)%4),altchars=b'-_',validate=True))
        if not isinstance(result,dict):
            raise ValueError()
        return result
    except (ValueError,TypeError,UnicodeError):
        _error(400,'Invalid cursor')


def _event_cursor(db,project_id,sequence=None):
    if sequence is None:
        sequence = db.execute('SELECT COALESCE(MAX(sequence),0) FROM workspace_events WHERE project_id=?',(project_id,)).fetchone()[0]
    return _encode({'kind':'events','project_id':project_id,'sequence':sequence})


def _coverage(db,project_id):
    row = db.execute('SELECT coverage_json FROM projects WHERE id=?',(project_id,)).fetchone()
    if not row:
        _error(404,'Scoped project not found')
    result = json.loads(row[0])
    return {'source':'SQLite workspace','complete':False,'indexed':['imported tasks'],'uncovered':['research observations','historical tasks outside imported coverage'],**result}


def _rows(db,project_id):
    rows={row['uid']:dict(row) for row in db.execute('SELECT * FROM tasks WHERE project_id=? ORDER BY id,uid',(project_id,))}
    for row in rows.values():
        aliases=[r[0] for r in db.execute("SELECT alias FROM entity_aliases WHERE project_id=? AND entity_type='task' AND entity_id=? ORDER BY CASE WHEN alias=? THEN 1 ELSE 0 END,alias",(project_id,row['uid'],row['id']))]
        row['alias']=aliases[0] if aliases else row['id']
    return rows


def progress_projection(db, project_id, task_ids, now):
    rows = _rows(db,project_id)
    children = {}
    required = {}
    for row in db.execute('SELECT * FROM containment WHERE project_id=? ORDER BY sibling_order,child_id',(project_id,)):
        children.setdefault(row['parent_id'],[]).append(row['child_id'])
        required[row['child_id']] = bool(row['required_for_parent'])
    result = {}
    redirects={r['absorbed_id']:r['survivor_id'] for r in db.execute('SELECT * FROM task_redirects WHERE project_id=? AND active=1',(project_id,))}
    def canonical(uid):
        seen=set()
        while uid in redirects and uid not in seen: seen.add(uid);uid=redirects[uid]
        return uid
    failures={canonical(r[0]) for r in db.execute("SELECT DISTINCT task_id FROM attempts WHERE project_id=? AND resolution_state='unresolved' AND outcome='failed'",(project_id,))}
    counts={}
    for attempt in db.execute("SELECT task_id,boundary_id FROM attempts WHERE project_id=? AND resolution_state='unresolved' AND outcome IN ('failed','blocked','inconclusive')",(project_id,)):
        boundary=(canonical(attempt['task_id']),attempt['boundary_id']);counts[boundary]=counts.get(boundary,0)+1
    routing={uid for (uid,boundary),count in counts.items() if count>=2}
    parent_uids={r['id']:r['uid'] for r in rows.values()}
    descendant_failures={uid:set() for uid in rows}
    for failed in failures:
        if failed not in rows: continue
        parent=parent_uids.get(rows[failed]['parent_id']);seen=set()
        while parent and parent not in seen:
            seen.add(parent);descendant_failures[parent].add(failed)
            parent=parent_uids.get(rows[parent]['parent_id'])
    for uid in task_ids:
        if uid not in rows:
            _error(404,'Scoped task not found')
        row = rows[uid]
        record = json.loads(row['record_json'])
        buckets = {'required':{'total':0,'by_state':{},'accepted_done':0},'optional':{'total':0,'by_state':{}}}
        archived = 0
        for child_id in children.get(uid,[]):
            child = rows[child_id]
            if child['archive_reason'] is not None or child['archived_at'] is not None:
                archived += 1
                continue
            bucket = buckets['required' if required[child_id] else 'optional']
            state = json.loads(child['record_json'])['status']
            bucket['total'] += 1
            bucket['by_state'][state] = bucket['by_state'].get(state,0)+1
            if required[child_id] and state=='done' and child['acceptance_state']=='passed' and child['validation_id']:
                bucket['accepted_done'] += 1
        claim = record.get('claim')
        claim_state = {'state':'unclaimed','principal_id':None,'label':None,'expires_at':None}
        if claim:
            principal = db.execute("SELECT p.id FROM principals p JOIN project_members m ON m.principal_id=p.id WHERE m.project_id=? AND p.label=? AND p.identity_kind='trusted_local_label' ORDER BY p.id LIMIT 1", (project_id,claim['by'])).fetchone()
            expiry = claim.get('expires_at')
            state = 'handoff_needed'
            if expiry:
                try:
                    state = 'live' if datetime.fromisoformat(expiry.replace('Z','+00:00'))>datetime.fromisoformat(now.replace('Z','+00:00')) else 'expired'
                except (ValueError,TypeError):
                    state = 'handoff_needed'
            claim_state = {'state':state,'principal_id':principal[0] if principal else None,'label':claim['by'],'expires_at':expiry}
        lease=db.execute('SELECT c.*,p.label FROM claims c JOIN principals p ON p.id=c.principal_id WHERE c.project_id=? AND c.task_id=? AND c.released_at IS NULL',(project_id,uid)).fetchone()
        if lease and lease['expires_at']:
            try:
                live=datetime.fromisoformat(lease['expires_at'].replace('Z','+00:00'))>datetime.fromisoformat(now.replace('Z','+00:00'))
                claim_state={'state':'live' if live else 'expired','principal_id':lease['principal_id'],'label':lease['label'],'expires_at':lease['expires_at']}
            except (ValueError,TypeError): pass
        group=[identity for identity in rows if canonical(identity)==uid];marks=','.join('?' for _ in group) or 'NULL'
        new_hints=db.execute(f"SELECT COUNT(*) FROM hints h JOIN questions q ON q.project_id=h.project_id AND q.id=h.question_id WHERE h.project_id=? AND q.owner_task_id IN ({marks}) AND h.state='new'",(project_id,*group)).fetchone()[0]
        blockers=[r[0] for r in db.execute(f'SELECT reason FROM task_blockers WHERE project_id=? AND task_id IN ({marks}) AND resolved_at IS NULL ORDER BY created_at,id',(project_id,*group))]
        result[uid] = {**buckets,'decomposition_complete':bool(row['decomposition_complete']),
                       'own_acceptance':row['acceptance_state'],'unresolved_failure':uid in failures,
                       'failed_descendant_tasks':len(descendant_failures[uid]),'claim':claim_state,
                       'routing_review_required':uid in routing,
                       'blocked_reason':'; '.join(blockers) or record.get('blocked_reason') or (record.get('next_step') if record['status']=='blocked' else None),
                       'review_required':record['status']=='review' or claim_state['state'] in ('expired','handoff_needed') or (record['status']=='done' and row['acceptance_state']!='passed'),
                       'new_hint_count':new_hints,'archived_children':archived}
    return result


def _task(row, rows, progress):
    record = json.loads(row['record_json'])
    parent = next((r['uid'] for r in rows.values() if r['id']==row['parent_id']),None)
    return {'id':row['uid'],'alias':row['alias'],'legacy_id':row['id'],'project_id':row['project_id'],
            'parent_id':parent,'title':record['title'],'status':record['status'],
            'summary':record.get('summary',''),'owner':record.get('owner'),
            'record_revision':row['revision'],'structure_revision':row['structure_revision'],
            'archived_at':row['archived_at'],'archive_reason':row['archive_reason'],
            'progress':progress[row['uid']]}


def read_projection(db, resource, project_id=None, *, key=None, limit=None, cursor=None, depth=1, after=None, now=None):
    meta = db.execute('SELECT * FROM store_meta WHERE singleton=1').fetchone()
    revision = f"{meta['store_id']}:{meta['revision']}"
    now = now or datetime.now(timezone.utc).isoformat()
    if resource=='projects':
        return {'api_version':API_VERSION,'project_id':None,'revision':revision,'items':[
            {**dict(row),'record_revision':row['revision'],'identity_kind':'trusted_local_label'} for row in db.execute('SELECT id,slug,goal,revision FROM projects WHERE archived_at IS NULL ORDER BY slug,id')],
            'next_cursor':None,'coverage':{'source':'SQLite workspace','complete':False,'uncovered':['projects not registered']}}
    if not isinstance(project_id,str) or not project_id:
        _error(400,'project_id is required')
    coverage = _coverage(db,project_id)
    envelope = {'api_version':API_VERSION,'project_id':project_id,'revision':revision,'coverage':coverage,'next_cursor':None}
    if resource=='capabilities':
        try:
            from . import workspace_concurrency
        except ImportError:
            import workspace_concurrency
        local = db.execute("SELECT principal_id FROM local_principals WHERE project_id=? AND label='local'",(project_id,)).fetchone()
        return {**envelope,'capabilities':{'limits':LIMITS,'identity_kind':'trusted_local_label','authenticated':False,'local_principal_id':local[0] if local else None,'concurrency_modes':['global','records'],'record_concurrency_types':sorted(workspace_concurrency.TYPES),'record_concurrency_operations':sorted(workspace_concurrency.OPERATIONS),'research_collections_available':True,'documents_available':True,'journal_sharing_review_available':True}}
    max_limit = LIMITS['neighborhood_nodes'] if resource=='neighborhood' else LIMITS['list_items']
    limit = max_limit if limit is None else limit
    if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=max_limit:
        _error(400,'Page limit outside configured bounds',limits=LIMITS)
    if resource=='events':
        sequence = 0
        if after:
            data = _decode(after)
            if set(data)!={'kind','project_id','sequence'} or data['kind']!='events' or data['project_id']!=project_id or type(data['sequence']) is not int or data['sequence']<0:
                _error(400,'Invalid event cursor')
            sequence=data['sequence']
        maximum=db.execute('SELECT COALESCE(MAX(sequence),0) FROM workspace_events WHERE project_id=?',(project_id,)).fetchone()[0]
        if sequence>maximum:
            _error(409,'Event cursor outside retained history',restart_required=True)
        rows=list(db.execute('SELECT * FROM workspace_events WHERE project_id=? AND sequence>? ORDER BY sequence LIMIT ?',(project_id,sequence,limit+1)))
        items=[]
        for row in rows[:limit]:
            item=dict(row)
            for name in ('before','after'):
                raw=item.pop(name+'_json')
                item[name]=json.loads(raw) if raw else None
            items.append(item)
        return {**envelope,'items':items,'next_cursor':_event_cursor(db,project_id,items[-1]['sequence']) if len(rows)>limit else None,'event_cursor':_event_cursor(db,project_id,items[-1]['sequence'] if items else sequence)}
    rows=_rows(db,project_id)
    uid=resolve_entity(db,project_id,'task',key) if resource in ('context','neighborhood','history') else None
    if uid is not None:
        original=db.execute("SELECT uid FROM tasks WHERE project_id=? AND (uid=? OR uid IN (SELECT entity_id FROM entity_aliases WHERE project_id=? AND entity_type='task' AND alias=?))",(project_id,key,project_id,key)).fetchone()
        if original and original['uid']!=uid:
            envelope['redirect']={'from':original['uid'],'to':uid}
    binding={'resource':resource,'project_id':project_id,'revision':revision,'key':uid,'depth':depth,'limit':limit}
    offset=0
    if cursor:
        page=_decode(cursor)
        if page.get('revision')!=revision:
            _error(409,'Snapshot cursor expired; restart required',restart_required=True)
        if {k:page.get(k) for k in binding}!=binding or set(page)!=set(binding)|{'offset'} or type(page.get('offset')) is not int or page['offset']<0:
            _error(400,'Cursor does not match project/filter')
        offset=page['offset']
    event_cursor=_event_cursor(db,project_id)
    if resource=='history':
        try:
            from .roadmap_structure import redirect_group
        except ImportError:
            from roadmap_structure import redirect_group
        legacy=[rows[identity]['id'] for identity in redirect_group(db,project_id,uid)]
        marks=','.join('?' for _ in legacy)
        events=list(db.execute(f"""SELECT e.sequence,e.event_id,e.request_id,e.task_id,e.actor_json,e.recorded_at,e.historical_at,e.kind,e.details_json,NULL AS origin_store_id,NULL AS source_sha256,NULL AS created_by,NULL AS disclosure_mode,NULL AS source_event_id,NULL AS public_sha256
            FROM events e WHERE e.task_id IN ({marks})
            UNION ALL
            SELECT json_extract(j.source_event_json,'$.sequence'),j.id,json_extract(j.source_event_json,'$.request_id'),t.id,json_extract(j.source_event_json,'$.actor_json'),j.recorded_at,j.historical_at,j.kind,j.details_json,j.origin_store_id,j.source_sha256,j.created_by,NULL,NULL,NULL
            FROM task_journal_entries j JOIN tasks t ON t.project_id=j.project_id AND t.uid=j.task_id
            WHERE j.project_id=? AND t.id IN ({marks}) AND NOT EXISTS(SELECT 1 FROM events e WHERE e.event_id=j.id)
            UNION ALL
            SELECT 0,d.id,NULL,t.id,d.source_actor_json,d.recorded_at,d.historical_at,d.kind,d.details_json,d.origin_store_id,d.source_sha256,d.created_by,d.mode,d.source_event_id,d.public_sha256
            FROM journal_disclosures d JOIN tasks t ON t.project_id=d.project_id AND t.uid=d.task_id
            WHERE d.project_id=? AND t.id IN ({marks})
              AND NOT EXISTS(SELECT 1 FROM task_journal_entries j WHERE j.project_id=d.project_id AND j.id=d.source_event_id)
              AND NOT EXISTS(SELECT 1 FROM events e JOIN tasks et ON et.id=e.task_id WHERE et.project_id=d.project_id AND e.event_id=d.source_event_id)
            ORDER BY recorded_at DESC,sequence DESC,event_id DESC LIMIT ? OFFSET ?""",(*legacy,project_id,*legacy,project_id,*legacy,limit+1,offset)))
        items=[]
        for event in events[:limit]:
            item=dict(event)
            item['actor']=json.loads(item.pop('actor_json')) if item['actor_json'] else None
            item['details']=json.loads(item.pop('details_json'))
            origin=item.pop('origin_store_id');source_hash=item.pop('source_sha256');principal=item.pop('created_by')
            mode=item.pop('disclosure_mode');source_event=item.pop('source_event_id');public_hash=item.pop('public_sha256')
            item['source']='reviewed journal disclosure' if mode else 'shared task journal' if origin else 'legacy-compatible task audit'
            if mode:item['disclosure']={'id':item['event_id'],'mode':mode,'source_event_id':source_event,'public_sha256':public_hash,'original_available':False,'classification':'derived public projection; original source hash is attribution, not local bytes'}
            if origin:item['provenance']={'origin_store_id':origin,'source_sha256':source_hash,'principal_id':principal,'attribution':'source event; not an authentication grant'}
            item['original_task_id']=next(identity for identity in rows if rows[identity]['id']==item['task_id'])
            item['task_id']=uid
            items.append(item)
        return {**envelope,'items':items,'event_cursor':event_cursor,
                'next_cursor':_encode({**binding,'offset':offset+limit}) if len(events)>limit else None}
    if resource=='tasks':
        ordered=list(rows.values())
        if offset>len(ordered):
            _error(400,'Cursor outside result bounds')
        selected=ordered[offset:offset+limit]
        progress=progress_projection(db,project_id,[r['uid'] for r in selected],now)
        return {**envelope,'items':[_task(r,rows,progress) for r in selected],
                'event_cursor':event_cursor,'next_cursor':_encode({**binding,'offset':offset+limit}) if offset+limit<len(ordered) else None}
    if resource=='neighborhood':
        if type(depth) is not int or not 0<=depth<=LIMITS['neighborhood_depth']:
            _error(400,'Neighborhood depth outside configured bounds',limits=LIMITS)
        children={}
        for row in db.execute('SELECT * FROM containment WHERE project_id=? ORDER BY sibling_order,child_id',(project_id,)):
            if rows[row['child_id']]['archive_reason'] is None and rows[row['child_id']]['archived_at'] is None:
                children.setdefault(row['parent_id'],[]).append(row['child_id'])
        selected=[]
        queue=[(uid,0)]
        index=0
        while index<len(queue):
            node,level=queue[index]; index+=1
            selected.append(node)
            if level<depth:
                queue.extend((c,level+1) for c in children.get(node,[]))
        page=selected[offset:offset+limit]
        if offset>len(selected):
            _error(400,'Cursor outside neighborhood bounds')
        progress=progress_projection(db,project_id,page,now)
        nodes=[_task(rows[n],rows,progress) for n in page]
        visible=set(page)
        edges=[{'id':'contains:'+n['parent_id']+':'+n['id'],'kind':'contains','source_id':n['parent_id'],'target_id':n['id']} for n in nodes if n['parent_id'] in visible]
        frontier=[{'id':n,'has_more_children':True} for n in page if any(c not in visible for c in children.get(n,[]))]
        return {**envelope,'root_id':uid,'nodes':nodes,'edges':edges,'frontier':frontier,'event_cursor':event_cursor,
                'next_cursor':_encode({**binding,'offset':offset+limit}) if offset+limit<len(selected) else None}
    if resource=='context':
        row=rows[uid]
        progress=progress_projection(db,project_id,[uid],now)
        ancestors=[]; parent=row['parent_id']
        by_legacy={r['id']:r for r in rows.values()}
        while parent is not None:
            ancestor=by_legacy[parent]; record=json.loads(ancestor['record_json'])
            ancestors.append({'id':ancestor['uid'],'alias':parent,'acceptance':record.get('acceptance'),'acceptance_state':ancestor['acceptance_state'],'record_revision':ancestor['revision']})
            parent=ancestor['parent_id']
        areas=[]; area=row['area_id']; seen=set()
        while area and area not in seen:
            seen.add(area)
            entry=db.execute('SELECT * FROM areas WHERE project_id=? AND id=?',(project_id,area)).fetchone()
            if not entry: break
            areas.append(area);area=entry['parent_id']
        instructions=[]
        for area in reversed(areas):
            for link in db.execute('SELECT * FROM guidance_links WHERE project_id=? AND area_id=? AND archived_at IS NULL ORDER BY id',(project_id,area)):
                if area==row['area_id'] or link['inheritance_scope']=='descendants':
                    instructions.append({'id':link['id'],'body':link['body'],'area_id':area,'source_path':link['source_path'],'document_version_id':link['document_version_id'],'inheritance_scope':link['inheritance_scope'],'record_revision':link['revision']})
        for link in db.execute('SELECT * FROM guidance_links WHERE project_id=? AND task_id=? AND archived_at IS NULL ORDER BY id',(project_id,uid)):
            instructions.append({'id':link['id'],'body':link['body'],'area_id':link['area_id'],'source_path':link['source_path'],'document_version_id':link['document_version_id'],'inheritance_scope':link['inheritance_scope'],'record_revision':link['revision']})
        record=json.loads(row['record_json'])
        dependencies=[{'id':by_legacy[link['target_id']]['uid'],'alias':link['target_id'],'kind':link['kind'],'rationale':link['rationale'],'review_state':link['review_state']} for link in db.execute("SELECT * FROM task_links WHERE source_id=? AND kind='depends_on' ORDER BY target_id",(row['id'],)) if link['target_id'] in by_legacy]
        history_response=read_projection(db,'history',project_id,key=uid,limit=LIMITS['history_items'],now=now)
        validation=None
        if row['validation_id']:
            found=db.execute('SELECT * FROM validation_records WHERE project_id=? AND id=?',(project_id,row['validation_id'])).fetchone()
            if found:
                validation=dict(found)
                for name in ('inputs','outputs','evidence'): validation[name]=json.loads(validation.pop(name+'_json'))
                validation['current']=validation['gate_revision']==row['gate_revision'] and row['acceptance_state']=='passed'
        try:
            from . import roadmap_structure
        except ImportError:
            import roadmap_structure
        attempt_page=roadmap_structure.read(db,'attempts',project_id,task_id=uid,limit=50,order='desc')
        blocker_page=roadmap_structure.read(db,'blockers',project_id,task_id=uid,limit=50,order='desc',state='open')
        attempts=attempt_page['items'];typed_blockers=blocker_page['items']
        omitted=['findings','hints']
        if attempt_page['next_cursor']: omitted.append('structured_attempts')
        if blocker_page['next_cursor']: omitted.append('blockers')
        if history_response['next_cursor']:omitted.append('history')
        section_cursors={'history':{'path':'/v1/tasks/'+uid+'/history','cursor':history_response['next_cursor'],'limit':LIMITS['history_items']}} if history_response['next_cursor'] else {}
        if attempt_page['next_cursor']: section_cursors['structured_attempts']={'path':'/v1/attempts','task_id':uid,'limit':50,'order':'desc','cursor':attempt_page['next_cursor']}
        if blocker_page['next_cursor']: section_cursors['blockers']={'path':'/v1/blockers','task_id':uid,'limit':50,'order':'desc','state':'open','cursor':blocker_page['next_cursor']}
        context={'task':_task(row,rows,progress),'fields':{k:v for k,v in record.items() if k!='id'},'instructions':instructions,'ancestors':ancestors,'acceptance':record.get('acceptance'),'acceptance_state':row['acceptance_state'],'gate_revision':row['gate_revision'],'dependencies':dependencies,'blockers':typed_blockers[:50]+record.get('blockers',[]),'reservations':record.get('reserved_paths',[]),'validation':validation,'structured_attempts':attempts[:50],'findings':[],'hints':[],'progress':progress[uid],'history':history_response['items'], 'event_cursor':event_cursor,'omitted_sections':omitted,'section_cursors':section_cursors}
        return {**envelope,'context':context,'event_cursor':event_cursor}
    _error(404,'Unknown workspace resource')
