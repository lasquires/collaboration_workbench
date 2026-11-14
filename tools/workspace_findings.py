"""Authored, revisioned findings; source support is never automatic acceptance."""
import json
import uuid
from datetime import datetime,timezone
try:
    from . import roadmap_research as research
except ImportError:
    import roadmap_research as research

OPS={'create_finding','revise_finding','archive_finding','restore_finding','set_evidence_state'}
RESOURCES={'finding-revisions','evidence-links','evidence-changes','research-review-notices'}
TABLES=('finding_revision_lineage','evidence_change_events','research_review_notices')


def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS finding_revision_lineage(project_id TEXT NOT NULL,revision_id TEXT NOT NULL,evidence_origin_id TEXT NOT NULL,PRIMARY KEY(project_id,revision_id),FOREIGN KEY(project_id,revision_id) REFERENCES finding_revisions(project_id,id),FOREIGN KEY(project_id,evidence_origin_id) REFERENCES finding_revisions(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS evidence_change_events(id TEXT NOT NULL,project_id TEXT NOT NULL,evidence_link_id TEXT NOT NULL,from_state TEXT NOT NULL,to_state TEXT NOT NULL,reason TEXT NOT NULL,replacement_id TEXT,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,evidence_link_id) REFERENCES evidence_links(project_id,id),FOREIGN KEY(project_id,replacement_id) REFERENCES evidence_links(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS research_review_notices(id TEXT NOT NULL,project_id TEXT NOT NULL,subject_type TEXT NOT NULL,subject_id TEXT NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL,evidence_link_id TEXT,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,evidence_link_id) REFERENCES evidence_links(project_id,id))')
    ensure_lineage(db)
    for table in TABLES:
        for action in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'finding provenance is immutable'); END")



def _complete_lineage(project,revisions,lineage):
    """Only self-origins with an unambiguous legacy counter can be inferred."""
    result=[dict(row) for row in lineage];known={row['revision_id'] for row in result};counts={}
    for row in revisions:
        key=(row['finding_id'],row['evidence_revision']);counts[key]=counts.get(key,0)+1
    for row in revisions:
        if row['id'] in known:continue
        if counts[(row['finding_id'],row['evidence_revision'])]!=1:research.error(409,'Missing finding lineage is ambiguous; review original revisions')
        result.append({'project_id':project,'revision_id':row['id'],'evidence_origin_id':row['id']})
    return sorted(result,key=lambda row:row['revision_id'])


def ensure_lineage(db,project=None):
    projects=[project] if project is not None else [row[0] for row in db.execute('SELECT id FROM projects')]
    for identity in projects:
        revisions=[dict(row) for row in db.execute('SELECT * FROM finding_revisions WHERE project_id=?',(identity,))]
        lineage=[dict(row) for row in db.execute('SELECT * FROM finding_revision_lineage WHERE project_id=?',(identity,))]
        for row in _complete_lineage(identity,revisions,lineage):
            db.execute('INSERT OR IGNORE INTO finding_revision_lineage VALUES(?,?,?)',(identity,row['revision_id'],row['evidence_origin_id']))


def export_lineage(project,records):
    records['finding_revision_lineage']=_complete_lineage(project,records['finding_revisions'],records['finding_revision_lineage'])


def check_withdrawals(db,project):
    invalid=db.execute("SELECT 1 FROM evidence_links e WHERE e.project_id=? AND e.state='available' AND EXISTS(SELECT 1 FROM evidence_change_events c WHERE c.project_id=e.project_id AND c.evidence_link_id=e.id) LIMIT 1",(project,)).fetchone()
    if invalid:research.error(409,'Imported withdrawal history cannot retain available source support; create new reviewed support instead')


def origin(db,project,revision):
    row=db.execute('SELECT evidence_origin_id FROM finding_revision_lineage WHERE project_id=? AND revision_id=?',(project,revision)).fetchone()
    return row[0] if row else revision


def _json(value,name,kind,limit=20000):
    if not isinstance(value,kind):research.error(400,'Invalid '+name)
    try:body=research.canonical(value)
    except (ValueError,TypeError):research.error(400,'Invalid '+name)
    if len(body)>limit:research.error(400,name+' exceeds bounded size')
    return body


def _scope(db,project,value):
    _json(value,'finding scope',dict)
    for field,table in (('artifact_version_id','artifact_versions'),('occurrence_id','occurrences'),('area_id','areas')):
        if value.get(field) is not None:research.scoped(db,table,project,value[field])
    if value.get('task_id') is not None:
        research.text(value['task_id'],'task UUID',200)
        if not db.execute('SELECT 1 FROM tasks WHERE project_id=? AND uid=?',(project,value['task_id'])).fetchone():research.error(404,'Scoped finding task UUID not found')
    for field in ('identifier','namespace','scope_key'):
        if field in value:research.text(value[field],'finding '+field,2000)
    return value


def _support(db,project,items):
    if not isinstance(items,list) or len(items)>100:research.error(400,'Expected at most 100 source support records')
    result=[];seen=set()
    for item in items:
        if not isinstance(item,dict) or set(item)!={'artifact_version_id','locator','method','limitations'}:research.error(400,'Source support requires version, locator, method and limitations')
        version=research.scoped(db,'artifact_versions',project,item['artifact_version_id'])
        locator=item['locator']
        if not isinstance(locator,(str,dict)) or not locator:research.error(400,'Exact source locator required')
        _json(locator,'source locator',(str,dict),10000)
        method=research.text(item['method'],'observation method',4000);limits=research.text(item['limitations'],'source limitations',4000)
        record={'artifact_version_id':version['id'],'locator':locator,'method':method,'limitations':limits}
        key=research.canonical(record)
        if key in seen:research.error(400,'Duplicate source support')
        seen.add(key);result.append({**record,'state':'available'})
    return sorted(result,key=research.canonical)


def _existing_support(db,project,revision):
    rows=db.execute("SELECT * FROM evidence_links WHERE project_id=? AND subject_type='finding_revision' AND subject_id=? ORDER BY id",(project,origin(db,project,revision)))
    return sorted([{'artifact_version_id':row['artifact_version_id'],'locator':json.loads(row['locator_json']),'method':row['method'],'limitations':row['limitations'],'state':row['state']} for row in rows],key=research.canonical)


def _links(db,project,revision,items,principal,now):
    for item in items:
        version=research.scoped(db,'artifact_versions',project,item['artifact_version_id'])
        support_hash=research.digest({key:value for key,value in item.items() if key!='state'}|{'source_sha256':version['sha256']})
        db.execute('INSERT INTO evidence_links VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),project,'finding_revision',revision,version['id'],research.canonical(item['locator']),item['method'],item['limitations'],support_hash,item['state'],principal,now))


def _guard(row,op):
    expected=op.get('expected_record_revision')
    if type(expected) is not int:research.error(400,'Expected finding record revision required')
    if expected!=row['revision']:research.error(409,'Finding changed; reread and reconcile')


def _event(db,project,principal,request_id,kind,subject,identity,payload,now):
    cur=db.execute('INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,after_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),project,request_id,principal,subject,identity,kind,research.canonical(payload),now))
    return cur.lastrowid


def _publish(db,project,principal,request_id,revision,now,*,from_import=False):
    payload={'type':'finding_revision','id':origin(db,project,revision)}
    sequence=_event(db,project,principal,request_id,'import_publication' if from_import else 'publish_finding','finding_revision',payload['id'],payload,now)
    db.execute('INSERT INTO research_outbox(id,project_id,event_sequence,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),project,sequence,'publish_finding',research.canonical(payload),now))


def _notice(db,project,principal,subject,identity,kind,reason,now,evidence=None):
    db.execute('INSERT INTO research_review_notices VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),project,subject,identity,kind,reason,evidence,principal,now))


def _stale(db,project):
    try:
        from . import roadmap_matcher
    except ImportError:
        import roadmap_matcher
    return roadmap_matcher._stale_unavailable_findings(db,project)


def apply_operation(db,project,principal,op,request_id):
    now=datetime.now(timezone.utc).isoformat();kind=op['op']
    if not db.in_transaction:research.error(409,'Finding changes require owning transaction')
    if kind=='create_finding':
        allowed={'op','id','claim','scope','classification','uncertainty','evidence'}
        if set(op)-allowed:research.error(400,'Unknown finding fields')
        identity=op.get('id') or str(uuid.uuid4());research.text(identity,'finding ID',200)
        claim=research.text(op.get('claim'),'finding claim');scope=_scope(db,project,op.get('scope',{}))
        classification=research.text(op.get('classification','unclassified'),'authored classification',120)
        uncertainty=research.optional_text(op.get('uncertainty',''),'finding uncertainty',allow_none=False)
        support=_support(db,project,op.get('evidence',[]));revision=str(uuid.uuid4())
        db.execute('INSERT INTO findings VALUES(?,?,?,1,?,?,NULL)',(identity,project,revision,principal,now))
        db.execute('INSERT INTO finding_revisions VALUES(?,?,?,?,?,?,?,1,1,?,?)',(revision,project,identity,claim,research.canonical(scope),classification,uncertainty,principal,now))
        db.execute('INSERT INTO finding_revision_lineage VALUES(?,?,?)',(project,revision,revision));_links(db,project,revision,support,principal,now)
        result={'type':'finding','id':identity,'revision_id':revision,'record_revision':1,'evidence_origin_id':revision,'evidence_review_state':'unreviewed'}
        _publish(db,project,principal,request_id,revision,now)
    elif kind=='revise_finding':
        if set(op)-{'op','finding_id','expected_record_revision','fields','rationale'}:research.error(400,'Unknown finding revision fields')
        finding=research.scoped(db,'findings',project,op.get('finding_id'));_guard(finding,op)
        if finding['archived_at'] is not None:research.error(409,'Restore archived finding before revising')
        previous=research.scoped(db,'finding_revisions',project,finding['current_revision_id'])
        fields=op.get('fields');rationale=research.text(op.get('rationale'),'revision rationale',4000)
        if not isinstance(fields,dict) or not fields or set(fields)-{'claim','scope','classification','uncertainty','evidence'}:research.error(400,'Invalid finding revision fields')
        claim=research.text(fields.get('claim',previous['claim']),'finding claim')
        scope=_scope(db,project,fields.get('scope',json.loads(previous['scope_json'])))
        classification=research.text(fields.get('classification',previous['classification']),'authored classification',120)
        uncertainty=research.optional_text(fields.get('uncertainty',previous['uncertainty']),'finding uncertainty',allow_none=False)
        old_support=_existing_support(db,project,previous['id'])
        support=_support(db,project,fields['evidence']) if 'evidence' in fields else old_support
        semantic=claim!=previous['claim'] or scope!=json.loads(previous['scope_json']) or classification!=previous['classification'] or support!=old_support
        editorial=uncertainty!=previous['uncertainty']
        if not semantic and not editorial:research.error(400,'Finding revision contains no changes')
        revision=str(uuid.uuid4());evidence_origin=revision if semantic else origin(db,project,previous['id'])
        db.execute('INSERT INTO finding_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(revision,project,finding['id'],claim,research.canonical(scope),classification,uncertainty,previous['evidence_revision']+int(semantic),previous['presentation_revision']+int(editorial),principal,now))
        db.execute('INSERT INTO finding_revision_lineage VALUES(?,?,?)',(project,revision,evidence_origin))
        if semantic:_links(db,project,revision,support,principal,now)
        db.execute('UPDATE findings SET current_revision_id=?,revision=revision+1 WHERE project_id=? AND id=?',(revision,project,finding['id']))
        result={'type':'finding','id':finding['id'],'revision_id':revision,'previous_revision_id':previous['id'],'record_revision':finding['revision']+1,'semantic_changed':semantic,'presentation_changed':editorial,'evidence_origin_id':evidence_origin,'rationale':rationale}
        if semantic:
            _notice(db,project,principal,'finding_revision',origin(db,project,previous['id']),'revision_superseded',rationale,now)
            _publish(db,project,principal,request_id,revision,now)
    elif kind in ('archive_finding','restore_finding'):
        if set(op)-{'op','finding_id','expected_record_revision','rationale'}:research.error(400,'Unknown finding archive fields')
        finding=research.scoped(db,'findings',project,op.get('finding_id'));_guard(finding,op)
        rationale=research.text(op.get('rationale'),'archive rationale',4000);archiving=kind=='archive_finding'
        if (finding['archived_at'] is not None)==archiving:research.error(400,'Finding archive state contains no changes')
        db.execute('UPDATE findings SET archived_at=?,revision=revision+1 WHERE project_id=? AND id=?',(now if archiving else None,project,finding['id']))
        result={'type':'finding','id':finding['id'],'record_revision':finding['revision']+1,'archived':archiving,'rationale':rationale}
        _notice(db,project,principal,'finding_revision',origin(db,project,finding['current_revision_id']),kind,rationale,now)
        if not archiving:_publish(db,project,principal,request_id,finding['current_revision_id'],now)
    elif kind=='set_evidence_state':
        if set(op)-{'op','evidence_link_id','expected_state','state','reason','replacement_id'}:research.error(400,'Unknown evidence change fields')
        link=research.scoped(db,'evidence_links',project,op.get('evidence_link_id'))
        if op.get('expected_state')!=link['state']:research.error(409,'Evidence state changed; reread and reconcile')
        if link['state']!='available' or op.get('state') not in ('retracted','superseded'):research.error(400,'Evidence withdrawal cannot revive or rewrite prior decisions')
        reason=research.text(op.get('reason'),'evidence change reason',4000);replacement=op.get('replacement_id')
        if op['state']=='superseded':
            other=research.scoped(db,'evidence_links',project,replacement)
            if other['id']==link['id'] or other['state']!='available':research.error(400,'Supersession requires distinct available replacement support')
        elif replacement is not None:research.error(400,'Retraction does not select replacement evidence')
        identity=str(uuid.uuid4())
        db.execute('INSERT INTO evidence_change_events VALUES(?,?,?,?,?,?,?,?,?)',(identity,project,link['id'],link['state'],op['state'],reason,replacement,principal,now))
        db.execute('UPDATE evidence_links SET state=? WHERE project_id=? AND id=?',(op['state'],project,link['id']))
        if link['subject_type']=='finding_revision':
            db.execute('UPDATE findings SET revision=revision+1 WHERE project_id=? AND COALESCE((SELECT evidence_origin_id FROM finding_revision_lineage l WHERE l.project_id=findings.project_id AND l.revision_id=findings.current_revision_id),current_revision_id)=?',(project,link['subject_id']))
        _notice(db,project,principal,link['subject_type'],link['subject_id'],'evidence_'+op['state'],reason,now,link['id'])
        result={'type':'evidence_change','id':identity,'evidence_link_id':link['id'],'state':op['state'],'reason':reason,'replacement_id':replacement}
    else:research.error(400,'Unknown finding operation')
    _event(db,project,principal,request_id,kind,result['type'],result['id'],result,now)
    _stale(db,project)
    return result


def describe(db,project,item):
    revision=item.get('current_revision_id',item.get('id'));root=origin(db,project,revision)
    support=db.execute("SELECT state,count(*) FROM evidence_links WHERE project_id=? AND subject_type='finding_revision' AND subject_id=? GROUP BY state",(project,root)).fetchall()
    states=dict(support);return {**item,'evidence_origin_id':root,'support_counts':states,'evidence_review_state':'unreviewed' if states.get('available') else 'unsupported','classification_origin':'authored assertion; not forensic acceptance'}


def read(db,resource,project,*,limit=None,cursor=None,subject_type=None,subject_id=None,**unused):
    try:
        from .roadmap_projection import API_VERSION,_coverage,_encode,_decode
    except ImportError:
        from roadmap_projection import API_VERSION,_coverage,_encode,_decode
    coverage=_coverage(db,project);meta=db.execute('SELECT store_id,revision FROM store_meta').fetchone();revision=f'{meta[0]}:{meta[1]}'
    limit=100 if limit is None else limit
    if type(limit) is not int or not 1<=limit<=100:research.error(400,'Page limit must be 1–100')
    if subject_id is not None:research.text(subject_id,'subject ID',200)
    if subject_type is not None:research.text(subject_type,'subject type',120)
    tables={'finding-revisions':'finding_revisions','evidence-links':'evidence_links','evidence-changes':'evidence_change_events','research-review-notices':'research_review_notices'}
    table=tables[resource];binding={'resource':resource,'project_id':project,'revision':revision,'limit':limit,'subject_type':subject_type,'subject_id':subject_id};offset=0
    if cursor:
        page=_decode(cursor)
        if page.get('revision')!=revision:research.error(409,'Snapshot cursor expired; restart required')
        if set(page)!=set(binding)|{'offset'} or any(page.get(key)!=value for key,value in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:research.error(400,'Cursor does not match project/filter')
        offset=page['offset']
    query=f'SELECT * FROM {table} WHERE project_id=?';args=[project]
    if subject_id is not None:
        field='finding_id' if resource=='finding-revisions' else 'evidence_link_id' if resource=='evidence-changes' else 'subject_id'
        query+=f' AND {field}=?';args.append(subject_id)
    if subject_type is not None:
        if resource not in ('evidence-links','research-review-notices'):research.error(400,'Subject type filter unavailable for resource')
        query+=' AND subject_type=?';args.append(subject_type)
    rows=db.execute(query+' ORDER BY created_at,id LIMIT ? OFFSET ?',(*args,limit+1,offset)).fetchall();items=[]
    for row in rows[:limit]:
        item=dict(row)
        for key in list(item):
            if key.endswith('_json'):item[key[:-5]]=json.loads(item.pop(key))
        if resource=='finding-revisions':item=describe(db,project,item)
        items.append(item)
    return {'api_version':API_VERSION,'project_id':project,'revision':revision,'coverage':coverage,'items':items,'next_cursor':_encode({**binding,'offset':offset+limit}) if len(rows)>limit else None}


def validate_bundle(db,project,records):
    """Check shared lineage and pinned support before any import SQL executes."""
    revisions={row['id']:row for row in records.get('finding_revisions',[])}
    findings={row['id']:row for row in records.get('findings',[])}
    links={row['id']:row for row in records.get('evidence_links',[])}
    for finding in findings.values():
        revision=revisions.get(finding['current_revision_id'])
        if revision is None or revision['finding_id']!=finding['id']:research.error(400,'Finding head lacks scoped revision')
    feature_tables=set(TABLES)&set(records)
    if feature_tables and feature_tables!=set(TABLES):research.error(400,'Incomplete finding lineage feature tables')
    supplied=records.get('finding_revision_lineage',[])
    if feature_tables and {row['revision_id'] for row in supplied}!=set(revisions):research.error(400,'Finding lineage coverage is incomplete')
    supplied=_complete_lineage(project,list(revisions.values()),supplied)
    for revision in revisions.values():
        try:scope=json.loads(revision['scope_json'])
        except (ValueError,TypeError):research.error(400,'Invalid imported finding scope')
        if not isinstance(scope,dict):research.error(400,'Invalid imported finding scope')
    for row in supplied:
        current=revisions.get(row['revision_id']);source=revisions.get(row['evidence_origin_id'])
        if (current is None or source is None or current['finding_id']!=source['finding_id']
                or any(current[key]!=source[key] for key in ('claim','scope_json','classification','evidence_revision'))):
            research.error(400,'Finding semantic lineage differs from original support')
        try:scope=json.loads(current['scope_json'])
        except (ValueError,TypeError):research.error(400,'Invalid imported finding scope')
        if not isinstance(scope,dict):research.error(400,'Invalid imported finding scope')
        for field,table,key in (('artifact_version_id','artifact_versions','id'),('occurrence_id','occurrences','id'),('task_id','tasks','uid'),('area_id','areas','id')):
            if field in scope and scope[field] is not None and scope[field] not in {item[key] for item in records.get(table,[])}:research.error(400,'Foreign finding scope')
    lineage={row['revision_id']:row['evidence_origin_id'] for row in supplied}
    if any(lineage.get(source,source)!=source for source in lineage.values()):research.error(400,'Finding lineage must reference original semantic revision')
    for link in links.values():
        try:locator=json.loads(link['locator_json'])
        except (ValueError,TypeError):research.error(400,'Invalid evidence locator')
        if not isinstance(locator,(str,dict)) or not locator:research.error(400,'Exact evidence locator required')
        research.sha(link['support_hash']);research.text(link['method'],'evidence method',4000);research.text(link['limitations'],'evidence limitations',4000)
        if link['subject_type']=='finding_revision' and link['subject_id'] not in revisions:research.error(400,'Foreign finding support')
        if link['state'] not in ('available','retracted','superseded'):research.error(400,'Invalid evidence state')
        previous=db.execute('SELECT * FROM evidence_links WHERE project_id=? AND id=?',(project,link['id'])).fetchone()
        if previous is not None and any(previous[key]!=link[key] for key in link if key!='state'):research.error(409,'Immutable evidence source identity collision')
    for row in records.get('evidence_change_events',[]):
        if (row['evidence_link_id'] not in links or row['from_state']!='available'
                or row['to_state'] not in ('retracted','superseded')
                or row['to_state']=='superseded' and (row['replacement_id'] not in links or row['replacement_id']==row['evidence_link_id'])
                or row['to_state']=='retracted' and row['replacement_id'] is not None):research.error(400,'Invalid evidence change provenance')
        if links[row['evidence_link_id']]['state']=='available':research.error(409,'Imported withdrawal history retains available source support')
        research.text(row['reason'],'evidence change reason',4000)
    for row in records.get('research_review_notices',[]):
        if (row['subject_type']=='finding_revision' and row['subject_id'] not in revisions
                or row['subject_type']=='finding' and row['subject_id'] not in findings):research.error(400,'Foreign research review notice')
        research.text(row['reason'],'research review reason',4000)


def after_import(db,project,principal,request_id,preview):
    """Import is publication, not a replay of remote workflow commands."""
    ensure_lineage(db,project)
    check_withdrawals(db,project)
    now=datetime.now(timezone.utc).isoformat();finding_ids=set();questions=set();runs=set();evidence_ids=set()
    for change in preview['changes']:
        if change['status'] in ('identical','keep-deleted'):continue
        key=json.loads(change['key'])
        if change['table']=='findings':finding_ids.add(key[-1])
        elif change['table']=='questions':questions.add(key[-1])
        elif change['table']=='index_runs':runs.add(key[-1])
        elif change['table']=='run_observations':runs.add(key[1])
        elif change['table']=='evidence_links':evidence_ids.add(key[-1])
    for identity in evidence_ids:
        link=research.scoped(db,'evidence_links',project,identity)
        if link['subject_type']=='finding_revision':
            owner=db.execute('SELECT finding_id FROM finding_revisions WHERE project_id=? AND id=?',(project,link['subject_id'])).fetchone()
            if owner:finding_ids.add(owner[0])
    for identity in sorted(finding_ids):
        finding=research.scoped(db,'findings',project,identity)
        if finding['archived_at'] is None:_publish(db,project,principal,request_id,finding['current_revision_id'],now,from_import=True)
        # Imported support may change even when a head row/counter did not.
        if evidence_ids:db.execute('UPDATE findings SET revision=revision+1 WHERE project_id=? AND id=?',(project,identity))
    for kind,identities,table in (('create_question',questions,'questions'),('publish_run',runs,'index_runs')):
        for identity in sorted(identities):
            record=research.scoped(db,table,project,identity)
            if kind=='publish_run' and record['state']!='published':continue
            payload={'id':identity}
            sequence=_event(db,project,principal,request_id,'import_publication','question' if table=='questions' else 'index_run',identity,payload,now)
            db.execute('INSERT INTO research_outbox(id,project_id,event_sequence,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),project,sequence,kind,research.canonical(payload),now))
    _stale(db,project)
