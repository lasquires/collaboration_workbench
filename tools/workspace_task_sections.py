"""Bounded, read-only sections for the selected task workflow."""
import json
import uuid

try:
    from . import roadmap_research as research
    from . import roadmap_structure as structure
    from . import workspace_answers as answers
    from . import workspace_findings as findings
    from .roadmap_projection import API_VERSION, _coverage, _decode, _encode
except ImportError:
    import roadmap_research as research
    import roadmap_structure as structure
    import workspace_answers as answers
    import workspace_findings as findings
    from roadmap_projection import API_VERSION, _coverage, _decode, _encode


RESOURCES = {'task-children', 'task-findings', 'task-questions'}
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _entity(row):
    """Convert SQLite JSON columns and revisions to public projection fields."""
    result = dict(row)
    for field in list(result):
        if field.endswith('_json'):
            result[field[:-5]] = json.loads(result.pop(field))
    if 'revision' in result:
        result['record_revision'] = result.pop('revision')
    return result


def _snapshot(db, project):
    coverage = _coverage(db, project)
    meta = db.execute('SELECT store_id,revision FROM store_meta').fetchone()
    revision = f'{meta[0]}:{meta[1]}'
    return coverage, revision


def _limit(value):
    if value is None:
        return DEFAULT_LIMIT
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        research.error(400, f'Task section page limit must be 1–{MAX_LIMIT}')
    return value


def _selected_task(db, project, task_id):
    if not isinstance(task_id, str):
        research.error(404, 'Selected stable task not found')
    try:
        if str(uuid.UUID(task_id)) != task_id:
            research.error(404, 'Selected stable task not found')
    except (ValueError, AttributeError):
        research.error(404, 'Selected stable task not found')
    row = db.execute(
        'SELECT * FROM tasks WHERE project_id=? AND uid=?', (project, task_id)
    ).fetchone()
    if row is None:
        research.error(404, 'Selected stable task not found')
    return row


def _binding(resource, project, task_id, limit, revision):
    return {
        'resource': resource,
        'project_id': project,
        'task_id': task_id,
        'limit': limit,
        'revision': revision,
    }


def _offset(cursor, binding):
    if cursor is None:
        return 0
    page = _decode(cursor)
    if page.get('revision') != binding['revision']:
        try:
            research.error(409, 'Task section cursor expired; restart required')
        except Exception as exc:
            exc.restart_required = True
            raise
    if (set(page) != set(binding) | {'offset'}
            or any(page.get(key) != value for key, value in binding.items())
            or type(page.get('offset')) is not int or page['offset'] < 0):
        research.error(400, 'Cursor does not match task section scope')
    return page['offset']


def _children(db, project, parent, limit, offset):
    rows = db.execute(
        'SELECT t.*,c.required_for_parent FROM containment c '
        'JOIN tasks t ON t.uid=c.child_id AND t.project_id=c.project_id '
        'WHERE c.project_id=? AND c.parent_id=? '
        'ORDER BY c.sibling_order,c.child_id LIMIT ? OFFSET ?',
        (project, parent['uid'], limit + 1, offset),
    ).fetchall()
    items = []
    for row in rows:
        record = json.loads(row['record_json'])
        aliases = [item[0] for item in db.execute(
            "SELECT alias FROM entity_aliases WHERE project_id=? "
            "AND entity_type='task' AND entity_id=? "
            'ORDER BY CASE WHEN alias=? THEN 0 ELSE 1 END,alias',
            (project, row['uid'], row['id']),
        )]
        archived_at = row['archived_at']
        archive_reason = row['archive_reason']
        items.append({
            'id': row['uid'],
            'alias': aliases[0] if aliases else row['id'],
            'aliases': aliases,
            'legacy_id': row['id'],
            'project_id': project,
            'title': record.get('title', ''),
            'summary': record.get('summary', ''),
            'status': record.get('status'),
            'parent_id': parent['uid'],
            'record_revision': row['revision'],
            'structure_revision': row['structure_revision'],
            'archived': archived_at is not None or archive_reason is not None,
            'archived_at': archived_at,
            'archive_reason': archive_reason,
            'required_for_parent': bool(row['required_for_parent']),
        })
    return items


def _findings(db, project, task_scope_ids, limit, offset):
    marks = ','.join('?' for _ in task_scope_ids)
    rows = db.execute(
        'SELECT f.*,r.claim,r.scope_json,r.classification,r.uncertainty,'
        'r.evidence_revision,r.presentation_revision '
        'FROM findings f JOIN finding_revisions r '
        'ON r.project_id=f.project_id AND r.id=f.current_revision_id '
        f"WHERE f.project_id=? AND json_extract(r.scope_json,'$.task_id') IN ({marks}) "
        'ORDER BY f.created_at,f.id LIMIT ? OFFSET ?',
        (project, *task_scope_ids, limit + 1, offset),
    ).fetchall()
    return [findings.describe(db, project, _entity(row)) for row in rows]


def _questions(db, project, task_scope_ids, limit, offset):
    marks = ','.join('?' for _ in task_scope_ids)
    rows = db.execute(
        'SELECT q.*,r.text,r.expected_evidence,r.criteria_json,'
        'r.criteria_revision,r.presentation_revision '
        'FROM questions q JOIN question_revisions r '
        'ON r.project_id=q.project_id AND r.id=q.current_revision_id '
        f'WHERE q.project_id=? AND q.owner_task_id IN ({marks}) '
        'ORDER BY q.created_at,q.id LIMIT ? OFFSET ?',
        (project, *task_scope_ids, limit + 1, offset),
    ).fetchall()
    return [answers.describe_question(db, project, _entity(row)) for row in rows]


def read(db, resource, project, *, task_id, limit=None, cursor=None, **unused):
    """Return one exact-task section page without mutating workspace state."""
    if resource not in RESOURCES:
        research.error(404, 'Unknown task section')
    if unused:
        research.error(400, 'Unsupported task section option')
    task = _selected_task(db, project, task_id)
    limit = _limit(limit)
    coverage, revision = _snapshot(db, project)
    task_scope_ids = (
        structure.redirect_group(db, project, task['uid'])
        if resource in {'task-findings', 'task-questions'}
        else [task['uid']]
    )
    binding = _binding(resource, project, task['uid'], limit, revision)
    offset = _offset(cursor, binding)
    if resource == 'task-children':
        rows = _children(db, project, task, limit, offset)
    elif resource == 'task-findings':
        rows = _findings(db, project, task_scope_ids, limit, offset)
    else:
        rows = _questions(db, project, task_scope_ids, limit, offset)
    next_cursor = None
    if len(rows) > limit:
        next_cursor = _encode({**binding, 'offset': offset + limit})
    return {
        'api_version': API_VERSION,
        'project_id': project,
        'revision': revision,
        'coverage': coverage,
        'task_id': task['uid'],
        'task_scope_ids': task_scope_ids,
        'items': rows[:limit],
        'next_cursor': next_cursor,
    }
