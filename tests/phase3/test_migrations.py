"""Tests for phase3.persistence.migrations — apply / idempotency / checksum / failure.

The contract is:
* ``apply()`` is idempotent — re-running an already-applied version is a no-op.
* A migration whose stored checksum differs from the registered checksum
  raises ``MigrationError`` (no silent re-apply).
* A migration that raises (e.g. invalid SQL) is recorded with
  ``error != NULL`` and the next ``apply()`` retries it.
* The migration runner correctly handles the v1 schema even when
  ``executescript`` is used to run the SQL (the original Phase 3B
  transaction bug).

We exercise the schema_v1 module's real migration as the integration
case and synthetic ad-hoc migrations for the unit-style tests.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from phase3.persistence import schema_v1
from phase3.persistence.migrations import (
    Migration,
    MigrationError,
    MigrationManager,
)
from phase3.persistence.sqlite import SQLiteStore


def _make_migration(version: int, name: str, sql: str) -> Migration:
    return Migration(version=version, name=name, sql=sql)


def _empty_migration(version: int, name: str) -> Migration:
    return _make_migration(
        version,
        name,
        f"CREATE TABLE IF NOT EXISTS t{version} (id INTEGER PRIMARY KEY);",
    )


class MigrationConstructorTests(unittest.TestCase):
    def test_empty_sql_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Migration(version=1, name="x", sql="   ")

    def test_zero_version_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Migration(version=0, name="x", sql="CREATE TABLE t(id);")

    def test_duplicate_versions_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                with self.assertRaises(ValueError):
                    MigrationManager(
                        store,
                        [
                            _empty_migration(1, "a"),
                            _empty_migration(1, "b"),
                        ],
                    )

    def test_fingerprint_includes_version_name_checksum(self) -> None:
        m = _empty_migration(2, "two")
        self.assertTrue(m.fingerprint().startswith("2:two:"))


class EnsureRegistryTests(unittest.TestCase):
    def test_creates_table(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                mgr = MigrationManager(store, [])
                mgr.ensure_registry()
                cur = store.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='schema_migrations'"
                )
                self.assertIsNotNone(cur.fetchone())

    def test_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                mgr = MigrationManager(store, [])
                mgr.ensure_registry()
                mgr.ensure_registry()
                self.assertEqual(mgr.current_version(), 0)


class ApplyIdempotencyTests(unittest.TestCase):
    def test_fresh_db_applies_pending(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                mgr = MigrationManager(
                    store,
                    [_empty_migration(1, "first"),
                     _empty_migration(2, "second")],
                )
                applied = mgr.apply()
                self.assertEqual([m.version for m in applied], [1, 2])
                self.assertEqual(mgr.current_version(), 2)
                self.assertEqual(
                    mgr.applied_versions(), [1, 2],
                )

    def test_second_apply_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                mgr = MigrationManager(
                    store, [_empty_migration(1, "first")],
                )
                mgr.apply()
                applied = mgr.apply()
                self.assertEqual(applied, [])

    def test_partial_apply_only_runs_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                mgr = MigrationManager(
                    store,
                    [_empty_migration(1, "first"),
                     _empty_migration(2, "second"),
                     _empty_migration(3, "third")],
                )
                mgr.apply()  # applies 1, 2, 3
                # Now construct a fresh manager with the same store but
                # add version 4 — the prior 3 stay applied.
                mgr2 = MigrationManager(
                    store,
                    [_empty_migration(1, "first"),
                     _empty_migration(2, "second"),
                     _empty_migration(3, "third"),
                     _empty_migration(4, "fourth")],
                )
                applied = mgr2.apply()
                self.assertEqual([m.version for m in applied], [4])

    def test_checksum_mismatch_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                mig = _empty_migration(1, "first")
                mgr = MigrationManager(store, [mig])
                mgr.apply()
                # Build a "tampered" migration with the same version+name
                # but different SQL → different checksum.
                tampered = _make_migration(
                    1, "first",
                    "CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY, extra TEXT);",
                )
                mgr2 = MigrationManager(store, [tampered])
                with self.assertRaises(MigrationError) as ctx:
                    mgr2.apply()
                self.assertIn("checksum", str(ctx.exception).lower())


class MigrationFailureTests(unittest.TestCase):
    def test_invalid_sql_records_error_and_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                # Use a script whose first statement is invalid so
                # nothing lands. (executescript runs valid statements
                # even after an error in some cases — a syntax error
                # in the very first statement guarantees the script
                # fails before any DDL is executed.)
                bad = _make_migration(
                    1, "bad", "THIS IS NOT VALID SQL AT ALL",
                )
                mgr = MigrationManager(store, [bad])
                with self.assertRaises(MigrationError):
                    mgr.apply()
                # Registry row is recorded with the failure.
                self.assertEqual(mgr.failed_versions(), [1])
                self.assertEqual(mgr.current_version(), 0)
                # ... and no leftover tables from a partial run.
                cur = store.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='t'"
                )
                self.assertIsNone(cur.fetchone())

    def test_retry_after_failure_applies_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                bad = _make_migration(
                    1, "bad", "NOT VALID SQL AT ALL",
                )
                mgr = MigrationManager(store, [bad])
                with self.assertRaises(MigrationError):
                    mgr.apply()
                # Replace the registered migration with a valid one
                # (same version + name → checksum differs but the
                # registry will reject that, so we wipe the row first
                # to model "operator reverted the file").
                store.execute(
                    "DELETE FROM schema_migrations WHERE version=1"
                )
                good = _make_migration(
                    1, "bad", "CREATE TABLE t (id INTEGER PRIMARY KEY);",
                )
                mgr2 = MigrationManager(store, [good])
                applied = mgr2.apply()
                self.assertEqual([m.version for m in applied], [1])
                self.assertEqual(mgr2.failed_versions(), [])
                self.assertEqual(mgr2.current_version(), 1)


class SchemaV1IntegrationTests(unittest.TestCase):
    """The real schema_v1 migration must apply cleanly with
    ``executescript`` inside the custom transaction context manager
    — this is the regression that motivated Task 1 fix.
    """

    def test_apply_v1_creates_all_tables(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                mgr = MigrationManager(store, [schema_v1.build()])
                applied = mgr.apply()
                self.assertEqual([m.version for m in applied], [1])
                cur = store.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "ORDER BY name"
                )
                tables = {r[0] for r in cur.fetchall()}
                for required in {
                    "signal_log",
                    "score_snapshot",
                    "graph_nodes",
                    "graph_edges",
                    "adapter_run_log",
                    "ingestion_errors",
                    "schema_migrations",
                }:
                    self.assertIn(required, tables)

    def test_apply_v1_creates_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                MigrationManager(store, [schema_v1.build()]).apply()
                cur = store.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "ORDER BY name"
                )
                triggers = {r[0] for r in cur.fetchall()}
                self.assertIn("trg_score_snapshot_no_update", triggers)
                self.assertIn("trg_score_snapshot_no_delete", triggers)

    def test_apply_v1_twice_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                mgr = MigrationManager(store, [schema_v1.build()])
                mgr.apply()
                applied_again = mgr.apply()
                self.assertEqual(applied_again, [])
                self.assertEqual(mgr.current_version(), 1)

    def test_apply_v1_on_preserved_data_does_not_lose_rows(self) -> None:
        """Pre-existing rows in a user-created table on a brand-new DB
        must not be wiped by the IF NOT EXISTS migration."""
        with tempfile.TemporaryDirectory() as td:
            with SQLiteStore(os.path.join(td, "m.db")) as store:
                store.execute(
                    "CREATE TABLE signal_log_extra (x INTEGER)"
                )
                store.execute(
                    "INSERT INTO signal_log_extra(x) VALUES (1)"
                )
                MigrationManager(store, [schema_v1.build()]).apply()
                # Pre-existing user table is preserved.
                self.assertEqual(
                    store.execute(
                        "SELECT COUNT(*) FROM signal_log_extra"
                    ).fetchone()[0],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
