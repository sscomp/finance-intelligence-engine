#!/usr/bin/env python3
"""WO C1 — backend-aware seed path tests (PostgreSQL Production Readiness).

Verifies the remediated ``phase3.bridge.seed_signals`` contract:

1. A PostgreSQL DSN selects the PostgreSQL backend for BOTH the seed
   target and the seed source — it is never interpreted as a filesystem
   path, and no bogus SQLite file is created (WO §7 negative test).
2. An unreachable PostgreSQL endpoint fails closed: the operation
   raises / the CLI exits non-zero; the failure can never surface as a
   successful zero-seed run.
3. The seed exposes machine-verifiable accounting:
   SOURCE_COUNT / SEEDED_COUNT / REJECTED_COUNT.
4. The CLI ``--min-seed-total`` floor (A2) fails non-zero when the
   seed yields fewer signals than the operator-declared floor, so
   ``rc == 0`` alone never defines a successful seed.
5. PostgreSQL end-to-end seeding (backend selector + per-backend
   schema twin) against the disposable cluster — skipped when psycopg
   or the cluster is absent (same gating contract as the Phase 6.3
   parity suite).

All PostgreSQL tests target the DISPOSABLE local cluster only
(``FIE_TEST_PG_DSN`` override; the /tmp default per the Phase 6.3
report). No cloud provisioning, no production databases.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from phase3.bridge.seed_signals import (
    PG_URL_PREFIXES,
    _is_pg_spec,
    _open_source,
    resolve_as_of_dates,
    seed_from_macro_history,
)
from phase3.persistence.backend import (
    BACKEND_POSTGRES,
    BACKEND_SQLITE,
    open_store,
    resolve_spec,
)

#: Unreachable-but-fast PostgreSQL DSN: TCP port 1 on loopback is
#: refused by the loopback stack (RST) with no real listener; a
#: bounded connect_timeout keeps the failure finite even behind a
#: silently dropping firewall.
UNREACHABLE_PG_DSN = (
    "postgresql://fie_gate_nothere@127.0.0.1:1/fie_gate_unreachable"
    "?host=127.0.0.1&port=1&connect_timeout=1"
)

PG_DSN = os.environ.get(
    "FIE_TEST_PG_DSN",
    "postgresql://fie@/fie_parity?host=/tmp/fie-pg&port=54329",
)


_RAW_DDL = """
        CREATE TABLE macro_daily (
            date TEXT PRIMARY KEY,
            us10y REAL, us2y REAL, us13w REAL,
            dxy REAL, vix REAL, usdtwd REAL,
            yield_spread REAL, score INTEGER, verdict TEXT,
            signals_json TEXT, created_at TEXT
        );
        CREATE TABLE stock_monthly (
            date TEXT, code TEXT, name TEXT, sector TEXT,
            price REAL, eps_ttm REAL, pe_trailing REAL, pe_forward REAL,
            roe REAL, roa REAL, gross_margin REAL, operating_margin REAL,
            profit_margin REAL, dividend_rate REAL, dividend_yield REAL,
            payout_ratio REAL, pb_ratio REAL, revenue_growth REAL,
            earnings_growth REAL, nim_growth REAL, interest_spread REAL,
            high_52 REAL, low_52 REAL, dist_from_high REAL,
            target_mean REAL, peg_ratio REAL, market_cap REAL,
            PRIMARY KEY (date, code)
        );
        CREATE TABLE institutional_daily (
            date TEXT, code TEXT, foreign_net INTEGER, prop_net INTEGER,
            total_net INTEGER, trading_days INTEGER,
            PRIMARY KEY (date, code)
        );
        """

_RAW_ROWS = """
        INSERT INTO macro_daily VALUES
            ('2026-07-28', 4.2, 4.0, 5.1, 100.5, 14.1, 32.4, -0.2, 70, 'neutral', '{}', 'x'),
            ('2026-07-29', 4.3, __BAD_US2Y__, 5.2, 100.1, 14.0, 32.3, -0.3, 71, 'neutral', '{}', 'x');
        INSERT INTO stock_monthly (date, code, pe_trailing, pb_ratio, market_cap)
            VALUES ('2026-07-29', '2330', 20.5, 5.0, 1.1e12);
        INSERT INTO institutional_daily (date, code, foreign_net, prop_net, total_net, trading_days)
            VALUES ('2026-07-29', '2330', 100, -20, 80, 3);
        """


def make_raw_source_script(bad_us2y: str = "'not-a-number'") -> str:
    """Return the shared raw-layer fixture DDL+DML (SQLite- and PG-safe).

    Two macro rows (one with a deliberately unusable ``us2y``), one
    stock row carrying only three of the nine company fields, and one
    institutional row — enough to exercise the accounting contract
    deterministically (same counts for both backends):

    * macro:    SOURCE 1 row, seeded 5, rejected 1 (unusable ``us2y``);
    * company:  SOURCE 1 row, seeded 3, rejected 6 (absent fields);
    * inst.:    SOURCE 1 row, seeded 2, rejected 0.

    ``bad_us2y`` is spliced in verbatim: SQLite uses a non-numeric
    string (exercising the ValueError branch), PostgreSQL a NULL
    (PG casts strings strictly; absence == rejection either way).
    """
    return _RAW_DDL + _RAW_ROWS.replace("__BAD_US2Y__", bad_us2y)


def _make_sqlite_source(path: str) -> None:
    """Build the raw-layer source fixture as a SQLite database."""
    conn = sqlite3.connect(path)
    conn.executescript(make_raw_source_script())
    conn.commit()
    conn.close()


def _make_empty_sqlite_source(path: str) -> None:
    """Build a schema-correct but row-free raw-layer source."""
    conn = sqlite3.connect(path)
    conn.executescript(_RAW_DDL)
    conn.commit()
    conn.close()


def _pg_available() -> tuple[bool, str]:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False, (
            "psycopg not installed; PostgreSQL parity backend is "
            "optional per Phase 6.3"
        )
    try:
        store = open_store(resolve_spec(PG_DSN))
        store.close()
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"disposable PostgreSQL cluster for {PG_DSN.split('?')[0]} not "
            f"reachable ({type(exc).__name__}); skip per Phase 6.3 §18"
        )
    return True, ""


PG_OK, PG_SKIP_REASON = _pg_available()


class BackendSelectionUnitTests(unittest.TestCase):
    """Selection rule unit tests (no server required)."""

    def test_pg_dsn_detection(self) -> None:
        for dsn in PG_URL_PREFIXES:
            self.assertTrue(_is_pg_spec(dsn + "x" ))
        self.assertTrue(_is_pg_spec("  POSTGRES://user@host/db"))
        self.assertFalse(_is_pg_spec("macro_history.db"))
        self.assertFalse(_is_pg_spec("mysql://user@host/db"))
        self.assertFalse(_is_pg_spec(None))

    def test_open_source_dispatches_postgres(self) -> None:
        """The source opener must dispatch on the spec, not the CWD."""
        # With psycopg absent, the PG path fails at the driver import —
        # which is itself the fail-closed behaviour; with psycopg
        # present but no server, the constructor fails at connect.
        # Dispatch evidence: the request NEVER becomes a sqlite3
        # connect on the DSN string.
        try:
            src = _open_source(UNREACHABLE_PG_DSN)
        except Exception as exc:  # noqa: BLE001 - expected: unreachable/import
            self.assertNotEqual(
                str(type(exc)), "<class 'sqlite3.OperationalError'>",
                "PG DSN must not be opened as a SQLite file path",
            )
            return
        # Reachable: must be the postgres backend with no file created.
        self.assertEqual(src.backend, "postgres")
        src.close()

    def test_open_source_dispatches_sqlite(self) -> None:
        tmp = tempfile.mkdtemp(prefix="fie_c1_sqlite_src_")
        path = os.path.join(tmp, "source.db")
        _make_sqlite_source(path)
        src = _open_source(path)
        self.assertEqual(src.backend, "sqlite")
        row = src.execute("SELECT * FROM macro_daily ORDER BY date DESC").fetchone()
        self.assertEqual(str(row["date"]), "2026-07-29")
        src.close()

    def test_no_bogus_file_for_pg_dsn(self) -> None:
        """A PG DSN left as the spec must not materialize a local file."""
        tmp = tempfile.mkdtemp(prefix="fie_c1_nofile_")
        cwd_guard = os.getcwd()
        os.chdir(tmp)
        try:
            try:
                seed_from_macro_history(
                    source_db=UNREACHABLE_PG_DSN,
                    target_db=os.path.join(tmp, "target.db"),
                    auto_resolve_dates=False,
                )
            except Exception:  # noqa: BLE001 - expected fail-closed
                pass
            for name in os.listdir(tmp):
                self.assertFalse(
                    name.startswith("postgres://") or name.startswith("postgresql://"),
                    f"bogus SQLite file materialized for the DSN: {name}",
                )
        finally:
            os.chdir(cwd_guard)
            tmpdir_entries = os.listdir(tmp)
        self.assertEqual(
            [p for p in tmpdir_entries if p.startswith("target.db")], [],
            "target store must not be created when the source fails closed",
        )

    def test_target_pg_dsn_not_a_file_path(self) -> None:
        """The TARGET dispatch follows the same DSN rule."""
        spec = resolve_spec("postgresql://fie@/db?host=/tmp/fie-pg&port=54329")
        self.assertEqual(spec.backend, BACKEND_POSTGRES)
        spec = resolve_spec(
            UNREACHABLE_PG_DSN,
        )
        self.assertEqual(spec.backend, BACKEND_POSTGRES)
        self.assertFalse(spec.backend == BACKEND_SQLITE)


class SeedAccountingTests(unittest.TestCase):
    """SOURCE_COUNT / SEEDED_COUNT / REJECTED_COUNT contract (WO C1)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="fie_c1_accounting_")
        cls.source = os.path.join(cls.tmp, "source.db")
        _make_sqlite_source(cls.source)
        cls.target = os.path.join(cls.tmp, "target-intelligence.db")

    def test_accounting_keys_present_and_consistent(self) -> None:
        result = seed_from_macro_history(
            source_db=self.source,
            target_db=self.target,
            macro_date="2026-07-29",
            company_date="2026-07-29",
            institutional_date="2026-07-29",
            # Keep the accounting deterministic: the repo-root
            # industry_config.json would otherwise aggregate the
            # fixture's institutional row into industry signals.
            industry_config_path=os.path.join(self.tmp, "no-industry-config.json"),
        )
        self.assertIn("SOURCE_COUNT", result)
        self.assertIn("SOURCE_COUNT_TOTAL", result)
        self.assertIn("SEEDED_COUNT", result)
        self.assertIn("REJECTED_COUNT", result)
        self.assertIn("REJECTED_COUNT_TOTAL", result)
        self.assertEqual(result["total"], result["SEEDED_COUNT"])
        # The 'not-a-number' us2y value in the second macro row is the
        # only macro field-value rejection in this fixture.
        self.assertEqual(result["REJECTED_COUNT"]["macro"], 1)
        # The stock row carries only pe_trailing/pb_ratio/market_cap of
        # the nine mapped company fields -> six absent-field rejections.
        self.assertEqual(result["REJECTED_COUNT"]["company"], 6)
        self.assertEqual(result["REJECTED_COUNT_TOTAL"], 7)
        # Selected rows: macro window with explicit date = 1 row;
        # company + institutional 1 each; industry config disabled.
        self.assertEqual(result["SOURCE_COUNT"]["macro"], 1)
        self.assertEqual(result["SOURCE_COUNT"]["company"], 1)
        self.assertEqual(result["SOURCE_COUNT"]["institutional"], 1)
        # Field expansion: 6 macro fields -> 5 usable; company 3; inst 2.
        self.assertEqual(result["macro"], 5)
        self.assertEqual(result["company"], 3)
        self.assertEqual(result["institutional"], 2)
        self.assertEqual(result["SEEDED_COUNT"], 10)
        self.assertEqual(
            result["total"],
            result["macro"] + result["company"] + result["institutional"],
        )
        self.assertEqual(result["new_rows"], result["SEEDED_COUNT"])


