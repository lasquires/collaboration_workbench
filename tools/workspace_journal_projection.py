"""Pure, deterministic public projections of explicitly reviewed task journals."""
import json
import re
import uuid

try:
    from . import roadmap_research as research
except ImportError:
    import roadmap_research as research

POLICY_VERSION = 1
COLUMNS = ('id','project_id','source_event_id','task_id','source_sha256','origin_store_id','mode','policy_version','details_json','source_actor_json','created_by','kind','recorded_at','historical_at','public_sha256')
_REDACT = '[LOCAL_PATH_REDACTED]'
_NOTICE = {'notice':'Original journal entry withheld after explicit local review.'}
_KINDS = {'observation','attempt','blocker','review','handoff'}
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _path_check(value, roots):
    # Lazy import avoids coupling module initialization and preserves the exchange gate.
    try:
        from .roadmap_exchange import _portable_path_check
    except ImportError:
        from roadmap_exchange import _portable_path_check
    try:
        _portable_path_check(value, roots)
        return False
    except Exception as exc:
        # The shared gate raises StoreError(409); do not suppress unrelated faults.
        if getattr(exc, 'status', None) == 409:
            return True
        raise


def _strict_json(value, expected, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError('invalid JSON column')
    parsed = json.loads(value)
    if not isinstance(parsed, expected) or research.canonical(parsed) != value:
        raise ValueError('noncanonical JSON column')
    return parsed


def _raw(row):
    if not isinstance(row, dict):
        row = dict(row)
    try:
        source = json.loads(row['source_event_json'])
        details = json.loads(row['details_json'])
        if (not isinstance(source, dict) or set(source) != {'sequence','event_id','request_id','task_id','actor_json','recorded_at','historical_at','kind','details_json'}
                or type(source['sequence']) is not int or source['sequence'] < 1
                or not isinstance(details, dict) or source['details_json'] != row['details_json']
                # The event preserves its authored task key; the journal table stores
                # the resolved stable task UID. These intentionally need not match.
                or not isinstance(source.get('task_id'), str) or not isinstance(source.get('request_id'),str)
                or source['event_id'] != row['id']
                or source['kind'] != row['kind'] or source['recorded_at'] != row['recorded_at']
                or source['historical_at'] != row['historical_at']
                or row['kind'] not in _KINDS or not _SHA.fullmatch(row['source_sha256'])
                or research.digest(source) != row['source_sha256']):
            raise ValueError('journal provenance mismatch')
        # Original JSON formatting is immutable input, not a new public format requirement.
        # Its parsed event digest and exact details equality are the existing exchange contract.
        actor = json.loads(source['actor_json']) if source.get('actor_json') else None
        if actor is not None and not isinstance(actor,dict):raise ValueError('invalid source actor')
        if actor is not None and (set(actor) != {'name','kind'} or not isinstance(actor['name'], str) or actor['kind'] not in ('human','agent')):
            raise ValueError('invalid source actor')
        return row, source, details, actor
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid journal source') from exc


def _unsafe_details(value, roots):
    if isinstance(value, dict):
        for key, child in value.items():
            if _path_check(key, roots):
                return True
            if _unsafe_details(child, roots):
                return True
        return False
    if isinstance(value, list):
        return any(_unsafe_details(child, roots) for child in value)
    if isinstance(value, str):
        if value.startswith(('{','[')):
            try:
                nested = json.loads(value)
            except ValueError:
                pass
            else:
                return _unsafe_details(nested, roots)
        return _path_check(value, roots)
    return False


def unsafe_journal(row, known_roots=()):
    try:
        source_row, _, details, actor = _raw(row)
        metadata = {key: source_row[key] for key in ('id','project_id','task_id','origin_store_id','created_by','kind','recorded_at','historical_at')}
        if _path_check(metadata, known_roots):
            return True
        if actor is not None and _path_check(actor, known_roots):
            return True
        return _unsafe_details(details, known_roots)
    except (ValueError, TypeError, KeyError):
        # Invalid source cannot be safely treated as shareable.
        return True


def _redact(value, roots):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if _path_check(key, roots):
                raise ValueError('unsafe object key cannot be rewritten')
            result[key] = _redact(child, roots)
        return result
    if isinstance(value, list):
        return [_redact(child, roots) for child in value]
    if isinstance(value, str):
        if value.startswith(('{','[')):
            try:
                nested = json.loads(value)
            except ValueError:
                nested = None
            if nested is not None:
                return research.canonical(_redact(nested, roots))
        return _REDACT if _path_check(value, roots) else value
    return value


def _safe_public_metadata(row, actor, roots):
    metadata = {key: row[key] for key in ('id','project_id','task_id','origin_store_id','created_by','kind','recorded_at','historical_at')}
    if actor is not None and _path_check(actor, roots):
        raise ValueError('unsafe actor attribution cannot be rewritten')
    if _path_check(metadata, roots):
        raise ValueError('unsafe public metadata')
    return metadata


def project_journal(row, mode, known_roots=()):
    row, source, details, actor = _raw(row)
    if mode not in ('redacted','omitted'):
        raise ValueError('mode must be redacted or omitted')
    metadata = _safe_public_metadata(row, actor, known_roots)
    if mode == 'omitted':
        public_details = _NOTICE
    else:
        if _unsafe_details(details, known_roots):
            # Unsafe object keys are identity-bearing and must fail closed.
            def check_keys(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if _path_check(key, known_roots): raise ValueError('unsafe object key cannot be rewritten')
                        check_keys(child)
                elif isinstance(value, list):
                    for child in value: check_keys(child)
                elif isinstance(value, str) and value.startswith(('{','[')):
                    try: nested = json.loads(value)
                    except ValueError: return
                    check_keys(nested)
            check_keys(details)
        public_details = _redact(details, known_roots)
    content = {
        'project_id':metadata['project_id'], 'source_event_id':metadata['id'], 'task_id':metadata['task_id'],
        'source_sha256':row['source_sha256'], 'origin_store_id':metadata['origin_store_id'], 'mode':mode,
        'policy_version':POLICY_VERSION, 'details_json':research.canonical(public_details),
        'source_actor_json':research.canonical(actor) if actor is not None else None,
        'created_by':metadata['created_by'], 'kind':metadata['kind'], 'recorded_at':metadata['recorded_at'],
        'historical_at':metadata['historical_at'],
    }
    public_sha = research.digest(content)
    result = {'id':str(uuid.uuid5(uuid.NAMESPACE_URL, 'workbench-journal-disclosure:'+public_sha)), **content, 'public_sha256':public_sha}
    validate_disclosure(result)
    return result


def validate_disclosure(row):
    if not isinstance(row, dict) or set(row) != set(COLUMNS):
        raise ValueError('disclosure columns mismatch')
    for key in ('id','project_id','source_event_id','task_id','origin_store_id','kind','recorded_at'):
        if not isinstance(row[key], str) or not row[key] or '\0' in row[key]: raise ValueError('invalid disclosure text')
    for key in ('created_by','historical_at'):
        if row[key] is not None and (not isinstance(row[key], str) or '\0' in row[key]): raise ValueError('invalid nullable disclosure text')
    if row['kind'] not in _KINDS or row['mode'] not in ('redacted','omitted') or type(row['policy_version']) is not int or row['policy_version'] != POLICY_VERSION:
        raise ValueError('invalid disclosure mode, kind, or policy')
    if not isinstance(row['source_sha256'], str) or not _SHA.fullmatch(row['source_sha256']) or not isinstance(row['public_sha256'], str) or not _SHA.fullmatch(row['public_sha256']):
        raise ValueError('invalid disclosure hash')
    details = _strict_json(row['details_json'], dict)
    actor = _strict_json(row['source_actor_json'], dict, nullable=True)
    if row['mode'] == 'omitted' and details != _NOTICE: raise ValueError('invalid omission notice')
    if actor is not None and (set(actor) != {'name','kind'} or not isinstance(actor['name'], str) or actor['kind'] not in ('human','agent')):
        raise ValueError('invalid disclosure actor')
    content = {key:row[key] for key in COLUMNS if key not in ('id','public_sha256')}
    if research.digest(content) != row['public_sha256'] or str(uuid.uuid5(uuid.NAMESPACE_URL, 'workbench-journal-disclosure:'+row['public_sha256'])) != row['id']:
        raise ValueError('disclosure identity or hash mismatch')
    if _path_check({key:row[key] for key in COLUMNS if key not in ('details_json','public_sha256','id')}, ()):
        raise ValueError('unsafe disclosure metadata')
    if _unsafe_details(details, ()):
        raise ValueError('unsafe disclosure details')
    return None
