"""Immutable answer proposals and reviews; imported history is not authority."""
import json
import uuid
from datetime import datetime,timezone
try:
    from . import roadmap_research as research,workspace_findings as findings
except ImportError:
    import roadmap_research as research,workspace_findings as findings

OPS={'create_answer','review_answer','resolve_question','set_question_state'}
RESOURCES={'answers','answer-reviews','question-resolutions'}
TABLES=('answer_contexts','answer_reviews','question_answer_selections','question_resolution_events')


def create_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS answer_contexts(id TEXT NOT NULL,project_id TEXT NOT NULL,question_revision_id TEXT NOT NULL,requirements_hash TEXT NOT NULL,support_digest TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,id) REFERENCES answers(project_id,id),FOREIGN KEY(project_id,question_revision_id) REFERENCES question_revisions(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS answer_reviews(id TEXT NOT NULL,project_id TEXT NOT NULL,answer_id TEXT NOT NULL,outcome TEXT NOT NULL,support_digest TEXT NOT NULL,reviewer_id TEXT NOT NULL REFERENCES principals(id),rationale TEXT NOT NULL,supersedes_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,answer_id) REFERENCES answers(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS question_answer_selections(project_id TEXT NOT NULL,question_id TEXT NOT NULL,answer_id TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)),revision INTEGER NOT NULL DEFAULT 1,selected_by TEXT REFERENCES principals(id),selected_at TEXT NOT NULL,PRIMARY KEY(project_id,question_id),FOREIGN KEY(project_id,question_id) REFERENCES questions(project_id,id),FOREIGN KEY(project_id,answer_id) REFERENCES answers(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS question_resolution_events(id TEXT NOT NULL,project_id TEXT NOT NULL,question_id TEXT NOT NULL,answer_id TEXT,action TEXT NOT NULL,reason TEXT NOT NULL,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,question_id) REFERENCES questions(project_id,id),FOREIGN KEY(project_id,answer_id) REFERENCES answers(project_id,id))')
    db.execute('CREATE TABLE IF NOT EXISTS answer_review_receipts(project_id TEXT NOT NULL,review_id TEXT NOT NULL,principal_id TEXT NOT NULL REFERENCES principals(id),PRIMARY KEY(project_id,review_id),FOREIGN KEY(project_id,review_id) REFERENCES answer_reviews(project_id,id))')
    for table in ('answers','answer_contexts','answer_reviews','question_resolution_events','answer_review_receipts'):
        for action in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'answer history is immutable'); END")


def requirements(revision):
    return research.digest({'question_id':revision['question_id'],'text':revision['text'],'expected_evidence':revision['expected_evidence'],'criteria':json.loads(revision['criteria_json'])})


def _question(db,project,identity,expected):
    row=research.scoped(db,'questions',project,identity)
    if type(expected) is not int:research.error(400,'Expected question record revision required')
    if row['revision']!=expected:research.error(409,'Question changed; reread and reconcile')
    return row


def _support_rows(db,project,answer):
    try:identities=json.loads(answer['evidence_json'])
    except (ValueError,TypeError):research.error(409,'Answer has incompatible legacy support')
    if not isinstance(identities,list) or not identities or len(identities)>100 or any(not isinstance(key,str) for key in identities) or len(set(identities))!=len(identities):research.error(409,'Answer needs bounded exact source link identities')
    return [research.scoped(db,'evidence_links',project,key) for key in sorted(identities)]


def support_digest(answer,rows,source_versions):
    return research.digest({'question_id':answer['question_id'],'claim':answer['claim'],'support':[{'id':row['id'],'subject_type':row['subject_type'],'subject_id':row['subject_id'],'artifact_version_id':row['artifact_version_id'],'source_sha256':source_versions[row['artifact_version_id']]['sha256'],'locator':json.loads(row['locator_json']),'method':row['method'],'limitations':row['limitations'],'support_hash':row['support_hash']} for row in sorted(rows,key=lambda row:row['id'])]})


