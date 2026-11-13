"""Migration recovery must preserve the old schema before startup upgrades it."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
import sys
from contextlib import closing
from pathlib import Path

from tools.roadmap_store import _SCHEMA, RoadmapStore
from tests.test_workspace_foundation import FoundationHttpTests


class StartupCheckpointTests(unittest.TestCase):
    def test_preflight_preserves_old_bytes_schema_and_integrity(self):
        # Import the same handler module that production starts as a script.
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        import roadmap_server
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'legacy.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript(_SCHEMA)
                db.execute('INSERT INTO store_meta VALUES(1,1,?,3,NULL,NULL,NULL)',('old-store',))
                db.commit()
            old_hash=hashlib.sha256(path.read_bytes()).hexdigest()
            checkpoint=roadmap_server.checkpoint_before_upgrade(path)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),old_hash)
            with closing(sqlite3.connect(checkpoint)) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0],1)
            manifest=json.loads(checkpoint.with_suffix(checkpoint.suffix+'.json').read_text())
            self.assertEqual(manifest['source_sha256'],old_hash)
            self.assertEqual(manifest['checkpoint_sha256'],hashlib.sha256(checkpoint.read_bytes()).hexdigest())
            RoadmapStore(path)
            self.assertIsNone(roadmap_server.checkpoint_before_upgrade(path))


if __name__=='__main__':
    unittest.main()
