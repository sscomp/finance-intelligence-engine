#!/usr/bin/env python3
"""WO C2 — raw-layer (db.py) PostgreSQL backend tests.

The legacy history layer (``db.py``: macro_daily / stock_monthly /
institutional_daily) must accept the same backend selection rule as
the Phase 3B persistence layer:

* a ``postgres://`` / ``postgresql://`` specifier selects PostgreSQL —
  first-class (direct driver), no live-SQLite dependency behind a
  wrapper (WO §8 Compatibility Requirement);
* an unreachable PostgreSQL endpoint fails closed: raises, never
  reports success, and NEVER materializes or writes a SQLite file;
* the SQLite path keeps its historical behavior (production rollback
  and testing stay possible).

All PostgreSQL legs target the DISPOSABLE local cluster dedicated
databases created and dropped per run (the repo's Phase 6.3
convention); production is never touched.
"""
from __future__ import annotations

import os
import sqlite3
import unittest
import uuid
from datetime import date, timedelta
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(REPO_ROOT))

import db  # repo-root raw layer module


def _test_pg_endpoint() -> tuple[str, int]:
    """Host/port (or socket dir, port) of the disposable test PG cluster.

    6.8C: derived from ``FIE_TEST_PG_DSN`` when set (the override actually
    steers these legs now), else the documented disposable default
    ``host=/tmp/fie-pg&port=54329``. No operator-host binary path.
    """
    from urllib.parse import urlsplit, parse_qs
    dsn = os.environ.get("FIE_TEST_PG_DSN", "")
    if dsn:
        parts = urlsplit(dsn)
        host = (parse_qs(parts.query).get("host") or
                ([parts.hostname] if parts.hostname else ["/tmp/fie-pg"]))[0]
        port = int((parse_qs(parts.query).get("port") or
                    ([parts.port] if parts.port else ["54329"]))[0])
        if host == "127.0.0.1" or host == "localhost":
            host = host  # TCP loopback; pg_isready accepts hostnames fine
        return host, port
    return "/tmp/fie-pg", 54329


def _test_pg_user() -> str:
    """Admin/role user of the disposable test cluster.

    6.9A-R3: the canonical provisioner owns per-run synthetic roles, so the
    user follows ``FIE_TEST_PG_DSN`` userinfo (never hardcoded 'fie' — the
    provisioner's roles are random per job); the static disposable default
    keeps its historical 'fie'.
    """
    dsn = os.environ.get("FIE_TEST_PG_DSN", "")
    if dsn:
        from urllib.parse import urlsplit
        user = urlsplit(dsn).username
        if user:
            return user
    return "fie"


