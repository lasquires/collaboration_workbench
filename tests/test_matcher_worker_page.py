"""Keep background matcher write transactions bounded to one candidate pair."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import roadmap_server


class MatcherWorkerPageTests(unittest.TestCase):
    def test_worker_limits_each_write_transaction_to_one_pair(self):
        calls = []

        class FakeStore:
            def __init__(self, path): pass
            def read_v1(self, resource): return {"items": [{"id": "project"}]}
            def process_jobs(self, project, *, limit): calls.append((project, limit))

        class StopAfterOnePass:
            stopped = False
            def is_set(self): return self.stopped
            def wait(self, interval): self.stopped = True

        class InlineThread:
            def __init__(self, *, target, **kwargs): self.target = target
            def start(self): self.target()

        with patch.object(roadmap_server, "RoadmapStore", FakeStore), \
             patch.object(roadmap_server.threading, "Event", StopAfterOnePass), \
             patch.object(roadmap_server.threading, "Thread", InlineThread):
            stop, _worker = roadmap_server.start_matcher_worker("unused", interval=0)

        self.assertTrue(stop.is_set())
        self.assertEqual([("project", 1)], calls)


if __name__ == "__main__":
    unittest.main()