def concerns(db,project,answer):
    context=db.execute('SELECT * FROM answer_contexts WHERE project_id=? AND id=?',(project,answer['id'])).fetchone()
    if context is None:return ['Legacy answer lacks pinned question and source context']
    question=research.scoped(db,'questions',project,answer['question_id']);current=research.scoped(db,'question_revisions',project,question['current_revision_id'])
    result=[]
    if requirements(current)!=context['requirements_hash']:result.append('Question requirements changed after this proposal')
    try:rows=_support_rows(db,project,answer)
    except Exception as exc:
        if not hasattr(exc,'status'):raise
        return result+['Pinned source support is unavailable or incompatible']
    sources={row['artifact_version_id']:research.scoped(db,'artifact_versions',project,row['artifact_version_id']) for row in rows}
    if support_digest(answer,rows,sources)!=context['support_digest']:result.append('Pinned source support digest changed')
    for row in rows:
        if row['state']!='available':result.append('Source support was '+row['state'])
        if row['subject_type']=='finding_revision':
            revision=research.scoped(db,'finding_revisions',project,row['subject_id']);finding=research.scoped(db,'findings',project,revision['finding_id'])
            if finding['archived_at'] is not None or findings.origin(db,project,finding['current_revision_id'])!=row['subject_id']:result.append('Supporting finding changed or was archived')
    return sorted(set(result))


def _review_info(db,project,answer):
    rows=db.execute('SELECT r.*,CASE WHEN x.review_id IS NOT NULL AND x.principal_id=r.reviewer_id THEN 1 ELSE 0 END AS receipt,m.capabilities_json,p.archived_at FROM answer_reviews r LEFT JOIN answer_review_receipts x ON x.project_id=r.project_id AND x.review_id=r.id LEFT JOIN project_members m ON m.project_id=r.project_id AND m.principal_id=r.reviewer_id LEFT JOIN principals p ON p.id=r.reviewer_id WHERE r.project_id=? AND r.answer_id=?',(project,answer['id'])).fetchall()
    trusted=[row for row in rows if row['receipt'] and row['reviewer_id']!=answer['created_by'] and row['archived_at'] is None and row['capabilities_json'] and 'review' in json.loads(row['capabilities_json']) and 'contribute' in json.loads(row['capabilities_json'])]
    superseded={key for row in trusted for key in json.loads(row['supersedes_json'])}
    heads=[row for row in trusted if row['id'] not in superseded]
    outcomes={row['outcome'] for row in heads}
    state='disputed' if len(outcomes)>1 else next(iter(outcomes)) if outcomes else 'needs_review' if rows else 'unreviewed'
    return {'review_state':state,'locally_authenticated_review_ids':sorted(row['id'] for row in heads),'untrusted_review_count':len(rows)-len(trusted)}


def describe_answer(db,project,item):
    raw=research.scoped(db,'answers',project,item['id']);issues=concerns(db,project,raw);info=_review_info(db,project,raw)
    context=db.execute('SELECT * FROM answer_contexts WHERE project_id=? AND id=?',(project,raw['id'])).fetchone()
    return {**item,**info,'review_state':'stale' if issues else info['review_state'],'review_concerns':issues,'pinned_context':dict(context) if context else None,'acceptance_scope':'Acceptance requires locally authenticated independent review; it is not automatic forensic proof'}


def describe_question(db,project,item):
    selection=db.execute('SELECT * FROM question_answer_selections WHERE project_id=? AND question_id=?',(project,item['id'])).fetchone()
    issues=[];effective=item['state'];resolution=dict(selection) if selection else None
    if item['state']=='resolved':
        if selection is None or not selection['active']:issues=['Recorded resolution lacks an active selected answer']
        else:
            answer=research.scoped(db,'answers',project,selection['answer_id']);status=describe_answer(db,project,dict(answer))
            issues=status['review_concerns'][:]
            if status['review_state']!='accepted':issues.append('Selected answer needs locally authenticated independent acceptance')
        if issues:effective='investigating'
    return {**item,'recorded_state':item['state'],'state':effective,'resolution':resolution,'resolution_concerns':issues}


def _resolution_event(db,project,principal,question,answer,action,reason,now):
    identity=str(uuid.uuid4())
    db.execute('INSERT INTO question_resolution_events VALUES(?,?,?,?,?,?,?,?)',(identity,project,question,answer,action,reason,principal,now))
    return identity


def reconcile(db,project,principal,request_id,*,reviewed_answer=None):
    now=datetime.now(timezone.utc).isoformat()
    for selection in db.execute('SELECT s.* FROM question_answer_selections s JOIN questions q ON q.project_id=s.project_id AND q.id=s.question_id WHERE s.project_id=? AND s.active=1',(project,)).fetchall():
        answer=research.scoped(db,'answers',project,selection['answer_id']);issues=concerns(db,project,answer)
        question=research.scoped(db,'questions',project,selection['question_id'])
        if question['state']!='resolved':issues.append('Question was explicitly reopened or withdrawn')
        if reviewed_answer==answer['id'] and _review_info(db,project,answer)['review_state']!='accepted':issues.append('Current independently authenticated reviews do not accept this answer')
        if not issues:continue
        reason='; '.join(sorted(set(issues)))
        db.execute('UPDATE question_answer_selections SET active=0,revision=revision+1 WHERE project_id=? AND question_id=?',(project,question['id']))
        if question['state']=='resolved':db.execute("UPDATE questions SET state='investigating',revision=revision+1,updated_at=? WHERE project_id=? AND id=?",(now,project,question['id']))
        identity=_resolution_event(db,project,principal,question['id'],answer['id'],'invalidated',reason,now)
        findings._notice(db,project,principal,'answer',answer['id'],'answer_requires_review',reason,now)
        findings._event(db,project,principal,request_id,'invalidate_answer_resolution','question',question['id'],{'answer_id':answer['id'],'resolution_event_id':identity,'reason':reason},now)


