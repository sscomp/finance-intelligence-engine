"""Phase 6.7B-R4 — ADR-017 SQLite deployment policy, machine-tested.

Architecture Review ruling §4.1 carried into R4: the ACCEPTED deployment
matrix is proven as a closed, machine-testable table — an explicit
ABSOLUTE SQLite path stays supported where ADR-017 permits it (including
the Phase 6.6R4 read-only deployment modes), while production-like
profiles REJECT implicit fallback, CWD-relative SQLite and unsupported
schemes, and PostgreSQL is never silently downgraded to SQLite.

Fail-closed shape of every case: deterministic refusal code, the DSN
value never echoed, and — after a PostgreSQL failure — no SQLite store
is EVER created or re-targeted.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service.runtime_config import ConfigurationError  # noqa: E402

MARKER = "PII_MARKER_R4"


def _env(**overrides: str | None) -> dict[str, str | None]:
    keys = (
        "FIE_DATABASE_URL", "FIE_SERVICE_ENV", "FIE_AUTH_MODE",
        "FIE_AUTH_TOKEN", "FIE_SQLITE_ACCESS_MODE",
    )
    saved = {key: os.environ.get(key) for key in keys}
    for key, value in overrides.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return saved


def _restore(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class _ProductionGate(unittest.TestCase):
    """Config-resolution gates for the deployment matrix rows."""

    def _load(self, **env):
        from phase3.transport import load_transport_config

        saved = _env(**env)
        try:
            return load_transport_config()
        finally:
            _restore(saved)


class TestDeploymentMatrix(_ProductionGate):
    def test_explicit_absolute_sqlite_allowed_in_local(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fie-r4-") as tmp:
            absolute = f"{tmp}/{MARKER}.db"
            config = self._load(FIE_DATABASE_URL=absolute)
            self.assertEqual(config.service_env, "local")
            from phase3.persistence.backend import resolve_spec

            spec = resolve_spec(absolute)
            self.assertEqual(spec.backend, "sqlite")
            self.assertEqual(spec.source, "explicit")

    def test_explicit_absolute_sqlite_allowed_in_production_like(self) -> None:
        # ADR-017 §4.4: an ABSOLUTE SQLite DSN is accepted under
        # production-like profiles (e.g. the immutable_snapshot
        # container contract) — it is the CWD-relative shape refused.
        with tempfile.TemporaryDirectory(prefix="fie-r4-") as tmp:
            absolute = f"{tmp}/{MARKER}.db"
            config = self._load(
                FIE_DATABASE_URL=absolute,
                FIE_SERVICE_ENV="staging",
                FIE_AUTH_MODE="token",
                FIE_AUTH_TOKEN="synthetic-staging-token",
            )
            self.assertEqual(config.service_env, "staging")
            # ...and it actually OPENS as SQLite through the production
            # composition root (immutable_snapshot deployment contract).
            from phase3.persistence.backend import resolve_spec, open_store
            from phase3.persistence.sqlite import SQLiteStore

            SQLiteStore(absolute).close()  # materialize the DB artifact
            spec = resolve_spec(absolute)
            store = open_store(spec, access_mode="immutable_snapshot")
            try:
                self.assertEqual(store.backend, "sqlite")
                store.execute("SELECT 1").fetchone()
            finally:
                store.close()

    def test_relative_sqlite_rejected_in_production_like(self) -> None:
        from phase3.transport.http import run_server
        from phase3.transport import load_transport_config

        saved = _env(
            FIE_DATABASE_URL=f"rel/{MARKER}.db",
            FIE_SERVICE_ENV="production",
            FIE_AUTH_MODE="token",
            FIE_AUTH_TOKEN="synthetic-prod-token",
        )
        try:
            config = load_transport_config()  # config resolution passes…
            with self.assertRaises(ConfigurationError) as cm:
                run_server(config)  # …the composition-root DSN gate refuses
            self.assertEqual(cm.exception.code, "DATABASE_URL_INVALID")
            self.assertNotIn(MARKER, cm.exception.message)
        finally:
            _restore(saved)

    def test_missing_db_config_rejected_in_production_like(self) -> None:
        for profile in ("staging", "production"):
            with self.subTest(profile=profile):
                with self.assertRaises(ConfigurationError) as cm:
                    self._load(
                        FIE_DATABASE_URL=None,
                        FIE_SERVICE_ENV=profile,
                        FIE_AUTH_MODE="token",
                        FIE_AUTH_TOKEN="synthetic-token",
                    )
                self.assertEqual(cm.exception.code, "DATABASE_URL_MISSING")
                self.assertNotIn("sqlite", cm.exception.message)

    def test_postgres_dsn_selects_postgres(self) -> None:
        from phase3.persistence.backend import resolve_spec

        spec = resolve_spec("postgresql://user:synthetic@pg.example/fie")
        self.assertEqual(spec.backend, "postgres")
        spec2 = resolve_spec("postgres://user:synthetic@pg.example/fie")
        self.assertEqual(spec2.backend, "postgres")
        # the production-like gate accepts the DSN SHAPE (no path rule)
        config = self._load(
            FIE_DATABASE_URL="postgres://user:synthetic@pg.example/fie",
            FIE_SERVICE_ENV="staging",
            FIE_AUTH_MODE="token",
            FIE_AUTH_TOKEN="synthetic-staging-token",
        )
        self.assertEqual(config.service_env, "staging")

    def test_malformed_db_url_refused_in_production_like(self) -> None:
        from phase3.transport.http import run_server

        # unsupported schemes are NOT SQLite-eligible absolute paths —
        # they resolve to a CWD-relative garbage path and the
        # composition-root gate refuses (R3-proved vocabulary)
        for url in (f"postgrex://user@{MARKER}/db", "db://fie.db",
                    f"file://{MARKER}.db"):
            with self.subTest(url=url):
                from phase3.transport import load_transport_config

                saved = _env(
                    FIE_DATABASE_URL=url,
                    FIE_SERVICE_ENV="production",
                    FIE_AUTH_MODE="token",
                    FIE_AUTH_TOKEN="synthetic-prod-token",
                )
                try:
                    config = load_transport_config()
                    with self.assertRaises(ConfigurationError) as cm:
                        run_server(config)
                    self.assertEqual(cm.exception.code, "DATABASE_URL_INVALID")
                    self.assertNotIn(MARKER, cm.exception.message)
                finally:
                    _restore(saved)

    def test_malformed_db_url_in_local_is_honest_sqlite(self) -> None:
        # local keeps the accepted compatibility boundary: the same
        # unsupported scheme resolves as a relative SQLite path — the
        # SQLite backend is SELECTED (no other backend silently chosen)
        # and a broken path fails at open, never by switching backend.
        from phase3.persistence.backend import resolve_spec, open_store

        workdir = tempfile.mkdtemp(prefix="fie-r4-scheme-")
        old_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            spec = resolve_spec("db://unusual_local.db")
            self.assertEqual(spec.backend, "sqlite")
            with self.assertRaises(Exception):
                open_store(spec)
        finally:
            os.chdir(old_cwd)
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)


def _psycopg_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("psycopg") is not None


@unittest.skipIf(not _psycopg_available(), "psycopg not installed")
class TestNoSilentPostgresToSqliteDowngrade(unittest.TestCase):
    """A PostgreSQL failure NEVER re-targets SQLite (ruling §4.1)."""

    def test_pg_connect_failure_leaves_no_sqlite_store(self) -> None:
        from pathlib import Path

        from phase3.persistence.backend import resolve_spec, open_store

        workdir = Path(tempfile.mkdtemp(prefix="fie-r4-nofallback-"))
        old_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            spec = resolve_spec(
                "postgresql://fie@/fie_contract"
                "?host=127.0.0.1&port=9999"
            )
            self.assertEqual(spec.backend, "postgres")
            for attempt in range(2):
                with self.assertRaises(Exception) as cm:
                    open_store(spec)
                text = str(cm.exception).lower()
                self.assertNotIn("sqlite", text)
                # second attempt fails identically — no downgrade path
            # and NOTHING silently appeared on disk in SQLite form
            left = [p.name for p in workdir.iterdir()]
            self.assertEqual(left, [], left)
        finally:
            os.chdir(old_cwd)
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()