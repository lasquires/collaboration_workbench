import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from tools.workspace_git_transport import GitWorkspaceTransport, MergeConflict, TransportError


def git(*args, cwd=None):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def merge(base, local, remote):
    result = dict(remote)
    for key, value in local.items():
        if key in base and base[key] != value and key in remote and remote[key] != base[key] and remote[key] != value:
            raise MergeConflict({'key': key, 'local': value, 'remote': remote[key]})
        result[key] = value
    return result


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'remote.git'
        git('init', '--bare', str(self.remote))
        self.a = GitWorkspaceTransport(self.root / 'a-cache', str(self.remote))
        self.b = GitWorkspaceTransport(self.root / 'b-cache', str(self.remote))

    def test_concurrent_additions_and_stale_client(self):
        self.assertTrue(self.a.sync({'a': 1}, merge).pushed)
        self.assertEqual(self.b.fetch().payload, {'a': 1})
        self.assertTrue(self.b.sync({'a': 1, 'b': 2}, merge).pushed)
        result = self.a.sync({'a': 1, 'c': 3}, merge)
        self.assertEqual(result.payload, {'a': 1, 'b': 2, 'c': 3})
        self.assertEqual(self.b.fetch().payload, result.payload)

    def test_rejected_push_refetches_and_merges(self):
        self.a.sync({'base': 1}, merge)
        self.b.sync({'base': 1}, merge)
        original_run = self.a._run
        raced = False

        def run_with_race(*args, **kwargs):
            nonlocal raced
            if args and args[0] == 'push' and not raced:
                raced = True
                self.b.sync({'base': 1, 'b': 2}, merge)
            return original_run(*args, **kwargs)

        self.a._run = run_with_race
        result = self.a.sync({'base': 1, 'a': 3}, merge)
        self.assertTrue(raced)
        self.assertEqual(result.payload, {'base': 1, 'a': 3, 'b': 2})
        self.assertEqual(self.b.fetch().payload, result.payload)
    def test_conflict_preserves_remote_and_local(self):
        self.a.sync({'field': 'base'}, merge)
        self.b.sync({'field': 'base'}, merge)
        self.a.sync({'field': 'remote'}, merge)
        with self.assertRaises(MergeConflict) as ctx:
            self.b.sync({'field': 'local'}, merge)
        self.assertEqual(ctx.exception.alternatives, {'key': 'field', 'local': 'local', 'remote': 'remote'})
        self.assertEqual(self.a.fetch().payload, {'field': 'remote'})
        self.assertEqual(self.b.accepted().payload, {'field': 'base'})

    def test_noop_does_not_commit(self):
        first = self.a.sync({'x': 1}, merge)
        second = self.a.sync({'x': 1}, merge)
        self.assertFalse(second.pushed)
        self.assertEqual(second.tip, first.tip)

    def test_source_clone_branch_and_stage_untouched(self):
        clone = self.root / 'source'
        git('clone', str(self.remote), str(clone))
        git('config', 'user.name', 'Tester', cwd=clone)
        git('config', 'user.email', 'test@example.invalid', cwd=clone)
        (clone / 'file.txt').write_text('staged')
        git('add', 'file.txt', cwd=clone)
        before = (git('symbolic-ref', 'HEAD', cwd=clone), git('status', '--porcelain', cwd=clone))
        self.a.sync({'data': True}, merge)
        after = (git('symbolic-ref', 'HEAD', cwd=clone), git('status', '--porcelain', cwd=clone))
        self.assertEqual(after, before)

    def test_remote_deletion_does_not_replace_accepted_history(self):
        self.a.sync({'old': 1}, merge)
        git('--git-dir', str(self.remote), 'update-ref', '-d', 'refs/heads/workbench-data')
        with self.assertRaises(Exception):
            self.a.sync({'old': 1, 'new': 2}, merge)
        self.assertIsNone(self.a.fetch().tip)

    def test_cache_binding_survives_restart(self):
        first = self.a.sync({'x': 1}, merge)
        reopened = GitWorkspaceTransport(self.root / 'a-cache', str(self.remote))
        self.assertEqual(reopened.accepted().tip, first.tip)
        with self.assertRaises(Exception):
            GitWorkspaceTransport(self.root / 'a-cache', str(self.root / 'other.git'))
    def test_git_command_output_is_file_backed_and_bounded(self):
        client = GitWorkspaceTransport(self.root / 'bounded', str(self.remote),
                                       max_payload_bytes=20, max_object_bytes=20)

        def noisy_run(command, **kwargs):
            self.assertNotIn('capture_output', kwargs)
            kwargs['stdout'].write(b'x' * 2048)
            kwargs['stderr'].write(b'secret-url' * 250)
            return SimpleNamespace(returncode=0)

        with patch('tools.workspace_git_transport.subprocess.run', side_effect=noisy_run):
            with self.assertRaisesRegex(TransportError, 'output exceeds'):
                client._run('status')
    def test_external_helper_remote_is_rejected_before_git(self):
        for remote in ('ext::arbitrary-command','custom::address'):
            with self.subTest(remote=remote),patch('tools.workspace_git_transport.subprocess.run') as run:
                with self.assertRaises(ValueError):GitWorkspaceTransport(self.root/'unsafe',remote)
                run.assert_not_called()

    def test_large_payload_uses_chunk_manifest_and_round_trips(self):
        client = GitWorkspaceTransport(self.root / 'large', str(self.remote),
                                       max_payload_bytes=20 * 1024 * 1024)
        payload = {'text': '🙂' * (3 * 1024 * 1024)}
        result = client.sync(payload, merge)
        self.assertTrue(result.pushed)
        self.assertEqual(client.fetch().payload, payload)
        raw = git('--git-dir', str(self.remote), 'show', f'{result.tip}:workspace.json')
        manifest = json.loads(raw)
        self.assertEqual(manifest['format'], 'workbench-chunked-json')
        self.assertEqual(len(manifest['chunks']), 4)

    def test_legacy_payload_remains_workspace_json(self):
        result = self.a.sync({'small': 'legacy'}, merge)
        raw = git('--git-dir', str(self.remote), 'show', f'{result.tip}:workspace.json')
        self.assertEqual(json.loads(raw), {'small': 'legacy'})
        self.assertEqual(self.a.fetch().payload, {'small': 'legacy'})

    def test_explicit_object_limit_does_not_limit_aggregate_payload(self):
        client = GitWorkspaceTransport(self.root / 'object-bound', str(self.remote),
                                       max_payload_bytes=20 * 1024 * 1024,
                                       max_object_bytes=1024*1024)
        payload = {'text': '🙂' * (3 * 1024 * 1024)}
        self.assertEqual(client.sync(payload, merge).payload, payload)
        self.assertEqual(client.fetch().payload,payload)

    def test_impossible_tiny_object_limit_rejects_before_publishing(self):
        client=GitWorkspaceTransport(self.root/'tiny-object',str(self.remote),max_object_bytes=20)
        with self.assertRaises(TransportError):client.sync({'text':'x'*100},merge)
        self.assertIsNone(client.accepted().tip)
        self.assertEqual(git('ls-remote',str(self.remote)),'')

    def test_rejects_source_branches_and_oversize(self):
        with self.assertRaises(ValueError):
            GitWorkspaceTransport(self.root / 'bad', str(self.remote), data_ref='refs/heads/main')
        small = GitWorkspaceTransport(self.root / 'small', str(self.remote), max_payload_bytes=20)
        with self.assertRaises(ValueError):
            small.sync({'large': 'x' * 100}, merge)


if __name__ == '__main__':
    unittest.main()
