"""Versioned project documents, pinned discussion and per-principal feedback.

Functions use the caller's SQLite connection and never commit or open a writer.
Document bodies are plain untrusted text; rendering and escaping belong to clients.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone

try:
    from . import roadmap_research
except ImportError:
    import roadmap_research

OPS = {'create_document', 'register_document_version', 'add_comment',
       'propose_edit', 'accept_proposal', 'reject_proposal', 'set_feedback',
       'approve_guidance'}
RESOURCES = {'documents', 'document-versions', 'discussions', 'proposals', 'feedback'}


def _err(status, message):
    roadmap_research.error(status, message)


def _id(value, name='ID'):
    return roadmap_research.text(value, name, 200)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(db, table, project, identity):
    return roadmap_research.scoped(db, table, project, identity)


def create_schema(db):
    statements = [
        "CREATE TABLE IF NOT EXISTS documents(id TEXT NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),kind TEXT NOT NULL,title TEXT NOT NULL,authority_kind TEXT NOT NULL CHECK(authority_kind IN ('registered_file','service_body')),current_version_id TEXT,revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,archived_at TEXT,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,current_version_id) REFERENCES document_versions(project_id,id) DEFERRABLE INITIALLY DEFERRED)",
        "CREATE TABLE IF NOT EXISTS document_versions(id TEXT NOT NULL,project_id TEXT NOT NULL,document_id TEXT NOT NULL,version_number INTEGER NOT NULL CHECK(version_number>=1),sha256 TEXT NOT NULL,body TEXT NOT NULL,source_artifact_version_id TEXT,source_created_at TEXT,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),UNIQUE(project_id,document_id,version_number),FOREIGN KEY(project_id,document_id) REFERENCES documents(project_id,id),FOREIGN KEY(project_id,source_artifact_version_id) REFERENCES artifact_versions(project_id,id))",
        "CREATE TABLE IF NOT EXISTS discussion_comments(id TEXT NOT NULL,project_id TEXT NOT NULL,document_id TEXT NOT NULL,document_version_id TEXT NOT NULL,parent_comment_id TEXT,anchor_json TEXT,body TEXT NOT NULL,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,document_id) REFERENCES documents(project_id,id),FOREIGN KEY(project_id,document_version_id) REFERENCES document_versions(project_id,id),FOREIGN KEY(project_id,parent_comment_id) REFERENCES discussion_comments(project_id,id))",
        "CREATE TABLE IF NOT EXISTS edit_proposals(id TEXT NOT NULL,project_id TEXT NOT NULL,document_id TEXT NOT NULL,base_version_id TEXT NOT NULL,proposed_body TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected','stale')),decision_reason TEXT,revision INTEGER NOT NULL DEFAULT 1,created_by TEXT REFERENCES principals(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(project_id,id),FOREIGN KEY(project_id,document_id) REFERENCES documents(project_id,id),FOREIGN KEY(project_id,base_version_id) REFERENCES document_versions(project_id,id))",
        "CREATE TABLE IF NOT EXISTS feedback(project_id TEXT NOT NULL,principal_id TEXT NOT NULL,document_version_id TEXT NOT NULL,value TEXT NOT NULL CHECK(value IN ('helpful','unhelpful')),reason TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(project_id,principal_id,document_version_id),FOREIGN KEY(project_id,principal_id) REFERENCES project_members(project_id,principal_id),FOREIGN KEY(project_id,document_version_id) REFERENCES document_versions(project_id,id))",
        "CREATE INDEX IF NOT EXISTS document_versions_order ON document_versions(project_id,document_id,version_number)",
        "CREATE INDEX IF NOT EXISTS discussions_by_version ON discussion_comments(project_id,document_version_id,created_at,id)",
        "CREATE INDEX IF NOT EXISTS proposals_by_document ON edit_proposals(project_id,document_id,state,created_at,id)",
    ]
    for sql in statements:
        db.execute(sql)
    for table in ('document_versions', 'discussion_comments'):
        for action in ('UPDATE', 'DELETE'):
            name = f'documents_immutable_{table}_{action.lower()}'
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'document versions and comments are immutable'); END")


def _capabilities(db, project, principal):
    row = db.execute('SELECT capabilities_json FROM project_members WHERE project_id=? AND principal_id=?',
                     (project, principal)).fetchone()
    if row is None:
        _err(403, 'Principal lacks project membership')
    return set(json.loads(row[0]))


def _event(db, project, principal, request_id, entity_type, entity_id, kind, after, before=None, rationale=None):
    event_id = str(uuid.uuid4())
    db.execute("INSERT INTO workspace_events(id,project_id,request_id,actor_id,entity_type,entity_id,kind,before_json,after_json,rationale,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (event_id, project, request_id, principal, entity_type, entity_id, kind,
                roadmap_research.canonical(before) if before is not None else None,
                roadmap_research.canonical(after) if after is not None else None,
                rationale, _now()))


def _new_version(db, project, principal, document, body, *, source_artifact_version_id=None,
                 source_created_at=None, source_hash=None):
    body = roadmap_research.text(body, 'document body', 2_000_000)
    actual = hashlib.sha256(body.encode('utf-8')).hexdigest()
    if source_hash is not None and source_hash != actual:
        _err(409, 'Document snapshot hash does not match source artifact')
    number = db.execute('SELECT COALESCE(MAX(version_number),0)+1 FROM document_versions WHERE project_id=? AND document_id=?',
                        (project, document['id'])).fetchone()[0]
    version_id = str(uuid.uuid4())
    now = _now()
    db.execute('INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?,?)',
               (version_id, project, document['id'], number, actual, body,
                source_artifact_version_id, source_created_at, principal, now))
    db.execute('UPDATE documents SET current_version_id=?,revision=revision+1,updated_at=? WHERE project_id=? AND id=?',
               (version_id, now, project, document['id']))
    return {'id': version_id, 'document_id': document['id'], 'version_number': number,
            'sha256': actual, 'source_artifact_version_id': source_artifact_version_id,
            'source_created_at': source_created_at, 'record_revision': number}


def _anchor(db, project, version_id, anchor):
    if anchor is None:
        return None
    if not isinstance(anchor, dict) or set(anchor) - {'heading', 'start', 'end', 'quote', 'sha256'}:
        _err(400, 'Invalid comment anchor')
    version = _row(db, 'document_versions', project, version_id)
    if 'heading' in anchor:
        heading = roadmap_research.text(anchor['heading'], 'anchor heading', 500)
        if heading not in version['body'].splitlines():
            _err(400, 'Anchored heading is absent from pinned document version')
    if any(k in anchor for k in ('start', 'end', 'quote', 'sha256')):
        if set(anchor) - {'heading', 'start', 'end', 'quote', 'sha256'} or not all(k in anchor for k in ('start','end','quote','sha256')):
            _err(400, 'Text-range anchor requires start, end, quote and sha256')
        start, end, quote = anchor['start'], anchor['end'], anchor['quote']
        if type(start) is not int or type(end) is not int or start < 0 or end < start or end > len(version['body']):
            _err(400, 'Invalid text-range anchor bounds')
        if version['body'][start:end] != quote or hashlib.sha256(quote.encode()).hexdigest() != anchor['sha256']:
            _err(409, 'Quoted anchor does not match pinned document version')
    return anchor


def apply_operation(db, project, principal, op, request_id):
    if not isinstance(op, dict) or not isinstance(op.get('op'), str):
        _err(400, 'Invalid document operation')
    kind = op['op']
    now = _now()
    identity = _id(op.get('id') or str(uuid.uuid4()))
    if kind == 'create_document':
        title = roadmap_research.text(op.get('title'), 'document title', 500)
        doc_kind = roadmap_research.text(op.get('kind'), 'document kind', 100)
        authority = op.get('authority_kind')
        if authority not in ('registered_file', 'service_body'):
            _err(400, 'Document authority must be registered_file or service_body')
        if authority == 'registered_file' and op.get('body') is not None:
            _err(400, 'Registered-file documents require a pinned artifact version, not an editable body')
        if authority == 'service_body' and 'body' not in op:
            _err(400, 'Service-body document requires an initial body')
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,NULL,1,?,?,?,NULL)',
                   (identity, project, doc_kind, title, authority, principal, now, now))
        result = {'type':'document','id':identity,'authority_kind':authority,'record_revision':1}
        _event(db, project, principal, request_id, 'document', identity, kind, result)
        if authority == 'service_body':
            doc = _row(db, 'documents', project, identity)
            version = _new_version(db, project, principal, doc, op['body'])
            _event(db, project, principal, request_id, 'document_version', version['id'], 'create_document_version', version)
            result['current_version_id'] = version['id']
            result['record_revision'] = _row(db, 'documents', project, identity)['revision']
        return result
    if kind == 'register_document_version':
        document = _row(db, 'documents', project, op.get('document_id'))
        if document['authority_kind'] != 'registered_file':
            _err(400, 'Service-body documents accept changes only through proposals')
        source = op.get('source')
        if not isinstance(source, dict) or set(source) != {'artifact_version_id', 'expected_sha256'}:
            _err(400, 'Registered-file version requires artifact_version_id and expected_sha256')
        source_version = _row(db, 'artifact_versions', project, _id(source['artifact_version_id'], 'artifact version ID'))
        expected = roadmap_research.sha(source['expected_sha256'])
        blob = db.execute('SELECT body FROM artifact_blobs WHERE project_id=? AND sha256=?',
                          (project, source_version['sha256'])).fetchone()
        if blob is None:
            _err(404, 'Pinned artifact bytes are unavailable')
        raw = bytes(blob['body'])
        actual = hashlib.sha256(raw).hexdigest()
        if actual != source_version['sha256'] or actual != expected:
            _err(409, 'Registered file hash differs from expected artifact hash')
        try:
            body = raw.decode('utf-8')
        except UnicodeDecodeError:
            _err(400, 'Document artifact must be valid UTF-8 text')
        version = _new_version(db, project, principal, document, body,
                               source_artifact_version_id=source_version['id'],
                               source_created_at=source_version['source_created_at'], source_hash=expected)
        _event(db, project, principal, request_id, 'document_version', version['id'], kind, version)
        return {'type':'document_version', **version}
    if kind == 'add_comment':
        version = _row(db, 'document_versions', project, op.get('document_version_id'))
        document = _row(db, 'documents', project, version['document_id'])
        parent_id = op.get('parent_comment_id')
        if parent_id:
            parent = _row(db, 'discussion_comments', project, _id(parent_id, 'parent comment ID'))
            if parent['document_id'] != document['id'] or parent['document_version_id'] != version['id']:
                _err(400, 'Replies must remain on the same document version and project')
        anchor = _anchor(db, project, version['id'], op.get('anchor'))
        body = roadmap_research.text(op.get('body'), 'comment body', 20000)
        db.execute('INSERT INTO discussion_comments VALUES(?,?,?,?,?,?,?,?,?)',
                   (identity, project, document['id'], version['id'], parent_id,
                    roadmap_research.canonical(anchor) if anchor is not None else None,
                    body, principal, now))
        result = {'type':'discussion_comment','id':identity,'document_version_id':version['id']}
        _event(db, project, principal, request_id, 'discussion_comment', identity, kind, result)
        return result
    if kind == 'propose_edit':
        document = _row(db, 'documents', project, op.get('document_id'))
        if document['authority_kind'] != 'service_body':
            _err(400, 'Registered-file documents must be edited in their source file')
        base = _row(db, 'document_versions', project, op.get('base_version_id'))
        if base['document_id'] != document['id']:
            _err(404, 'Proposal base version does not belong to document')
        body = roadmap_research.text(op.get('proposed_body'), 'proposed document body', 2_000_000)
        db.execute('INSERT INTO edit_proposals VALUES(?,?,?,?,? ,\'pending\',NULL,1,?,?,?)',
                   (identity, project, document['id'], base['id'], body, principal, now, now))
        result = {'type':'edit_proposal','id':identity,'base_version_id':base['id'],'state':'pending'}
        _event(db, project, principal, request_id, 'edit_proposal', identity, kind, result)
        return result
    if kind in ('accept_proposal', 'reject_proposal'):
        proposal = _row(db, 'edit_proposals', project, op.get('proposal_id'))
        document = _row(db, 'documents', project, proposal['document_id'])
        if proposal['state'] != 'pending':
            _err(409, 'Proposal is no longer pending')
        reason = roadmap_research.text(op.get('reason'), 'proposal decision reason', 20000)
        if kind == 'reject_proposal':
            db.execute("UPDATE edit_proposals SET state='rejected',decision_reason=?,revision=revision+1,updated_at=? WHERE project_id=? AND id=?",
                       (reason, now, project, proposal['id']))
            result = {'type':'edit_proposal','id':proposal['id'],'state':'rejected'}
            _event(db, project, principal, request_id, 'edit_proposal', proposal['id'], kind, result, before=dict(proposal), rationale=reason)
            return result
        if document['authority_kind'] != 'service_body':
            _err(400, 'Registered-file documents cannot accept service proposals')
        if document['current_version_id'] != proposal['base_version_id']:
            _err(409, 'Proposal base is stale; reconcile explicitly against the current document version')
        version = _new_version(db, project, principal, document, proposal['proposed_body'])
        db.execute("UPDATE edit_proposals SET state='accepted',decision_reason=?,revision=revision+1,updated_at=? WHERE project_id=? AND id=?",
                   (reason, now, project, proposal['id']))
        result = {'type':'edit_proposal','id':proposal['id'],'state':'accepted','version':version}
        _event(db, project, principal, request_id, 'document_version', version['id'], kind, result, before=dict(proposal), rationale=reason)
        return result
    if kind == 'set_feedback':
        version = _row(db, 'document_versions', project, op.get('document_version_id'))
        _capabilities(db, project, principal)
        value = op.get('value')
        if value not in ('helpful', 'unhelpful'):
            _err(400, 'Feedback value must be helpful or unhelpful')
        reason = roadmap_research.text(op.get('reason'), 'feedback reason', 20000)
        old = db.execute('SELECT * FROM feedback WHERE project_id=? AND principal_id=? AND document_version_id=?',
                         (project, principal, version['id'])).fetchone()
        before = dict(old) if old else None
        db.execute('INSERT INTO feedback VALUES(?,?,?,?,?,1,?,?) ON CONFLICT(project_id,principal_id,document_version_id) DO UPDATE SET value=excluded.value,reason=excluded.reason,revision=feedback.revision+1,updated_at=excluded.updated_at',
                   (project, principal, version['id'], value, reason, now, now))
        after = dict(db.execute('SELECT * FROM feedback WHERE project_id=? AND principal_id=? AND document_version_id=?',
                                (project, principal, version['id'])).fetchone())
        _event(db, project, principal, request_id, 'document_feedback', version['id'], kind, after, before=before, rationale=reason)
        return {'type':'document_feedback','id':version['id'],'value':value,'record_revision':after['revision']}
    if kind == 'approve_guidance':
        caps = _capabilities(db, project, principal)
        if 'approve_guidance' not in caps:
            _err(403, 'Principal lacks approve_guidance capability')
        version = _row(db, 'document_versions', project, op.get('document_version_id'))
        area_id, task_id = op.get('area_id'), op.get('task_id')
        if bool(area_id) == bool(task_id):
            _err(400, 'Guidance requires exactly one scoped area_id or task_id')
        if area_id:
            _row(db, 'areas', project, area_id)
        else:
            if not db.execute('SELECT 1 FROM tasks WHERE project_id=? AND uid=?', (project, _id(task_id,'task ID'))).fetchone():
                _err(404, 'Scoped task not found')
        identity = str(uuid.uuid4())
        db.execute('INSERT INTO guidance_links(id,project_id,area_id,task_id,document_version_id,inheritance_scope,body,source_path,revision,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?,?)',
                   (identity, project, area_id, task_id, version['id'], op.get('inheritance_scope','self'),
                    version['body'], None, principal, now, now))
        result = {'type':'guidance_link','id':identity,'document_version_id':version['id']}
        _event(db, project, principal, request_id, 'guidance_link', identity, kind, result)
        return result
    _err(400, 'Unknown document operation')


def read(db, resource, project, *, limit=None, cursor=None, document_id=None, **unused):
    try:
        from .roadmap_projection import API_VERSION, _coverage, _encode, _decode
    except ImportError:
        from roadmap_projection import API_VERSION, _coverage, _encode, _decode
    if resource not in RESOURCES:
        _err(404, 'Unknown document resource')
    if not db.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone():
        _err(404, 'Scoped project not found')
    limit = 100 if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= 100:
        _err(400, 'Page limit must be 1–100')
    meta = db.execute('SELECT * FROM store_meta WHERE singleton=1').fetchone()
    revision = f"{meta['store_id']}:{meta['revision']}"
    binding = {'resource':resource,'project_id':project,'revision':revision,'limit':limit,'document_id':document_id}
    offset = 0
    if cursor:
        page = _decode(cursor)
        if page.get('revision') != revision:
            exc = None
            try: _err(409, 'Snapshot cursor expired; restart required')
            except Exception as raised: exc = raised
            exc.restart_required = True
            raise exc
        if set(page) != set(binding) | {'offset'} or any(page.get(k) != v for k,v in binding.items()) or type(page.get('offset')) is not int or page['offset'] < 0:
            _err(400, 'Cursor does not match project/filter')
        offset = page['offset']
    if document_id is not None:
        _row(db, 'documents', project, document_id)
    if resource == 'documents':
        sql = "SELECT d.*,v.body AS current_body,v.sha256 AS current_sha256,v.version_number AS current_version_number,v.source_created_at FROM documents d LEFT JOIN document_versions v ON v.project_id=d.project_id AND v.id=d.current_version_id WHERE d.project_id=?"
        params = [project]
        if document_id is not None: sql += ' AND d.id=?'; params.append(document_id)
        sql += ' ORDER BY d.created_at,d.id LIMIT ? OFFSET ?'; params.extend([limit+1,offset])
    elif resource == 'document-versions':
        sql = 'SELECT * FROM document_versions WHERE project_id=?'; params=[project]
        if document_id is not None: sql += ' AND document_id=?'; params.append(document_id)
        sql += ' ORDER BY document_id,version_number LIMIT ? OFFSET ?'; params.extend([limit+1,offset])
    elif resource == 'discussions':
        sql = 'SELECT c.*,d.current_version_id,v.body AS pinned_body FROM discussion_comments c JOIN documents d ON d.project_id=c.project_id AND d.id=c.document_id JOIN document_versions v ON v.project_id=c.project_id AND v.id=c.document_version_id WHERE c.project_id=?'; params=[project]
        if document_id is not None: sql += ' AND c.document_id=?'; params.append(document_id)
        sql += ' ORDER BY c.created_at,c.id LIMIT ? OFFSET ?'; params.extend([limit+1,offset])
    elif resource == 'proposals':
        sql = 'SELECT * FROM edit_proposals WHERE project_id=?'; params=[project]
        if document_id is not None: sql += ' AND document_id=?'; params.append(document_id)
        sql += ' ORDER BY created_at,id LIMIT ? OFFSET ?'; params.extend([limit+1,offset])
    else:
        sql = 'SELECT f.*,v.document_id FROM feedback f JOIN document_versions v ON v.project_id=f.project_id AND v.id=f.document_version_id WHERE f.project_id=?'; params=[project]
        if document_id is not None: sql += ' AND v.document_id=?'; params.append(document_id)
        sql += ' ORDER BY f.document_version_id,f.created_at,f.principal_id LIMIT ? OFFSET ?'; params.extend([limit+1,offset])
    rows = db.execute(sql, params).fetchall()
    items=[]
    for raw in rows[:limit]:
        item=dict(raw)
        if resource=='discussions':
            item['anchor']=json.loads(item.pop('anchor_json')) if item['anchor_json'] else None
            item['outdated']=bool(item['anchor']) and item['current_version_id'] != item['document_version_id']
            item.pop('pinned_body',None)
        items.append(item)
    if resource=='feedback':
        all_items=[]
        for item in items:
            version_id=item['document_version_id']
            aggregate=db.execute('SELECT COUNT(*) AS sample_size,SUM(value=\'helpful\') AS helpful_count,SUM(value=\'unhelpful\') AS unhelpful_count FROM feedback WHERE project_id=? AND document_version_id=?',(project,version_id)).fetchone()
            reasons=[dict(x) for x in db.execute('SELECT value,reason FROM feedback WHERE project_id=? AND document_version_id=? ORDER BY updated_at DESC,principal_id LIMIT 20',(project,version_id))]
            if not any(x['document_version_id']==version_id for x in all_items):
                all_items.append({'document_version_id':version_id,'document_id':item['document_id'],'sample_size':aggregate['sample_size'],'helpful_count':aggregate['helpful_count'] or 0,'unhelpful_count':aggregate['unhelpful_count'] or 0,'reasons':reasons})
        items=all_items
    out={'api_version':API_VERSION,'project_id':project,'revision':revision,'items':items,
         'coverage':{**_coverage(db,project),'source':'project documents and discussion','complete':True},
         'next_cursor':_encode({**binding,'offset':offset+limit}) if len(rows)>limit else None}
    return out
