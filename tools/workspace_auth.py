"""Optional bearer authentication for the live workspace service.

All operations use the caller's SQLite connection and transaction.  The server
must authenticate each remote request and enforce the returned project scope.
Local principal labels are never credentials.  Owner bootstrap is a trusted,
explicit service operation and must not be exposed as a public endpoint.
"""

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

try:
    from .roadmap_store import StoreError
except ImportError:
    from roadmap_store import StoreError


CAPABILITIES = frozenset({'contribute', 'review', 'manage_claims', 'manage_projects'})
MAX_INVITE_TTL = 30 * 24 * 60 * 60
MAX_TOKEN_TTL = 90 * 24 * 60 * 60


def _fail(status, message):
    raise StoreError(status, message)


def _time(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        _fail(400, 'A timezone-aware time is required')
    return now.astimezone(timezone.utc)


def _ttl(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        _fail(400, 'Credential lifetime is outside the allowed range')
    return value


def _secret():
    return secrets.token_urlsafe(48)  # 384 random bits; returned once.


def _digest(secret):
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def _valid_secret(secret):
    if not isinstance(secret, str) or not 32 <= len(secret) <= 256:
        _fail(401, 'Invalid credential')
    return _digest(secret)


def _capabilities(value):
    if value is None:
        return ['contribute']
    if not isinstance(value, (list, tuple, set)) or not value or any(not isinstance(v, str) for v in value):
        _fail(400, 'Invalid capabilities')
    caps = set(value)
    if not caps <= CAPABILITIES:
        _fail(400, 'Unknown capability')
    return sorted(caps)


def _member_caps(db, project_id, principal_id):
    row = db.execute('SELECT m.capabilities_json FROM project_members m '
                     'JOIN projects p ON p.id=m.project_id '
                     'JOIN principals a ON a.id=m.principal_id '
                     'WHERE m.project_id=? AND m.principal_id=? '
                     'AND p.archived_at IS NULL AND a.archived_at IS NULL',
                     (project_id, principal_id)).fetchone()
    return set(json.loads(row[0])) if row else set()


def create_schema(db):
    """Create credential tables inside the caller's migration transaction."""
    db.execute('CREATE TABLE IF NOT EXISTS auth_tokens('
               'id TEXT PRIMARY KEY,secret_hash TEXT NOT NULL UNIQUE,'
               'principal_id TEXT NOT NULL REFERENCES principals(id),'
               'project_id TEXT REFERENCES projects(id),'
               'is_owner INTEGER NOT NULL CHECK(is_owner IN (0,1)),'
               'issued_at TEXT NOT NULL,expires_at TEXT NOT NULL,revoked_at TEXT,'
               'CHECK((is_owner=1 AND project_id IS NULL) OR '
               '(is_owner=0 AND project_id IS NOT NULL)))')
    db.execute('CREATE INDEX IF NOT EXISTS auth_tokens_principal ON auth_tokens(principal_id,project_id)')
    db.execute('CREATE TABLE IF NOT EXISTS auth_invites('
               'id TEXT PRIMARY KEY,secret_hash TEXT NOT NULL UNIQUE,'
               'project_id TEXT NOT NULL REFERENCES projects(id),'
               'issuer_id TEXT NOT NULL REFERENCES principals(id),'
               'label TEXT NOT NULL,capabilities_json TEXT NOT NULL,'
               'issued_at TEXT NOT NULL,expires_at TEXT NOT NULL,'
               'redeemed_at TEXT,redeemed_by TEXT REFERENCES principals(id),'
               'revoked_at TEXT)')
    db.execute('CREATE INDEX IF NOT EXISTS auth_invites_project ON auth_invites(project_id,issuer_id)')


def issue_owner_token(db, principal_id, *, ttl_seconds=MAX_TOKEN_TTL, now=None):
    """Explicit trusted bootstrap only; never wire this to a public route."""
    instant = _time(now)
    ttl = _ttl(ttl_seconds, MAX_TOKEN_TTL)
    principal = db.execute('SELECT 1 FROM principals WHERE id=? AND archived_at IS NULL',
                           (principal_id,)).fetchone()
    if not principal:
        _fail(404, 'Principal not found')
    secret = _secret()
    token_id = str(uuid.uuid4())
    db.execute('INSERT INTO auth_tokens VALUES(?,?,?,?,?,?,?,NULL)',
               (token_id, _digest(secret), principal_id, None, 1,
                instant.isoformat(), (instant + timedelta(seconds=ttl)).isoformat()))
    return {'id': token_id, 'token': secret, 'principal_id': principal_id,
            'expires_at': (instant + timedelta(seconds=ttl)).isoformat()}


def authenticate(db, token, *, now=None):
    """Reject absent, invalid, expired, revoked, or detached credentials."""
    digest = _valid_secret(token)
    instant = _time(now)
    row = db.execute('SELECT t.id,t.principal_id,t.project_id,t.is_owner,t.expires_at,t.revoked_at '
                     'FROM auth_tokens t JOIN principals p ON p.id=t.principal_id '
                     'WHERE t.secret_hash=? AND p.archived_at IS NULL', (digest,)).fetchone()
    if not row or row[5] is not None or instant >= datetime.fromisoformat(row[4]):
        _fail(401, 'Invalid credential')
    if row[3]:
        return {'token_id': row[0], 'principal_id': row[1], 'project_id': None,
                'is_owner': True, 'capabilities': []}
    caps = _member_caps(db, row[2], row[1])
    if not caps:
        _fail(401, 'Invalid credential')
    return {'token_id': row[0], 'principal_id': row[1], 'project_id': row[2],
            'is_owner': False, 'capabilities': sorted(caps)}


def issue_invite(db, project_id, issuer_id, label, capabilities=None, *,
                 ttl_seconds=7 * 24 * 60 * 60, now=None):
    instant = _time(now)
    ttl = _ttl(ttl_seconds, MAX_INVITE_TTL)
    if not isinstance(label, str) or not label.strip() or len(label) > 200 or '\0' in label:
        _fail(400, 'Invalid invite label')
    issuer_caps = _member_caps(db, project_id, issuer_id)
    if 'manage_projects' not in issuer_caps:
        _fail(403, 'Project administration capability required')
    caps = _capabilities(capabilities)
    if not set(caps) <= issuer_caps:
        _fail(403, 'Cannot grant capabilities the issuer lacks')
    secret = _secret()
    invite_id = str(uuid.uuid4())
    expires = (instant + timedelta(seconds=ttl)).isoformat()
    db.execute('INSERT INTO auth_invites VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL)',
               (invite_id, _digest(secret), project_id, issuer_id, label.strip(),
                json.dumps(caps, separators=(',', ':')), instant.isoformat(), expires))
    return {'id': invite_id, 'secret': secret, 'project_id': project_id, 'expires_at': expires}


def redeem_invite(db, secret, *, token_ttl_seconds=MAX_TOKEN_TTL, now=None):
    """One-time redemption; caller commits or rolls back invitation and token together."""
    digest = _valid_secret(secret)
    instant = _time(now)
    ttl = _ttl(token_ttl_seconds, MAX_TOKEN_TTL)
    row = db.execute('SELECT id,project_id,label,capabilities_json,expires_at,redeemed_at,revoked_at,issuer_id '
                     'FROM auth_invites WHERE secret_hash=?', (digest,)).fetchone()
    if not row or row[5] is not None or row[6] is not None or instant >= datetime.fromisoformat(row[4]):
        _fail(401, 'Invalid invitation')
    if not db.execute('SELECT 1 FROM projects WHERE id=? AND archived_at IS NULL', (row[1],)).fetchone():
        _fail(401, 'Invalid invitation')
    principal_id = str(uuid.uuid4())
    token = _secret()
    token_id = str(uuid.uuid4())
    expires = (instant + timedelta(seconds=ttl)).isoformat()
    db.execute('SAVEPOINT auth_redeem')
    try:
        changed = db.execute('UPDATE auth_invites SET redeemed_at=? '
                             'WHERE id=? AND redeemed_at IS NULL AND revoked_at IS NULL',
                             (instant.isoformat(), row[0]))
        if changed.rowcount != 1:
            _fail(401, 'Invalid invitation')
        issuer_caps = _member_caps(db, row[1], row[7])
        if 'manage_projects' not in issuer_caps or not set(json.loads(row[3])) <= issuer_caps:
            _fail(403, 'Invitation issuer no longer has grant authority')
        db.execute("INSERT INTO principals(id,label,identity_kind,created_at) "
                   "VALUES(?,?,'trusted_local_label',?)", (principal_id, row[2], instant.isoformat()))
        db.execute('UPDATE auth_invites SET redeemed_by=? WHERE id=?', (principal_id, row[0]))
        db.execute('INSERT INTO project_members(project_id,principal_id,capabilities_json) VALUES(?,?,?)',
                   (row[1], principal_id, row[3]))
        db.execute('INSERT INTO auth_tokens VALUES(?,?,?,?,?,?,?,NULL)',
                   (token_id, _digest(token), principal_id, row[1], 0, instant.isoformat(), expires))
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT auth_redeem')
        db.execute('RELEASE SAVEPOINT auth_redeem')
        raise
    db.execute('RELEASE SAVEPOINT auth_redeem')
    return {'id': token_id, 'token': token, 'principal_id': principal_id,
            'project_id': row[1], 'expires_at': expires}


def revoke_token(db, token_id, actor_id, *, now=None):
    row = db.execute('SELECT principal_id,project_id FROM auth_tokens WHERE id=?', (token_id,)).fetchone()
    if not row:
        _fail(404, 'Credential not found')
    if actor_id != row[0] and (row[1] is None or 'manage_projects' not in _member_caps(db, row[1], actor_id)):
        _fail(403, 'Credential administration capability required')
    return db.execute('UPDATE auth_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                      (_time(now).isoformat(), token_id)).rowcount == 1


def revoke_invite(db, invite_id, actor_id, *, now=None):
    row = db.execute('SELECT project_id FROM auth_invites WHERE id=?', (invite_id,)).fetchone()
    if not row:
        _fail(404, 'Invitation not found')
    if 'manage_projects' not in _member_caps(db, row[0], actor_id):
        _fail(403, 'Project administration capability required')
    return db.execute('UPDATE auth_invites SET revoked_at=? WHERE id=? '
                      'AND revoked_at IS NULL AND redeemed_at IS NULL',
                      (_time(now).isoformat(), invite_id)).rowcount == 1
