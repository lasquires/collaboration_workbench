"""Bounded Git transport for one project's portable workspace JSON.

This module owns a bare cache only. Domain merging and SQLite transactions belong to
its caller; a merge callback must return a reconciled dict or raise.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tools import workspace_bundle_chunks as chunk_codec

DEFAULT_DATA_REF = 'refs/heads/workbench-data'
PAYLOAD_PATH = 'workspace.json'
LEGACY_MAX_BYTES = 8 * 1024 * 1024
CHUNK_DIR = 'chunks'
_EMPTY = '0' * 40


class TransportError(RuntimeError):
    pass


class MergeConflict(TransportError):
    def __init__(self, alternatives):
        self.alternatives = alternatives
        super().__init__('Workspace changes require explicit reconciliation')


@dataclass(frozen=True)
class Snapshot:
    tip: str | None
    payload: dict
    pushed: bool = False


class GitWorkspaceTransport:
    def __init__(self, cache_dir, remote, *, data_ref=DEFAULT_DATA_REF,
                 max_payload_bytes=chunk_codec.DEFAULT_MAX_BYTES, max_object_bytes=9 * 1024 * 1024,
                 command_timeout=30, max_retries=3):
        if not isinstance(remote, str) or not remote.strip() or remote.startswith('-') or '\n' in remote or '\r' in remote:
            raise ValueError('An explicit project Git remote is required')
        scheme=re.match(r'^([A-Za-z][A-Za-z0-9+.-]*)://',remote)
        if (remote!=remote.strip() or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*::',remote)
                or scheme and scheme.group(1).lower() not in ('http','https','ssh','git','file')):
            raise ValueError('External Git helper remotes are not supported')
        if (not isinstance(data_ref, str) or
                not re.fullmatch(r'refs/heads/workbench-data(?:/[A-Za-z0-9._/-]+)?', data_ref) or
                '..' in data_ref or data_ref.endswith('/') or '/.' in data_ref):
            raise ValueError('Data ref must be in the workbench-data namespace')
        if max_payload_bytes < 2 or max_object_bytes < 2 or command_timeout <= 0 or max_retries < 1:
            raise ValueError('Invalid transport bounds')
        self.cache_dir = Path(cache_dir).resolve()
        self.remote = remote
        self.data_ref = data_ref
        self.max_payload_bytes = max_payload_bytes
        self.max_object_bytes = max_object_bytes
        self.command_timeout = command_timeout
        self.max_retries = max_retries
        self.binding = hashlib.sha256((remote + '\0' + data_ref).encode()).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not (self.cache_dir / 'HEAD').exists():
            if any(self.cache_dir.iterdir()):
                raise TransportError('Cache directory is not an empty Git repository')
            self._run('init', '--bare', str(self.cache_dir), git_dir=False)
        if not (self.cache_dir / 'objects').is_dir():
            raise TransportError('Cache directory is not a bare Git repository')
        self.accepted()  # Check binding before network or writes.

    def _run(self, *args, input_bytes=None, git_dir=True, allow_failure=False):
        command = ['git'] + (['--git-dir', str(self.cache_dir)] if git_dir else []) + list(args)
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never',
                   GIT_AUTHOR_NAME='Workspace Transport', GIT_AUTHOR_EMAIL='transport@localhost',
                   GIT_COMMITTER_NAME='Workspace Transport', GIT_COMMITTER_EMAIL='transport@localhost')
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                stdin_options = {'stdin': subprocess.DEVNULL} if input_bytes is None else {'input': input_bytes}
                result = subprocess.run(command, **stdin_options, stdout=stdout, stderr=stderr,
                                        timeout=self.command_timeout, env=env)
                if stdout.tell() > self.max_object_bytes + 1024 or stderr.tell() > self.max_object_bytes + 1024:
                    raise TransportError('Git output exceeds object bound')
                if result.returncode and not allow_failure:
                    # Git stderr may contain an authenticated URL; never surface it.
                    raise TransportError('Git operation failed')
                stdout.seek(0)
                result.stdout = stdout.read()
                return result
        except (subprocess.TimeoutExpired, OSError):
            raise TransportError('Git operation timed out or could not start') from None

    def _canonical(self, payload):
        return chunk_codec.canonical(payload, self.max_payload_bytes)

    def accepted(self):
        path = self.cache_dir / 'accepted.json'
        if not path.exists():
            return Snapshot(None, {})
        try:
            if path.stat().st_size > self.max_payload_bytes + 4096:
                raise ValueError()
            state = json.loads(path.read_text(encoding='utf-8'))
            if state['binding'] != self.binding:
                raise TransportError('Cache belongs to another project remote or data ref')
            self._canonical(state['payload'])
            tip = state['tip']
            if tip is not None and not re.fullmatch('[a-f0-9]{40,64}', tip):
                raise ValueError()
            return Snapshot(tip, state['payload'])
        except TransportError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise TransportError('Accepted cache state is invalid') from None

    def _save_accepted(self, snapshot):
        raw = json.dumps({'binding': self.binding, 'tip': snapshot.tip,
                          'payload': snapshot.payload}, sort_keys=True,
                         separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache_dir, prefix='accepted-',
                                             delete=False) as file:
                name = file.name
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.cache_dir / 'accepted.json')
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def fetch(self):
        listing = self._run('ls-remote', '--refs', self.remote, self.data_ref).stdout.decode('ascii', 'strict').strip()
        if not listing:
            return Snapshot(None, {})
        lines = listing.splitlines()
        if len(lines) != 1 or not lines[0].endswith('\t' + self.data_ref):
            raise TransportError('Unexpected remote data ref')
        self._run('fetch', '--no-tags', self.remote, self.data_ref)
        tip = self._run('rev-parse', 'FETCH_HEAD').stdout.decode('ascii').strip()
        tree = self._run('ls-tree', '-r', tip).stdout.decode('ascii', 'strict').splitlines()
        entries = {}
        for line in tree:
            match = re.fullmatch(r'(100644) blob ([a-f0-9]{40,64})\t(.+)', line)
            if not match or match.group(3) in entries:
                raise TransportError('Remote workspace tree has unexpected type')
            entries[match.group(3)] = (match.group(1), match.group(2))
        payload_entry = entries.get(PAYLOAD_PATH)
        if not payload_entry or any(path != PAYLOAD_PATH and not re.fullmatch(r'chunks/[a-f0-9]{64}', path) for path in entries):
            raise TransportError('Remote workspace tree has unexpected paths')
        mode, blob = payload_entry
        size = int(self._run('cat-file', '-s', blob).stdout)
        if size > min(self.max_object_bytes, self.max_payload_bytes + 4096):
            raise TransportError('Remote workspace payload exceeds object bound')
        raw = self._run('cat-file', 'blob', blob).stdout
        if len(raw) != size:
            raise TransportError('Remote workspace payload is truncated')
        try:
            header = json.loads(raw)
            if isinstance(header, dict) and header.get('format') == chunk_codec.FORMAT:
                chunk_codec.validate_manifest(header, max_total_bytes=self.max_payload_bytes)
                expected = {f'chunks/{ref["sha256"]}' for ref in header['chunks']}
                if set(entries) != {PAYLOAD_PATH} | expected:
                    raise ValueError('Unexpected or missing chunk paths')
                blobs = {}
                for ref in header['chunks']:
                    path = f'chunks/{ref["sha256"]}'
                    chunk_blob = entries[path][1]
                    chunk_size = int(self._run('cat-file', '-s', chunk_blob).stdout)
                    if chunk_size != ref['byte_length'] or chunk_size > min(chunk_codec.DEFAULT_CHUNK_BYTES,self.max_object_bytes):
                        raise ValueError('Chunk object size is invalid')
                    data = self._run('cat-file', 'blob', chunk_blob).stdout
                    if len(data) != chunk_size:
                        raise ValueError('Chunk object is truncated')
                    blobs[ref['sha256']] = data
                payload = chunk_codec.unpack(header, blobs.__getitem__, max_total_bytes=self.max_payload_bytes)
            else:
                if size > min(LEGACY_MAX_BYTES, self.max_object_bytes):
                    raise ValueError('Legacy payload exceeds bound')
                payload = json.loads(raw)
                self._canonical(payload)
                if set(entries) != {PAYLOAD_PATH}:
                    raise ValueError('Legacy tree has unexpected paths')
        except (ValueError, UnicodeDecodeError, TypeError, KeyError):
            raise TransportError('Remote workspace payload is invalid') from None
        return Snapshot(tip, payload)

    read = fetch

    def _commit(self, payload, parent):
        raw = self._canonical(payload)
        paths = [(PAYLOAD_PATH, raw)]
        if len(raw) > min(LEGACY_MAX_BYTES, self.max_object_bytes):
            manifest, chunks = chunk_codec.pack(payload, chunk_bytes=min(chunk_codec.DEFAULT_CHUNK_BYTES,self.max_object_bytes), max_total_bytes=self.max_payload_bytes)
            manifest_raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('utf-8')
            paths = [(PAYLOAD_PATH, manifest_raw)]
            paths.extend((f'{CHUNK_DIR}/{digest}', data) for digest, data in chunks.items())
        if any(len(data)>self.max_object_bytes for _,data in paths):
            raise TransportError('Workspace object exceeds configured byte bound')
        root_entries = []
        chunk_entries = []
        for path, data in paths:
            blob = self._run('hash-object', '-w', '--stdin', input_bytes=data).stdout.decode('ascii').strip()
            if path.startswith(CHUNK_DIR + '/'):
                chunk_entries.append((path.split('/', 1)[1], blob))
            else:
                root_entries.append((path, blob))
        if chunk_entries:
            chunk_input = ''.join(f'100644 blob {blob}\t{name}\n' for name, blob in chunk_entries).encode('ascii')
            chunk_tree = self._run('mktree', input_bytes=chunk_input).stdout.decode('ascii').strip()
            root_entries.append((CHUNK_DIR, chunk_tree))
        tree_input = ''.join(f'{"040000 tree" if name == CHUNK_DIR else "100644 blob"} {blob}\t{name}\n' for name, blob in root_entries).encode('ascii')
        tree = self._run('mktree', input_bytes=tree_input).stdout.decode('ascii').strip()
        args = ['commit-tree', tree]
        if parent:
            args += ['-p', parent]
        args += ['-m', 'Update workspace data']
        return self._run(*args).stdout.decode('ascii').strip()

    def sync(self, local_payload: dict, merge_callback: Callable[[dict, dict, dict], dict]) -> Snapshot:
        self._canonical(local_payload)
        accepted = self.accepted()
        desired = local_payload
        base = accepted
        for _ in range(self.max_retries):
            remote = self.fetch()
            if accepted.tip and (not remote.tip or self._run('merge-base', '--is-ancestor', accepted.tip, remote.tip, allow_failure=True).returncode):
                raise TransportError('Remote data history was deleted or rewritten')
            if remote.tip != base.tip:
                desired = merge_callback(base.payload, desired, remote.payload)
                self._canonical(desired)
            if self._canonical(desired) == self._canonical(remote.payload):
                result = Snapshot(remote.tip, remote.payload, False)
                self._save_accepted(result)
                return result
            commit = self._commit(desired, remote.tip)
            push = self._run('push', self.remote, f'{commit}:{self.data_ref}', allow_failure=True)
            if push.returncode == 0:
                result = Snapshot(commit, desired, True)
                self._save_accepted(result)
                return result
            newer = self.fetch()
            if newer.tip == remote.tip:
                raise TransportError('Git push failed without remote advancement')
            base = remote
        raise TransportError('Remote advanced too often; retry synchronization later')