def apply_operation(db,project,principal,op,request_id):
    if not db.in_transaction:research.error(409,'Answer operations require owning transaction')
    now=datetime.now(timezone.utc).isoformat();kind=op['op']
    if kind=='create_answer':
        if set(op)-{'op','id','question_id','expected_question_revision','claim','evidence_link_ids','evidence'}:research.error(400,'Unknown answer proposal fields')
        question=_question(db,project,op.get('question_id'),op.get('expected_question_revision'))
        if question['state'] not in ('open','investigating'):research.error(409,'Reopen question before proposing an answer')
        identity=op.get('id') or str(uuid.uuid4());research.text(identity,'answer ID',200)
        claim=research.text(op.get('claim'),'answer claim');keys=op.get('evidence_link_ids',[])
        if not isinstance(keys,list) or len(keys)>100 or any(not isinstance(key,str) for key in keys) or len(set(keys))!=len(keys):research.error(400,'Expected distinct source link identities')
        for key in keys:research.scoped(db,'evidence_links',project,key)
        direct=findings._support(db,project,op.get('evidence',[]))
        for item in direct:
            source=research.scoped(db,'artifact_versions',project,item['artifact_version_id']);key=str(uuid.uuid4());keys=keys+[key]
            support_hash=research.digest({key:value for key,value in item.items() if key!='state'}|{'source_sha256':source['sha256']})
            db.execute('INSERT INTO evidence_links VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(key,project,'answer',identity,source['id'],research.canonical(item['locator']),item['method'],item['limitations'],support_hash,'available',principal,now))
        if not keys or len(keys)>100:research.error(400,'Answer requires 1–100 pinned source links')
        db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?,?)',(identity,project,question['id'],claim,research.canonical(sorted(keys)),'proposed/v1',principal,now))
        answer=research.scoped(db,'answers',project,identity);rows=_support_rows(db,project,answer)
        sources={row['artifact_version_id']:research.scoped(db,'artifact_versions',project,row['artifact_version_id']) for row in rows}
        revision=research.scoped(db,'question_revisions',project,question['current_revision_id'])
        digest=support_digest(answer,rows,sources)
        db.execute('INSERT INTO answer_contexts VALUES(?,?,?,?,?)',(identity,project,revision['id'],requirements(revision),digest))
        issues=concerns(db,project,answer)
        if issues:research.error(409,'Answer support needs review: '+'; '.join(issues))
        result={'type':'answer','id':identity,'question_id':question['id'],'review_state':'unreviewed','support_digest':digest}
    elif kind=='review_answer':
        if set(op)-{'op','answer_id','expected_question_revision','outcome','rationale','supersedes'}:research.error(400,'Unknown answer review fields')
        answer=research.scoped(db,'answers',project,op.get('answer_id'));question=_question(db,project,answer['question_id'],op.get('expected_question_revision'))
        member=db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',(project,principal)).fetchone()
        if not member or not {'contribute','review'}<=set(json.loads(member[0])):research.error(403,'Current independent review capability required')
        if answer['created_by']==principal:research.error(403,'Answer author cannot independently accept or reject their own answer')
        outcome=op.get('outcome');rationale=research.text(op.get('rationale'),'answer review rationale',4000)
        if outcome not in ('accepted','rejected'):research.error(400,'Review outcome must be accepted or rejected')
        context=research.scoped(db,'answer_contexts',project,answer['id'])
        if outcome=='accepted' and (concerns(db,project,answer) or question['state']=='withdrawn'):research.error(409,'Stale or withdrawn question support cannot be accepted; propose a new answer')
        supersedes=op.get('supersedes',[])
        if not isinstance(supersedes,list) or len(supersedes)>100 or any(not isinstance(key,str) for key in supersedes) or len(set(supersedes))!=len(supersedes):research.error(400,'Expected bounded review supersession identities')
        for key in supersedes:
            old=research.scoped(db,'answer_reviews',project,key)
            if old['reviewer_id']!=principal or old['answer_id']!=answer['id']:research.error(403,'Reviewers may supersede only their own decisions for this answer')
        identity=str(uuid.uuid4())
        db.execute('INSERT INTO answer_reviews VALUES(?,?,?,?,?,?,?,?,?)',(identity,project,answer['id'],outcome,context['support_digest'],principal,rationale,research.canonical(sorted(supersedes)),now))
        db.execute('INSERT INTO answer_review_receipts VALUES(?,?,?)',(project,identity,principal))
        reconcile(db,project,principal,request_id,reviewed_answer=answer['id'])
        result={'type':'answer_review','id':identity,'answer_id':answer['id'],'outcome':outcome,'rationale':rationale}
    elif kind in ('resolve_question','set_question_state'):
        allowed={'op','question_id','expected_record_revision','rationale'}|({'answer_id'} if kind=='resolve_question' else {'state'})
        if set(op)-allowed:research.error(400,'Unknown question resolution fields')
        question=_question(db,project,op.get('question_id'),op.get('expected_record_revision'));reason=research.text(op.get('rationale'),'question resolution rationale',4000)
        if kind=='resolve_question':
            answer=research.scoped(db,'answers',project,op.get('answer_id'))
            if answer['question_id']!=question['id']:research.error(400,'Answer belongs to another question')
            state=describe_answer(db,project,dict(answer))
            if state['review_state']!='accepted':research.error(409,'Resolution requires fresh locally authenticated independent answer acceptance')
            if question['state'] not in ('open','investigating'):research.error(409,'Reopen question before selecting a new resolution')
            db.execute('INSERT INTO question_answer_selections VALUES(?,?,?,1,1,?,?) ON CONFLICT(project_id,question_id) DO UPDATE SET answer_id=excluded.answer_id,active=1,revision=question_answer_selections.revision+1,selected_by=excluded.selected_by,selected_at=excluded.selected_at',(project,question['id'],answer['id'],principal,now))
            target='resolved';answer_id=answer['id'];action='resolved'
        else:
            target=op.get('state')
            if target not in ('open','investigating','withdrawn') or target==question['state']:research.error(400,'Select a changed open, investigating or withdrawn state')
            previous=db.execute('SELECT answer_id FROM question_answer_selections WHERE project_id=? AND question_id=?',(project,question['id'])).fetchone();answer_id=previous[0] if previous else None
            db.execute('UPDATE question_answer_selections SET active=0,revision=revision+1 WHERE project_id=? AND question_id=?',(project,question['id']))
            action=target
        event=_resolution_event(db,project,principal,question['id'],answer_id,action,reason,now)
        db.execute('UPDATE questions SET state=?,revision=revision+1,updated_at=? WHERE project_id=? AND id=?',(target,now,project,question['id']))
        result={'type':'question','id':question['id'],'state':target,'record_revision':question['revision']+1,'answer_id':answer_id,'resolution_event_id':event,'rationale':reason}
    else:research.error(400,'Unknown answer operation')
    findings._event(db,project,principal,request_id,kind,result['type'],result['id'],result,now)
    return result


