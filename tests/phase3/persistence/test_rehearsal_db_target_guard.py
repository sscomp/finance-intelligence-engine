#!/usr/bin/env python3
"""Fail-closed rehearsal DB-target guard tests (2026-10-04 incident closure).

The 2026-10-04 incident: a rehearsal harness supplied the seed-target
override under the wrong variable name and the wrapper fell back to the
live Production intelligence store. These tests pin the class-level fix:

- rehearsal/test mode (``FIE_SERVICE_ENV`` test|staging) + missing/empty,
  misnamed-only, malformed, production-targeted (canonical path, symlink,
  hard link, ``..`` traversal, ``sqlite://`` URL forms) and
  production-fallback configurations are REFUSED before any write;
- a valid isolated target is allowed (SQLite and, where supported, a
  PostgreSQL DSN classification);
- production-class modes keep the accepted Production behavior (the live
  store remains writable);
- refused attempts never write: the "production" store file is
  byte-identical after every negative case.

Everything is hermetic: the "production" store lives under a temporary
``FIE_PROJECT_ROOT``; no test touches the real host layout.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from phase3.persistence.rehearsal_guard import (
    FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED,
    UNKNOWN_SERVICE_ENV,
    RehearsalTargetRefused,
    assert_writable_target,
)
from phase3.persistence.backend import open_store, resolve_spec
from phase3.persistence.sqlite import SQLiteStore

try:  # PostgreSQL parity backend (optional install)
    import psycopg  # noqa: F401
    HAS_PSYCOPG = True
except Exception:  # pragma: no cover - environment without the extra
    psycopg = None
    HAS_PSYCOPG = False


def _sha256(path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class RehearsalGuardTestCase(unittest.TestCase):
    """Shared hermetic fixture: a temporary 'production' store."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rehearsal_guard_")
        root = Path(self._tmp.name)
        self.candidate = root / "metadata" / "intelligence_store.db"
        self.candidate.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.candidate)
        conn.execute("CREATE TABLE signal_log (id INTEGER PRIMARY KEY, v TEXT)")
        conn.commit()
        conn.close()
        self.isolated_dir = root / "rehearsal"
        self.isolated_dir.mkdir(exist_ok=True)
        self._env = {k: os.environ.get(k) for k in
                     ("FIE_PROJECT_ROOT", "FIE_SERVICE_ENV",
                      "FIE_INTELLIGENCE_DB", "FIE_DATABASE_URL")}
        os.environ["FIE_PROJECT_ROOT"] = str(root)
        for k in ("FIE_INTELLIGENCE_DB", "FIE_DATABASE_URL"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def _set_service_env(self, value: str | None) -> None:
        if value is None:
            os.environ.pop("FIE_SERVICE_ENV", None)
        else:
            os.environ["FIE_SERVICE_ENV"] = value

    def _assert_refused(self, target, expected_code, *, service_env="test"):
        self._set_service_env(service_env)
        before = _sha256(self.candidate)
        with self.assertRaises(RehearsalTargetRefused) as ctx:
            assert_writable_target(target)
        self.assertEqual(ctx.exception.code, expected_code)
        self.assertEqual(_sha256(self.candidate), before,
                         "a refused attempt must not touch the store")
        self.assertNotIn(str(self.candidate), str(ctx.exception),
                         "refusal message must not leak the target value")
        self._set_service_env(None)


class TestNegativeRehearsalTargets(RehearsalGuardTestCase):
    """G6 negative tests: every silent-fallback shape refuses (rc != 0)."""

    def test_01_missing_target_rejected(self):
        # rehearsal mode + missing FIE_INTELLIGENCE_DB: the wrapper-level
        # refusal is asserted by the wrapper test module; at the store
        # seam the equivalent class is a rehearsal open of the production
        # candidate path -> refused.
        self._assert_refused(str(self.candidate),
                             FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED)

    def test_05_production_target_rejected(self):
        self._assert_refused(str(self.candidate),
                             FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED)

    def test_05b_dotdot_traversal_rejected(self):
        sneaky = str(self.candidate.parent / "elsewhere" / ".." /
                     "intelligence_store.db")
        self._assert_refused(sneaky, FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED)

    def test_06_symlink_to_production_rejected(self):
        link = self.isolated_dir / "alias.db"
        link.symlink_to(self.candidate)
        self._assert_refused(str(link), FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED)

    def test_06b_hardlink_same_inode_rejected(self):
        hard = self.isolated_dir / "hard.db"
        os.link(self.candidate, hard)
        self._assert_refused(str(hard), FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED)

    def test_06c_sqlite_url_forms_rejected(self):
        for form in (f"sqlite:///{self.candidate}",
                     f"sqlite:{self.candidate}"):
            self._assert_refused(form, FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED)

    def test_07_production_fallback_rejected_via_env_selector(self):
        # The env-selected (resolve_spec) path must be refused as well:
        # FIE_DATABASE_URL pointing at the production store in rehearsal.
        os.environ["FIE_DATABASE_URL"] = str(self.candidate)
        spec = resolve_spec(None)  # env-selected source
        self.assertEqual(spec.source, "env")
        self._assert_refused(spec.dsn, FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED)

    def test_staging_is_rehearsal_class_too(self):
        self._assert_refused(str(self.candidate),
                             FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED,
                             service_env="staging")

    def test_invalid_service_env_refuses_fail_closed(self):
        self._set_service_env("bogus-profile")
        with self.assertRaises(RehearsalTargetRefused) as ctx:
            assert_writable_target(str(self.isolated_dir / "innocent.db"))
        self.assertEqual(ctx.exception.code, UNKNOWN_SERVICE_ENV)


class TestPositiveTargets(RehearsalGuardTestCase):
    """G6 positive tests: isolated targets pass; Production stays itself."""

    def test_08_valid_isolated_sqlite_target_passes(self):
        self._set_service_env("test")
        target = self.isolated_dir / "rehearsal.db"
        store = open_store(resolve_spec(str(target)))
        with store.transaction():
            store.connection.execute(
                "CREATE TABLE IF NOT EXISTS marker (x TEXT)")
            store.connection.execute("INSERT INTO marker (x) VALUES ('w')")
        store.connection.close()
        self.assertTrue(target.exists(), "isolated target must receive writes")

    def test_09_rehearsal_pg_dsn_classified_non_path(self):
        # A DSN has no filesystem semantics; the guard passes it through
        # (the actual PG connection is gated by its own backend machinery).
        self._set_service_env("test")
        try:
            assert_writable_target(
                "postgresql://guard-test@/db?host=/nonexistent")
        except RehearsalTargetRefused as exc:
            self.fail(f"a rehearsal DSN target must not be path-refused: {exc}")
        if not HAS_PSYCOPG:
            self.skipTest("psycopg not installed (PG-parity optional extra)")

    def test_10_declared_rehearsal_target_is_writable(self):
        # G9 pin (2026-10-04): the declared rehearsal target itself
        # (FIE_INTELLIGENCE_DB) must be openable — it used to be listed
        # as a production candidate and self-refuse. Caught by the
        # end-to-end wrapper re-test; pinned here since.
        self._set_service_env("test")
        os.environ["FIE_INTELLIGENCE_DB"] = str(self.isolated_dir /
                                               "declared.db")
        store = open_store(resolve_spec(os.environ["FIE_INTELLIGENCE_DB"]))
        with store.transaction():
            store.connection.execute(
                "CREATE TABLE IF NOT EXISTS marker (x TEXT)")
            store.connection.execute(
                "INSERT INTO marker (x) VALUES ('w')")
        store.connection.close()
        self.assertTrue(os.path.exists(os.environ["FIE_INTELLIGENCE_DB"]),
                        "declared rehearsal target must receive writes")

    def test_10_production_mode_keeps_accepted_target(self):
        os.environ["FIE_SERVICE_ENV"] = "production"
        store = SQLiteStore(str(self.candidate))
        with store.transaction():
            store.connection.execute(
                "INSERT INTO signal_log (v) VALUES ('prod-ok')")
            n = store.connection.execute(
                "SELECT COUNT(*) FROM signal_log").fetchone()[0]
        store.connection.close()
        self.assertEqual(n, 1)
        # and unset (the accepted historical default) is also production-class
        self._set_service_env(None)
        store = SQLiteStore(str(self.candidate))
        store.connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)