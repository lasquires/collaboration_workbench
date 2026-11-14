"""Scoped task collaboration on the store's existing transaction connection.

Workflow validation is separate from native forensic classification. Expired
leases retain reservations until an explicit release or reviewed handoff.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

try:
    from . import roadmap_research as research, roadmap_schema
except ImportError:
    import roadmap_research as research, roadmap_schema

OPS={'move_task','attach_child','detach_child','archive_task','reopen_task',
     'claim_task','heartbeat_claim','handoff_claim','release_claim','record_attempt',
     'resolve_attempt','record_validation','complete_task','set_task_scope','record_blocker','resolve_blocker',
     'merge_tasks','reverse_merge'}
RESOURCES={'attempts','validations','blockers','claims'}


def now():
    return datetime.now(timezone.utc)


def normalize_path(value):
    value=research.text(value,'reservation path',4000).replace('\\','/')
    if value.startswith('/') or ':' in value or any(p in ('','.','..') for p in value.split('/')):
        research.error(400,'Reservation must remain in the project root')
    return value.casefold()


def reservations(values):
    if not isinstance(values,list) or len(values)>500:
        research.error(400,'Expected at most 500 reservations')
    result=[]
    for item in values:
        if isinstance(item,str): path,kind=item,'file'
        elif isinstance(item,dict) and set(item)=={'path','kind'}: path,kind=item['path'],item['kind']
        else: research.error(400,'Invalid reservation')
        if kind=='directory': kind='tree'
        if kind not in ('file','tree'): research.error(400,'Reservation kind must be file or tree')
        result.append((normalize_path(path),kind))
    return sorted(set(result))


def create_schema(db):
    if 'gate_revision' not in {r[1] for r in db.execute('PRAGMA table_info(tasks)')}:
        db.execute('ALTER TABLE tasks ADD COLUMN gate_revision INTEGER NOT NULL DEFAULT 1 CHECK(gate_revision>=1)')
    statements=[
        "CREATE TABLE claims(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,task_id TEXT NOT NULL,principal_id TEXT NOT NULL,session_id TEXT,heartbeat_at TEXT,expires_at TEXT,released_at TEXT,legacy_claim INTEGER NOT NULL DEFAULT 0,UNIQUE(project_id,id),FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid),FOREIGN KEY(project_id,principal_id) REFERENCES project_members(project_id,principal_id),FOREIGN KEY(session_id) REFERENCES agent_sessions(id))",
        "CREATE UNIQUE INDEX claims_one_owner ON claims(project_id,task_id) WHERE released_at IS NULL",
        "CREATE TABLE reservations(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,claim_id TEXT NOT NULL,normalized_path TEXT NOT NULL,path_kind TEXT NOT NULL CHECK(path_kind IN ('file','tree')),UNIQUE(project_id,claim_id,normalized_path,path_kind),FOREIGN KEY(project_id,claim_id) REFERENCES claims(project_id,id))",
        "CREATE TABLE attempts(id TEXT NOT NULL,project_id TEXT NOT NULL,task_id TEXT NOT NULL,boundary_id TEXT NOT NULL,outcome TEXT NOT NULL CHECK(outcome IN ('failed','passed','blocked','inconclusive')),resolution_state TEXT NOT NULL CHECK(resolution_state IN ('unresolved','resolved')),evidence_json TEXT NOT NULL,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,resolved_at TEXT,resolution_reason TEXT,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid))",
        "CREATE INDEX attempts_task_boundary ON attempts(project_id,task_id,boundary_id,resolution_state)",
        "CREATE TABLE validation_records(id TEXT NOT NULL,project_id TEXT NOT NULL,task_id TEXT NOT NULL,gate_revision INTEGER NOT NULL,outcome TEXT NOT NULL CHECK(outcome IN ('passed','failed')),inputs_json TEXT NOT NULL,outputs_json TEXT NOT NULL,evidence_json TEXT NOT NULL,reviewer_id TEXT NOT NULL REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid))",
        "CREATE TABLE task_blockers(id TEXT NOT NULL,project_id TEXT NOT NULL,task_id TEXT NOT NULL,kind TEXT NOT NULL,target_id TEXT,reason TEXT NOT NULL,resolved_at TEXT,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid))",
        "CREATE TABLE merge_records(id TEXT NOT NULL,project_id TEXT NOT NULL,survivor_id TEXT NOT NULL,absorbed_id TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,touched_revisions_json TEXT NOT NULL,reversed_by TEXT,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,survivor_id) REFERENCES tasks(project_id,uid),FOREIGN KEY(project_id,absorbed_id) REFERENCES tasks(project_id,uid))",
        "CREATE TABLE task_redirects(project_id TEXT NOT NULL,absorbed_id TEXT NOT NULL,survivor_id TEXT NOT NULL,merge_id TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)),PRIMARY KEY(project_id,absorbed_id,merge_id),FOREIGN KEY(project_id,absorbed_id) REFERENCES tasks(project_id,uid),FOREIGN KEY(project_id,survivor_id) REFERENCES tasks(project_id,uid),FOREIGN KEY(project_id,merge_id) REFERENCES merge_records(project_id,id))",
    ]
    for statement in statements: db.execute(statement)
    for verb in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER immutable_validation_{verb.lower()} BEFORE {verb} ON validation_records BEGIN SELECT RAISE(ABORT,'validation records are immutable'); END")
    # Only the service-bootstrap local identity receives administrative local
    # capabilities. A matching label imported from elsewhere is insufficient.
    bootstrap_local_capabilities(db)
    sync_legacy_claims(db,historical=True)


def bootstrap_local_capabilities(db):
    for project in db.execute('SELECT id FROM projects').fetchall():
        try:
            from .roadmap_schema import principal_for_label
        except ImportError:
            from roadmap_schema import principal_for_label
        local=principal_for_label(db,project['id'],'local')
        existing=db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(project['id'],local)).fetchone()
        if existing is None: continue
        capabilities=set(json.loads(existing[0]))|{'contribute','review','manage_claims','manage_projects'}
        db.execute('UPDATE project_members SET capabilities_json=? WHERE project_id=? AND principal_id=?',
                   (research.canonical(sorted(capabilities)),project['id'],local))


def sync_legacy_claims(db, *, historical=False):
    """Compatibility records stay a projection of the same protected ownership."""
    for task in db.execute('SELECT uid,project_id,record_json FROM tasks').fetchall():
        record=json.loads(task['record_json']); claim=record.get('claim')
        active=db.execute('SELECT * FROM claims WHERE project_id=? AND task_id=? AND released_at IS NULL',(task['project_id'],task['uid'])).fetchone()
        if not claim:
            if active: db.execute('UPDATE claims SET released_at=? WHERE id=?',(now().isoformat(),active['id']))
            continue
        principal=claim.get('principal_id')
        if not isinstance(principal,str) or not db.execute('SELECT 1 FROM project_members WHERE project_id=? AND principal_id=?',(task['project_id'],principal)).fetchone():
            principal=roadmap_schema.principal_for_label(db,task['project_id'],claim['by'])
        if active and active['principal_id']==principal:
            # Legacy labels can change reservations; lease renewal remains explicit.
            db.execute('DELETE FROM reservations WHERE claim_id=?',(active['id'],))
            identity=active['id']
        else:
            if active: db.execute('UPDATE claims SET released_at=? WHERE id=?',(now().isoformat(),active['id']))
            identity=str(uuid.uuid4())
            expiry=claim.get('expires_at')
            if expiry is None and not historical: expiry=(now()+timedelta(seconds=900)).isoformat()
            db.execute('INSERT INTO claims VALUES(?,?,?,?,NULL,NULL,?,NULL,1)',(identity,task['project_id'],task['uid'],principal,expiry))
        for path,kind in reservations(record.get('reserved_paths',[])):
            db.execute('INSERT INTO reservations VALUES(?,?,?,?,?)',(str(uuid.uuid4()),task['project_id'],identity,path,kind))


def task_row(db,project,key):
    research.text(key,'task reference',200)
    row=db.execute("SELECT t.* FROM tasks t WHERE t.project_id=? AND (t.uid=? OR t.id=? OR t.uid IN(SELECT entity_id FROM entity_aliases WHERE project_id=? AND entity_type='task' AND alias=?))",(project,key,key,project,key)).fetchone()
    if row is None: research.error(404,'Scoped task not found')
    return row


def guard(row,op):
    expected=op.get('expected_record_revision')
    if type(expected) is not int: research.error(400,'Expected task record revision is required')
    if expected!=row['revision']: research.error(409,'Task record changed; reread and reconcile')


def ancestors(db,project,uid):
    result=[]; seen=set()
    while uid:
        if uid in seen: research.error(400,'Containment cycle')
        seen.add(uid); row=task_row(db,project,uid);result.append(row)
        if row['parent_id'] is None: break
        uid=task_row(db,project,row['parent_id'])['uid']
    return result


def preview_merge(db,project,survivor_id,absorbed_id):
    """Describe a prospective merge without changing records or choosing winners.

    The digest guards the captured scope, including children and incident links.
    It is a preview contract only; it does not authorize or perform a merge.
    """
    survivor=task_row(db,project,survivor_id)
    absorbed=task_row(db,project,absorbed_id)
    if survivor['uid']==absorbed['uid']:
        research.error(400,'Merge requires two distinct tasks')
    pair={survivor['uid'],absorbed['uid']}
    records=[json.loads(row['record_json']) for row in (survivor,absorbed)]
    conflicts={}
    for key in sorted(set(records[0])|set(records[1])):
        if key=='id': continue
        values=[{'present':key in record,**({'value':record[key]} if key in record else {})}
                for record in records]
        if values[0]!=values[1]:
            conflicts[key]={'survivor':values[0],'absorbed':values[1]}
    sources={research.canonical(value):value for record in records for value in record.get('evidence',[])}
    children=[dict(row) for row in db.execute(
        'SELECT * FROM containment WHERE project_id=? AND parent_id IN (?,?) ORDER BY parent_id,sibling_order,child_id',
        (project,survivor['uid'],absorbed['uid']))]
    affected={row['uid']:dict(row) for identity in pair for row in ancestors(db,project,identity)}
    for edge in children:
        row=task_row(db,project,edge['child_id']);affected[row['uid']]=dict(row)
    claims=[]
    for row in db.execute('SELECT * FROM claims WHERE project_id=? AND task_id IN (?,?) AND released_at IS NULL ORDER BY id',
                          (project,survivor['uid'],absorbed['uid'])):
        claim=dict(row)
        claim['reservations']=[dict(item) for item in db.execute(
            'SELECT * FROM reservations WHERE project_id=? AND claim_id=? ORDER BY normalized_path,path_kind',
            (project,row['id']))]
        claims.append(claim)
    problems=[]
    if any(row['uid']==survivor['uid'] for row in ancestors(db,project,absorbed['uid'])[1:]) or any(
            row['uid']==absorbed['uid'] for row in ancestors(db,project,survivor['uid'])[1:]):
        problems.append({'kind':'containment_ancestor','reason':'Merging ancestor and descendant requires an explicit containment plan'})
    for row in (survivor,absorbed):
        if row['archive_reason'] is not None:
            problems.append({'kind':'archived_task','task_id':row['uid']})
    redirects=[dict(row) for row in db.execute(
        'SELECT * FROM task_redirects WHERE project_id=? AND active=1 AND (absorbed_id IN (?,?) OR survivor_id IN (?,?)) ORDER BY absorbed_id,merge_id',
        (project,*sorted(pair),*sorted(pair)))]
    if redirects: problems.append({'kind':'existing_redirect','reason':'Resolve existing merge redirects before composing another merge'})
    all_links=[dict(row) for row in db.execute(
        'SELECT l.* FROM task_links l JOIN tasks s ON s.id=l.source_id JOIN tasks t ON t.id=l.target_id WHERE s.project_id=? AND t.project_id=? ORDER BY l.source_id,l.target_id,l.kind',
        (project,project))]
    links=[];proposed={};dependencies={}
    for original in all_links:
        edge=dict(original)
        edge['source_id']=survivor['id'] if edge['source_id']==absorbed['id'] else edge['source_id']
        edge['target_id']=survivor['id'] if edge['target_id']==absorbed['id'] else edge['target_id']
        incident=original['source_id'] in (survivor['id'],absorbed['id']) or original['target_id'] in (survivor['id'],absorbed['id'])
        if incident:
            links.append(original)
            for identity in (original['source_id'],original['target_id']):
                row=task_row(db,project,identity);affected[row['uid']]=dict(row)
            key=(edge['source_id'],edge['target_id'],edge['kind'])
            target=proposed.setdefault(key,{'source_id':edge['source_id'],'target_id':edge['target_id'],
                                          'kind':edge['kind'],'provenance':[]})
            target['provenance'].append(original)
            if edge['source_id']==edge['target_id']:
                problems.append({'kind':'dependency_self_edge' if edge['kind']=='depends_on' else 'relationship_self_edge',
                                 'edge':original,'reason':'Explicit removal or resolution with rationale required'})
        if edge['kind']=='depends_on' and edge['source_id']!=edge['target_id']:
            dependencies.setdefault(edge['source_id'],set()).add(edge['target_id'])
            dependencies.setdefault(edge['target_id'],set())
    # Iterative topological check also detects cycles introduced by collapsing
    # the ends of a longer prerequisite chain, without recursion depth limits.
    incoming={key:0 for key in dependencies}
    for targets in dependencies.values():
        for target in targets: incoming[target]+=1
    ready=[key for key,count in incoming.items() if count==0];visited=0
    while ready:
        key=ready.pop();visited+=1
        for target in dependencies[key]:
            incoming[target]-=1
            if incoming[target]==0: ready.append(target)
    if visited!=len(dependencies): problems.append({'kind':'dependency_cycle'})
    for row in affected.values():
        if json.loads(row['record_json'])['status']=='done':
            problems.append({'kind':'completed_scope','task_id':row['uid'],'reason':'Reopen affected completed scope before merging'})
    metadata=['acceptance_state','validation_id','gate_revision','decomposition_complete','area_id']
    snapshot={'tasks':sorted(affected.values(),key=lambda row:row['uid']),
              'children':children,'links':all_links,'claims':claims,'redirects':redirects}
    return {'survivor_id':survivor['uid'],'absorbed_id':absorbed['uid'],
            'guard_digest':research.digest(snapshot),
            'guards':{uid:{'record_revision':row['revision'],'structure_revision':row['structure_revision']}
                      for uid,row in sorted(affected.items())},
            'field_conflicts':conflicts,
            'metadata_conflicts':{key:{'survivor':survivor[key],'absorbed':absorbed[key]}
                                  for key in metadata if survivor[key]!=absorbed[key]},
            'source_union':[sources[key] for key in sorted(sources)],
            'proposed_children':[{**edge,'parent_id':survivor['uid']} for edge in children if edge['child_id'] not in pair],
            'original_links':links,'proposed_links':list(proposed.values()),
            'claim_conflicts':claims,'problems':problems,
            'redirect':{'from':absorbed['uid'],'to':survivor['uid']},
            'acceptance_requires_review':True,'apply_available':not problems and not claims}


def redirect_group(db,project,uid):
    result={uid};pending=[uid]
    while pending:
        for row in db.execute('SELECT absorbed_id FROM task_redirects WHERE project_id=? AND survivor_id=? AND active=1',(project,pending.pop())):
            if row[0] not in result: result.add(row[0]);pending.append(row[0])
    return sorted(result)


def _merge_snapshot(db,project,task_ids,pair):
    tasks=[dict(task_row(db,project,uid)) for uid in sorted(task_ids)]
    legacy=[task_row(db,project,uid)['id'] for uid in pair]
    marks=','.join('?' for _ in task_ids)
    snapshot={'tasks':tasks,'links':[dict(row) for row in db.execute(
        'SELECT * FROM task_links WHERE source_id IN (?,?) OR target_id IN (?,?) ORDER BY source_id,target_id,kind',(*legacy,*legacy))]}
    for table in ('claims','attempts','validation_records','task_blockers'):
        snapshot[table]=[dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE project_id=? AND task_id IN ({marks}) ORDER BY id',(project,*sorted(task_ids)))]
    snapshot['containment']=[dict(row) for row in db.execute(f'SELECT * FROM containment WHERE project_id=? AND child_id IN ({marks}) ORDER BY child_id',(project,*sorted(task_ids)))]
    return snapshot


def finalize_merges(db,request_id):
    for merge in db.execute('SELECT * FROM merge_records WHERE reversed_by IS NULL').fetchall():
        marker=json.loads(merge['touched_revisions_json'])
        if marker.get('pending_request')!=request_id: continue
        after=_merge_snapshot(db,merge['project_id'],marker['task_ids'],(merge['survivor_id'],merge['absorbed_id']))
        revisions={row['uid']:row['revision'] for row in after['tasks']}
        db.execute('UPDATE merge_records SET after_json=?,touched_revisions_json=? WHERE project_id=? AND id=?',
                   (research.canonical(after),research.canonical(revisions),merge['project_id'],merge['id']))


def preview_merge_reversal(db,project,merge_id):
    merge=research.scoped(db,'merge_records',project,merge_id)
    after=json.loads(merge['after_json']);before=json.loads(merge['before_json'])
    current=_merge_snapshot(db,project,[row['uid'] for row in after['tasks']],(merge['survivor_id'],merge['absorbed_id']))
    can_reverse=merge['reversed_by'] is None and current==after
    return {'merge_id':merge['id'],'survivor_id':merge['survivor_id'],'absorbed_id':merge['absorbed_id'],
            'guard_digest':research.digest(current),'can_reverse':can_reverse,
            'reversed_by':merge['reversed_by'],
            'compensating_preview':None if can_reverse else {'original':before,'expected_after':after,'current':current,
                'reason':'Intervening edits require an explicit new compensating plan; automatic reversal would overwrite work'}}


def _apply_merge(db,project,principal,op,request_id,row,rationale):
    kind=op['op'];current=now().isoformat()
    if kind=='reverse_merge':
        merge=research.scoped(db,'merge_records',project,op.get('merge_id'))
        if merge['survivor_id']!=row['uid']: research.error(400,'Reversal task must be the merge survivor')
        preview=preview_merge_reversal(db,project,merge['id'])
        if not preview['can_reverse'] or preview['guard_digest']!=op.get('preview_guard'):
            research.error(409,'Merge reversal conflicts with current state; inspect reversal preview and plan compensation')
        before=json.loads(merge['before_json']);pair=(merge['survivor_id'],merge['absorbed_id'])
        after_rows={item['uid']:item for item in json.loads(merge['after_json'])['tasks']}
        restore_tasks=[item for item in before['tasks'] if item!=after_rows[item['uid']]]
        legacy=[task_row(db,project,uid)['id'] for uid in pair]
        db.execute('DELETE FROM task_links WHERE source_id IN (?,?) OR target_id IN (?,?)',(*legacy,*legacy))
        for link in before['links']:
            db.execute('INSERT INTO task_links VALUES(?,?,?,?,?)',tuple(link[key] for key in ('source_id','target_id','kind','rationale','review_state')))
        # Clear temporary containment before restoring the exact original forest.
        for old in restore_tasks: db.execute('UPDATE tasks SET parent_id=NULL WHERE uid=?',(old['uid'],))
        for old in restore_tasks:
            columns=['record_json','parent_id','sibling_order','archive_reason','archived_at','area_id','decomposition_complete']
            db.execute('UPDATE tasks SET '+','.join(key+'=?' for key in columns)+',updated_at=? WHERE uid=?',
                       (*[old[key] for key in columns],current,old['uid']))
        for edge in before['containment']:
            db.execute('UPDATE containment SET required_for_parent=? WHERE project_id=? AND child_id=?',
                       (edge['required_for_parent'],project,edge['child_id']))
        db.execute('UPDATE task_redirects SET active=0 WHERE project_id=? AND merge_id=?',(project,merge['id']))
        db.execute('UPDATE merge_records SET reversed_by=? WHERE project_id=? AND id=?',(request_id,project,merge['id']))
        touched={old['uid'] for old in restore_tasks}
        for uid in touched: invalidate_gate(db,uid)
        return {'type':'task','id':row['uid'],'legacy_id':row['id'],'merge_id':merge['id'],
                'changed_task_ids':sorted(touched),'structure_task_ids':sorted(touched),
                'event':{'rationale':rationale,'merge_id':merge['id'],'restored_scope':before,'acceptance_requires_review':True}}
    absorbed=task_row(db,project,op.get('absorbed_id'))
    preview=preview_merge(db,project,row['uid'],absorbed['uid'])
    if preview['guard_digest']!=op.get('preview_guard'): research.error(409,'Merge preview changed; reread and reconcile explicitly')
    if preview['claim_conflicts']: research.error(409,'Release or hand off claims explicitly before merging')
    if preview['problems']: research.error(409,'Resolve preview problems before merging')
    sources=op.get('source_references')
    if not isinstance(sources,list) or not sources or len(sources)>100: research.error(400,'Merge requires explanation source references')
    for source in sources: research.text(source,'merge source reference',4000)
    choices=op.get('choices');metadata=op.get('metadata_choices')
    for supplied,conflicts in ((choices,preview['field_conflicts']),(metadata,preview['metadata_conflicts'])):
        if not isinstance(supplied,dict) or set(supplied)!=set(conflicts) or any(value not in ('survivor','absorbed') for value in supplied.values()):
            research.error(400,'Choose a side explicitly for every conflicting field')
    touched=set(preview['guards']);pair=(row['uid'],absorbed['uid'])
    before=_merge_snapshot(db,project,touched,pair)
    record=json.loads(row['record_json'])
    for key,choice in choices.items():
        selected=preview['field_conflicts'][key][choice]
        if selected['present']: record[key]=selected['value']
        else: record.pop(key,None)
    record['id']=row['id'];record['evidence']=preview['source_union'];record['acceptance_gate_passed']=False
    record['status']='review'
    db.execute('UPDATE tasks SET record_json=?,decomposition_complete=0,updated_at=? WHERE uid=?',(research.canonical(record),current,row['uid']))
    for key,choice in metadata.items():
        if key=='area_id': db.execute('UPDATE tasks SET area_id=? WHERE uid=?',(preview['metadata_conflicts'][key][choice],row['uid']))
    for edge in preview['proposed_children']:
        if any(original['child_id']==edge['child_id'] and original['parent_id']==absorbed['uid'] for original in before['containment']):
            db.execute('UPDATE tasks SET parent_id=?,updated_at=? WHERE uid=?',(row['id'],current,edge['child_id']))
    # Detach archived source so it cannot keep a required child slot outstanding.
    db.execute('UPDATE tasks SET parent_id=NULL,archive_reason=?,archived_at=?,updated_at=? WHERE uid=?',
               ('Merged into '+row['uid']+': '+rationale,current,current,absorbed['uid']))
    legacy=(row['id'],absorbed['id'])
    db.execute('DELETE FROM task_links WHERE source_id IN (?,?) OR target_id IN (?,?)',(*legacy,*legacy))
    for edge in preview['proposed_links']:
        provenance=edge['provenance']
        db.execute('INSERT INTO task_links VALUES(?,?,?,?,?)',(edge['source_id'],edge['target_id'],edge['kind'],
                   '\n'.join(dict.fromkeys(link['rationale'] for link in provenance)),
                   provenance[0]['review_state'] if len({link['review_state'] for link in provenance})==1 else 'unreviewed'))
    dependency_changes=set()
    for source_id in ({edge['source_id'] for edge in preview['original_links'] if edge['kind']=='depends_on'}|{row['id']})-{absorbed['id']}:
        source=task_row(db,project,source_id);source_record=json.loads(source['record_json'])
        targets=[link[0] for link in db.execute("SELECT target_id FROM task_links WHERE source_id=? AND kind='depends_on' ORDER BY target_id",(source_id,))]
        if sorted(source_record.get('dependencies',[]))!=targets:
            source_record['dependencies']=targets;dependency_changes.add(source['uid'])
            db.execute('UPDATE tasks SET record_json=?,updated_at=? WHERE uid=?',(research.canonical(source_record),current,source['uid']))
    identity=str(uuid.uuid4())
    db.execute('INSERT INTO merge_records VALUES(?,?,?,?,?,?,?,?,?,?)',
               (identity,project,row['uid'],absorbed['uid'],research.canonical(before),'{}',
                research.canonical({'pending_request':request_id,'task_ids':sorted(touched)}),None,principal,current))
    db.execute('INSERT INTO task_redirects VALUES(?,?,?,?,1)',(project,absorbed['uid'],row['uid'],identity))
    mutated=dependency_changes|{row['uid'],absorbed['uid']}|{edge['child_id'] for edge in before['containment'] if edge['parent_id']==absorbed['uid']}
    for uid in mutated: invalidate_gate(db,uid)
    return {'type':'task','id':row['uid'],'legacy_id':row['id'],'merge_id':identity,
            'changed_task_ids':sorted(mutated),'structure_task_ids':sorted(mutated),
            'event':{'rationale':rationale,'source_references':sources,'merge_id':identity,'before':before,
                     'choices':choices,'metadata_choices':metadata,'proposed_links':preview['proposed_links']}}


def parent_guards(db,project,parent_ids,op):
    expected=op.get('expected_parents',{})
    if not isinstance(expected,dict): research.error(400,'Expected parent revisions must be an object')
    rows={row['uid']:row for identity in parent_ids if identity for row in ancestors(db,project,identity)}
    for uid,row in rows.items():
        if type(expected.get(uid)) is not int: research.error(400,'Missing affected parent structure guard')
        if expected[uid]!=row['structure_revision']: research.error(409,'Affected parent scope changed')
        if json.loads(row['record_json'])['status']=='done': research.error(409,'Reopen completed ancestors before changing required scope')
    return list(rows)


def invalidate_gate(db,uid):
    db.execute("UPDATE tasks SET gate_revision=gate_revision+1,acceptance_state=CASE WHEN validation_id IS NULL THEN 'pending' ELSE 'invalidated' END WHERE uid=?",(uid,))


def _caps(db,project,principal):
    row=db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(project,principal)).fetchone()
    if row is None: research.error(403,'Principal lacks project membership')
    return set(json.loads(row[0]))


def _lease(db,project,row,principal,caps):
    lease=db.execute('SELECT * FROM claims WHERE project_id=? AND task_id=? AND released_at IS NULL',(project,row['uid'])).fetchone()
    if lease is None: research.error(409,'Task has no active claim to release or renew')
    if lease['principal_id']!=principal and 'manage_claims' not in caps: research.error(403,'Only the claimant or an explicit claim administrator can act')
    return lease


def apply_operation(db,project,principal,op,request_id):
    row=task_row(db,project,op.get('task_id'))
    guard(row,op)
    rationale=research.text(op.get('rationale'),'operation rationale')
    caps=_caps(db,project,principal)
    kind=op['op']; record=json.loads(row['record_json']); before=dict(row)
    if kind in ('merge_tasks','reverse_merge'): return _apply_merge(db,project,principal,op,request_id,row,rationale)
    if db.execute('SELECT 1 FROM task_redirects WHERE project_id=? AND absorbed_id=? AND active=1',(project,row['uid'])).fetchone(): research.error(409,'Task was merged; use its survivor or reverse the merge')
    changed={row['uid']}; structure=set(); current=now().isoformat()
    if row['archive_reason'] is not None and kind not in ('reopen_task','resolve_attempt','release_claim'):
        research.error(409,'Reopen archived task before editing it')
    if kind in ('move_task','attach_child','detach_child'):
        target=None if kind=='detach_child' else op.get('parent_id')
        parent=task_row(db,project,target) if target is not None else None
        old_parent=task_row(db,project,row['parent_id']) if row['parent_id'] else None
        structure.update(parent_guards(db,project,[p['uid'] for p in (parent,old_parent) if p],op))
        if parent and row['uid'] in {p['uid'] for p in ancestors(db,project,parent['uid'])}:
            research.error(400,'Containment cycle')
        if record['status']=='done': research.error(409,'Reopen completed task before changing containment')
        required=op.get('required_for_parent',True)
        if type(required) is not bool: research.error(400,'required_for_parent must be boolean')
        order=db.execute('SELECT COALESCE(MAX(sibling_order),-1)+1 FROM tasks WHERE parent_id IS ?',(parent['id'] if parent else None,)).fetchone()[0]
        db.execute('UPDATE tasks SET parent_id=?,sibling_order=? WHERE uid=?',(parent['id'] if parent else None,order,row['uid']))
        db.execute('UPDATE containment SET required_for_parent=? WHERE project_id=? AND child_id=?',(int(required),project,row['uid']))
        structure.add(row['uid'])
        for uid in structure: invalidate_gate(db,uid)
    elif kind=='archive_task':
        if db.execute('SELECT 1 FROM claims WHERE project_id=? AND task_id=? AND released_at IS NULL',(project,row['uid'])).fetchone(): research.error(409,'Release or hand off the claim before archiving')
        parent=task_row(db,project,row['parent_id']) if row['parent_id'] else None
        structure.update(parent_guards(db,project,[parent['uid']] if parent else [],op))
        if record['status']=='done': research.error(409,'Reopen completed task before archiving')
        db.execute('UPDATE tasks SET archive_reason=?,archived_at=? WHERE uid=?',(rationale,current,row['uid']))
        structure.add(row['uid'])
        for uid in structure: invalidate_gate(db,uid)
    elif kind=='reopen_task':
        record['status']='review'; record['acceptance_gate_passed']=False
        db.execute('UPDATE tasks SET archive_reason=NULL,archived_at=NULL WHERE uid=?',(row['uid'],))
        invalidate_gate(db,row['uid']);structure.add(row['uid'])
    elif kind=='set_task_scope':
        if record['status']=='done': research.error(409,'Reopen task before changing scope')
        if 'decomposition_complete' in op:
            if type(op['decomposition_complete']) is not bool: research.error(400,'decomposition_complete must be boolean')
            db.execute('UPDATE tasks SET decomposition_complete=? WHERE uid=?',(int(op['decomposition_complete']),row['uid']))
        if 'area_id' in op:
            if op['area_id'] is not None: research.scoped(db,'areas',project,op['area_id'])
            db.execute('UPDATE tasks SET area_id=? WHERE uid=?',(op['area_id'],row['uid']))
        invalidate_gate(db,row['uid']);structure.add(row['uid'])
    elif kind in ('claim_task','heartbeat_claim','handoff_claim','release_claim'):
        if kind=='claim_task':
            if db.execute('SELECT 1 FROM claims WHERE project_id=? AND task_id=? AND released_at IS NULL',(project,row['uid'])).fetchone(): research.error(409,'An unreleased claim protects this task even after expiry')
            paths=reservations(op.get('reserved_paths',[]))
            recipient=principal
        else:
            lease=_lease(db,project,row,principal,caps)
            paths=[(r['normalized_path'],r['path_kind']) for r in db.execute('SELECT * FROM reservations WHERE claim_id=?',(lease['id'],))]
            recipient=lease['principal_id']
        if kind=='release_claim':
            db.execute('UPDATE claims SET released_at=? WHERE id=?',(current,lease['id']))
            record['claim']=None;record['reserved_paths']=[]
        else:
            seconds=op.get('lease_seconds',900)
            if type(seconds) is not int or not 60<=seconds<=86400: research.error(400,'Lease must be 60–86400 seconds')
            expires=(now()+timedelta(seconds=seconds)).isoformat()
            if kind=='handoff_claim':
                recipient=research.text(op.get('to_principal_id'),'handoff principal',200)
                _caps(db,project,recipient)
                db.execute('UPDATE claims SET released_at=? WHERE id=?',(current,lease['id']))
            if kind!='heartbeat_claim':
                for path,path_kind in paths:
                    for other in db.execute('SELECT r.normalized_path,r.path_kind FROM reservations r JOIN claims c ON c.id=r.claim_id WHERE c.project_id=? AND c.released_at IS NULL',(project,)):
                        if path==other['normalized_path'] or (path_kind=='tree' and other['normalized_path'].startswith(path+'/')) or (other['path_kind']=='tree' and path.startswith(other['normalized_path']+'/')):
                            research.error(409,'An unreleased reservation overlaps this path')
                identity=str(uuid.uuid4())
                session=op.get('session_id')
                if session is not None and not db.execute('SELECT 1 FROM agent_sessions WHERE id=? AND principal_id=?',(session,recipient)).fetchone(): research.error(403,'Session does not belong to claimant')
                db.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?,NULL,0)',(identity,project,row['uid'],recipient,session,current,expires))
                for path,path_kind in paths: db.execute('INSERT INTO reservations VALUES(?,?,?,?,?)',(str(uuid.uuid4()),project,identity,path,path_kind))
            else:
                db.execute('UPDATE claims SET heartbeat_at=?,expires_at=? WHERE id=?',(current,expires,lease['id']))
            label=db.execute('SELECT label FROM principals WHERE id=?',(recipient,)).fetchone()[0]
            record['claim']={'by':label,'principal_id':recipient,'expires_at':expires}
            record['reserved_paths']=[{'path':path,'kind':'directory' if path_kind=='tree' else 'file'} for path,path_kind in paths]
    elif kind=='record_attempt':
        outcome=op.get('outcome')
        if outcome not in ('failed','passed','blocked','inconclusive'): research.error(400,'Invalid attempt outcome')
        evidence=op.get('evidence')
        if not isinstance(evidence,list) or not evidence: research.error(400,'Attempt evidence is required')
        db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)',(str(uuid.uuid4()),project,row['uid'],research.text(op.get('boundary_id'),'attempt boundary',200),outcome,'unresolved' if outcome in ('failed','blocked','inconclusive') else 'resolved',research.canonical(evidence),principal,current))
        if outcome!='passed': invalidate_gate(db,row['uid'])
    elif kind=='resolve_attempt':
        attempt=research.scoped(db,'attempts',project,op.get('attempt_id'))
        if attempt['task_id'] not in redirect_group(db,project,row['uid']): research.error(404,'Attempt does not belong to this task')
        db.execute("UPDATE attempts SET resolution_state='resolved',resolved_at=?,resolution_reason=? WHERE project_id=? AND id=?",(current,rationale,project,attempt['id']))
    elif kind=='record_validation':
        if 'review' not in caps: research.error(403,'Independent review capability is required')
        lease=db.execute('SELECT principal_id FROM claims WHERE project_id=? AND task_id=? AND released_at IS NULL',(project,row['uid'])).fetchone()
        if row['created_by']==principal or (lease and lease['principal_id']==principal): research.error(403,'A task contributor cannot independently approve their own gate')
        outcome=op.get('outcome')
        if outcome not in ('passed','failed') or not isinstance(op.get('inputs'),dict) or not isinstance(op.get('outputs'),dict): research.error(400,'Validation needs outcome, inputs and outputs')
        evidence=op.get('evidence')
        if not isinstance(evidence,list) or not evidence: research.error(400,'Pinned validation evidence is required')
        for link in evidence:
            if not isinstance(link,dict): research.error(400,'Validation evidence must name source versions')
            version=research.scoped(db,'artifact_versions',project,link.get('artifact_version_id'))
            if not db.execute('SELECT 1 FROM artifact_blobs WHERE project_id=? AND sha256=?',(project,version['sha256'])).fetchone(): research.error(400,'Validation source bytes unavailable')
            research.text(link.get('method'),'validation method');research.text(link.get('limitations'),'validation limitations')
        identity=str(uuid.uuid4())
        db.execute('INSERT INTO validation_records VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,project,row['uid'],row['gate_revision'],outcome,research.canonical(op['inputs']),research.canonical(op['outputs']),research.canonical(evidence),principal,current))
        db.execute('UPDATE tasks SET validation_id=?,acceptance_state=? WHERE uid=?',(identity,'passed' if outcome=='passed' else 'pending',row['uid']))
    elif kind=='complete_task':
        validation=db.execute('SELECT * FROM validation_records WHERE project_id=? AND id=?',(project,row['validation_id'])).fetchone()
        if row['acceptance_state']!='passed' or not validation or validation['gate_revision']!=row['gate_revision'] or validation['outcome']!='passed': research.error(409,'Completion requires this task\'s current independent gate')
        children=db.execute('SELECT t.*,c.required_for_parent FROM containment c JOIN tasks t ON t.uid=c.child_id AND t.project_id=c.project_id WHERE c.project_id=? AND c.parent_id=?',(project,row['uid'])).fetchall()
        if children and not row['decomposition_complete']: research.error(409,'Declare complete scope before parent completion')
        for child in children:
            if child['required_for_parent'] and (child['archive_reason'] is not None or json.loads(child['record_json'])['status']!='done' or child['acceptance_state']!='passed'):
                research.error(409,'Required child scope has not passed its own acceptance')
        group=redirect_group(db,project,row['uid']);marks=','.join('?' for _ in group)
        if db.execute(f"SELECT 1 FROM attempts WHERE project_id=? AND task_id IN ({marks}) AND resolution_state='unresolved'",(project,*group)).fetchone() or db.execute(f'SELECT 1 FROM task_blockers WHERE project_id=? AND task_id IN ({marks}) AND resolved_at IS NULL',(project,*group)).fetchone(): research.error(409,'Resolve outstanding attempts and blockers before completion')
        record['status']='done';record['acceptance_gate_passed']=True
    elif kind=='record_blocker':
        blocker_kind=op.get('kind')
        if blocker_kind not in ('question','prerequisite','review'): research.error(400,'Unsupported blocker kind')
        target=op.get('target_id')
        if blocker_kind=='question': research.scoped(db,'questions',project,target)
        elif blocker_kind=='prerequisite': task_row(db,project,target)
        db.execute('INSERT INTO task_blockers VALUES(?,?,?,?,?,?,NULL,?,?)',(str(uuid.uuid4()),project,row['uid'],blocker_kind,target,rationale,principal,current))
    elif kind=='resolve_blocker':
        blocker=research.scoped(db,'task_blockers',project,op.get('blocker_id'))
        if blocker['task_id'] not in redirect_group(db,project,row['uid']): research.error(404,'Blocker does not belong to task')
        db.execute('UPDATE task_blockers SET resolved_at=? WHERE project_id=? AND id=?',(current,project,blocker['id']))
    else: research.error(400,'Unknown structural operation')
    db.execute('UPDATE tasks SET record_json=?,updated_at=? WHERE uid=?',(research.canonical(record),current,row['uid']))
    after=dict(db.execute('SELECT * FROM tasks WHERE uid=?',(row['uid'],)).fetchone())
    return {'type':'task','id':row['uid'],'legacy_id':row['id'],'changed_task_ids':sorted(changed|structure),
            'structure_task_ids':sorted(structure),'event':{'rationale':rationale,'before':before,'after':after}}


def read(db,resource,project,*,task_id=None,limit=None,cursor=None,order='asc',state=None,**unused):
    try:
        from .roadmap_projection import _coverage,_encode,_decode,API_VERSION
    except ImportError:
        from roadmap_projection import _coverage,_encode,_decode,API_VERSION
    coverage=_coverage(db,project)
    if order not in ('asc','desc'):
        research.error(400,'Order must be asc or desc')
    if state is not None and (resource!='blockers' or state!='open'):
        research.error(400,'Only blockers accept the open-state filter')
    limit=100 if limit is None else limit
    if type(limit) is not int or not 1<=limit<=100: research.error(400,'Page limit must be 1–100')
    uid=task_row(db,project,task_id)['uid'] if task_id is not None else None
    meta=db.execute('SELECT * FROM store_meta').fetchone()
    revision=f"{meta['store_id']}:{meta['revision']}"
    binding={'resource':resource,'project_id':project,'task_id':uid,'limit':limit,'revision':revision,'order':order,'state':state}
    offset=0
    if cursor:
        page=_decode(cursor)
        if page.get('revision')!=revision:
            try: research.error(409,'Snapshot cursor expired; restart required')
            except Exception as exc:
                exc.restart_required=True;raise
        if set(page)!=set(binding)|{'offset'} or any(page.get(k)!=v for k,v in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:
            research.error(400,'Cursor does not match project/filter')
        offset=page['offset']
    table={'validations':'validation_records','blockers':'task_blockers'}.get(resource,resource)
    if resource not in RESOURCES: research.error(404,'Unknown collaboration resource')
    query=f'SELECT * FROM {table} WHERE project_id=?';args=[project]
    if uid is not None:
        group=redirect_group(db,project,uid);query+=' AND task_id IN ('+','.join('?' for _ in group)+')';args.extend(group)
    if state=='open': query+=' AND resolved_at IS NULL'
    columns=['id'] if resource=='claims' else ['created_at','id']
    query+=' ORDER BY '+','.join(column+' '+order.upper() for column in columns)+' LIMIT ? OFFSET ?';args.extend([limit+1,offset])
    rows=db.execute(query,args).fetchall();items=[]
    for raw in rows[:limit]:
        item=dict(raw)
        for key in list(item):
            if key.endswith('_json'): item[key[:-5]]=json.loads(item.pop(key))
        actor=item.get('created_by') or item.get('reviewer_id') or item.get('principal_id')
        if actor:
            label=db.execute('SELECT label FROM principals WHERE id=?',(actor,)).fetchone()
            item['contributor_label']=label[0] if label else None
        items.append(item)
    return {'api_version':API_VERSION,'project_id':project,'revision':revision,'items':items,'coverage':coverage,
            'next_cursor':_encode({**binding,'offset':offset+limit}) if len(rows)>limit else None}
