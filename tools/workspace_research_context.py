"""Single-entity research contexts and bounded exact answer citations."""
import json
try:
    from . import roadmap_research as research,workspace_findings as findings,workspace_answers as answers
    from .roadmap_projection import API_VERSION,_coverage,_encode,_decode
except ImportError:
    import roadmap_research as research
    import workspace_findings as findings
    import workspace_answers as answers
    from roadmap_projection import API_VERSION,_coverage,_encode,_decode

RESOURCES={'finding-context','question-context','answer-context','answer-support'}


def _entity(row):
    result=dict(row)
    for field in list(result):
        if field.endswith('_json'):result[field[:-5]]=json.loads(result.pop(field))
    if 'revision' in result:result['record_revision']=result.pop('revision')
    return result


def _question(db,project,key):
    row=db.execute('SELECT q.*,r.text,r.expected_evidence,r.criteria_json,r.criteria_revision,r.presentation_revision FROM questions q JOIN question_revisions r ON r.project_id=q.project_id AND r.id=q.current_revision_id WHERE q.project_id=? AND q.id=?',(project,key)).fetchone()
    if row is None:research.error(404,'Scoped question context not found')
    return answers.describe_question(db,project,_entity(row))


def _support(db,project,identity,limit,cursor,envelope):
    research.text(identity,'answer support identity',200)
    answer=research.scoped(db,'answers',project,identity)
    limit=100 if limit is None else limit
    if type(limit) is not int or not 1<=limit<=100:research.error(400,'Answer support page limit must be 1–100')
    binding={'resource':'answer-support','project_id':project,'revision':envelope['revision'],'subject_id':identity,'limit':limit}
    offset=0
    if cursor is not None:
        page=_decode(cursor)
        if page.get('revision')!=envelope['revision']:research.error(409,'Snapshot cursor expired; restart required')
        if set(page)!=set(binding)|{'offset'} or any(page.get(key)!=value for key,value in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:research.error(400,'Cursor does not match answer support scope')
        offset=page['offset']
    # The answer contract permits at most100 exact references, including links
    # owned by finding revisions. Do not scan all project evidence to find them.
    rows=sorted(answers._support_rows(db,project,answer),key=lambda row:row['id'])
    if offset>len(rows):research.error(400,'Cursor outside answer support bounds')
    versions={row['artifact_version_id'] for row in rows}
    sources={row['id']:row for row in db.execute('SELECT v.id,v.sha256,v.binary_build_id,v.availability,EXISTS(SELECT 1 FROM artifact_blobs b WHERE b.project_id=v.project_id AND b.sha256=v.sha256) AS local_bytes_available FROM artifact_versions v WHERE v.project_id=? AND v.id IN ('+','.join('?' for _ in versions)+')',(project,*sorted(versions)))}
    items=[]
    for raw in rows[offset:offset+limit]:
        item=_entity(raw);source=sources.get(item['artifact_version_id'])
        if source is None:research.error(409,'Pinned source version is unavailable')
        item.update(source_sha256=source['sha256'],binary_build_id=source['binary_build_id'],availability=source['availability'],local_bytes_available=bool(source['local_bytes_available']))
        items.append(item)
    return {**envelope,'items':items,'next_cursor':_encode({**binding,'offset':offset+limit}) if offset+limit<len(rows) else None}


def read(db,resource,project,*,key=None,limit=None,cursor=None,subject_id=None,**options):
    if resource not in RESOURCES:research.error(404,'Unknown research context')
    if options:research.error(400,'Unsupported research context option')
    coverage=_coverage(db,project);meta=db.execute('SELECT store_id,revision FROM store_meta').fetchone()
    envelope={'api_version':API_VERSION,'project_id':project,'revision':f'{meta[0]}:{meta[1]}','coverage':coverage}
    if resource=='answer-support':
        if key is not None:research.error(400,'Answer support uses its subject identity')
        return _support(db,project,subject_id,limit,cursor,envelope)
    if limit is not None or cursor is not None or subject_id is not None:research.error(400,'Single research context accepts only its exact identity')
    research.text(key,'research identity',200)
    def related(resource,**filters):return {'resource':resource,'params':{'project_id':project,**filters}}
    if resource=='finding-context':
        kind='finding'
        row=db.execute('SELECT f.*,r.claim,r.scope_json,r.classification,r.uncertainty,r.evidence_revision,r.presentation_revision FROM findings f JOIN finding_revisions r ON r.project_id=f.project_id AND r.id=f.current_revision_id WHERE f.project_id=? AND f.id=?',(project,key)).fetchone()
        if row is None:research.error(404,'Scoped finding context not found')
        entity=findings.describe(db,project,_entity(row));scope={'subject_type':'finding_revision','subject_id':entity['evidence_origin_id']}
        sections={'revisions':related('finding-revisions',subject_id=key),'evidence':related('evidence-links',**scope),'notices':related('research-review-notices',**scope)}
    elif resource=='question-context':
        kind='question';entity=_question(db,project,key)
        sections={'answers':related('answers',subject_id=key),'resolutions':related('question-resolutions',subject_id=key)}
    else:
        kind='answer';entity=answers.describe_answer(db,project,_entity(research.scoped(db,'answers',project,key)))
        sections={'support':related('answer-support',subject_id=key),'reviews':related('answer-reviews',subject_id=key),'notices':related('research-review-notices',subject_type='answer',subject_id=key)}
    context={'entity_type':kind,'entity':entity,'related':sections}
    if kind=='answer':context['question']=_question(db,project,entity['question_id'])
    return {**envelope,'next_cursor':None,'context':context}
