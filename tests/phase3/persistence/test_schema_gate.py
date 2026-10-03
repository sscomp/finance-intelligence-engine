"""Schema compatibility gate — deterministic unit suite (Phase 6.7B-R3).

Proves the HP-07/06 / OI-08 contract on the persistence layer directly:
every incompatible metadata state fails CLOSED with the stable gate
code, healthy schema passes, and gate exceptions carry NO DSN/host/
SQL detail. SQLite-backed where determinism needs no cluster; the
real-PostgreSQL legs live in tests/phase3/transport/
test_67b_r3_pg_runtime.py (disposable cluster, skipIf absent).
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.persistence.backend import open_store, resolve_spec  # noqa: E402
from phase3.persistence.migrations import (  # noqa: E402
    Migration,
    MigrationManager,
    default_migrations_for,
)
from phase3.persistence.schema_gate import (  # noqa: E402
    REGISTRY_TABLE_MISSING,
    REQUIRED_OBJECTS_MISSING,
    SCHEMA_CHECKSUM_MISMATCH,
    SCHEMA_FUTURE_VERSION,
    SCHEMA_GATE_CODES,
    SCHEMA_MALFORMED,
    SCHEMA_MIGRATION_FAILED,
    SCHEMA_STALE,
    SchemaGateUnavailable,
    SchemaIncompatible,
    VERSION_RECORD_ABSENT,
    check_schema_compatibility,
)


def _fresh_store(tmp: tempfile.TemporaryDirectory) -> Any:
    """Open a writable SQLite store with migrations applied."""
    path = Path(tmp.name) / "gate.db"
    store = open_store(resolve_spec(str(path)))
    MigrationManager(store, default_migrations_for(store)).apply()
    return store


class TestGatePositive(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _fresh_store(self._tmp)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_healthy_schema_passes_with_expected_version(self) -> None:
        info = check_schema_compatibility(self.store)
        self.assertEqual(info["current_version"], 1)
        self.assertEqual(info["expected_version"], 1)
        self.assertEqual(info["applied"], [1])

    def test_report_is_non_sensitive(self) -> None:
        import json
        blob = json.dumps(check_schema_compatibility(self.store))
        for fragment in ("sqlite:", "postgres", "/", "\\"):
            self.assertNotIn(fragment, blob)


class TestGateNegativeSqlite(unittest.TestCase):
    """Negative metadata states reproducible deterministically on SQLite."""

    def _gate_raised(self, store: Any, expected_code: str) -> None:
        with self.assertRaises(SchemaIncompatible) as cm:
            check_schema_compatibility(store)
        self.assertEqual(cm.exception.code, expected_code)

    def test_registry_table_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "noreg.db"
            store = open_store(resolve_spec(str(path)))
            try:
                from phase3.persistence.schema_v1 import build
                store.executescript(build().sql)  # objects but NO registry
                self._gate_raised(store, REGISTRY_TABLE_MISSING)
            finally:
                store.close()

    def test_registry_table_absent_untouched_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "empty.db"
            store = open_store(resolve_spec(str(path)))
            try:
                self._gate_raised(store, REGISTRY_TABLE_MISSING)
            finally:
                store.close()

    def test_version_record_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _fresh_store_by_dir(tmpdir)
            try:
                store.execute("DELETE FROM schema_migrations")
                self._gate_raised(store, VERSION_RECORD_ABSENT)
            finally:
                store.close()

    def test_stale_schema(self) -> None:
        # Deterministic construction: patch the REGISTERED identity (the
        # code side) to two versions while the DB applied only v1 —
        # exactly the older-than-supported state case 3.
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _fresh_store_by_dir(tmpdir)
            try:
                fake_v2 = Migration(
                    version=2, name="future_maintenance",
                    sql="CREATE TABLE IF NOT EXISTS gate_probe_future(x INTEGER)",
                )
                with mock.patch(
                    "phase3.persistence.schema_gate.default_migrations_for",
                    return_value=default_migrations_for(store) + [fake_v2],
                ):
                    self._gate_raised(store, SCHEMA_STALE)
            finally:
                store.close()

    def test_future_unknown_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _fresh_store_by_dir(tmpdir)
            try:
                store.executemany(
                    "INSERT INTO schema_migrations (version, name, checksum,"
                    " applied_at, error) VALUES (%s, %s, %s, %s, NULL)",
                    [(999, "unknown_future", "0" * 64, "now")],
                )
                self._gate_raised(store, SCHEMA_FUTURE_VERSION)
            finally:
                store.close()

    def test_malformed_version_metadata(self) -> None:
        # A schema_migrations.version that is not an integer must fail
        # closed rather than compare garbage. Typed backends refuse to
        # STORE such a row, so the unit exposes the gate's own handling
        # through a fake store (the real-PostgreSQL legs cover every
        # at-rest-tamperable state end to end).
        class _MalformedRegistryStore:
            backend = "postgres"

            def execute(self, *a: Any, **k: Any) -> Any:
                class _Cur:
                    def fetchall(self) -> list[Any]:
                        return [("not-a-number", "x", "0" * 64, "now", None)]

                return _Cur()

        self._gate_raised(_MalformedRegistryStore(), SCHEMA_MALFORMED)

    def test_null_checksum_metadata_fails_closed(self) -> None:
        class _NullChecksumStore:
            backend = "postgres"

            def execute(self, *a: Any, **k: Any) -> Any:
                class _Cur:
                    def fetchall(self) -> list[Any]:
                        return [(1, "initial_schema", None, "now", None)]

                return _Cur()

        self._gate_raised(_NullChecksumStore(), SCHEMA_MALFORMED)

    def test_checksum_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _fresh_store_by_dir(tmpdir)
            try:
                store.execute("UPDATE schema_migrations SET checksum = ?",
                              ("f" * 64,))
                self._gate_raised(store, SCHEMA_CHECKSUM_MISMATCH)
            finally:
                store.close()

    def test_failed_migration_row_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _fresh_store_by_dir(tmpdir)
            try:
                store.execute(
                    "UPDATE schema_migrations SET error = 'boom' WHERE version = 1"
                )
                self._gate_raised(store, SCHEMA_MIGRATION_FAILED)
            finally:
                store.close()

    def test_required_objects_missing(self) -> None:
        # Registry says applied, but an application table is gone —
        # e.g. an operator dropped/migrated outside the tooling.
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _fresh_store_by_dir(tmpdir)
            try:
                store.executescript(
                    "DROP TABLE ingestion_errors;"
                    "DROP TABLE adapter_run_log;"
                )
                self._gate_raised(store, REQUIRED_OBJECTS_MISSING)
            finally:
                store.close()

    def test_gate_query_failure_is_unavailable_not_incompatible(self) -> None:
        class _BrokenStore:
            backend = "sqlite"

            def execute(self, *a: Any, **k: Any) -> Any:
                raise sqlite3.OperationalError("simulated outage")

        with self.assertRaises(SchemaGateUnavailable):
            check_schema_compatibility(_BrokenStore())


class TestGateContract(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _fresh_store(self._tmp)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_gate_code_vocabulary_is_a_closed_stable_set(self) -> None:
        self.assertEqual(
            SCHEMA_GATE_CODES,
            {
                "REGISTRY_TABLE_MISSING",
                "VERSION_RECORD_ABSENT",
                "SCHEMA_STALE",
                "SCHEMA_FUTURE_VERSION",
                "SCHEMA_MALFORMED",
                "SCHEMA_CHECKSUM_MISMATCH",
                "SCHEMA_MIGRATION_FAILED",
                "REQUIRED_OBJECTS_MISSING",
            },
        )

    def test_gate_exception_never_carries_dsn(self) -> None:
        # The gate must be safe even when the underlying error text
        # embeds a DSN: SchemaGateUnavailable redacts url-shaped text.
        class _LeakyStore:
            backend = "postgres"

            def execute(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError(
                    "connect failed: postgresql://user:secret@db.host/x"
                )

        with self.assertRaises(SchemaGateUnavailable) as cm:
            check_schema_compatibility(_LeakyStore())
        blob = str(cm.exception)
        self.assertNotIn("postgres", blob)
        self.assertNotIn("secret", blob)

    def test_gate_detail_never_carries_sql_or_paths(self) -> None:
        # The gate's exception detail must stay abstract (stable code +
        # version/counts only) even for a dropped registry: never raw
        # SQL text, never the database path.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "det.db"
            store = open_store(resolve_spec(str(path)))
            try:
                with self.assertRaises(SchemaIncompatible) as cm:
                    check_schema_compatibility(store)
                self.assertEqual(cm.exception.code, REGISTRY_TABLE_MISSING)
                self.assertNotIn("SELECT", cm.exception.detail)
                self.assertNotIn(str(path.resolve()), cm.exception.detail)
            finally:
                store.close()


def _fresh_store_by_dir(tmpdir: str) -> Any:
    path = Path(tmpdir) / "gate-case.db"
    store = open_store(resolve_spec(str(path)))
    MigrationManager(store, default_migrations_for(store)).apply()
    return store


if __name__ == "__main__":  # pragma: no cover
    unittest.main()