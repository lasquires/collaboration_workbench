"""Exact source reads and explicitly authored derivation assertions.

The marker records an assertion, never independent forensic verification. Reads
preserve revision and withdrawal history without choosing a preferred witness.
"""
import json
from pathlib import PurePosixPath, PureWindowsPath
try:
    from . import roadmap_research as research
    from .roadmap_projection import API_VERSION, _coverage, _decode, _encode, _error
except ImportError:
    import roadmap_research as research
    from roadmap_projection import API_VERSION, _coverage, _decode, _encode, _error

FORMAT = 'workbench-derived-source/v1'
RESOURCES = {'source-context', 'source-derivations', 'observations', 'negative-searches'}


def is_derivation(scope):
    derivation = scope.get('derivation') if isinstance(scope, dict) else None
    return isinstance(derivation, dict) and derivation.get('format') == FORMAT


def _resolve_scope(scope, version, run, has_input):
    if not is_derivation(scope):
        return None
    derivation = scope['derivation']
    if set(derivation) - {'format', 'parent_version_id', 'index_run_id'}:
        research.error(400, 'Unknown source derivation fields')
    child_id = research.text(scope.get('artifact_version_id'), 'child artifact version', 200)
    parent_id = research.text(derivation.get('parent_version_id'), 'parent artifact version', 200)
    if child_id == parent_id:
        research.error(400, 'A source derivation requires distinct parent and child versions')
    child, parent = version(child_id), version(parent_id)
    origin = None
    if 'index_run_id' in derivation:
        run_id = research.text(derivation['index_run_id'], 'index run ID', 200)
        origin = run(run_id)
        if origin['state'] != 'published':
            research.error(409, 'Source derivation index run is not published')
        if not has_input(run_id, parent_id):
            research.error(400, 'Source derivation run did not include the parent version')
    return {'parent_version': dict(parent), 'child_version': dict(child),
            'origin_run': dict(origin) if origin is not None else None}


def validate_scope(db, project, scope):
    return _resolve_scope(scope,
        lambda identity: research.scoped(db, 'artifact_versions', project, identity),
        lambda identity: research.scoped(db, 'index_runs', project, identity),
        lambda run, version: db.execute(
            'SELECT 1 FROM index_run_inputs WHERE project_id=? AND run_id=? AND artifact_version_id=?',
            (project, run, version)).fetchone() is not None)


def validate_support(scope, items):
    if not is_derivation(scope):
        return
    required = {scope.get('artifact_version_id'), scope['derivation'].get('parent_version_id')}
    cited = set()
    for item in items:
        if not isinstance(item, dict) or item.get('artifact_version_id') not in required:
            continue
        locator = item.get('locator')
        if locator is None and 'locator_json' in item:
            try:
                locator = json.loads(item['locator_json'])
            except (ValueError, TypeError):
                research.error(400, 'Invalid source derivation citation locator')
        if not isinstance(locator, (str, dict)) or not locator:
            research.error(400, 'Source derivation citation requires an exact locator')
        research.text(item.get('method'), 'source derivation citation method', 4000)
        research.text(item.get('limitations'), 'source derivation citation limitations', 4000)
        cited.add(item['artifact_version_id'])
    if not required <= cited:
        research.error(400, 'Source derivation requires citations for both parent and child versions')