def read(db,resource,project,*,limit=None,cursor=None,subject_id=None,**unused):
    try:
        from .roadmap_projection import API_VERSION,_coverage,_encode,_decode
    except ImportError:
        from roadmap_projection import API_VERSION,_coverage,_encode,_decode
    coverage=_coverage(db,project);meta=db.execute('SELECT store_id,revision FROM store_meta').fetchone();revision=f'{meta[0]}:{meta[1]}'
    limit=100 if limit is None else limit
    if type(limit) is not int or not 1<=limit<=100:research.error(400,'Page limit must be 1–100')
    if subject_id is not None:research.text(subject_id,'answer filter identity',200)
    binding={'resource':resource,'project_id':project,'revision':revision,'limit':limit,'subject_id':subject_id};offset=0
    if cursor:
        page=_decode(cursor)
        if page.get('revision')!=revision:research.error(409,'Snapshot cursor expired; restart required')
        if set(page)!=set(binding)|{'offset'} or any(page.get(k)!=v for k,v in binding.items()) or type(page.get('offset')) is not int or page['offset']<0:research.error(400,'Cursor does not match project/filter')
        offset=page['offset']
    table={'answers':'answers','answer-reviews':'answer_reviews','question-resolutions':'question_resolution_events'}[resource]
    query=f'SELECT * FROM {table} WHERE project_id=?';args=[project]
    if subject_id is not None:query+=' AND '+('answer_id' if resource=='answer-reviews' else 'question_id')+'=?';args.append(subject_id)
    rows=db.execute(query+' ORDER BY created_at,id LIMIT ? OFFSET ?',(*args,limit+1,offset)).fetchall();items=[]
    for row in rows[:limit]:
        item=dict(row)
        for key in list(item):
            if key.endswith('_json'):item[key[:-5]]=json.loads(item.pop(key))
        if resource=='answers':item=describe_answer(db,project,item)
        items.append(item)
    return {'api_version':API_VERSION,'project_id':project,'revision':revision,'coverage':coverage,'items':items,'next_cursor':_encode({**binding,'offset':offset+limit}) if len(rows)>limit else None}


