"""Lifecycle and option checks for the optional paired listener."""
import contextlib
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import roadmap_server as server
from roadmap_store import RoadmapStore


class PairedHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'workspace.sqlite3'
        self.store = RoadmapStore(self.db)
        self.old_path = server.STORE_PATH
        self.addCleanup(lambda: setattr(server, 'STORE_PATH', self.old_path))

    def _run_main(self, args, server_factory=None):
        """Run main through its normal teardown with synthetic workers."""
        instances = []
        real_server = server.ThreadingHTTPServer

        def make_server(address, handler):
            if server_factory:
                instance = server_factory(address, handler, len(instances))
            else:
                instance = real_server(address, handler)
            if instance is not None:
                if len(instances) == 0:
                    instance.serve_forever = mock.Mock(side_effect=KeyboardInterrupt)
                instances.append(instance)
            return instance

        worker_calls = []

        def worker(path):
            worker_calls.append(Path(path))
            return mock.Mock(), mock.Mock()

        with mock.patch.object(server, 'service_lock', return_value=contextlib.nullcontext()), \
             mock.patch.object(server, 'checkpoint_before_upgrade', return_value=None), \
             mock.patch.object(server, '_store', return_value=self.store), \
             mock.patch.object(server, 'startup_security', return_value=None), \
             mock.patch.object(server, 'start_matcher_worker', side_effect=worker), \
             mock.patch.object(server, 'start_repository_worker', side_effect=worker), \
             mock.patch.object(server, 'start_git_sync_worker', side_effect=worker), \
             mock.patch.object(server, 'ThreadingHTTPServer', side_effect=make_server):
            server.main(['--store', str(self.db), '--port', '0', *args])
        return instances, worker_calls

    @staticmethod
    def _remote_options():
        return ['--remote-port', '39091', '--remote-public-host', 'workbench.example',
                '--remote-tls-cert', 'cert.pem', '--remote-tls-key', 'key.pem']

    def test_partial_remote_options_and_shared_mode_fail_before_binding(self):
        for args in (['--remote-port', '39091'],
                     ['--shared-mode', *self._remote_options()]):
            with self.subTest(args=args), mock.patch.object(server, 'ThreadingHTTPServer') as bind:
                with self.assertRaises(SystemExit):
                    server.main(['--store', str(self.db), *args])
                bind.assert_not_called()

    def test_remote_start_failure_closes_already_bound_local_listener(self):
        opened = []
        real_server = server.ThreadingHTTPServer

        def fail_second(address, handler, index):
            if index == 1:
                raise OSError('synthetic remote bind failure')
            instance = real_server(address, handler)
            opened.append(instance)
            return instance

        with self.assertRaisesRegex(OSError, 'synthetic remote bind failure'):
            self._run_main(self._remote_options(), fail_second)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].socket.fileno(), -1)

    def test_listeners_keep_per_listener_mode_share_one_worker_set_and_close(self):
        # An ephemeral local port avoids conflicts; the remote port is validated
        # by main and its bind is local to this disposable test process.
        remote_port = self._free_port()
        args = self._remote_options()
        args[1] = str(remote_port)
        instances, worker_calls = self._run_main(args)
        self.assertEqual(len(instances), 2)
        local, remote = instances
        self.assertFalse(local.shared_mode)
        self.assertTrue(remote.shared_mode)
        self.assertEqual(remote.server_address[0], '127.0.0.1')
        self.assertEqual(len(worker_calls), 3)
        self.assertEqual(worker_calls, [self.db.resolve()] * 3)
        self.assertEqual(local.socket.fileno(), -1)
        self.assertEqual(remote.socket.fileno(), -1)

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]


if __name__ == '__main__':
    unittest.main()