def validate_bundle(db, project, records):
    """Validate incoming identities using only the supplied portable records.

    Local records must never launder an incomplete or foreign incoming reference.
    The ordinary finding validator retains responsibility for general lineage,
    immutable support and withdrawal checks, including untyped findings.
    """
    def table(name):
        return {row['id']: row for row in records.get(name, []) if row.get('project_id') == project}
    versions, runs = table('artifact_versions'), table('index_runs')
    revisions, findings = table('finding_revisions'), table('findings')
    inputs = {(row.get('run_id'), row.get('artifact_version_id'))
              for row in records.get('index_run_inputs', []) if row.get('project_id') == project}
    lineage = {row['revision_id']: row['evidence_origin_id']
               for row in records.get('finding_revision_lineage', []) if row.get('project_id') == project}
    links = {}
    for row in records.get('evidence_links', []):
        if row.get('project_id') == project and row.get('subject_type') == 'finding_revision' and row.get('state') in {'available', 'retracted', 'superseded'}:
            links.setdefault(row.get('subject_id'), []).append(row)
    def required(rows, identity, label):
        row = rows.get(identity)
        if row is None:
            research.error(400, 'Typed source derivation references a missing or foreign '+label)
        return row
    for revision in revisions.values():
        scope = json.loads(revision['scope_json'])
        if not is_derivation(scope):
            continue
        required(findings, revision.get('finding_id'), 'finding')
        _resolve_scope(scope,
            lambda identity: required(versions, identity, 'source version'),
            lambda identity: required(runs, identity, 'index run'),
            lambda run, version: (run, version) in inputs)
        origin_id = lineage.get(revision['id'], revision['id'])
        origin = required(revisions, origin_id, 'evidence origin')
        if origin.get('finding_id') != revision.get('finding_id') or origin.get('evidence_revision') != revision.get('evidence_revision') or json.loads(origin['scope_json']) != scope or lineage.get(origin_id, origin_id) != origin_id:
            research.error(400, 'Typed source derivation evidence lineage differs from assertion scope')
        validate_support(scope, links.get(origin_id, []))


def _entity(row):
    item = dict(row)
    for field in list(item):
        if field.endswith('_json'):
            item[field[:-5]] = json.loads(item.pop(field))
    if 'revision' in item:
        item['record_revision'] = item.pop('revision')
    return item


def _version(db, project, identity):
    research.text(identity, 'source version ID', 200)
    row = db.execute('SELECT v.*,a.relative_path,a.kind FROM artifact_versions v JOIN artifacts a ON a.project_id=v.project_id AND a.id=v.artifact_id WHERE v.project_id=? AND v.id=?', (project, identity)).fetchone()
    if row is None:
        research.error(404, 'Source version not found')
    item = dict(row)
    # Root bindings are intentionally absent. Reject unsafe imported metadata too.
    path = item['relative_path']
    if not isinstance(path, str) or not path or '\x00' in path or '\\' in path or ':' in path or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive or '..' in PurePosixPath(path).parts:
        research.error(409, 'Source artifact relative path requires review')
    item['local_bytes_available'] = db.execute('SELECT 1 FROM artifact_blobs WHERE project_id=? AND sha256=?', (project, row['sha256'])).fetchone() is not None
    return item


def _paging(resource, project, subject, limit, cursor, revision):
    limit = 20 if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= 100:
        research.error(400, 'Source page limit must be between 1 and 100')
    binding = {'project_id': project, 'resource': resource, 'subject_id': subject,
               'limit': limit, 'revision': revision}
    offset = 0
    if cursor is not None:
        page = _decode(cursor)
        if set(page) != set(binding) | {'offset'} or type(page.get('offset')) is not int or page['offset'] < 0 or type(page.get('limit')) is not int or not isinstance(page.get('revision'), str) or any(page.get(key) != value for key, value in binding.items() if key != 'revision'):
            research.error(400, 'Cursor does not match source scope')
        if page['revision'] != revision:
            _error(409, 'Snapshot cursor expired; restart required', restart_required=True)
        offset = page['offset']
    return limit, offset, binding


