"""Phase 6.7B-R3 — /readyz schema gate over HTTP (SQLite, deterministic).

Proves the integrated readiness contract (Task D) on the outermost
surface, in TOKEN mode (the production boundary shape):

* healthy schema             → /readyz 200;
* tampered schema states      → /readyz 503 with the STABLE
  ``SCHEMA_INCOMPATIBLE`` code — never a raw driver exception;
* /healthz stays 200 ``alive`` through EVERY readiness failure
  (liveness strictly independent of the persistent store, R2/ADR-018);
* authentication is NOT weakened by readiness failure: credentialless
  and invalid-bearer callers still get 401 on /readyz and /v1/health —
  a failing dependency never becomes an auth bypass;
* production-like env still refuses absent / relative / unsupported
  DSNs (R1/ADR-017 preservation), and an unreachable PostgreSQL DSN
  fails closed, bounded, with NO SQLite fallback.

Tampering mutates the disposable SQLite database through a second
writable connection; each scenario class re-bootstraps its own
database, so cases stay independent and deterministic.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tests.phase3.transport import runtime_harness  # noqa: E402

#: Synthetic credential — never a real secret (work order §7).
R3_TOKEN = "r3-fixture-synthetic-token"


class _ReadyzContractSuite(runtime_harness.RuntimeHarness):
    """Shared assertions for the readiness gate matrix (token mode)."""

    tamper_sql: tuple[str, ...] = ()
    config_overrides = {"auth_mode": "token", "auth_token": R3_TOKEN}

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if cls.tamper_sql:
            conn = __import__("sqlite3").connect(str(cls.bundle["path"]))
            try:
                for sql in cls.tamper_sql:
                    conn.executescript(sql)
                conn.commit()
            finally:
                conn.close()

    def _with_bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {R3_TOKEN}"}

    def assert_readyz_503(self, expected_code: str) -> None:
        status, env, _ = self.get("/readyz", headers=self._with_bearer())
        self.assertEqual(status, 503, env)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], expected_code)
        blob = json.dumps(env)
        for fragment in ("sqlite:", "postgres", "SELECT", "schema_migrations("):
            self.assertNotIn(fragment, blob.lower())

    def assert_unauthenticated_still_401(self) -> None:
        # Readiness failure must not open an auth bypass: the
        # credentialless and wrong-bearer calls stay unauthorized on the
        # gate-gated surface AND on the detail health surface.
        status, env, _ = self.get("/readyz")
        self.assertEqual(status, 401)
        self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
        bad = self.get("/readyz", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(bad[0], 401)
        self.assertEqual(bad[1]["error"]["code"], "UNAUTHENTICATED")
        detail = self.get("/v1/health")
        self.assertEqual(detail[0], 401)
        self.assertEqual(detail[1]["error"]["code"], "UNAUTHENTICATED")

    def assert_liveness_never_fails(self) -> None:
        # §3.3: /healthz is independent of the store's state (R2).
        for _ in range(3):
            status, env, _ = self.get("/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(env["kind"], "healthz")
            self.assertEqual(env["status"], "alive")


class TestReadyzHealthyBaseline(_ReadyzContractSuite):
    def test_healthy_schema_is_ready(self) -> None:
        status, env, _ = self.get("/readyz", headers=self._with_bearer())
        self.assertEqual(status, 200)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["kind"], "health")
        self.assertEqual(env["payload"]["schema_current_version"], 1)
        self.assertEqual(env["warnings"], [])

    def test_healthy_liveness_and_auth(self) -> None:
        self.assert_liveness_never_fails()
        self.assert_unauthenticated_still_401()


class TestReadyzRegistryRowsCleared(_ReadyzContractSuite):
    tamper_sql = ("DELETE FROM schema_migrations;",)

    def test_readyz_fails_closed(self) -> None:
        self.assert_readyz_503("SCHEMA_INCOMPATIBLE")

    def test_liveness_independent_of_readiness_failure(self) -> None:
        self.assert_liveness_never_fails()

    def test_auth_not_weakened_by_readiness_failure(self) -> None:
        self.assert_unauthenticated_still_401()


class TestReadyzRegistryTableDropped(_ReadyzContractSuite):
    # Counts survive (application tables exist) — the GATE catches the
    # missing registry: the classic "reachable but never migrated" state.
    tamper_sql = ("DROP TABLE schema_migrations;",)

    def test_readyz_fails_closed(self) -> None:
        self.assert_readyz_503("SCHEMA_INCOMPATIBLE")

    def test_liveness_independent_of_readiness_failure(self) -> None:
        self.assert_liveness_never_fails()

    def test_auth_not_weakened_by_readiness_failure(self) -> None:
        self.assert_unauthenticated_still_401()


class TestReadyzFutureVersion(_ReadyzContractSuite):
    tamper_sql = (
        "INSERT INTO schema_migrations (version, name, checksum, applied_at,"
        " error) VALUES (999, 'from_the_future', "
        "'%s', '2026-01-01T00:00:00Z', NULL);" % ("0" * 64),
    )

    def test_readyz_fails_closed(self) -> None:
        self.assert_readyz_503("SCHEMA_INCOMPATIBLE")

    def test_liveness_independent_of_readiness_failure(self) -> None:
        self.assert_liveness_never_fails()


class TestReadyzChecksumTamper(_ReadyzContractSuite):
    tamper_sql = (
        "UPDATE schema_migrations SET checksum = "
        "'%s';" % ("f" * 64,),
    )

    def test_readyz_fails_closed(self) -> None:
        self.assert_readyz_503("SCHEMA_INCOMPATIBLE")

    def test_liveness_independent_of_readiness_failure(self) -> None:
        self.assert_liveness_never_fails()


class TestReadyzMigrationFailed(_ReadyzContractSuite):
    tamper_sql = ("UPDATE schema_migrations SET error = 'simulated failure';",)

    def test_readyz_fails_closed(self) -> None:
        self.assert_readyz_503("SCHEMA_INCOMPATIBLE")

    def test_liveness_independent_of_readiness_failure(self) -> None:
        self.assert_liveness_never_fails()


class TestReadyzNonCountedTableDropped(_ReadyzContractSuite):
    # Drop required objects the counts do NOT touch, so the GATE (not
    # count()) is what proves the failure — REQUIRED_OBJECTS_MISSING
    # classification exists for exactly this state.
    tamper_sql = ("DROP TABLE graph_edges; DROP TABLE graph_nodes;",)

    def test_readyz_fails_closed(self) -> None:
        self.assert_readyz_503("SCHEMA_INCOMPATIBLE")

    def test_liveness_independent_of_readiness_failure(self) -> None:
        self.assert_liveness_never_fails()

    def test_auth_not_weakened_by_readiness_failure(self) -> None:
        self.assert_unauthenticated_still_401()


class TestEnvironmentContractR3(_ReadyzContractSuite):
    """Production-like DSN environment boundaries (R1 preserved + R3).

    R1/ADR-017 accepted contract preserved: an explicit FIE_DATABASE_URL
    is required under staging/production and a CWD-relative SQLite
    value refuses. R3 adds the complementary refusals — an unsupported
    scheme must not silently classify as a SQLite path, and an
    unreachable PostgreSQL DSN fails closed + bounded with NO SQLite
    fallback anywhere.
    """

    def test_unsupported_scheme_refuses_in_production(self) -> None:
        import os

        from phase3.service.runtime_config import ConfigurationError
        from phase3.transport import load_transport_config
        from phase3.transport.http import run_server

        for bad in ("postgrex://host/db", "db://host/x", "file://etc/passwd"):
            with self.subTest(bad=bad):
                saved = {
                    k: os.environ.get(k)
                    for k in ("FIE_SERVICE_ENV", "FIE_AUTH_MODE",
                              "FIE_AUTH_TOKEN", "FIE_DATABASE_URL")
                }
                os.environ.update(
                    FIE_SERVICE_ENV="production",
                    FIE_AUTH_MODE="token",
                    FIE_AUTH_TOKEN="r3-fixture-token",
                    FIE_DATABASE_URL=bad,
                )
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        run_server(load_transport_config())
                    self.assertEqual(cm.exception.code, "DATABASE_URL_INVALID")
                    # refusal never echoes the value (ADR-017 §4.4)
                    self.assertNotIn(bad, str(cm.exception))
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v

    def test_unreachable_pg_dsn_fails_closed_bounded_and_is_not_sqlite(self) -> None:
        # A postgres://-scheme DSN stays PostgreSQL: an unavailable
        # server fails at open, bounded, with the driver error — and
        # no SQLite fallback file is created anywhere.
        import time

        from phase3.persistence.backend import open_store, resolve_spec

        spec = resolve_spec("postgresql://fie@127.0.0.1:9999/r3_closed")
        self.assertEqual(spec.backend, "postgres")
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg not installed in this environment")
        started = time.monotonic()
        with self.assertRaises(Exception) as cm:
            open_store(spec)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 15.0, "connect failure must be bounded")
        self.assertEqual(type(cm.exception).__name__, "OperationalError")
        self.assertNotIn("sqlite", str(cm.exception).lower())
        # determinism: repeated opens fail the same closed way — the
        # driver never degrades into a fallback backend file
        started = time.monotonic()
        with self.assertRaises(Exception) as cm2:
            open_store(spec)
        self.assertLess(time.monotonic() - started, 15.0)
        self.assertEqual(type(cm2.exception).__name__, "OperationalError")

    def test_explicit_dsn_argument_beats_local_env(self) -> None:
        # The precedence contract (resolve_spec) keeps an explicit
        # argument authoritative over the environment — the fallback
        # hazard is precisely when env leaks into explicit resolution.
        from phase3.persistence.backend import is_pg_dsn, resolve_spec
        from unittest import mock

        with mock.patch.dict(
            "os.environ", {"FIE_DATABASE_URL": "postgresql://env/db"}
        ):
            spec = resolve_spec("/tmp/explicit-wins.db")
        self.assertEqual(spec.backend, "sqlite")
        self.assertTrue(is_pg_dsn("postgresql://env/db"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()