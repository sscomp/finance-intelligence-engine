"""Phase 6.7B-R3 — PostgreSQL runtime gate on the disposable cluster.

Two families of legs (work order §3.2/§3.3), both SKIP-closed cleanly
(no disposable cluster → skip with reason, never a fake pass):

A. **Schema gate on real PostgreSQL, over HTTP** — the same tamper
   matrix as the SQLite suite (test_67b_r3_readyz_schema_gate.py), run
   against ``postgresql`` databases through the production bootstrap:
   every incompatible metadata state answers ``/readyz`` 503 with the
   stable ``SCHEMA_INCOMPATIBLE`` code, ``/healthz`` stays 200
   ``alive`` through every failure, auth is not weakened, and the
   corrupted schema RECOVERS explicitly when the metadata is repaired
   (same server process — no restart required).

B. **Bounded connection lifecycle** — finite acquisition (closed port
   fails bounded), finite statements (``statement_timeout`` cancels a
   stuck query WITHOUT tearing down the healthy session), deterministic
   recovery after the backend disappears (terminated backend → next
   operation reconnects once; a failed statement is never re-run),
   no per-operation session growth (leak proof via ``pg_stat_activity``),
   managed-transaction rollback + session reuse, constructor timeout
   validation, and the sqlstate-first error classification that keeps a
   statement timeout (57014) from being treated as a dead session.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tests.phase3.service import helpers  # noqa: E402
from tests.phase3.transport import runtime_harness  # noqa: E402

#: Synthetic credential — never a real secret (work order §7).
R3_TOKEN = "r3-fixture-synthetic-token"


def _pg_dsn() -> str | None:
    import os

    dsn = os.environ.get("FIE_TEST_PG_DSN")
    if dsn:
        return dsn
    candidate = "postgresql://fie@/fie_contract?host=/tmp/fie-pg&port=54329"
    if Path("/tmp/fie-pg/pgdata").exists():
        return candidate
    return None


PG_PRESENT = _pg_dsn() is not None


def _psycopg():
    import psycopg

    return psycopg


def _pg_store(dsn: str, **kwargs):
    from phase3.persistence.postgres import PostgresStore

    return PostgresStore(dsn, **kwargs)


class _PgTamperSuite(runtime_harness.RuntimeHarness):
    """HTTP readiness contract on real PostgreSQL; tamper hook hookable."""

    tamper_sql: tuple[str, ...] = ()
    db_spec = _pg_dsn()
    config_overrides = {"auth_mode": "token", "auth_token": R3_TOKEN}

    @classmethod
    def setUpClass(cls) -> None:
        if cls.db_spec is None:
            raise unittest.SkipTest("disposable PostgreSQL cluster not available")
        super().setUpClass()
        if cls.tamper_sql:
            conn = _psycopg().connect(cls.db_spec, autocommit=True)
            try:
                for sql in cls.tamper_sql:
                    conn.execute(sql)
            finally:
                conn.close()

    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {R3_TOKEN}"}

    @classmethod
    def _writable_conn(cls):
        return _psycopg().connect(cls.db_spec, autocommit=True)

    def assert_readyz_incompatible(self) -> None:
        status, env, _ = self.get("/readyz", headers=self.bearer())
        self.assertEqual(status, 503, env)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "SCHEMA_INCOMPATIBLE")

    def assert_auth_not_weakened(self) -> None:
        status, env, _ = self.get("/readyz")
        self.assertEqual(status, 401)
        self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
        bad = self.get("/readyz", headers={"Authorization": "Bearer nope"})
        self.assertEqual(bad[0], 401)
        self.assertEqual(bad[1]["error"]["code"], "UNAUTHENTICATED")

    def assert_liveness_alive(self) -> None:
        for _ in range(3):
            status, env, _ = self.get("/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(env["status"], "alive")
            self.assertEqual(env["kind"], "healthz")


@unittest.skipIf(not PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgHealthyPostgresReady(_PgTamperSuite):
    def test_readyz_200_over_postgres(self) -> None:
        status, env, _ = self.get("/readyz", headers=self.bearer())
        self.assertEqual(status, 200)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["payload"]["backend_kind"], "postgres")
        self.assertEqual(env["payload"]["schema_current_version"], 1)
        self.assertEqual(env["warnings"], [])

    def test_liveness_and_auth_healthy(self) -> None:
        self.assert_liveness_alive()
        self.assert_auth_not_weakened()


@unittest.skipIf(not PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgRegistryTableDropped(_PgTamperSuite):
    # Real-PostgreSQL state that the SQLite suite could only simulate:
    # the registry query itself hits 42P01 (undefined_table).
    tamper_sql = ("DROP TABLE schema_migrations",)

    def test_readyz_503_registry_missing(self) -> None:
        self.assert_readyz_incompatible()

    def test_liveness_alive_through_pg_gate_failure(self) -> None:
        self.assert_liveness_alive()

    def test_auth_not_weakened_by_pg_gate_failure(self) -> None:
        self.assert_auth_not_weakened()


@unittest.skipIf(not PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgRegistryRowsCleared(_PgTamperSuite):
    tamper_sql = ("DELETE FROM schema_migrations",)

    def test_readyz_503_version_record_absent(self) -> None:
        self.assert_readyz_incompatible()

    def test_liveness_alive_through_pg_gate_failure(self) -> None:
        self.assert_liveness_alive()


@unittest.skipIf(not PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgFutureVersion(_PgTamperSuite):
    tamper_sql = (
        "INSERT INTO schema_migrations (version, name, checksum, applied_at,"
        " error) VALUES (999, 'from_the_future', '%s', '', NULL)"
        % ("0" * 64),
    )

    def test_readyz_503_future_version(self) -> None:
        self.assert_readyz_incompatible()

    def test_liveness_alive_through_pg_gate_failure(self) -> None:
        self.assert_liveness_alive()


@unittest.skipIf(not PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgChecksumTamper(_PgTamperSuite):
    tamper_sql = (
        "UPDATE schema_migrations SET checksum = '%s'" % ("f" * 64,),
    )

    def test_readyz_503_checksum_mismatch(self) -> None:
        self.assert_readyz_incompatible()

    def test_liveness_alive_through_pg_gate_failure(self) -> None:
        self.assert_liveness_alive()


@unittest.skipIf(not PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgMigrationFailed(_PgTamperSuite):
    tamper_sql = ("UPDATE schema_migrations SET error = 'simulated failure'",)

    def test_readyz_503_migration_failed(self) -> None:
        self.assert_readyz_incompatible()

    def test_liveness_alive_through_pg_gate_failure(self) -> None:
        self.assert_liveness_alive()


@unittest.skipIf(not PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgRequiredObjectsMissing(_PgTamperSuite):
    tamper_sql = ("DROP TABLE graph_edges", "DROP TABLE graph_nodes")

    def test_readyz_503_objects_missing(self) -> None:
        self.assert_readyz_incompatible()

    def test_liveness_alive_through_pg_gate_failure(self) -> None:
        self.assert_liveness_alive()


@unittest.skipIf(not PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgRegistryRepairRecovery(_PgTamperSuite):
    """§3.3: explicit recovery after schema corruption, no restart."""

    def test_readyz_recovers_after_registry_repair(self) -> None:
        conn = self._writable_conn()
        try:
            rows = conn.execute(
                "SELECT name, checksum, applied_at FROM schema_migrations"
                " WHERE version = 1"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            name, checksum, applied_at = rows[0]
            # corrupt
            conn.execute("DELETE FROM schema_migrations")
        finally:
            conn.close()
        self.assert_readyz_incompatible()
        self.assert_liveness_alive()
        # repair: re-record exactly the same authoritative migration row
        conn = self._writable_conn()
        try:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum,"
                " applied_at, error) VALUES (%s, %s, %s, %s, NULL)",
                (1, name, checksum, applied_at),
            )
        finally:
            conn.close()
        status, env, _ = self.get("/readyz", headers=self.bearer())
        self.assertEqual(status, 200, env)
        self.assertEqual(env["payload"]["backend_kind"], "postgres")


@unittest.skipUnless(PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgConnectionLifecycle(unittest.TestCase):
    """Bounded connect / bounded statements / deterministic recovery."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.psycopg = _psycopg()
        cls.dsn = _pg_dsn()
        cls.own_store = _pg_store(cls.dsn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.own_store.close()

    def _fresh_store(self, **kwargs):
        from phase3.persistence.postgres import PostgresStore

        return PostgresStore(self.dsn, **kwargs)

    # -- finite acquisition --------------------------------------------------

    def test_connect_to_closed_port_fails_bounded_no_fallback(self) -> None:
        from phase3.persistence.postgres import PostgresStore

        closed = "host=127.0.0.1 port=9999 dbname=fie_contract user=fie"
        started = time.monotonic()
        with self.assertRaises(Exception) as cm:
            PostgresStore(closed, connect_timeout=2)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8.0, "connect must be bounded by its timeout")
        text = str(cm.exception).lower()
        self.assertNotIn("sqlite", text)
        self.assertNotIn("/tmp/", str(cm.exception))

    def test_constructor_rejects_non_positive_timeouts(self) -> None:
        from phase3.persistence.postgres import PostgresStore

        for kwargs in ({"connect_timeout": 0}, {"connect_timeout": -3},
                       {"statement_timeout_ms": 0},
                       {"statement_timeout_ms": -1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    PostgresStore(self.dsn, **kwargs)

    # -- finite statements ---------------------------------------------------

    def test_statement_timeout_cancels_query_keeps_session(self) -> None:
        psycopg = self.psycopg
        store = self._fresh_store(statement_timeout_ms=500)
        try:
            self.assertEqual(store.describe()["session_open"], True)
            started = time.monotonic()
            with self.assertRaises(psycopg.errors.QueryCanceled) as cm:
                store.execute("SELECT pg_sleep(5)")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 4.0)
            self.assertEqual(getattr(cm.exception, "sqlstate", None), "57014")
            # a timed-out statement is NOT a dead session: sqlstate-first
            # classification keeps it healthy — reconnect_count stays 0
            self.assertEqual(store.reconnect_count, 0)
            self.assertEqual(store.describe()["session_open"], True)
            self.assertEqual(store.execute("SELECT 1").fetchone()[0], 1)
        finally:
            store.close()

    # -- deterministic recovery after the backend disappears -----------------

    def _backend_pid(self, store) -> int:
        return int(store.execute("SELECT pg_backend_pid()").fetchone()[0])

    def _terminate(self, killer, pid: int) -> None:
        killer.execute("SELECT pg_terminate_backend(%s)", (pid,))

    def test_terminated_backend_recovers_with_single_bounded_reconnect(self) -> None:
        store = self._fresh_store()
        killer = self._fresh_store()
        try:
            pid = self._backend_pid(store)
            self._terminate(killer, pid)
            # next operation: the dead session is detected and marked
            # broken; the operation itself still FAILS (never retried)
            with self.assertRaises(Exception) as cm:
                store.execute("SELECT 1 as probe")
            self.assertNotIn("sqlite", str(cm.exception).lower())
            self.assertEqual(store.describe()["session_open"], False)
            # subsequent operations reconnect exactly once, bounded
            self.assertEqual(store.execute("SELECT 1").fetchone()[0], 1)
            self.assertEqual(store.reconnect_count, 1)
            # same connection lifecycle, not a per-operation connection
            self.assertEqual(store.execute("SELECT 5").fetchone()[0], 5)
            self.assertEqual(store.reconnect_count, 1)
        finally:
            store.close()
            killer.close()

    def test_no_session_leak_and_failure_does_not_reexecute(self) -> None:
        from phase3.persistence.postgres import PostgresStore

        probe = self._fresh_store()
        a = self._fresh_store()
        b = self._fresh_store()
        try:
            def live_sessions() -> int:
                row = probe.execute(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE application_name = 'fie-phase3b'"
                    " AND pid <> pg_backend_pid()"
                ).fetchone()
                return int(row[0])

            before_cycle = live_sessions()
            writer_seen = []
            for _ in range(3):
                a_pid = self._backend_pid(a)
                self._terminate(b, a_pid)
                with self.assertRaises(Exception):
                    a.execute("SELECT 1 as cycleprobe")
                a.execute("SELECT 1")  # reconnects
                writer_seen.append(a.reconnect_count)
            self.assertEqual(writer_seen, [1, 2, 3],
                             "each recovery is exactly one bounded reconnect")
            # failed statements were never re-executed: reconnects are
            # strictly additive per recovery (no silent retries on top)
            after_cycle = live_sessions()
            self.assertEqual(after_cycle, before_cycle,
                             "no dead-session accumulation between cycles")
        finally:
            probe.close()
            a.close()
            b.close()

    # -- managed transaction semantics --------------------------------------

    def test_transaction_rolls_back_and_reuses_the_session(self) -> None:
        store = self._fresh_store()
        try:
            store.execute(
                "CREATE TABLE IF NOT EXISTS r3_rollback_probe (v INTEGER)"
            )
            try:
                with store.transaction() as conn:
                    conn.execute("INSERT INTO r3_rollback_probe VALUES (42)")
                    raise RuntimeError("synthetic body failure")
            except RuntimeError:
                pass
            rows = store.execute(
                "SELECT count(*) FROM r3_rollback_probe"
            ).fetchone()
            self.assertEqual(int(rows[0]), 0, "body failure must roll back")
            # commit path on the SAME session
            with store.transaction() as conn:
                conn.execute("INSERT INTO r3_rollback_probe VALUES (42)")
            self.assertEqual(store.execute(
                "SELECT count(*) FROM r3_rollback_probe").fetchone()[0], 1)
            store.execute("DROP TABLE r3_rollback_probe")
        finally:
            store.close()

    def test_describe_reports_reconnect_and_open_session(self) -> None:
        info = self.own_store.describe()
        self.assertEqual(info["backend"], "postgres")
        self.assertIn("reconnect_count", info)
        self.assertIn("session_open", info)
        self.assertEqual(info["session_open"], True)
        blob = str(info)
        self.assertNotIn("password=", blob)
        self.assertNotIn("secret", blob)

    # -- error classification (unit, no cluster needed to reason about) ------

    def test_sqlstate_first_classification(self) -> None:
        from phase3.persistence.postgres import _is_connection_level

        class _Fake(BaseException):
            def __init__(self, sqlstate: str | None) -> None:
                self.sqlstate = sqlstate

        # statement-level errors NEVER kill the session
        self.assertFalse(_is_connection_level(_Fake("57014"), self.psycopg))
        self.assertFalse(_is_connection_level(_Fake("42601"), self.psycopg))
        self.assertFalse(_is_connection_level(_Fake("23505"), self.psycopg))
        # connection-level states and classes DO
        for state in ("08A01", "08006", "57P01", "57P02", "57P03"):
            self.assertTrue(_is_connection_level(_Fake(state), self.psycopg), state)
        # sqlstate-less transport faults: fall back to the class name
        self.assertTrue(
            _is_connection_level(self._named(RuntimeError, "OperationalError"),
                                 self.psycopg)
        )
        self.assertFalse(
            _is_connection_level(self._named(RuntimeError, "IntegrityError"),
                                 self.psycopg)
        )

    @staticmethod
    def _named(base: type, name: str) -> Exception:
        exc = type(name, (base,), {})(name)
        return exc


if __name__ == "__main__":
    unittest.main()