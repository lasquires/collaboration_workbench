"""Explicit record preconditions on the caller's existing write transaction."""
import json
try:
    from . import roadmap_structure as structure,roadmap_research as research
except ImportError:
    import roadmap_structure as structure,roadmap_research as research

TASK_OPERATIONS={'create','create_task','update','set_parent','archive','journal','add_link','remove_link'}|structure.OPS
DOCUMENT_OPERATIONS={'create_document','register_document_version','add_comment','propose_edit','accept_proposal','reject_proposal','set_feedback'}
OPERATIONS=TASK_OPERATIONS|DOCUMENT_OPERATIONS|{'create_question','revise_question'}
TABLES={'project':'projects','area':'areas','question':'questions','document':'documents','proposal':'edit_proposals'}
TYPES={'task','task_structure','feedback'}|set(TABLES)


def envelope_mode(payload):
    common={'project_id','actor','request_id','operations'}
    if not isinstance(payload,dict):research.error(400,'Expected a scoped transaction envelope')
    mode=payload.get('concurrency_mode','global')
    if mode=='records':
        if set(payload)!=common|{'concurrency_mode','expected_records'}:
            research.error(400,'Record mode requires expected_records and omits expected_revision')
        guards=payload['expected_records']
        if not isinstance(guards,list) or len(guards)>10000:research.error(400,'Expected a bounded record guard list')
    elif mode=='global':
        required=common|{'expected_revision'}
        if not required<=set(payload) or set(payload)-required-{'concurrency_mode'}:
            research.error(400,'Global mode requires expected_revision without record guards')
        if not isinstance(payload['expected_revision'],str):research.error(400,'Expected global revision string')
    else:research.error(400,'Unsupported concurrency mode')
    return mode


def _guard_revision(db,project,actor,kind,identity):
    if kind in ('task','task_structure'):
        row=db.execute('SELECT revision,structure_revision FROM tasks WHERE project_id=? AND uid=?',(project,identity)).fetchone()
        if row is None:research.error(404,'Scoped guarded task UUID not found')
        return row[0 if kind=='task' else 1]
    if kind=='feedback':
        if not db.execute('SELECT 1 FROM document_versions WHERE project_id=? AND id=?',(project,identity)).fetchone():research.error(404,'Scoped feedback document version not found')
        row=db.execute('SELECT revision FROM feedback WHERE project_id=? AND principal_id=? AND document_version_id=?',(project,actor,identity)).fetchone()
        return row[0] if row else 0
    table=TABLES[kind]
    query='SELECT revision FROM projects WHERE id=?' if kind=='project' else f'SELECT revision FROM {table} WHERE project_id=? AND id=?'
    row=db.execute(query,(identity,) if kind=='project' else (project,identity)).fetchone()
    if row is None or kind=='project' and identity!=project:research.error(404,'Scoped guarded record not found')
    return row[0]


