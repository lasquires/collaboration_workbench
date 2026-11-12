import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tools.migrate_roadmap import extract_records, inspect_board, migrate, backup_database
from tools.roadmap_store import RoadmapStore

START = "<!-- ROADMAP_TASK_RECORDS:START -->"
END = "<!-- ROADMAP_TASK_RECORDS:END -->"


def board_bytes(records):
    return ("Before\r\n" + START + "\r\n" + json.dumps(records, indent=2) + "\r\n" + END + "\r\nAfter\r\n").encode()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.records = [{"id": "P", "title": "Parent", "status": "ready", "unknown": None,
                         "subtasks": [{"id": "C", "title": "Child", "status": "review", "evidence": ["docs/x.md"]}]},
                        {"id": "I", "title": "Island", "status": "blocked", "dependencies": ["P"]}]
        self.board = self.root / "BOARD.md"
        self.board.write_bytes(board_bytes(self.records))

    def test_dry_run_reads_exact_bytes_hash_counts_and_leaves_source_untouched(self):
        before = self.board.read_bytes()
        result = inspect_board(self.board)
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["task_count"], 3)
        self.assertEqual(result["unresolved_links"], [])
        self.assertEqual(self.board.read_bytes(), before)

    def test_requires_one_marked_block_and_valid_store_semantics(self):
        self.board.write_bytes((START + "[]" + END + START + "[]" + END).encode())
        with self.assertRaises(ValueError):
            inspect_board(self.board)
        self.board.write_bytes(board_bytes([{"id": "dup", "title": "A", "status": "ready"},
                                           {"id": "dup", "title": "B", "status": "ready"}]))
        with self.assertRaises(Exception):
            inspect_board(self.board)

    def test_apply_hash_gates_installs_only_selected_new_db_after_round_trip(self):
        target = self.root / "selected.sqlite3"
        source_hash = hashlib.sha256(self.board.read_bytes()).hexdigest()
        result = migrate(self.board, target, source_hash)
        store = RoadmapStore(target)
        self.assertEqual(store.export_legacy_records(), self.records)
        self.assertEqual(result["task_count"], 3)
        self.assertFalse((self.root / "analysis" / "roadmap.sqlite3").exists())

    def test_apply_wrong_hash_or_existing_destination_does_not_change_target(self):
        target = self.root / "target.sqlite3"
        with self.assertRaises(ValueError):
            migrate(self.board, target, "0" * 64)
        self.assertFalse(target.exists())
        target.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            migrate(self.board, target, hashlib.sha256(self.board.read_bytes()).hexdigest())
        self.assertEqual(target.read_bytes(), b"existing")

    def test_backup_uses_sqlite_api_and_preserves_store_snapshot(self):
        source = self.root / "source.sqlite3"
        store = RoadmapStore(source)
        store.import_v1(self.records, "a" * 64)
        backup = self.root / "backup.sqlite3"
        backup_database(source, backup)
        self.assertEqual(RoadmapStore(backup).export_legacy_records(), self.records)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