def validate_bundle(project,records):
    answers={row['id']:row for row in records.get('answers',[])};questions={row['id']:row for row in records.get('questions',[])}
    revisions={row['id']:row for row in records.get('question_revisions',[])};links={row['id']:row for row in records.get('evidence_links',[])}
    versions={row['id']:row for row in records.get('artifact_versions',[])};contexts={row['id']:row for row in records.get('answer_contexts',[])}
    reviews={row['id']:row for row in records.get('answer_reviews',[])}
    feature=set(TABLES)&set(records)
    if feature and feature!=set(TABLES):research.error(400,'Incomplete answer provenance tables')
    for answer in answers.values():
        if answer['question_id'] not in questions:research.error(400,'Foreign answer question')
        if answer['review_gate']=='proposed/v1' and answer['id'] not in contexts:research.error(400,'Missing pinned answer context')
    for context in contexts.values():
        answer=answers.get(context['id']);revision=revisions.get(context['question_revision_id'])
        if answer is None or revision is None or revision['question_id']!=answer['question_id']:research.error(400,'Foreign pinned answer context')
        try:
            identities=json.loads(answer['evidence_json'])
            if not isinstance(identities,list) or not identities or len(identities)>100 or any(not isinstance(key,str) or key not in links for key in identities) or len(set(identities))!=len(identities):raise ValueError()
            support=[links[key] for key in identities]
            if any(row['artifact_version_id'] not in versions for row in support):raise ValueError()
            if context['requirements_hash']!=requirements(revision) or context['support_digest']!=support_digest(answer,support,versions):raise ValueError()
        except (ValueError,TypeError,KeyError):research.error(400,'Answer pinned question or source digest mismatch')
    predecessors={}
    for review in reviews.values():
        answer=answers.get(review['answer_id']);context=contexts.get(review['answer_id'])
        if answer is None or context is None or review['reviewer_id']==answer['created_by'] or review['outcome'] not in ('accepted','rejected') or review['support_digest']!=context['support_digest']:research.error(400,'Invalid independent answer review provenance')
        try:supersedes=json.loads(review['supersedes_json'])
        except (ValueError,TypeError):research.error(400,'Invalid answer review supersession')
        if not isinstance(supersedes,list) or len(supersedes)>100 or any(not isinstance(key,str) for key in supersedes) or len(set(supersedes))!=len(supersedes):research.error(400,'Invalid answer review supersession')
        for identity in supersedes:
            old=reviews.get(identity)
            if old is None or old['answer_id']!=review['answer_id'] or old['reviewer_id']!=review['reviewer_id'] or identity==review['id']:research.error(400,'Foreign or self-referential review supersession')
        predecessors[review['id']]=supersedes
    pending=set(predecessors);done=set()
    while pending:
        ready={identity for identity in pending if all(key in done for key in predecessors[identity])}
        if not ready:research.error(400,'Cyclic answer review supersession')
        done.update(ready);pending-=ready
    for row in records.get('question_answer_selections',[]):
        answer=answers.get(row['answer_id'])
        if answer is None or row['question_id'] not in questions or answer['question_id']!=row['question_id'] or row['active'] not in (0,1):research.error(400,'Invalid selected answer scope')
    for row in records.get('question_resolution_events',[]):
        answer=answers.get(row['answer_id']) if row['answer_id'] is not None else None
        if row['question_id'] not in questions or row['answer_id'] is not None and (answer is None or answer['question_id']!=row['question_id']) or row['action'] not in ('resolved','open','investigating','withdrawn','invalidated'):research.error(400,'Invalid question resolution provenance')
        research.text(row['reason'],'question resolution reason',4000)
    for row in records.get('evidence_links',[]):
        if row['subject_type']=='answer' and row['subject_id'] not in answers:research.error(400,'Foreign answer source support')
    for row in records.get('research_review_notices',[]):
        if row['subject_type']=='answer' and row['subject_id'] not in answers:research.error(400,'Foreign answer review notice')