def check_records(db,payload,operations):
    if not db.in_transaction:research.error(409,'Record guards require the owning transaction')
    project=payload['project_id'];actor=payload['actor']['principal_id'];supplied={}
    for entry in payload['expected_records']:
        if (not isinstance(entry,dict) or set(entry)!={'type','id','revision'}
                or not isinstance(entry['type'],str) or entry['type'] not in TYPES
                or not isinstance(entry['id'],str) or not 0<len(entry['id'])<=200
                or type(entry['revision']) is not int or entry['revision']<(0 if entry['type']=='feedback' else 1)):
            research.error(400,'Invalid typed record revision guard')
        key=(entry['type'],entry['id'])
        if key in supplied:research.error(400,'Duplicate record revision guard')
        supplied[key]=entry['revision']
        if _guard_revision(db,project,actor,*key)!=entry['revision']:
            research.error(409,'Guarded record changed; reread and reconcile explicitly')
    required=set()
    created={op['record']['id'] for op in operations if op.get('op')=='create' and isinstance(op.get('record'),dict) and isinstance(op['record'].get('id'),str)}

    def task(key,aggregate=False):
        if key is None:return None
        if not isinstance(key,str) or not 0<len(key)<=200:research.error(400,'Expected scoped task reference')
        existing=db.execute('SELECT 1 FROM tasks WHERE project_id=? AND (id=? OR uid=?)',(project,key,key)).fetchone() if isinstance(key,str) else None
        if key in created and not existing:return None
        row=structure.task_row(db,project,key)
        required.add(('task_structure' if aggregate else 'task',row['uid']))
        return row

    def ancestry(key):
        if key is None:return
        row=task(key,True)
        if row is None:return
        for ancestor in structure.ancestors(db,project,row['uid']):required.add(('task_structure',ancestor['uid']))

    def scoped(kind,identity):
        if not isinstance(identity,str):research.error(400,'Expected guarded record identity')
        required.add((kind,identity))
        return research.scoped(db,TABLES[kind],project,identity)

    for operation in operations:
        name=operation.get('op')
        if not isinstance(name,str) or name not in OPERATIONS:research.error(400,'Operation requires global concurrency mode')
        if name in TASK_OPERATIONS:
            if name in ('create','create_task'):
                ancestry(operation.get('parent_id'))
                for target in operation.get('record',{}).get('dependencies',[]):task(target)
                continue
            row=task(operation.get('task_id'))
            if row is None:continue  # Created by this atomic transaction.
            if name=='update':
                fields=operation.get('fields',{})
                if isinstance(fields,dict) and set(fields)&{'status','acceptance','parent_gate','dependencies'}:ancestry(row['parent_id'])
                if isinstance(fields,dict) and 'dependencies' in fields:
                    for target in fields['dependencies']:task(target)
                    for old in db.execute("SELECT target_id FROM task_links WHERE source_id=? AND kind='depends_on'",(row['id'],)):task(old[0])
            elif name in ('add_link','remove_link'):
                task(operation.get('target_id'))
                if operation.get('kind')=='depends_on':ancestry(row['parent_id'])
            elif name in ('set_parent','move_task','attach_child','detach_child','archive','archive_task','reopen_task','set_task_scope'):
                ancestry(row['parent_id'])
                if name in ('set_parent','move_task','attach_child'):ancestry(operation.get('parent_id'))
            elif name=='complete_task':
                ancestry(row['uid'])
                for child in db.execute('SELECT child_id FROM containment WHERE project_id=? AND parent_id=?',(project,row['uid'])):task(child[0])
            elif name=='merge_tasks':
                preview=structure.preview_merge(db,project,row['uid'],operation.get('absorbed_id'))
                for identity in preview['guards']:task(identity);task(identity,True)
            elif name=='compensate_merge':
                try:
                    from . import workspace_merge_compensation
                except ImportError:
                    import workspace_merge_compensation
                preview=workspace_merge_compensation.preview_merge_compensation(db,project,operation.get('merge_id'))
                for identity in preview['guards']:task(identity);task(identity,True)
            elif name=='reverse_merge':
                merge=research.scoped(db,'merge_records',project,operation.get('merge_id'))
                for original in json.loads(merge['after_json'])['tasks']:task(original['uid']);task(original['uid'],True)
        elif name=='revise_question':scoped('question',operation.get('question_id'))
        elif name=='create_question':
            if operation.get('owner_task_id'):task(operation['owner_task_id'])
            if operation.get('area_id'):scoped('area',operation['area_id'])
        elif name in ('register_document_version','propose_edit'):scoped('document',operation.get('document_id'))
        elif name in ('accept_proposal','reject_proposal'):
            proposal=scoped('proposal',operation.get('proposal_id'))
            if name=='accept_proposal':scoped('document',proposal['document_id'])
        elif name=='set_feedback':
            identity=operation.get('document_version_id')
            if not isinstance(identity,str):research.error(400,'Expected feedback document version')
            required.add(('feedback',identity))
    missing=required-set(supplied)
    if missing:research.error(400,'Missing affected record guards: '+', '.join(kind+':'+str(identity) for kind,identity in sorted(missing)))