def _derivations(db, project, subject, limit, offset):
    # Filter before paging: historical typed revisions remain visible even when
    # their finding's current revision changes scope or is archived.
    query = """SELECT r.*,f.archived_at,f.current_revision_id,l.evidence_origin_id
        FROM finding_revisions r JOIN findings f ON f.project_id=r.project_id AND f.id=r.finding_id
        LEFT JOIN finding_revision_lineage l ON l.project_id=r.project_id AND l.revision_id=r.id
        WHERE r.project_id=? AND json_extract(r.scope_json,'$.derivation.format')=?
        AND (json_extract(r.scope_json,'$.artifact_version_id')=? OR json_extract(r.scope_json,'$.derivation.parent_version_id')=?)
        ORDER BY r.created_at,r.id LIMIT ? OFFSET ?"""
    rows = db.execute(query, (project, FORMAT, subject, subject, limit + 1, offset)).fetchall()
    items = []
    for row in rows[:limit]:
        item = _entity(row)
        scope = item['scope']
        origin_id = item['evidence_origin_id'] or item['id']
        counts = {'available': 0, 'retracted': 0, 'superseded': 0}
        for count in db.execute("SELECT state,COUNT(*) AS count FROM evidence_links WHERE project_id=? AND subject_type='finding_revision' AND subject_id=? GROUP BY state", (project, origin_id)):
            counts[count['state']] = count['count']
        run_id = scope['derivation'].get('index_run_id')
        item.update(is_current=item['id'] == item.pop('current_revision_id'),
                    archived=item['archived_at'] is not None,
                    parent_version=_version(db, project, scope['derivation']['parent_version_id']),
                    child_version=_version(db, project, scope['artifact_version_id']),
                    origin_run=_entity(research.scoped(db, 'index_runs', project, run_id)) if run_id else None,
                    evidence_origin_id=origin_id, support_counts=counts)
        items.append(item)
    return items, len(rows) > limit


def read(db, resource, project, *, key=None, subject_id=None, limit=None, cursor=None, **options):
    if resource not in RESOURCES:
        research.error(404, 'Unknown source context resource')
    if options:
        research.error(400, 'Unsupported source context options')
    coverage = _coverage(db, project)
    meta = db.execute('SELECT store_id,revision FROM store_meta WHERE singleton=1').fetchone()
    revision = f'{meta[0]}:{meta[1]}'
    envelope = {'api_version': API_VERSION, 'project_id': project, 'revision': revision, 'coverage': coverage}
    if resource == 'source-context':
        if key is None or subject_id is not None or limit is not None or cursor is not None:
            research.error(400, 'Source context requires key and does not support paging')
        version = _version(db, project, key)
        related = {name: {'resource': target, 'params': {'project_id': project, 'subject_id': key}}
                   for name, target in [('derivations', 'source-derivations'), ('observations', 'observations'), ('negative_searches', 'negative-searches')]}
        return {**envelope, 'context': {'entity_type': 'source', 'entity': version, 'related': related}, 'next_cursor': None}
    if subject_id is None or key is not None:
        research.error(400, 'Source page requires subject_id and does not accept key')
    _version(db, project, subject_id)
    limit, offset, binding = _paging(resource, project, subject_id, limit, cursor, revision)
    if resource == 'source-derivations':
        items, more = _derivations(db, project, subject_id, limit, offset)
    else:
        if resource == 'negative-searches':
            query = 'SELECT * FROM negative_searches WHERE project_id=? AND artifact_version_id=? ORDER BY created_at,id LIMIT ? OFFSET ?'
        else:
            query = """SELECT o.*,c.artifact_version_id,c.namespace,c.scope_key,c.locator_key,
                v.sha256 AS source_sha256,v.binary_build_id,v.availability
                FROM observations o JOIN occurrences c ON c.project_id=o.project_id AND c.id=o.occurrence_id
                JOIN artifact_versions v ON v.project_id=c.project_id AND v.id=c.artifact_version_id
                WHERE o.project_id=? AND c.artifact_version_id=?
                AND EXISTS(SELECT 1 FROM run_observations ro JOIN index_runs r ON r.project_id=ro.project_id AND r.id=ro.run_id WHERE ro.project_id=o.project_id AND ro.observation_id=o.id AND r.state='published')
                ORDER BY o.created_at,o.id LIMIT ? OFFSET ?"""
        rows = db.execute(query, (project, subject_id, limit + 1, offset)).fetchall()
        more = len(rows) > limit
        items = [_entity(row) for row in rows[:limit]]
        if resource == 'observations':
            for item in items:
                item['locator_key'] = json.loads(item['locator_key'])
                item['candidate_type'] = 'observation'
                item['support_hashes'] = [item['source_sha256'], item['content_hash']]
    return {**envelope, 'subject_id': subject_id, 'items': items,
            'next_cursor': _encode({**binding, 'offset': offset + limit}) if more else None}
