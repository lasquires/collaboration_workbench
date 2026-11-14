"""Service-owned, opt-in Git synchronization of portable exchange bundles."""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    from . import roadmap_exchange as exchange
    from .roadmap_store import StoreError
    from .workspace_git_transport import GitWorkspaceTransport, TransportError
except ImportError:
    import roadmap_exchange as exchange
    from roadmap_store import StoreError
    from workspace_git_transport import GitWorkspaceTransport, TransportError


class PrivacyBlocked(RuntimeError):
    pass


class SyncConflict(RuntimeError):
    def __init__(self, alternatives):
        self.alternatives = alternatives
        super().__init__('Workspace exchange requires explicit review')


class _RemoteAdvanced(Exception):
    pass


@dataclass(frozen=True)
class SyncResult:
    tip: str | None
    bundle_id: str
    pushed: bool
    pending_local: bool
    missing_sources: tuple[str, ...] = ()


class WorkspaceGitSync:
    def __init__(self, store, project_id, actor_id, remote, cache_dir, *,
                 export_selected=False, data_ref='refs/heads/workbench-data',
                 quiet_seconds=30, interval_seconds=300,
                 max_rounds=3):
        if quiet_seconds < 0 or interval_seconds < 180 or max_rounds < 1:
            raise ValueError('Invalid sync schedule bounds')
        self.store, self.project_id, self.actor_id = store, project_id, actor_id
        self.export_selected = export_selected
        self.quiet_seconds, self.interval_seconds = quiet_seconds, interval_seconds
        self.max_rounds = max_rounds
        self.transport = GitWorkspaceTransport(cache_dir, remote, data_ref=data_ref)
        self.state_path = Path(cache_dir) / 'sync-state.json'

    def _state(self):
        if not self.state_path.exists():
            return {'applied_tip': None, 'last_sync': 0, 'dirty_since': None}
        try:
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
            if not isinstance(state, dict) or set(state) != {'applied_tip', 'last_sync', 'dirty_since'}:
                raise ValueError()
            return state
        except (OSError, ValueError):
            raise TransportError('Local sync state is invalid') from None

    def _save_state(self, state):
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.state_path.parent, prefix='sync-', delete=False) as f:
                name = f.name
                f.write(json.dumps(state, sort_keys=True, separators=(',', ':')).encode())
                f.flush(); os.fsync(f.fileno())
            os.replace(name, self.state_path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def status(self):
        """Local schedule/checkpoint only; never expose the project remote URL."""
        state = self._state()
        return {'last_sync': state['last_sync'], 'dirty_since': state['dirty_since'],
                'accepted_tip': self.transport.accepted().tip,
                'applied_tip': state['applied_tip']}
    def note_dirty(self, now=None):
        now = time.time() if now is None else now
        state = self._state()
        state['dirty_since'] = now
        self._save_state(state)

    def due(self, now=None):
        now = time.time() if now is None else now
        state = self._state()
        return (state['dirty_since'] is not None and
                now - state['dirty_since'] >= self.quiet_seconds and
                now - state['last_sync'] >= self.interval_seconds)

    def sync_if_due(self, now=None):
        if not self.due(now):
            return None
        return self.sync_now(now=now)

    def _safe(self, bundle):
        if not self.export_selected:
            raise PrivacyBlocked('Explicit portable export selection is required')
        if bundle.get('format') != 'workbench-exchange' or bundle.get('version') != 2:
            raise PrivacyBlocked('Git network sync requires portable exchange version 2')
        records = bundle['records']
        if records['artifact_blobs'] or any(
                row.get('path') != 'workspace-root:' + row.get('id', '')
                for row in records['artifact_roots']):
            raise PrivacyBlocked('Portable bundle contains raw artifact bytes or host roots')
    def _export(self, ancestor=None):
        with self.store._connection() as db:
            db.execute('BEGIN')
            try:bundle = exchange.export_bundle(db, self.project_id, ancestor, portable=True)
            finally:db.rollback()
        self._safe(bundle)
        return bundle

    @staticmethod
    def _portable(bundle, *sources):
        # Exchange import audit entries stay local. Preserve remote historical
        # events even though an import stores them in snapshots, not event rows.
        events = {}
        for source in (*sources, bundle):
            if source:
                for event in source['history']:
                    if event['kind'] == 'import_bundle':
                        continue
                    old = events.setdefault(event['id'], event)
                    if old != event:
                        raise SyncConflict({'event_id': event['id'], 'local': old, 'remote': event})
        bundle = dict(bundle)
        records = {table: {exchange._key(bundle['schema'], table, row): row for row in rows}
                   for table, rows in bundle['records'].items()}
        for source in sources:
            if source:
                for table, rows in source['records'].items():
                    for row in rows:
                        records[table].setdefault(exchange._key(bundle['schema'], table, row), row)
        bundle['records'] = {table: [rows[key] for key in sorted(rows)]
                             for table, rows in records.items()}
        bundle['history'] = sorted(events.values(), key=lambda e: e['id'])
        bundle['id'] = exchange.research.digest({k: v for k, v in bundle.items() if k != 'id'})
        return bundle

    def _transact(self, operation, expected_revision=None):
        for _ in range(1 if expected_revision else 4):
            revision = expected_revision or self.store.exchange_preview(self.project_id)['revision']
            payload = {'project_id': self.project_id, 'actor': {'principal_id': self.actor_id},
                       'request_id': 'git-sync-' + str(uuid.uuid5(uuid.NAMESPACE_URL, operation['op'] + ':' + operation['bundle']['id'] + ':' + revision)),
                       'expected_revision': revision, 'operations': [operation]}
            try:
                return self.store.transact_v1(payload)
            except StoreError as exc:
                if expected_revision or exc.status != 409 or 'revision changed' not in str(exc).lower():
                    raise
        raise TransportError('Service revision advanced too often during sync')

    def _register(self, bundle):
        with self.store._connection() as db:
            found = db.execute('SELECT 1 FROM exchange_snapshots WHERE project_id=? AND id=?',
                               (self.project_id, bundle['id'])).fetchone()
        if not found:
            self._transact({'op': 'register_exchange_snapshot', 'bundle': bundle})

    def _import(self, bundle, ancestor_bundle=None, choices=None, reviewed_revision=None):
        self._safe(bundle)
        with self.store._connection() as db:
            imported = db.execute('SELECT 1 FROM exchange_imports WHERE project_id=? AND id=?',
                                  (self.project_id, bundle['id'])).fetchone()
        if imported:
            return []
        if ancestor_bundle and bundle['ancestor_id'] == ancestor_bundle['id']:
            self._register(ancestor_bundle)
        try:
            envelope = self.store.exchange_preview(self.project_id, bundle=bundle)
            preview = envelope['preview']
            if reviewed_revision is not None and envelope['revision'] != reviewed_revision:
                raise SyncConflict({'reason': 'Local service revision changed since preview'})
            conflicts = [c for c in preview['changes'] if any(f['conflict'] for f in c['fields'].values())]
            if conflicts and choices is None:
                raise SyncConflict({'changes': conflicts, 'missing_sources': preview['missing_sources']})
            self._transact({'op': 'import_bundle', 'bundle': bundle, 'choices': choices or {}},
                           expected_revision=envelope['revision'])
            return preview['missing_sources']
        except StoreError as exc:
            if exc.status == 409:
                raise SyncConflict({'service_error': str(exc)}) from None
            raise

    def preview_remote(self):
        """Return the exact remote tip and reviewed service preview for UI choices."""
        remote = self.transport.fetch()
        if not remote.tip:
            raise TransportError('No remote workspace snapshot to review')
        self._safe(remote.payload)
        accepted = self.transport.accepted()
        if accepted.tip and remote.payload['ancestor_id'] == accepted.payload['id']:
            self._register(accepted.payload)
        envelope = self.store.exchange_preview(self.project_id, bundle=remote.payload)
        return {'tip': remote.tip, 'bundle_id': remote.payload['id'],
                'local_revision': envelope['revision'], 'preview': envelope['preview'],
                'bundle': remote.payload}

    def apply_remote(self, choices, remote_tip, bundle_id, expected_revision):
        """Apply reviewed choices only for the exact previewed remote and local state."""
        if not isinstance(choices, dict):
            raise ValueError('Reviewed field choices must be an object')
        remote = self.transport.fetch()
        if remote.tip != remote_tip or not remote.tip or remote.payload['id'] != bundle_id:
            raise SyncConflict({'reason': 'Remote tip changed since preview'})
        self._safe(remote.payload)
        accepted = self.transport.accepted()
        ancestor = accepted.payload if accepted.tip else None
        missing = self._import(remote.payload, ancestor, choices,
                               reviewed_revision=expected_revision)
        # A later remote push cannot change which snapshot was applied locally.
        self.transport._save_accepted(remote)
        state = self._state()
        state['applied_tip'] = remote.tip
        state['dirty_since'] = time.time()
        self._save_state(state)
        return SyncResult(remote.tip, bundle_id, False, True, tuple(missing))

    def bootstrap_remote(self, choices):
        """Explicitly accept an existing v2 remote for an unbound installation."""
        if self.transport.accepted().tip or self._state()['applied_tip']:
            raise TransportError('This installation already has a Git sync base')
        review = self.preview_remote()
        return self.apply_remote(choices, review['tip'], review['bundle_id'],
                                 review['local_revision'])
    def sync_now(self, *, now=None):
        now = time.time() if now is None else now
        state = self._state()
        missing_sources = set()
        accepted = self.transport.accepted()
        if state['applied_tip'] != accepted.tip and accepted.tip:
            # A push may have succeeded before the previous DB apply. Recover it
            # before another export or acknowledgement.
            missing_sources.update(self._import(accepted.payload))
            state['applied_tip'] = accepted.tip
            self._save_state(state)
        for _ in range(self.max_rounds):
            accepted = self.transport.accepted()
            remote = self.transport.fetch()
            ancestor = accepted.payload if accepted.tip else None
            if remote.tip != accepted.tip and remote.tip:
                local_before = self._portable(self._export())
                if ancestor is None and remote.payload['id'] != local_before['id']:
                    raise SyncConflict({'reason': 'Unknown initial Git ancestor; explicit bootstrap required',
                                        'local': local_before['id'], 'remote': remote.payload['id']})
                if remote.payload['id'] != local_before['id']:
                    missing_sources.update(self._import(remote.payload, ancestor))
            basis = remote.payload if remote.tip else ancestor
            if basis:
                self._register(basis)
            local = self._portable(self._export(basis['id'] if basis else None), basis)
            # Ancestor metadata does not itself create new research data.
            if basis and local['records'] == basis['records'] and local['history'] == basis['history']:
                desired = basis
            else:
                desired = local

            def stop_on_advance(base, ours, theirs):
                if ours == theirs:
                    return theirs
                if all(all(row in ours['records'][table] for row in rows)
                       for table, rows in theirs['records'].items()) and all(
                       event in ours['history'] for event in theirs['history']):
                    return ours
                raise _RemoteAdvanced()

            try:
                result = self.transport.sync(desired, stop_on_advance)
            except _RemoteAdvanced:
                continue
            # The DB may have changed during Git I/O. Import is CAS checked and
            # retains compatible local edits; the checkpoint records only apply.
            if result.payload['id'] != basis['id'] if basis else True:
                missing_sources.update(self._import(result.payload, basis))
            current = self._portable(self._export(), result.payload)
            pending = current['records'] != result.payload['records'] or current['history'] != result.payload['history']
            state = {'applied_tip': result.tip, 'last_sync': now,
                     'dirty_since': now if pending else None}
            self._save_state(state)
            return SyncResult(result.tip, result.payload['id'], result.pushed, pending,
                              tuple(sorted(missing_sources)))
        raise TransportError('Remote advanced too often; retry synchronization later')
