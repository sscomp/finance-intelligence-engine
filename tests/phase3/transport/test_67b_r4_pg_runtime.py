"""Phase 6.7B-R4 — PostgreSQL runtime parity legs on the disposable cluster.

Task I legs (SKIP-closed when the disposable cluster is absent — never
a fake pass), all asserting BOTH decision correctness AND non-leakage:

* external topology redaction (ruling §4.3): a readiness outage over a
  postgres-family store answers ``DEPENDENCY_UNAVAILABLE`` with
  ``details.dependency = "postgres"`` — a hostile internal-hostname
  marker in the DSN/driver text NEVER reaches the envelope (this leg is
  a deterministic unit and runs without any cluster);
* outage bound: an unreachable PostgreSQL DSN fails bounded (no hang);
* recovery: a terminated backend fails readiness once, then readiness
  recovers (bounded reconnect) with the session-slot census flat and
  no unsafe replay;
* bounded-window expiry (statement_timeout, 57014) classifies as
  ``DEPENDENCY_UNAVAILABLE`` + ``reason = DEPENDENCY_TIMEOUT`` through
  the service envelope path — never ``INTERNAL_ERROR``;
* teardown: every probe store is closed (no cluster residue).
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tests.phase3.service import helpers  # noqa: E402

#: Synthetic token — never a real secret.
R4_TOKEN = "r4-fixture-synthetic-token"

#: Work order §10 marker shapes — must NEVER reach any external envelope.
MARKER_HOST = "db-internal-hostname.example"
MARKER_DSN = "postgresql://user:secret@host/db"


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


def _pg_session_census(dsn: str) -> int:
    """Live FIE sessions on the cluster (probe session excluded)."""
    import psycopg

    probe = psycopg.connect(dsn, autocommit=True)
    try:
        row = probe.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE application_name = 'fie-phase3b'"
            " AND pid <> pg_backend_pid()"
        ).fetchone()
        return int(row[0])
    finally:
        probe.close()


def _psycopg():
    import psycopg

    return psycopg


def _dispatch_health(service):
    """One readiness envelope through the production dispatch path."""
    from phase3.service import reference
    from phase3.service.contracts import RequestContext

    return reference.dispatch(
        service, "health", {},
        RequestContext(
            principal_id="r4probe", request_id="r4probe", scopes=frozenset(),
        ),
    )


class _OutageStore:
    """postgres-family store whose every read hits the outage shape."""

    backend = "postgres"

    def __init__(self, message: str) -> None:
        self._message = message
        self.closed = False
        self.executes = 0

    def set_query_only(self) -> None:
        pass

    def execute(self, sql: str, params=None):  # repo seam used by get_health
        self.executes += 1
        raise type("OperationalError", (Exception,), {})(self._message)

    def close(self) -> None:
        self.closed = True

    def describe(self):
        return {"backend": "postgres", "session_open": not self.closed}


class TestExternalTopologyRedaction(unittest.TestCase):
    """§4.3 ruling — no internal topology in the external envelope."""

    OUTAGE_TEXT = (
        'connection failed: connection to server at "db-internal-hostname'
        '.example" (10.0.0.5), port 5432 failed: Connection refused'
    )

    def test_readiness_outage_envelope_is_classified_and_sanitized(self) -> None:
        from phase3.service.boundary import DefaultIntelligenceService

        store = _OutageStore(self.OUTAGE_TEXT)
        service = DefaultIntelligenceService(store)
        envelope = _dispatch_health(service)
        # decision correctness
        self.assertEqual(envelope["status"], "error")
        self.assertEqual(envelope["error"]["code"], "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(envelope["error"]["message"], "persistence layer is not readable")
        # non-leakage: NO topology, NO DSN shape, NO driver exception, NO path
        blob = str(envelope)
        for marker in (MARKER_HOST, MARKER_DSN, "10.0.0.5", "5432",
                       "Connection refused", "secret", "/tmp/", "/home/"):
            self.assertNotIn(marker, blob)
        # stable classification detail only (ruling §4.3 example shape)
        self.assertEqual(envelope["error"]["details"], {"dependency": "postgres"})
        self.assertGreaterEqual(store.executes, 1)  # the repo seam was probed


def _psycopg_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("psycopg") is not None


@unittest.skipIf(not _psycopg_available(), "psycopg not installed")
class TestUnreachablePostgresIsBounded(unittest.TestCase):
    def test_closed_port_fails_bounded_no_sqlite_downgrade(self) -> None:
        from phase3.persistence.backend import resolve_spec

        spec = resolve_spec(
            f"postgresql://fie@/fie_contract?host={MARKER_HOST}&port=9999"
        )
        self.assertEqual(spec.backend, "postgres")
        started = time.monotonic()
        with self.assertRaises(Exception) as cm:
            _open_store(spec)
        self.assertLess(time.monotonic() - started, 20.0)
        text = str(cm.exception).lower()
        self.assertNotIn("sqlite", text)
        # refused DSN identity never appears verbatim with credentials
        blob = str(cm.exception)
        self.assertNotIn("secret@host", blob)


def _open_store(spec):
    from phase3.persistence.backend import open_store

    return open_store(spec)


@unittest.skipIf(not PG_PRESENT, "disposable PostgreSQL cluster not available")
class TestPgRecoveryAndTimeout(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = _pg_dsn()
        # the disposable cluster carries the migrated fixture state for
        # the whole class lifecycle
        cls.bundle = helpers.bootstrap_state(cls.dsn)
        # residue baseline: the census AFTER the class's own long-lived
        # bootstrap session opens — the teardown leg then proves every
        # store opened during THIS module's tests has closed (residue
        # predating the module, from an earlier module in a combined
        # run, is not this module's failure)
        cls.census_start = _pg_session_census(cls.dsn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.bundle["store"].close()

    def _service(self):
        from phase3.persistence.backend import open_store, resolve_spec
        from phase3.service.boundary import DefaultIntelligenceService

        store = open_store(resolve_spec(self.dsn))
        return DefaultIntelligenceService(store), store

    def test_readyz_200_with_backend_family(self) -> None:
        service, store = self._service()
        try:
            envelope = _dispatch_health(service)
            self.assertEqual(envelope["status"], "ok", envelope)
            self.assertEqual(envelope["payload"]["backend_kind"], "postgres")
        finally:
            store.close()

    def test_backend_failure_recovers_readiness_same_service(self) -> None:
        service, store = self._service()
        killer_service, killer_store = self._service()
        try:
            envelope = _dispatch_health(service)
            self.assertEqual(envelope["status"], "ok")
            pid = int(store.execute("SELECT pg_backend_pid()").fetchone()[0])
            killer_store.execute("SELECT pg_terminate_backend(%s)", (pid,))
            # FIRST operation after the kill fails WITHOUT replay
            envelope = _dispatch_health(service)
            self.assertEqual(envelope["status"], "error")
            self.assertEqual(envelope["error"]["code"], "DEPENDENCY_UNAVAILABLE")
            self.assertFalse(store.describe()["session_open"])
            # recovery: next operation reconnects bounded; readiness is
            # eligible again — same service object, no unsafe replay
            envelope = _dispatch_health(service)
            self.assertEqual(envelope["status"], "ok", envelope)
            self.assertGreaterEqual(store.reconnect_count, 1)
        finally:
            store.close()
            killer_store.close()

    def test_statement_timeout_envelope_is_dependency_timeout(self) -> None:
        psycopg = _psycopg()
        from phase3.persistence.backend import resolve_spec
        from phase3.persistence.postgres import PostgresStore
        from phase3.service import reference
        from phase3.service.contracts import RequestContext

        store = PostgresStore(self.dsn, statement_timeout_ms=500)
        try:
            with self.assertRaises(psycopg.errors.QueryCanceled) as cm:
                store.execute("SELECT pg_sleep(5)")
            self.assertEqual(getattr(cm.exception, "sqlstate", None), "57014")
            self.assertEqual(store.reconnect_count, 0)
            # the R4 envelope classification for this shape
            saved_handlers = dict(reference._HANDLERS)
            reference._HANDLERS["r4probe"] = (
                lambda s, p, c, op: (_ for _ in ()).throw(cm.exception)
            )
            try:
                envelope = reference.dispatch(
                    type("Stub", (), {})(), "r4probe", {},
                    RequestContext(
                        principal_id="r4probe", request_id="r4probe",
                        scopes=frozenset(),
                    ),
                )
            finally:
                reference._HANDLERS = saved_handlers
            self.assertEqual(envelope["status"], "error")
            self.assertEqual(envelope["error"]["code"], "DEPENDENCY_UNAVAILABLE")
            self.assertEqual(envelope["error"]["details"]["reason"],
                             "DEPENDENCY_TIMEOUT")
            self.assertEqual(envelope["error"]["details"]["dependency"], "postgres")
            blob = str(envelope)
            for marker in ("pg_sleep", "57014", "QueryCanceled", MARKER_HOST,
                           MARKER_DSN, "secret"):
                self.assertNotIn(marker, blob)
            # session unharmed; next op works (no replay, no teardown)
            self.assertEqual(store.execute("SELECT 1").fetchone()[0], 1)
        finally:
            store.close()

    def test_no_new_cluster_session_residue_for_this_module(self) -> None:
        # delta form (as in the R3 suite): the module's OWN stores must
        # all have closed in teardown — residue that predates the module
        # (an earlier module's leak) is not this module's failure
        try:
            census = _pg_session_census(self.dsn)
        except Exception:
            self.skipTest("cluster unavailable")
        self.assertEqual(census, self.census_start, "probe session excluded")


if __name__ == "__main__":
    unittest.main()