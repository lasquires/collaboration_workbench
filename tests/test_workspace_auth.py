"""Isolated credential tests with SQLite foreign keys enabled."""
import sqlite3
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from tools import workspace_auth as auth
from tools.roadmap_store import StoreError


NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY,archived_at TEXT)')
        self.db.execute("CREATE TABLE principals(id TEXT PRIMARY KEY,label TEXT NOT NULL,"
                        "identity_kind TEXT NOT NULL CHECK(identity_kind='trusted_local_label'),"
                        "created_at TEXT,archived_at TEXT)")
        self.db.execute('CREATE TABLE project_members(project_id TEXT NOT NULL REFERENCES projects(id),'
                        'principal_id TEXT NOT NULL REFERENCES principals(id),capabilities_json TEXT NOT NULL,'
                        'PRIMARY KEY(project_id,principal_id))')
        self.project = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.owner = str(uuid.uuid4())
        self.manager = str(uuid.uuid4())
        for project in (self.project, self.other):
            self.db.execute('INSERT INTO projects(id) VALUES(?)', (project,))
        for principal in (self.owner, self.manager):
            self.db.execute("INSERT INTO principals(id,label,identity_kind) VALUES(?,'local','trusted_local_label')",
                            (principal,))
        self.db.execute('INSERT INTO project_members VALUES(?,?,?)',
                        (self.project, self.owner,
                         '["contribute","review","manage_claims","manage_projects"]'))
        self.db.execute('INSERT INTO project_members VALUES(?,?,?)',
                        (self.project, self.manager, '["contribute","manage_projects"]'))
        auth.create_schema(self.db)

    def denied(self, status, action):
        with self.assertRaises(StoreError) as caught:
            action()
        self.assertEqual(caught.exception.status, status)

    def test_owner_bootstrap_and_no_label_auth(self):
        self.denied(401, lambda: auth.authenticate(self.db, ''))
        self.denied(401, lambda: auth.authenticate(self.db, self.owner))
        issued = auth.issue_owner_token(self.db, self.owner, ttl_seconds=60, now=NOW)
        context = auth.authenticate(self.db, issued['token'], now=NOW)
        self.assertEqual(context['principal_id'], self.owner)
        self.assertTrue(context['is_owner'])
        self.assertIsNone(context['project_id'])
        self.denied(401, lambda: auth.authenticate(self.db, issued['token'], now=NOW + timedelta(seconds=60)))

    def test_invite_scope_capabilities_and_one_time_redeem(self):
        self.denied(403, lambda: auth.issue_invite(self.db, self.other, self.owner, 'worker', now=NOW))
        self.denied(403, lambda: auth.issue_invite(self.db, self.project, self.manager, 'worker',
                                                  ['review'], now=NOW))
        self.denied(400, lambda: auth.issue_invite(self.db, self.project, self.owner, 'worker',
                                                  ['approve_guidance'], now=NOW))
        invite = auth.issue_invite(self.db, self.project, self.owner, 'worker', now=NOW)
        redeemed = auth.redeem_invite(self.db, invite['secret'], now=NOW)
        context = auth.authenticate(self.db, redeemed['token'], now=NOW)
        self.assertEqual(context['project_id'], self.project)
        self.assertEqual(context['capabilities'], ['contribute'])
        self.assertFalse(context['is_owner'])
        self.assertNotEqual(redeemed['principal_id'], self.owner)
        self.denied(401, lambda: auth.redeem_invite(self.db, invite['secret'], now=NOW))
        self.assertEqual(self.db.execute('SELECT count(*) FROM principals').fetchone()[0], 3)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_expiry_and_revocation(self):
        invite = auth.issue_invite(self.db, self.project, self.owner, 'short', ttl_seconds=1, now=NOW)
        self.denied(401, lambda: auth.redeem_invite(self.db, invite['secret'], now=NOW + timedelta(seconds=1)))
        pending = auth.issue_invite(self.db, self.project, self.owner, 'revoke', now=NOW)
        self.denied(403, lambda: auth.revoke_invite(self.db, pending['id'], str(uuid.uuid4()), now=NOW))
        self.assertTrue(auth.revoke_invite(self.db, pending['id'], self.owner, now=NOW))
        self.denied(401, lambda: auth.redeem_invite(self.db, pending['secret'], now=NOW))
        good = auth.issue_invite(self.db, self.project, self.owner, 'member', now=NOW)
        token = auth.redeem_invite(self.db, good['secret'], token_ttl_seconds=2, now=NOW)
        self.denied(403, lambda: auth.revoke_token(self.db, token['id'], str(uuid.uuid4()), now=NOW))
        self.assertTrue(auth.revoke_token(self.db, token['id'], token['principal_id'], now=NOW))
        self.denied(401, lambda: auth.authenticate(self.db, token['token'], now=NOW))
        later = auth.issue_invite(self.db, self.project, self.owner, 'expiring', now=NOW)
        expiring = auth.redeem_invite(self.db, later['secret'], token_ttl_seconds=2, now=NOW)
        self.denied(401, lambda: auth.authenticate(self.db, expiring['token'], now=NOW + timedelta(seconds=2)))

    def test_hash_only_storage_and_caller_rollback(self):
        owner = auth.issue_owner_token(self.db, self.owner, now=NOW)
        invite = auth.issue_invite(self.db, self.project, self.owner, 'member', now=NOW)
        redeemed = auth.redeem_invite(self.db, invite['secret'], now=NOW)
        snapshot = '\n'.join(self.db.iterdump())
        for secret in (owner['token'], invite['secret'], redeemed['token']):
            self.assertNotIn(secret, snapshot)
        self.db.commit()
        self.db.execute('BEGIN')
        rolled = auth.issue_invite(self.db, self.project, self.owner, 'rolled back', now=NOW)
        self.db.rollback()
        self.denied(401, lambda: auth.redeem_invite(self.db, rolled['secret'], now=NOW))

    def test_lifetime_bounds_and_membership_revocation(self):
        self.denied(400, lambda: auth.issue_invite(self.db, self.project, self.owner, 'bad', ttl_seconds=0))
        self.denied(400, lambda: auth.issue_owner_token(self.db, self.owner, ttl_seconds=auth.MAX_TOKEN_TTL + 1))
        invite = auth.issue_invite(self.db, self.project, self.owner, 'member', now=NOW)
        token = auth.redeem_invite(self.db, invite['secret'], now=NOW)
        self.db.execute('DELETE FROM project_members WHERE project_id=? AND principal_id=?',
                        (self.project, token['principal_id']))
        self.denied(401, lambda: auth.authenticate(self.db, token['token'], now=NOW))

    def test_redemption_failure_rolls_back_its_own_partial_writes(self):
        invite = auth.issue_invite(self.db, self.project, self.owner, 'retry', now=NOW)
        self.db.execute("CREATE TRIGGER reject_token BEFORE INSERT ON auth_tokens "
                        "BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            auth.redeem_invite(self.db, invite['secret'], now=NOW)
        self.assertEqual(self.db.execute('SELECT count(*) FROM principals').fetchone()[0], 2)
        self.assertIsNone(self.db.execute('SELECT redeemed_at FROM auth_invites WHERE id=?',
                                          (invite['id'],)).fetchone()[0])
        self.db.execute('DROP TRIGGER reject_token')
        self.assertEqual(auth.redeem_invite(self.db, invite['secret'], now=NOW)['project_id'], self.project)

    def test_redemption_rechecks_issuer_authority_without_consuming_invite(self):
        invite = auth.issue_invite(self.db, self.project, self.owner, 'reviewer',
                                   ['contribute', 'review'], now=NOW)
        self.db.execute('UPDATE project_members SET capabilities_json=? '
                        'WHERE project_id=? AND principal_id=?',
                        ('["contribute","manage_projects"]', self.project, self.owner))
        self.denied(403, lambda: auth.redeem_invite(self.db, invite['secret'], now=NOW))
        self.assertIsNone(self.db.execute('SELECT redeemed_at FROM auth_invites WHERE id=?',
                                          (invite['id'],)).fetchone()[0])
        self.assertEqual(self.db.execute('SELECT count(*) FROM principals').fetchone()[0], 2)
        self.db.execute('UPDATE project_members SET capabilities_json=? '
                        'WHERE project_id=? AND principal_id=?',
                        ('["contribute","review","manage_projects"]', self.project, self.owner))
        self.db.execute('DELETE FROM project_members WHERE project_id=? AND principal_id=?',
                        (self.project, self.owner))
        self.denied(403, lambda: auth.redeem_invite(self.db, invite['secret'], now=NOW))
        self.assertIsNone(self.db.execute('SELECT redeemed_at FROM auth_invites WHERE id=?',
                                          (invite['id'],)).fetchone()[0])
        self.assertEqual(self.db.execute('SELECT count(*) FROM principals').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