class CliSeedFloorTests(unittest.TestCase):
    """A2 — CLI rc=0 must not define a successful seed by itself."""

    def _run_cli(self, args: list[str]) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "phase3.cli", *args],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=120,
        )

    def test_unreachable_pg_source_fails_nonzero_cli(self) -> None:
        tmp = tempfile.mkdtemp(prefix="fie_c1_cli_")
        target = os.path.join(tmp, "target-intelligence.db")
        result = self._run_cli([
            "pipeline-export",
            "--seed-from-history",
            "--source-db", UNREACHABLE_PG_DSN,
            "--db-path", target,
            "--output-dir", os.path.join(tmp, "out"),
        ])
        self.assertNotEqual(result.returncode, 0, result.stderr)
        for name in os.listdir(tmp):
            self.assertFalse(
                name.startswith("postgres://") or name.startswith("postgresql://"),
                f"bogus file materialized: {name}",
            )

    def test_min_seed_total_floor_fails_on_zero_seed(self) -> None:
        """Explicit floor: rc==0 can never stand for an empty seed."""
        tmp = tempfile.mkdtemp(prefix="fie_c1_floor_")
        source = os.path.join(tmp, "empty_source.db")
        _make_empty_sqlite_source(source)
        target = os.path.join(tmp, "target-intelligence.db")
        result = self._run_cli([
            "pipeline-export",
            "--seed-from-history",
            "--source-db", source,
            "--db-path", target,
            "--industry-config", os.path.join(tmp, "no-industry-config.json"),
            "--min-seed-total", "1",
            "--date", "2026-07-29",
            "--output-dir", os.path.join(tmp, "out"),
        ])
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("below the", result.stderr)

    def test_min_seed_total_passes_with_real_seed(self) -> None:
        """The same floor passes once the seed actually produces signals."""
        tmp = tempfile.mkdtemp(prefix="fie_c1_floor_ok_")
        source = os.path.join(tmp, "source.db")
        _make_sqlite_source(source)
        target = os.path.join(tmp, "target-intelligence.db")
        result = self._run_cli([
            "pipeline-export",
            "--seed-from-history",
            "--source-db", source,
            "--db-path", target,
            "--min-seed-total", "1",
            "--date", "2026-07-29",
            "--output-dir", os.path.join(tmp, "out"),
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SEEDED_COUNT", result.stderr)


class SeedPostgresEndToEndTests(unittest.TestCase):
    """Backend-selector flow against the disposable cluster (gated).

    Each run provisions its own dedicated databases on the disposable
    local cluster and drops them on teardown — no shared state, and
    nothing outside /tmp/fie-pg is touched.
    """

    ADMIN_DSN = "postgresql://fie@/postgres?host=/tmp/fie-pg&port=54329"
    DSN_TEMPLATE = "postgresql://fie@/{db}?host=/tmp/fie-pg&port=54329"

    @classmethod
    def _admin(cls, sql: str) -> None:
        import psycopg

        with psycopg.connect(cls.ADMIN_DSN, autocommit=True) as conn:
            conn.execute(sql)

    @classmethod
    def setUpClass(cls) -> None:
        if not PG_OK:
            raise unittest.SkipTest(PG_SKIP_REASON)
        cls.tmp = tempfile.mkdtemp(prefix="fie_c1_pg_")
        cls.sqlite_source = os.path.join(cls.tmp, "source.db")
        _make_sqlite_source(cls.sqlite_source)
        run = uuid.uuid4().hex[:8]
        cls.src_db = f"fie_c1_src_{run}"
        cls.tgt_a_db = f"fie_c1_tgt_a_{run}"
        cls.tgt_b_db = f"fie_c1_tgt_b_{run}"
        for db in (cls.src_db, cls.tgt_a_db, cls.tgt_b_db):
            cls._admin(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
            cls._admin(f"CREATE DATABASE {db}")
        cls.pg_src_dsn = cls.DSN_TEMPLATE.format(db=cls.src_db)
        cls.pg_tgt_a_dsn = cls.DSN_TEMPLATE.format(db=cls.tgt_a_db)
        cls.pg_tgt_b_dsn = cls.DSN_TEMPLATE.format(db=cls.tgt_b_db)
        # Populate the PG raw-layer source (same fixture, PG backend;
        # NULL spliced for the unusable us2y — same rejection accounting).
        from phase3.persistence.backend import open_store as _open_store
        from phase3.persistence.backend import resolve_spec as _resolve_spec

        store = _open_store(_resolve_spec(cls.pg_src_dsn))
        store.executescript(make_raw_source_script(bad_us2y="NULL"))
        store.close()

    @classmethod
    def tearDownClass(cls) -> None:
        for db in (getattr(cls, "src_db", None), getattr(cls, "tgt_a_db", None),
                   getattr(cls, "tgt_b_db", None)):
            if db:
                try:
                    cls._admin(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass

    def test_seed_sqlite_source_to_pg_target(self) -> None:
        """SQLite raw layer -> PostgreSQL intelligence store."""
        from phase3.persistence.schema_pg import build as pg_build

        result = seed_from_macro_history(
            source_db=self.sqlite_source,
            target_db=self.pg_tgt_a_dsn,
            macro_date="2026-07-29",
            company_date="2026-07-29",
            institutional_date="2026-07-29",
            industry_config_path=os.path.join(self.tmp, "no-industry-config.json"),
        )
        self.assertGreater(result["SEEDED_COUNT"], 0, result)
        self.assertEqual(result["backend_target"], "postgres")
        self.assertEqual(result["source_kind"], "sqlite")
        self.assertEqual(result["SEEDED_COUNT"], 10)
        self.assertEqual(result["REJECTED_COUNT_TOTAL"], 7)

        # Registry identity: the PG store carries the PG migration
        # checksum (per-backend registry contract, WO C4) — NOT the
        # SQLite checksum.
        store = open_store(resolve_spec(self.pg_tgt_a_dsn))
        rows = store.execute(
            "SELECT version, checksum, error FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        self.assertTrue(rows)
        self.assertEqual(int(rows[0][0]), 1)
        self.assertEqual(rows[0][1], pg_build().checksum)
        self.assertIsNone(rows[0][2])
        count = store.execute("SELECT count(*) FROM signal_log").fetchone()[0]
        self.assertEqual(count, result["SEEDED_COUNT"])
        store.close()

    def test_seed_pg_source_to_pg_target(self) -> None:
        """PostgreSQL raw layer -> PostgreSQL intelligence store."""
        result = seed_from_macro_history(
            source_db=self.pg_src_dsn,
            target_db=self.pg_tgt_b_dsn,
            macro_date="2026-07-29",
            company_date="2026-07-29",
            institutional_date="2026-07-29",
            industry_config_path=os.path.join(self.tmp, "no-industry-config.json"),
        )
        # Proves the SOURCE dispatched to PostgreSQL — not to SQLite.
        self.assertEqual(result["source_kind"], "postgres")
        self.assertEqual(result["backend_target"], "postgres")
        self.assertEqual(result["SEEDED_COUNT"], 10)
        self.assertEqual(result["REJECTED_COUNT_TOTAL"], 7)

    def test_resolve_as_of_dates_on_pg(self) -> None:
        try:
            resolved = resolve_as_of_dates(self.pg_src_dsn, requested_date="2026-07-30")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"resolve_as_of_dates failed on PG source: {exc}")
        self.assertEqual(resolved["macro_date"], "2026-07-29")
        self.assertEqual(resolved["company_date"], "2026-07-29")
        self.assertEqual(resolved["institutional_date"], "2026-07-29")


if __name__ == "__main__":
    unittest.main(verbosity=2)