def capture_heads(db,project=None):
    """Capture local mutable heads, not foreign counters, before a write."""
    def rows(table):
        query=f'SELECT * FROM {table}'
        args=()
        if project is not None:
            query+=' WHERE '+('id' if table=='projects' else 'project_id')+'=?';args=(project,)
        return [dict(row) for row in db.execute(query,args)]
    tasks={row['uid']:row for row in rows('tasks')}
    by_legacy={row['id']:uid for uid,row in tasks.items()}
    owned={uid:[] for uid in tasks};children={uid:[] for uid in tasks}
    for table in ('claims','attempts','validation_records','task_blockers'):
        for row in rows(table):
            if row['task_id'] in owned:owned[row['task_id']].append((table,research.canonical(row)))
    for link in db.execute('SELECT * FROM task_links ORDER BY source_id,target_id,kind'):
        uid=by_legacy.get(link['source_id'])
        if uid in owned:owned[uid].append(('task_links',research.canonical(dict(link))))
    for edge in rows('containment'):
        if edge['parent_id'] in children:children[edge['parent_id']].append((edge['child_id'],edge['required_for_parent'],edge['sibling_order']))
        if edge['child_id'] in owned:owned[edge['child_id']].append(('own_containment',research.canonical(edge)))
    scopes={}
    for uid,row in tasks.items():
        record=json.loads(row['record_json'])
        scopes[uid]=research.canonical({'parent':row['parent_id'],'order':row['sibling_order'],
            'archived':row['archive_reason'],'area':row['area_id'],'decomposition':row['decomposition_complete'],
            'acceptance':record.get('acceptance'),'parent_gate':record.get('parent_gate'),'children':sorted(children[uid])})
    heads={}
    for table in ('projects','areas','questions','documents','edit_proposals','feedback'):
        for row in rows(table):
            identity=(row['principal_id'],row['document_version_id']) if table=='feedback' else (row['id'],)
            heads[(table,identity)]=row
    return {'project':project,'tasks':tasks,'owned':{uid:sorted(values) for uid,values in owned.items()},'scopes':scopes,'heads':heads}


def _head_content(row):
    result={key:value for key,value in row.items() if key not in ('revision','structure_revision','updated_at')}
    if 'record_json' in result:result['record_json']=json.loads(result['record_json'])
    return result


def preserve_local_revisions(db,before):
    """Keep CAS revisions monotonic when imports bring independent counters."""
    after=capture_heads(db,before['project']);old_tasks=before['tasks'];new_tasks=after['tasks']
    scope_changes={uid for uid in set(old_tasks)&set(new_tasks) if before['scopes'][uid]!=after['scopes'][uid]}
    # A changed descendant scope affects both its old and new ancestry.
    starts=scope_changes|(set(new_tasks)-set(old_tasks))
    for tree in (old_tasks,new_tasks):
        by_legacy={row['id']:key for key,row in tree.items()};seen=set()
        for uid in starts:
            current=uid
            while current in tree and current not in seen:
                seen.add(current);parent=by_legacy.get(tree[current]['parent_id'])
                if parent is None:break
                scope_changes.add(parent);current=parent
    for uid in set(old_tasks)&set(new_tasks):
        old=old_tasks[uid];current=new_tasks[uid]
        changed=(_head_content(old)!=_head_content(current) or before['owned'][uid]!=after['owned'][uid] or uid in scope_changes)
        revision=max(current['revision'],old['revision']+int(changed))
        structure_revision=max(current['structure_revision'],old['structure_revision']+int(uid in scope_changes))
        if (revision,structure_revision)!=(current['revision'],current['structure_revision']):
            db.execute('UPDATE tasks SET revision=?,structure_revision=? WHERE uid=?',(revision,structure_revision,uid))
    for key,old in before['heads'].items():
        current=after['heads'].get(key)
        if current is None:continue
        revision=max(current['revision'],old['revision']+int(_head_content(old)!=_head_content(current)))
        if revision==current['revision']:continue
        table,identity=key
        if table=='feedback':
            db.execute('UPDATE feedback SET revision=? WHERE project_id=? AND principal_id=? AND document_version_id=?',(revision,current['project_id'],*identity))
        else:db.execute(f'UPDATE {table} SET revision=? WHERE id=?'+('' if table=='projects' else ' AND project_id=?'),(revision,*identity,*(() if table=='projects' else (current['project_id'],))))
