#!/usr/bin/env python3
"""WO C4 — backend-specific registry checksum contract.

Formalizes owner decision AD-1's mapping, verbatim from the work order:

    backend=sqlite     -> expected sqlite registry identity
    backend=postgresql -> expected postgresql registry identity

The two identities are DISTINCT BY DESIGN (different DDL dialects are
different schemas). Any test or code that asserts
``SQLITE_REGISTRY_CHECKSUM == POSTGRES_REGISTRY_CHECKSUM`` is wrong
under this architecture; asserting *inequality* here guards against
accidental cross-backend checksum unification.

The three-step PostgreSQL readyz leg (valid registry -> readiness PASS;
tampered identity -> readiness FAIL CLOSED with the stable 503 code;
repaired registry -> readiness PASS) is exercised live over HTTP by
``tests/phase3/transport/test_67b_r3_pg_runtime.py``
(TestPgHealthyPostgresReady / TestPgChecksumTamper /
TestPgRegistryRepairRecovery). This module pins the contract's
mechanical invariants without duplicating that harness.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from phase3.persistence.backend import (
    BACKEND_POSTGRES,
    BACKEND_SQLITE,
    is_pg_dsn,
    open_store,
    resolve_spec,
)
from phase3.persistence.postgres import PostgresStore
from phase3.persistence.sqlite import SQLiteStore
from phase3.persistence.migrations import default_migrations_for


PG_DSN = os.environ.get(
    "FIE_TEST_PG_DSN",
    "postgresql://fie@/fie_contract?host=/tmp/fie-pg&port=54329",
)

# Cross-checking the mapping against the checksums recorded as evidence
# in the Phase 6.3 preflight report keeps a silent DDL rewrite from
# changing registry identity without anyone noticing.
EXPECTED_SQLITE_V1_CHECKSUM_PREFIX = "4765a381"
EXPECTED_PG_V1_CHECKSUM_PREFIX = "a281ddde"


class RegistryContractTests(unittest.TestCase):
    """backend -> registry identity mapping and its non-equality."""

    def test_sqlite_backend_gets_sqlite_migration(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            self.assertEqual(store.backend, "sqlite")
            migs = default_migrations_for(store)
            self.assertEqual(len(migs), 1)
            self.assertEqual(migs[0].version, 1)
            self.assertTrue(migs[0].checksum.startswith(
                EXPECTED_SQLITE_V1_CHECKSUM_PREFIX),
                f"SQLite v1 checksum drifted: {migs[0].checksum}")
        finally:
            store.close()

    @unittest.skipUnless(
        os.path.exists("/tmp/fie-pg/pgdata"),
        "disposable PostgreSQL cluster not available",
    )
    def test_postgres_backend_gets_postgres_migration(self) -> None:
        try:
            store = open_store(resolve_spec(PG_DSN))
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"disposable cluster unreachable: {exc}")
        try:
            self.assertEqual(store.backend, "postgres")
            migs = default_migrations_for(store)
            self.assertEqual(len(migs), 1)
            self.assertEqual(migs[0].version, 1)
            self.assertTrue(migs[0].checksum.startswith(
                EXPECTED_PG_V1_CHECKSUM_PREFIX),
                f"PG v1 checksum drifted: {migs[0].checksum}")
        finally:
            store.close()

    def test_registry_identities_are_backend_specific_and_distinct(self) -> None:
        """The WO's forbidden-equality rule pinned as a positive test."""
        from phase3.persistence.schema_v1 import build as sqlite_build
        from phase3.persistence.schema_pg import build as pg_build

        sql = sqlite_build().checksum
        pg = pg_build().checksum
        self.assertNotEqual(
            sql, pg,
            "SQLite and PostgreSQL registry checksums must stay "
            "backend-specific (WO C4); treat unification as an explicit "
            "future architecture decision if ever intended",
        )
        self.assertTrue(sql.startswith(EXPECTED_SQLITE_V1_CHECKSUM_PREFIX))
        self.assertTrue(pg.startswith(EXPECTED_PG_V1_CHECKSUM_PREFIX))

    def test_wrong_backend_migration_is_detected(self) -> None:
        """Applying with the WRONG backend identity fails closed.

        A valid PG store whose v1 registry row is then rewritten to the
        SQLite identity must FAIL CLOSED on the next migration apply —
        the mechanical counterpart of "incorrect registry identity ->
        readiness FAIL CLOSED" (the readyz tamper suite drives this
        over HTTP). Restore the true identity afterwards (third leg).
        """
        from phase3.persistence.migrations import MigrationManager
        from phase3.persistence.schema_v1 import build as sqlite_build
        from phase3.persistence.schema_pg import build as pg_build

        try:
            store = open_store(resolve_spec(PG_DSN))
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"disposable cluster unreachable: {exc}")
        try:
            # Establish the valid per-backend registry first (leg 1).
            MigrationManager(store, default_migrations_for(store)).apply()
            rows = store.execute(
                "SELECT version, checksum FROM schema_migrations"
                " WHERE version = 1"
            ).fetchall()
            self.assertEqual(rows[0][1], pg_build().checksum)
            # Tamper: cross-backend identity (the SQLite checksum) -> FAIL.
            store.execute(
                "UPDATE schema_migrations SET checksum = %s"
                " WHERE version = 1",
                (sqlite_build().checksum,),
            )
            mgr = MigrationManager(store, default_migrations_for(store))
            with self.assertRaises(Exception) as cm:
                mgr.apply()
            self.assertIn("checksum", str(cm.exception).lower())

            # Repair (restore): the true PG identity restores PASS (leg 3).
            store.execute(
                "UPDATE schema_migrations SET checksum = %s"
                " WHERE version = 1",
                (pg_build().checksum,),
            )
            MigrationManager(store, default_migrations_for(store)).apply()
            rows = store.execute(
                "SELECT version, checksum, error FROM schema_migrations"
                " WHERE version = 1"
            ).fetchall()
            self.assertEqual(rows[0][1], pg_build().checksum)
            self.assertIsNone(rows[0][2])
        finally:
            store.close()

    def test_is_pg_dsn_matches_seed_bridge_rule(self) -> None:
        """One selection rule across the service and bridge surfaces."""
        for dsn in ("postgres://x/y", "POSTGRESQL://x/y",
                    "postgresql://u:p@h/db"):
            self.assertTrue(is_pg_dsn(dsn), dsn)
        self.assertEqual(is_pg_dsn("macro_history.db"), False)
        # Phase 6.8A: a relative bare path is a MALFORMED target — the
        # sqlite selection rule is only exercised with an ABSOLUTE path.
        from phase3.runtime_contract import FailClosedTarget

        with self.assertRaises(FailClosedTarget):
            resolve_spec("macro_history.db")
        self.assertEqual(resolve_spec("/tmp/registry_bridge_fixture.db").backend,
                         BACKEND_SQLITE)
        self.assertFalse(BACKEND_POSTGRES == BACKEND_SQLITE)


if __name__ == "__main__":
    unittest.main(verbosity=2)