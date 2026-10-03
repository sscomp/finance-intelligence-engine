"""Tests for FIE T-2 Historical Backfill Tooling.

Tests governance §8 (Historical Backfill Policy) from
``fie_historical_data_retention_freshness_backfill_governance.md``.

Covers:
  - Gap detection (macro_daily, stock_monthly, institutional_daily)
  - Dry-run plan mode
  - Idempotent repeat run (INSERT OR REPLACE)
  - Duplicate prevention
  - Controlled upsert/skip semantics
  - Invalid schema/date/entity rejection
  - Provenance preservation
  - Temp-DB write behavior
  - Production-path protection
  - Empty/no-gap intervals
  - Partial source availability
  - Blocked source classes (industry_derived)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

# Must import from phase3.backfill
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from phase3.backfill import (
    BLOCKED_SOURCES,
    PRODUCTION_DB_NAME,
    PRODUCTION_DB_PATH,
    REQUIRED_COLUMNS,
    SOURCE_INDUSTRY_DERIVED,
    SOURCE_INSTITUTIONAL_DAILY,
    SOURCE_MACRO_DAILY,
    SOURCE_STOCK_MONTHLY,
    SUPPORTED_SOURCES,
    TABLE_PKS,
    TABLE_SCHEMAS,
    BackfillPlan,
    BackfillResult,
    GapResult,
    assert_not_production,
    count_rows,
    create_temp_copy,
    create_temp_db,
    detect_gaps,
    execute_backfill,
    generate_monthly_dates,
    generate_trading_days,
    get_existing_pks,
    is_production_db,
    plan_backfill,
    validate_plan,
    validate_row,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_test_db(path: str) -> str:
    """Create a fresh test DB with all 3 tables (empty)."""
    return create_temp_db(path)


def _insert_macro_row(conn, d: str, us10y=4.0, us2y=4.5, us13w=4.3,
                      dxy=102.0, vix=18.0, usdtwd=32.0, spread=-0.5):
    conn.execute(
        "INSERT OR REPLACE INTO macro_daily "
        "(date, us10y, us2y, us13w, dxy, vix, usdtwd, yield_spread, "
        "score, verdict, signals_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (d, us10y, us2y, us13w, dxy, vix, usdtwd, spread,
         0, "test", "{}", "2026-01-01T00:00:00+08:00"),
    )


def _insert_stock_row(conn, d: str, code: str, name="Test", sector="半導體",
                      pe=20.0, roe=0.15, price=100.0):
    conn.execute(
        "INSERT OR REPLACE INTO stock_monthly "
        "(date, code, name, sector, price, pe_trailing, roe) "
        "VALUES (?,?,?,?,?,?,?)",
        (d, code, name, sector, price, pe, roe),
    )


def _insert_inst_row(conn, d: str, code: str, foreign=100, prop=50, total=150, td=20):
    conn.execute(
        "INSERT OR REPLACE INTO institutional_daily "
        "(date, code, foreign_net, prop_net, total_net, trading_days) "
        "VALUES (?,?,?,?,?,?)",
        (d, code, foreign, prop, total, td),
    )


def _sample_macro_row(d: str) -> dict:
    return {
        "date": d,
        "us10y": 4.0, "us2y": 4.5, "us13w": 4.3,
        "dxy": 102.0, "vix": 18.0, "usdtwd": 32.0,
        "yield_spread": -0.5, "score": 0, "verdict": "test",
        "signals_json": "{}", "created_at": "2026-01-01T00:00:00+08:00",
    }


def _sample_stock_row(d: str, code: str) -> dict:
    return {
        "date": d, "code": code, "name": "Test", "sector": "半導體",
        "price": 100.0, "eps_ttm": 5.0, "pe_trailing": 20.0,
        "pe_forward": 18.0, "roe": 0.15, "roa": 0.10,
        "gross_margin": 0.50, "operating_margin": 0.30,
        "profit_margin": 0.25, "dividend_rate": 2.0,
        "dividend_yield": 0.02, "payout_ratio": 0.40,
        "pb_ratio": 3.0, "revenue_growth": 0.10,
        "earnings_growth": 0.15, "nim_growth": None,
        "interest_spread": None, "high_52": 120.0,
        "low_52": 80.0, "dist_from_high": -16.7,
        "target_mean": 110.0, "peg_ratio": 1.3,
        "market_cap": 1e12,
    }


def _sample_inst_row(d: str, code: str) -> dict:
    return {
        "date": d, "code": code,
        "foreign_net": 100, "prop_net": 50,
        "total_net": 150, "trading_days": 20,
    }


# ---------------------------------------------------------------------------
# Production-path protection tests
# ---------------------------------------------------------------------------

class TestProductionPathProtection(unittest.TestCase):

    def test_is_production_db_by_abs_path(self):
        self.assertTrue(is_production_db(PRODUCTION_DB_PATH))

    def test_is_production_db_by_name_only(self):
        self.assertTrue(is_production_db("/tmp/macro_history.db"))
        self.assertTrue(is_production_db("/some/random/path/macro_history.db"))

    def test_is_not_production_db(self):
        self.assertFalse(is_production_db("/tmp/test_backfill.db"))
        self.assertFalse(is_production_db("/tmp/macro_history_backup.db"))

    def test_assert_not_production_raises(self):
        with self.assertRaises(ValueError) as ctx:
            assert_not_production(PRODUCTION_DB_PATH)
        self.assertIn("REFUSED", str(ctx.exception))

    def test_assert_not_production_raises_by_name(self):
        with self.assertRaises(ValueError):
            assert_not_production("/tmp/macro_history.db")

    def test_assert_not_production_passes_for_temp(self):
        # Should NOT raise
        assert_not_production("/tmp/test_backfill_temp.db")

    def test_execute_backfill_refuses_production_target(self):
        rows = [_sample_macro_row("2026-01-15")]
        with self.assertRaises(ValueError):
            execute_backfill(
                source_db="/tmp/test_src.db",
                target_db=PRODUCTION_DB_PATH,
                source_class=SOURCE_MACRO_DAILY,
                start_date="2026-01-01",
                end_date="2026-01-31",
                rows=rows,
                dry_run=False,
            )


# ---------------------------------------------------------------------------
# Gap detection tests
# ---------------------------------------------------------------------------

class TestGapDetectionMacroDaily(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_gaps.db")
        _make_test_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir)

    def test_no_gaps_when_all_dates_present(self):
        # Insert all weekdays in a 2-week range
        for d in generate_trading_days("2026-01-05", "2026-01-16"):
            _insert_macro_row(self.conn, d)
        self.conn.commit()

        result = detect_gaps(self.db_path, SOURCE_MACRO_DAILY,
                             "2026-01-05", "2026-01-16")
        self.assertEqual(result.missing_dates, [])
        self.assertEqual(len(result.existing_dates), 10)

    def test_detects_missing_trading_days(self):
        # Insert Mon-Thu, skip Friday
        for d in generate_trading_days("2026-01-05", "2026-01-08"):
            _insert_macro_row(self.conn, d)
        self.conn.commit()

        result = detect_gaps(self.db_path, SOURCE_MACRO_DAILY,
                             "2026-01-05", "2026-01-09")
        self.assertIn("2026-01-09", result.missing_dates)
        self.assertEqual(len(result.missing_dates), 1)

    def test_weekends_not_counted_as_gaps(self):
        # Saturday/Sunday should not appear in expected or missing
        result = detect_gaps(self.db_path, SOURCE_MACRO_DAILY,
                             "2026-01-03", "2026-01-04")  # Sat, Sun
        self.assertEqual(result.missing_dates, [])
        self.assertEqual(len(result.expected_dates), 0)

    def test_empty_db_all_dates_are_gaps(self):
        result = detect_gaps(self.db_path, SOURCE_MACRO_DAILY,
                             "2026-01-05", "2026-01-09")
        self.assertEqual(len(result.missing_dates), 5)
        self.assertIn("2026-01-05", result.missing_dates)
        self.assertIn("2026-01-09", result.missing_dates)

    def test_gap_detection_is_read_only(self):
        import hashlib
        sha_before = hashlib.sha256(
            open(self.db_path, "rb").read()
        ).hexdigest()
        detect_gaps(self.db_path, SOURCE_MACRO_DAILY,
                    "2026-01-01", "2026-01-31")
        sha_after = hashlib.sha256(
            open(self.db_path, "rb").read()
        ).hexdigest()
        self.assertEqual(sha_before, sha_after)


class TestGapDetectionStockMonthly(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_stock.db")
        _make_test_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir)

    def test_detects_missing_monthly_snapshot(self):
        # Insert July data but not August
        for code in ["2330", "2454"]:
            _insert_stock_row(self.conn, "2026-07-12", code)
        self.conn.commit()

        result = detect_gaps(self.db_path, SOURCE_STOCK_MONTHLY,
                             "2026-07-01", "2026-08-31",
                             expected_entity_count=2)
        self.assertIn("2026-08-12", result.missing_dates)
        self.assertNotIn("2026-07-12", result.missing_dates)

    def test_detects_partial_entity_coverage(self):
        # Only 1 of 2 expected companies in August
        _insert_stock_row(self.conn, "2026-07-12", "2330")
        _insert_stock_row(self.conn, "2026-07-12", "2454")
        _insert_stock_row(self.conn, "2026-08-12", "2330")
        self.conn.commit()

        result = detect_gaps(self.db_path, SOURCE_STOCK_MONTHLY,
                             "2026-07-01", "2026-08-31",
                             expected_entity_count=2)
        self.assertIn("2026-08-12", result.missing_dates)

    def test_no_gaps_when_all_entities_present(self):
        for d in ["2026-07-12", "2026-08-12"]:
            for code in ["2330", "2454"]:
                _insert_stock_row(self.conn, d, code)
        self.conn.commit()

        result = detect_gaps(self.db_path, SOURCE_STOCK_MONTHLY,
                             "2026-07-01", "2026-08-31",
                             expected_entity_count=2)
        self.assertEqual(result.missing_dates, [])


class TestGapDetectionInstitutionalDaily(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_inst.db")
        _make_test_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir)

    def test_detects_missing_institutional_snapshot(self):
        for code in ["2330"]:
            _insert_inst_row(self.conn, "2026-07-12", code)
        self.conn.commit()

        result = detect_gaps(self.db_path, SOURCE_INSTITUTIONAL_DAILY,
                             "2026-07-01", "2026-08-31",
                             expected_entity_count=1)
        self.assertIn("2026-08-12", result.missing_dates)
        self.assertNotIn("2026-07-12", result.missing_dates)


# ---------------------------------------------------------------------------
# Date generation tests
# ---------------------------------------------------------------------------

class TestDateGeneration(unittest.TestCase):

    def test_generate_trading_days_skips_weekends(self):
        # 2026-01-03 is Saturday, 2026-01-04 is Sunday
        days = generate_trading_days("2026-01-03", "2026-01-09")
        self.assertNotIn("2026-01-03", days)  # Sat
        self.assertNotIn("2026-01-04", days)  # Sun
        self.assertIn("2026-01-05", days)     # Mon
        self.assertIn("2026-01-09", days)     # Fri

    def test_generate_trading_days_with_holidays(self):
        holidays = {"2026-01-05"}  # Monday holiday
        days = generate_trading_days("2026-01-05", "2026-01-09", holidays)
        self.assertNotIn("2026-01-05", days)
        self.assertIn("2026-01-06", days)

    def test_generate_monthly_dates(self):
        dates = generate_monthly_dates("2026-01-01", "2026-03-31", day_of_month=12)
        self.assertEqual(dates, ["2026-01-12", "2026-02-12", "2026-03-12"])

    def test_generate_monthly_dates_day_31_uses_last_day(self):
        dates = generate_monthly_dates("2026-02-01", "2026-02-28", day_of_month=31)
        self.assertEqual(dates, ["2026-02-28"])  # 28 is last day of Feb 2026


# ---------------------------------------------------------------------------
# Dry-run plan tests
# ---------------------------------------------------------------------------

class TestDryRunPlan(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.source_db = os.path.join(self.tmpdir, "source.db")
        self.target_db = os.path.join(self.tmpdir, "target.db")
        _make_test_db(self.source_db)
        _make_test_db(self.target_db)
        self.conn = sqlite3.connect(self.source_db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir)

    def test_plan_reports_would_insert_for_gaps(self):
        # No rows in source → all dates are gaps
        plan = plan_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
        )
        self.assertEqual(plan.would_insert, 5)  # 5 weekdays
        self.assertEqual(plan.would_skip, 0)
        self.assertEqual(len(plan.validation_errors), 0)

    def test_plan_reports_would_skip_for_existing(self):
        for d in generate_trading_days("2026-01-05", "2026-01-09"):
            _insert_macro_row(self.conn, d)
        self.conn.commit()

        plan = plan_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
        )
        self.assertEqual(plan.would_skip, 5)
        self.assertEqual(plan.would_insert, 0)

    def test_plan_no_gaps_emits_warning(self):
        for d in generate_trading_days("2026-01-05", "2026-01-09"):
            _insert_macro_row(self.conn, d)
        self.conn.commit()

        plan = plan_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
        )
        self.assertTrue(
            any("No gaps" in w for w in plan.validation_warnings)
        )

    def test_plan_does_not_mutate_target(self):
        import hashlib
        sha_before = hashlib.sha256(
            open(self.target_db, "rb").read()
        ).hexdigest()

        plan_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
        )

        sha_after = hashlib.sha256(
            open(self.target_db, "rb").read()
        ).hexdigest()
        self.assertEqual(sha_before, sha_after)


# ---------------------------------------------------------------------------
# Idempotent execution tests
# ---------------------------------------------------------------------------

class TestIdempotentExecution(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.source_db = os.path.join(self.tmpdir, "source.db")
        self.target_db = os.path.join(self.tmpdir, "target.db")
        _make_test_db(self.source_db)
        _make_test_db(self.target_db)
        self.conn = sqlite3.connect(self.source_db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir)

    def test_dry_run_does_not_write(self):
        rows = [_sample_macro_row("2026-01-05")]
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=rows,
            dry_run=True,
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(count_rows(self.target_db, "macro_daily"), 0)

    def test_first_write_inserts_rows(self):
        rows = [_sample_macro_row("2026-01-05"),
                _sample_macro_row("2026-01-06")]
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=rows,
            dry_run=False,
        )
        self.assertEqual(result.rows_inserted, 2)
        self.assertEqual(result.rows_updated, 0)
        self.assertEqual(count_rows(self.target_db, "macro_daily"), 2)

    def test_second_run_is_idempotent(self):
        rows = [_sample_macro_row("2026-01-05"),
                _sample_macro_row("2026-01-06")]

        # First run
        result1 = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=rows,
            dry_run=False,
        )
        count_after_first = count_rows(self.target_db, "macro_daily")

        # Second run with same rows
        result2 = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=rows,
            dry_run=False,
        )
        count_after_second = count_rows(self.target_db, "macro_daily")

        self.assertEqual(count_after_first, count_after_second)
        self.assertEqual(result2.rows_inserted, 0)
        self.assertEqual(result2.rows_updated, 2)

    def test_duplicate_pks_are_replaced_not_duplicated(self):
        rows = [_sample_macro_row("2026-01-05"),
                _sample_macro_row("2026-01-05")]  # duplicate PK

        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=rows,
            dry_run=False,
        )
        # Only 1 row should exist (INSERT OR REPLACE)
        self.assertEqual(count_rows(self.target_db, "macro_daily"), 1)


# ---------------------------------------------------------------------------
# Controlled upsert/skip semantics tests
# ---------------------------------------------------------------------------

class TestUpsertSkipSemantics(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.target_db = os.path.join(self.tmpdir, "target.db")
        self.source_db = os.path.join(self.tmpdir, "source.db")
        _make_test_db(self.source_db)
        _make_test_db(self.target_db)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_existing_rows_are_updated_not_inserted(self):
        # Pre-insert a row
        conn = sqlite3.connect(self.target_db)
        _insert_macro_row(conn, "2026-01-05", us10y=3.5)
        conn.commit()
        conn.close()

        # Backfill with updated value
        rows = [_sample_macro_row("2026-01-05")]  # us10y=4.0
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=rows,
            dry_run=False,
        )
        self.assertEqual(result.rows_inserted, 0)
        self.assertEqual(result.rows_updated, 1)

        # Verify value was replaced
        conn = sqlite3.connect(self.target_db)
        row = conn.execute(
            "SELECT us10y FROM macro_daily WHERE date = '2026-01-05'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 4.0)  # new value, not 3.5

    def test_new_rows_are_inserted(self):
        rows = [_sample_macro_row("2026-01-05")]
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=rows,
            dry_run=False,
        )
        self.assertEqual(result.rows_inserted, 1)
        self.assertEqual(result.rows_updated, 0)


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestRowValidation(unittest.TestCase):

    def test_valid_macro_row(self):
        row = _sample_macro_row("2026-01-05")
        errors, warnings = validate_row(SOURCE_MACRO_DAILY, row)
        self.assertEqual(errors, [])

    def test_missing_required_column_macro(self):
        row = _sample_macro_row("2026-01-05")
        del row["us10y"]
        errors, warnings = validate_row(SOURCE_MACRO_DAILY, row)
        self.assertTrue(any("us10y" in e for e in errors))

    def test_null_pk_column(self):
        row = _sample_macro_row("2026-01-05")
        row["date"] = None
        errors, warnings = validate_row(SOURCE_MACRO_DAILY, row)
        self.assertTrue(any("date" in e for e in errors))

    def test_empty_pk_column(self):
        row = _sample_macro_row("2026-01-05")
        row["date"] = ""
        errors, warnings = validate_row(SOURCE_MACRO_DAILY, row)
        self.assertTrue(any("date" in e for e in errors))

    def test_invalid_date_format(self):
        row = _sample_macro_row("2026-01-05")
        row["date"] = "01/05/2026"
        errors, warnings = validate_row(SOURCE_MACRO_DAILY, row)
        self.assertTrue(any("Date format invalid" in e for e in errors))

    def test_future_dated_row_warning(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        row = _sample_macro_row(future)
        errors, warnings = validate_row(SOURCE_MACRO_DAILY, row)
        self.assertTrue(any("Future-dated" in w for w in warnings))

    def test_negative_pe_warning(self):
        row = _sample_stock_row("2026-07-12", "2330")
        row["pe_trailing"] = -5.0
        errors, warnings = validate_row(SOURCE_STOCK_MONTHLY, row)
        self.assertTrue(any("pe_trailing" in w for w in warnings))

    def test_negative_vix_warning(self):
        row = _sample_macro_row("2026-01-05")
        row["vix"] = -1.0
        errors, warnings = validate_row(SOURCE_MACRO_DAILY, row)
        self.assertTrue(any("VIX" in w for w in warnings))

    def test_null_required_column_warning(self):
        row = _sample_macro_row("2026-01-05")
        row["vix"] = None
        errors, warnings = validate_row(SOURCE_MACRO_DAILY, row)
        self.assertTrue(any("vix" in w for w in warnings))

    def test_unknown_source_class(self):
        errors, warnings = validate_row("unknown_class", {})
        self.assertTrue(any("Unknown source class" in e for e in errors))


class TestPlanValidation(unittest.TestCase):

    def test_start_after_end_is_error(self):
        gaps = GapResult(SOURCE_MACRO_DAILY, "2026-02-01", "2026-01-01")
        errors, warnings = validate_plan(
            SOURCE_MACRO_DAILY, "2026-02-01", "2026-01-01",
            "/tmp/test.db", gaps,
        )
        self.assertTrue(any("after" in e for e in errors))

    def test_production_target_is_error(self):
        gaps = GapResult(SOURCE_MACRO_DAILY, "2026-01-01", "2026-01-31")
        errors, warnings = validate_plan(
            SOURCE_MACRO_DAILY, "2026-01-01", "2026-01-31",
            PRODUCTION_DB_PATH, gaps,
        )
        self.assertTrue(any("production" in e for e in errors))

    def test_blocked_source_is_error(self):
        gaps = GapResult(SOURCE_INDUSTRY_DERIVED, "2026-01-01", "2026-01-31")
        errors, warnings = validate_plan(
            SOURCE_INDUSTRY_DERIVED, "2026-01-01", "2026-01-31",
            "/tmp/test.db", gaps,
        )
        self.assertTrue(any("BLOCKED" in e for e in errors))

    def test_no_gaps_is_warning(self):
        gaps = GapResult(
            SOURCE_MACRO_DAILY, "2026-01-01", "2026-01-31",
            expected_dates=["2026-01-05"],
            existing_dates=["2026-01-05"],
            missing_dates=[],
        )
        errors, warnings = validate_plan(
            SOURCE_MACRO_DAILY, "2026-01-01", "2026-01-31",
            "/tmp/test.db", gaps,
        )
        self.assertTrue(any("No gaps" in w for w in warnings))


# ---------------------------------------------------------------------------
# Provenance tests
# ---------------------------------------------------------------------------

class TestProvenance(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.source_db = os.path.join(self.tmpdir, "source.db")
        self.target_db = os.path.join(self.tmpdir, "target.db")
        _make_test_db(self.source_db)
        _make_test_db(self.target_db)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_provenance_preserved_in_result(self):
        rows = [_sample_macro_row("2026-01-05")]
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=rows,
            dry_run=False,
        )
        prov = result.provenance
        self.assertEqual(prov["source_class"], SOURCE_MACRO_DAILY)
        self.assertIn("source_db", prov)
        self.assertIn("target_db", prov)
        self.assertIn("governance_ref", prov)
        self.assertIn("§8", prov["governance_ref"])

    def test_provenance_preserved_in_dry_run(self):
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=None,
            dry_run=True,
        )
        self.assertIn("source_class", result.provenance)
        self.assertTrue(result.dry_run)


# ---------------------------------------------------------------------------
# Temp-DB write behavior tests
# ---------------------------------------------------------------------------

class TestTempDbWriteBehavior(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.source_db = os.path.join(self.tmpdir, "source.db")
        self.target_db = os.path.join(self.tmpdir, "target.db")
        _make_test_db(self.source_db)
        _make_test_db(self.target_db)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_write_to_temp_db_succeeds(self):
        rows = [_sample_macro_row("2026-01-05"),
                _sample_macro_row("2026-01-06"),
                _sample_macro_row("2026-01-07")]
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=rows,
            dry_run=False,
        )
        self.assertEqual(result.rows_inserted, 3)
        self.assertEqual(count_rows(self.target_db, "macro_daily"), 3)

    def test_write_stock_monthly_to_temp(self):
        rows = [_sample_stock_row("2026-07-12", "2330"),
                _sample_stock_row("2026-07-12", "2454")]
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_STOCK_MONTHLY,
            start_date="2026-07-01",
            end_date="2026-07-31",
            rows=rows,
            dry_run=False,
        )
        self.assertEqual(result.rows_inserted, 2)
        self.assertEqual(count_rows(self.target_db, "stock_monthly"), 2)

    def test_write_institutional_to_temp(self):
        rows = [_sample_inst_row("2026-07-12", "2330")]
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_INSTITUTIONAL_DAILY,
            start_date="2026-07-01",
            end_date="2026-07-31",
            rows=rows,
            dry_run=False,
        )
        self.assertEqual(result.rows_inserted, 1)
        self.assertEqual(count_rows(self.target_db, "institutional_daily"), 1)

    def test_temp_copy_preserves_data(self):
        # Create source with data
        conn = sqlite3.connect(self.source_db)
        _insert_macro_row(conn, "2026-01-05")
        conn.commit()
        conn.close()

        copy_path = os.path.join(self.tmpdir, "copy.db")
        create_temp_copy(self.source_db, copy_path)
        self.assertEqual(count_rows(copy_path, "macro_daily"), 1)

    def test_create_temp_db_has_all_tables(self):
        conn = sqlite3.connect(self.target_db)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        table_names = {t[0] for t in tables}
        self.assertIn("macro_daily", table_names)
        self.assertIn("stock_monthly", table_names)
        self.assertIn("institutional_daily", table_names)


# ---------------------------------------------------------------------------
# Empty/no-gap interval tests
# ---------------------------------------------------------------------------

class TestEmptyNoGapIntervals(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.source_db = os.path.join(self.tmpdir, "source.db")
        self.target_db = os.path.join(self.tmpdir, "target.db")
        _make_test_db(self.source_db)
        _make_test_db(self.target_db)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_empty_interval_no_gaps(self):
        result = detect_gaps(self.source_db, SOURCE_MACRO_DAILY,
                             "2026-01-05", "2026-01-09")
        self.assertEqual(len(result.missing_dates), 5)

    def test_no_gap_plan_has_zero_would_insert(self):
        conn = sqlite3.connect(self.source_db)
        for d in generate_trading_days("2026-01-05", "2026-01-09"):
            _insert_macro_row(conn, d)
        conn.commit()
        conn.close()

        plan = plan_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
        )
        self.assertEqual(plan.would_insert, 0)

    def test_empty_rows_list_in_execute(self):
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=[],
            dry_run=False,
        )
        self.assertEqual(result.rows_inserted, 0)
        self.assertEqual(result.rows_skipped, 0)


# ---------------------------------------------------------------------------
# Partial source availability tests
# ---------------------------------------------------------------------------

class TestPartialSourceAvailability(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.source_db = os.path.join(self.tmpdir, "source.db")
        self.target_db = os.path.join(self.tmpdir, "target.db")
        _make_test_db(self.source_db)
        _make_test_db(self.target_db)
        self.conn = sqlite3.connect(self.source_db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir)

    def test_partial_stock_coverage_detected_as_gap(self):
        # 3 of 5 companies present
        for code in ["2330", "2454", "2308"]:
            _insert_stock_row(self.conn, "2026-07-12", code)
        self.conn.commit()

        result = detect_gaps(self.source_db, SOURCE_STOCK_MONTHLY,
                            "2026-07-01", "2026-07-31",
                            expected_entity_count=5)
        self.assertIn("2026-07-12", result.missing_dates)

    def test_partial_rows_still_write(self):
        # Only 2 of 50 expected rows
        rows = [_sample_stock_row("2026-07-12", "2330"),
                _sample_stock_row("2026-07-12", "2454")]
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_STOCK_MONTHLY,
            start_date="2026-07-01",
            end_date="2026-07-31",
            rows=rows,
            dry_run=False,
        )
        self.assertEqual(result.rows_inserted, 2)


# ---------------------------------------------------------------------------
# Blocked source class tests
# ---------------------------------------------------------------------------

class TestBlockedSources(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.source_db = os.path.join(self.tmpdir, "source.db")
        self.target_db = os.path.join(self.tmpdir, "target.db")
        _make_test_db(self.source_db)
        _make_test_db(self.target_db)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_industry_derived_is_blocked(self):
        self.assertIn(SOURCE_INDUSTRY_DERIVED, BLOCKED_SOURCES)

    def test_plan_for_blocked_source(self):
        plan = plan_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_INDUSTRY_DERIVED,
            start_date="2026-01-01",
            end_date="2026-06-30",
        )
        self.assertTrue(plan.blocked)
        self.assertIn("derived", plan.block_reason)

    def test_execute_blocked_source_returns_error(self):
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_INDUSTRY_DERIVED,
            start_date="2026-01-01",
            end_date="2026-06-30",
            rows=[],
            dry_run=False,
        )
        self.assertTrue(len(result.validation_errors) > 0)


# ---------------------------------------------------------------------------
# Invalid row rejection tests
# ---------------------------------------------------------------------------

class TestInvalidRowRejection(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.source_db = os.path.join(self.tmpdir, "source.db")
        self.target_db = os.path.join(self.tmpdir, "target.db")
        _make_test_db(self.source_db)
        _make_test_db(self.target_db)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_invalid_schema_rejected(self):
        row = {"date": "2026-01-05"}  # missing most columns
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=[row],
            dry_run=False,
        )
        self.assertTrue(len(result.validation_errors) > 0)

    def test_invalid_date_rejected(self):
        row = _sample_macro_row("bad-date")
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=[row],
            dry_run=False,
        )
        self.assertTrue(len(result.validation_errors) > 0)

    def test_null_pk_rejected(self):
        row = _sample_macro_row("2026-01-05")
        row["date"] = None
        result = execute_backfill(
            source_db=self.source_db,
            target_db=self.target_db,
            source_class=SOURCE_MACRO_DAILY,
            start_date="2026-01-05",
            end_date="2026-01-09",
            rows=[row],
            dry_run=False,
        )
        self.assertTrue(len(result.validation_errors) > 0)


# ---------------------------------------------------------------------------
# Get existing PKs tests
# ---------------------------------------------------------------------------

class TestGetExistingPKs(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_pks.db")
        _make_test_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir)

    def test_get_existing_pks_macro(self):
        _insert_macro_row(self.conn, "2026-01-05")
        _insert_macro_row(self.conn, "2026-01-06")
        self.conn.commit()

        pks = get_existing_pks(self.db_path, SOURCE_MACRO_DAILY)
        self.assertIn(("2026-01-05",), pks)
        self.assertIn(("2026-01-06",), pks)

    def test_get_existing_pks_stock(self):
        _insert_stock_row(self.conn, "2026-07-12", "2330")
        self.conn.commit()

        pks = get_existing_pks(self.db_path, SOURCE_STOCK_MONTHLY)
        self.assertIn(("2026-07-12", "2330"), pks)

    def test_get_existing_pks_empty_db(self):
        pks = get_existing_pks(self.db_path, SOURCE_MACRO_DAILY)
        self.assertEqual(pks, set())


# ---------------------------------------------------------------------------
# Count rows tests
# ---------------------------------------------------------------------------

class TestCountRows(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_count.db")
        _make_test_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir)

    def test_count_rows_empty(self):
        self.assertEqual(count_rows(self.db_path, "macro_daily"), 0)

    def test_count_rows_after_insert(self):
        _insert_macro_row(self.conn, "2026-01-05")
        self.conn.commit()
        self.assertEqual(count_rows(self.db_path, "macro_daily"), 1)

    def test_count_rows_nonexistent_table(self):
        self.assertEqual(count_rows(self.db_path, "nonexistent"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)