def _pg_available() -> tuple[bool, str]:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False, "psycopg not installed"
    try:
        from phase3.service.pg_runtime_preflight import _pg_bin
        import subprocess
        host, port = _test_pg_endpoint()
        proc = subprocess.run(
            [_pg_bin("pg_isready"), "-h", str(host),
             "-p", str(port)], capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            return False, "disposable cluster not accepting connections"
    except Exception as exc:  # noqa: BLE001
        return False, f"disposable cluster unusable: {exc}"
    return True, ""


PG_OK, PG_SKIP_REASON = _pg_available()
UNREACHABLE_PG_DSN = (
    "postgresql://fie_nothere@127.0.0.1:1/db"
    "?host=127.0.0.1&port=1&connect_timeout=1"
)


def _pg_dsn_for(name: str) -> str:
    # 6.8C: the per-run database name is still used for the disposable
    # databases created below, but host/port/socket-dir now follow the
    # FIE_TEST_PG_DSN override when set (portable test-host provisioning).
    # 6.9A-R3: the role user follows the DSN userinfo as well.
    host, port = _test_pg_endpoint()
    return f"postgresql://{_test_pg_user()}@/{name}?host={host}&port={port}"


@unittest.skipUnless(PG_OK, PG_SKIP_REASON)
class RawLayerPostgresTests(unittest.TestCase):
    """First-class PostgreSQL raw layer against the disposable cluster."""

    @classmethod
    def _ADMIN_DSN(cls) -> str:
        host, port = _test_pg_endpoint()
        return (f"postgresql://{_test_pg_user()}@/postgres"
                f"?host={host}&port={port}")

    @classmethod
    def _admin(cls, sql: str) -> None:
        import psycopg
        with psycopg.connect(cls._ADMIN_DSN(), autocommit=True) as conn:
            conn.execute(sql)

    @classmethod
    def setUpClass(cls) -> None:
        run = uuid.uuid4().hex[:8]
        cls.db_name = f"fie_c2_raw_{run}"
        cls.dsn = _pg_dsn_for(cls.db_name)
        cls._admin(f"DROP DATABASE IF EXISTS {cls.db_name} WITH (FORCE)")
        cls._admin(f"CREATE DATABASE {cls.db_name}")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._admin(f"DROP DATABASE IF EXISTS {cls.db_name} WITH (FORCE)")
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass

    def _use_pg(self):
        return mock.patch.object(db, "DB_PATH", self.dsn)

    def test_backend_selection(self) -> None:
        # Phase 6.8A: outside an explicit target the raw layer reports
        # "unresolved" (fail closed at use) — never a silent SQLite
        # CWD fallback.
        with self._use_pg():
            self.assertEqual(db.raw_layer_backend(), "postgres")
        self.assertEqual(db.raw_layer_backend(), "unresolved")

    def test_init_and_saves_upsert_on_pg(self) -> None:
        with self._use_pg():
            self.assertEqual(db.raw_layer_backend(), "postgres")
            macro = {
                "US10Y": {"date": "2026-10-04", "value": 4.05},
                "US2Y": {"date": "2026-10-04", "value": 3.9},
                "US13W": {"date": "2026-10-04", "value": 4.6},
                "DXY": {"date": "2026-10-04", "value": 101.2},
                "VIX": {"date": "2026-10-04", "value": 13.9},
                "USDTWD": {"date": "2026-10-04", "value": 32.1},
                "YIELD_SPREAD": {"date": "2026-10-04", "value": 0.15},
            }
            db.save_macro_daily(macro, "neutral", 66, [{"k": "v"}])
            db.save_macro_daily({**macro, "US10Y": {"date": "2026-10-04",
                                                    "value": 4.08}},
                                "cautious", 63, [])
            # same date -> UPSERT (not duplicate row), values replaced
            hist = db.get_macro_history(days=5)
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]["us10y"], 4.08)
            self.assertEqual(hist[0]["score"], 63)

            db.save_stock_monthly(
                "2330", "TSMC", "semiconductor",
                {"price": 1000.0, "pe_trailing": 22.0,
                 "pb_ratio": 5.5, "market_cap": 1.2e12})
            # repeat same (date, code): upsert keeps a single row
            db.save_stock_monthly(
                "2330", "TSMC", "semiconductor", {"pe_trailing": 22.5})
            db.save_institutional_snapshot("2330", 500, -100, 400, 5)
            db.save_institutional_snapshot("2330", 620, -90, 530, 5)

            # verify directly through a fresh connection
            conn = db.get_db()
            rows = conn.cursor().execute(
                "SELECT code, pe_trailing FROM stock_monthly"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(float(rows[0]["pe_trailing"]), 22.5)
            inst = conn.cursor().execute(
                "SELECT foreign_net, prop_net, total_net, trading_days"
                " FROM institutional_daily").fetchall()
            self.assertEqual(len(inst), 1)
            self.assertEqual(int(inst[0]["foreign_net"]), 620)
            conn.close()

    def test_queries_on_pg(self) -> None:
        """Date-window query twins return rows the SQLite dialect yields."""
        with self._use_pg():
            today = date.today().isoformat()
            import psycopg
            conn = psycopg.connect(self.dsn, autocommit=True)
            rows_tpl = [
                (today, 100), (str(date.today() - timedelta(days=1)), 80),
                (str(date.today() - timedelta(days=2)), 60),
                (str(date.today() - timedelta(days=3)), 40),
            ]
            for d, net in rows_tpl:
                conn.execute(
                    "INSERT INTO institutional_daily (date, code,"
                    " foreign_net, prop_net, total_net, trading_days)"
                    " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (date, code)"
                    " DO UPDATE SET foreign_net = EXCLUDED.foreign_net",
                    (d, "9999", net, 0, net, 1))
            conn.close()
            streak, direction = db.get_foreign_streak("9999", days=14)
            self.assertEqual(streak, 3)
            self.assertEqual(direction, "買超")

            # PE percentile needs >= 3 positive trailing-PE points where
            # the newest is current
            conn = psycopg.connect(self.dsn, autocommit=True)
            for i, pe in enumerate([25.0, 20.0, 15.0]):
                conn.execute(
                    "INSERT INTO stock_monthly (date, code, pe_trailing)"
                    " VALUES (%s,'pe1',%s) ON CONFLICT (date, code)"
                    " DO UPDATE SET pe_trailing = EXCLUDED.pe_trailing",
                    (str(date.today() - timedelta(days=30 * i)), pe))
            conn.close()
            pe = db.get_pe_percentile("pe1", months=3)
            self.assertEqual(pe["current_pe"], 25.0)
            self.assertEqual(pe["percentile"], 100.0)
            self.assertEqual(pe["data_points"], 3)


@unittest.skipUnless(PG_OK, PG_SKIP_REASON)
class RawLayerFailClosedTests(unittest.TestCase):
    """Unreachable PG endpoint: raise — no fallback, no bogus file."""

    def test_unreachable_pg_fails_closed_no_sqlite_file(self) -> None:
        tmp = Path("/tmp") / f"fie_c2_closed_{uuid.uuid4().hex[:8]}"
        tmp.mkdir()
        try:
            with mock.patch.object(db, "DB_PATH", UNREACHABLE_PG_DSN):
                self.assertEqual(db.raw_layer_backend(), "postgres")
                with self.assertRaises(Exception) as cm:
                    db.init_db()
                self.assertFalse(isinstance(cm.exception, sqlite3.OperationalError),
                                 "PG failure must not surface as a local-file error")
            for name in tmp.iterdir():
                self.assertNotEqual(name.name, "macro_history.db")
                self.assertFalse(
                    name.name.startswith("postgres"),
                    f"bogus file materialized: {name}",
                )
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class RawLayerSqliteRegressionTests(unittest.TestCase):
    """The SQLite path keeps its historical behavior (rollback safety)."""

    def test_sqlite_backend_unchanged(self) -> None:
        tmp = Path("/tmp") / f"fie_c2_sqlite_{uuid.uuid4().hex[:8]}"
        tmp.mkdir()
        try:
            path = str(tmp / "macro_history.db")
            with mock.patch.object(db, "DB_PATH", path):
                self.assertEqual(db.raw_layer_backend(), "sqlite")
                db.init_db()
                db.save_macro_daily(
                    {"US10Y": {"date": "2026-10-04", "value": 4.2}},
                    "neutral", 70, [])
                db.save_macro_daily(
                    {"US10Y": {"date": "2026-10-04", "value": 4.3}},
                    "neutral", 71, [])
                # INSERT OR REPLACE semantics preserved: single row
                with sqlite3.connect(path) as conn:
                    n, latest = conn.execute(
                        "SELECT count(*), max(us10y) FROM macro_daily"
                    ).fetchone()
                self.assertEqual(n, 1)
                self.assertEqual(latest, 4.3)
                self.assertEqual(len(db.get_macro_history(7)), 1)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_unresolved_raises_fail_closed(self) -> None:
        """6.8A: with no raw-target contract in the environment the module
        default is UNRESOLVED (fail closed) — never a CWD default."""
        with mock.patch.object(db, "DB_PATH", None):
            with self.assertRaises(db.FailClosedTarget):
                db.raw_db_spec()

    def test_default_spec_is_a_path_not_a_dsn(self) -> None:
        with mock.patch.object(db, "DB_PATH", "/tmp/fie_default_path_probe.db"):
            self.assertFalse(db._is_pg_spec(db.raw_db_spec()))

    def test_env_dsn_flows_through_paths_boundary(self) -> None:
        """The DSN override must survive the path boundary VERBATIM.

        Path-normalization would mangle ``postgres://x`` into
        ``postgres:/x`` (a silently broken specifier) — the boundary
        exposes the raw spec for backend-aware callers.
        """
        from phase3.paths import macro_history_db_spec
        with mock.patch.dict(os.environ,
                             {"FIE_DB_PATH": "postgres://fie@/x?host=/h"}):
            spec = macro_history_db_spec()
            self.assertEqual(spec, "postgres://fie@/x?host=/h")
            self.assertTrue(db._is_pg_spec(spec))


if __name__ == "__main__":
    unittest.main(verbosity=2)