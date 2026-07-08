"""Tests for phase3.persistence.backup — backup / restore / integrity helpers.

The contract:
* ``backup_db`` and ``restore_db`` are inverse operations that copy the
  full database using the SQLite online backup API.
* Both refuse to write to ``macro_history.db`` (path guard).
* quick_check / integrity_check return ``"ok"`` for the original and
  the copy.
* Restoring into a different location preserves all rows.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from phase3.persistence import schema_v1
from phase3.persistence.backup import (
    backup_db,
    integrity_check,
    quick_check,
    restore_db,
)
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.sqlite import (
    FORBIDDEN_DB_NAME,
    PathGuardError,
    SQLiteStore,
)


def _seed_db(path: str) -> None:
    with SQLiteStore(path) as store:
        MigrationManager(store, [schema_v1.build()]).apply()
        store.execute(
            "INSERT INTO signal_log "
            "(signal_id, entity_type, entity_id, signal_type, value, "
            " unit, direction, timestamp) "
            "VALUES ('s-1', 'company', '2330', 'pe', 22.0, 'x', "
            " 'bullish', '2026-07-08T00:00:00+00:00')"
        )


class BackupTests(unittest.TestCase):
    def test_backup_writes_dest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.db")
            dest = os.path.join(td, "dest.db")
            _seed_db(src)
            n = backup_db(src, dest)
            self.assertGreater(n, 0)
            self.assertTrue(os.path.exists(dest))

    def test_backup_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.db")
            dest = os.path.join(td, "dest.db")
            _seed_db(src)
            backup_db(src, dest)
            with SQLiteStore(dest) as store:
                self.assertEqual(
                    store.execute(
                        "SELECT COUNT(*) FROM signal_log"
                    ).fetchone()[0],
                    1,
                )

    def test_backup_quick_check_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.db")
            dest = os.path.join(td, "dest.db")
            _seed_db(src)
            backup_db(src, dest)
            self.assertEqual(quick_check(dest), "ok")
            self.assertEqual(integrity_check(dest), "ok")

    def test_backup_rejects_forbidden_dest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.db")
            dest = os.path.join(td, FORBIDDEN_DB_NAME)
            _seed_db(src)
            with self.assertRaises(PathGuardError):
                backup_db(src, dest)

    def test_backup_rejects_forbidden_src(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            forbidden = os.path.join(td, FORBIDDEN_DB_NAME)
            dest = os.path.join(td, "dest.db")
            # Create the forbidden file so the path resolves to a real path.
            open(forbidden, "wb").close()
            with self.assertRaises(PathGuardError):
                backup_db(forbidden, dest)

    def test_backup_rejects_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "x.db")
            _seed_db(src)
            with self.assertRaises(ValueError):
                backup_db(src, src)

    def test_backup_creates_dest_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.db")
            dest = os.path.join(td, "sub", "dir", "dest.db")
            _seed_db(src)
            backup_db(src, dest)
            self.assertTrue(os.path.exists(dest))


class RestoreTests(unittest.TestCase):
    def test_restores_data(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.db")
            # Pre-existing dest with no schema — restore should overwrite.
            dest = os.path.join(td, "dest.db")
            _seed_db(src)
            restore_db(src, dest)
            with SQLiteStore(dest) as store:
                self.assertEqual(
                    store.execute(
                        "SELECT COUNT(*) FROM signal_log"
                    ).fetchone()[0],
                    1,
                )
                # Schema is restored too.
                self.assertIsNotNone(
                    store.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='signal_log'"
                    ).fetchone()
                )

    def test_restore_rejects_forbidden_dest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.db")
            dest = os.path.join(td, FORBIDDEN_DB_NAME)
            _seed_db(src)
            with self.assertRaises(PathGuardError):
                restore_db(src, dest)


class IntegrityHelperTests(unittest.TestCase):
    def test_quick_check_ok_on_seeded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "x.db")
            _seed_db(db)
            self.assertEqual(quick_check(db), "ok")
            self.assertEqual(integrity_check(db), "ok")

    def test_quick_check_rejects_forbidden(self) -> None:
        with self.assertRaises(PathGuardError):
            quick_check("macro_history.db")

    def test_integrity_check_rejects_forbidden(self) -> None:
        with self.assertRaises(PathGuardError):
            integrity_check("macro_history.db")


if __name__ == "__main__":
    unittest.main()
