"""Tests for FIE Historical Fetch Range Enablement (T-2.1).

Covers:
  - Macro explicit start/end range fetch (mocked)
  - Macro legacy default behavior (no range args)
  - Interval boundary semantics (inclusive start, yfinance exclusive end)
  - Empty/no-data interval
  - Company historical interval rejection (non-fabrication)
  - Institutional regression (unchanged behavior)
  - Industry blocked behavior
  - Backfill-tool integration with range fetch
  - No duplicate / date drift

All tests use mocks/fixtures — no network calls, no production DB access.
"""
import sys
import os
from pathlib import Path
import json
import tempfile
import sqlite3
from datetime import date, timedelta
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from phase3.backfill import (
    SUPPORTED_SOURCES,
    BLOCKED_SOURCES,
    SOURCE_MACRO_DAILY,
    SOURCE_STOCK_MONTHLY,
    SOURCE_INSTITUTIONAL_DAILY,
    SOURCE_INDUSTRY_DERIVED,
    HISTORICAL_FETCH_STATUS,
    HISTORICAL_FETCH_REASONS,
    fetch_macro_range,
    fetch_company_as_of,
    fetch_institutional_range,
    plan_backfill,
    execute_backfill,
    create_temp_db,
    count_rows,
    is_production_db,
    assert_not_production,
    TABLE_SCHEMAS,
    TABLE_PKS,
)


# ---------------------------------------------------------------------------
# Helper: build a mock yfinance history DataFrame
# ---------------------------------------------------------------------------

def _mock_hist_df(dates_and_closes):
    """Build a minimal DataFrame-like object that yf.Ticker().history() returns."""
    import pandas as pd
    idx = pd.DatetimeIndex([d[0] for d in dates_and_closes])
    data = {"Close": [d[1] for d in dates_and_closes]}
    return pd.DataFrame(data, index=idx)


class TestHistoricalFetchStatusRegistry(TestCase):
    """Test the HISTORICAL_FETCH_STATUS registry."""

    def test_macro_daily_is_ready(self):
        self.assertEqual(HISTORICAL_FETCH_STATUS[SOURCE_MACRO_DAILY], "READY")

    def test_stock_monthly_is_blocked(self):
        self.assertEqual(HISTORICAL_FETCH_STATUS[SOURCE_STOCK_MONTHLY], "BLOCKED")

    def test_institutional_daily_is_ready(self):
        self.assertEqual(HISTORICAL_FETCH_STATUS[SOURCE_INSTITUTIONAL_DAILY], "READY")

    def test_industry_derived_is_blocked(self):
        self.assertEqual(HISTORICAL_FETCH_STATUS[SOURCE_INDUSTRY_DERIVED], "BLOCKED")

    def test_all_sources_have_reasons(self):
        for src in SUPPORTED_SOURCES | BLOCKED_SOURCES:
            self.assertIn(src, HISTORICAL_FETCH_REASONS)

    def test_registry_covers_all_sources(self):
        for src in SUPPORTED_SOURCES | BLOCKED_SOURCES:
            self.assertIn(src, HISTORICAL_FETCH_STATUS)


class TestMacroRangeFetch(TestCase):
    """Test fetch_macro_range with mocked yfinance."""

    def _mock_yf_data(self, dates_values):
        """Build a mock return value for fetch_indicators(start_date=, end_date=)."""
        data = {}
        history_list = [{"date": d, "value": v} for d, v in dates_values]
        for key in ("US10Y", "US2Y", "US13W", "DXY", "VIX", "USDTWD"):
            data[key] = {
                "name": key,
                "unit": "%",
                "history": history_list,
                "count": len(history_list),
                "value": history_list[-1]["value"] if history_list else None,
                "date": history_list[-1]["date"] if history_list else None,
            }
        return data

    @mock.patch("macro_daily.fetch_indicators")
    def test_explicit_start_end_returns_rows(self, mock_fetch):
        """Macro range fetch returns rows for each trading day."""
        mock_fetch.return_value = self._mock_yf_data([
            ("2026-01-05", 4.20),
            ("2026-01-06", 4.25),
            ("2026-01-07", 4.30),
        ])
        rows, status = fetch_macro_range("2026-01-05", "2026-01-07")
        self.assertEqual(status, "OK")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["date"], "2026-01-05")
        self.assertEqual(rows[0]["us10y"], 4.20)
        self.assertEqual(rows[1]["date"], "2026-01-06")
        self.assertEqual(rows[2]["us10y"], 4.30)

    @mock.patch("macro_daily.fetch_indicators")
    def test_yield_spread_computed(self, mock_fetch):
        """yield_spread = us10y - us2y in each row."""
        mock_fetch.return_value = {
            "US10Y": {"history": [{"date": "2026-01-05", "value": 4.20}], "name": "t", "unit": "%", "count": 1, "value": 4.20, "date": "2026-01-05"},
            "US2Y": {"history": [{"date": "2026-01-05", "value": 3.80}], "name": "t", "unit": "%", "count": 1, "value": 3.80, "date": "2026-01-05"},
            "US13W": {"history": [{"date": "2026-01-05", "value": 4.00}], "name": "t", "unit": "%", "count": 1, "value": 4.00, "date": "2026-01-05"},
            "DXY": {"history": [{"date": "2026-01-05", "value": 105.0}], "name": "t", "unit": "", "count": 1, "value": 105.0, "date": "2026-01-05"},
            "VIX": {"history": [{"date": "2026-01-05", "value": 15.0}], "name": "t", "unit": "", "count": 1, "value": 15.0, "date": "2026-01-05"},
            "USDTWD": {"history": [{"date": "2026-01-05", "value": 32.5}], "name": "t", "unit": "", "count": 1, "value": 32.5, "date": "2026-01-05"},
        }
        rows, status = fetch_macro_range("2026-01-05", "2026-01-05")
        self.assertEqual(status, "OK")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["yield_spread"], 0.40, places=2)

    @mock.patch("macro_daily.fetch_indicators")
    def test_empty_interval_returns_no_data(self, mock_fetch):
        """Empty history returns NO_DATA status."""
        mock_fetch.return_value = {
            "US10Y": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "US2Y": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "US13W": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "DXY": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
            "VIX": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
            "USDTWD": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
        }
        rows, status = fetch_macro_range("2026-01-01", "2026-01-01")
        self.assertEqual(status, "NO_DATA")
        self.assertEqual(len(rows), 0)

    @mock.patch("macro_daily.fetch_indicators")
    def test_fetch_error_returns_error_status(self, mock_fetch):
        """Exception in fetcher returns ERROR status."""
        mock_fetch.side_effect = RuntimeError("Network timeout")
        rows, status = fetch_macro_range("2026-01-05", "2026-01-06")
        self.assertTrue(status.startswith("ERROR"))
        self.assertEqual(len(rows), 0)

    @mock.patch("macro_daily.fetch_indicators")
    def test_yfinance_exclusive_end_adjustment(self, mock_fetch):
        """fetch_macro_range adds 1 day to end_date for yfinance exclusive semantics."""
        mock_fetch.return_value = self._mock_yf_data([
            ("2026-01-05", 4.20),
            ("2026-01-06", 4.25),
        ])
        fetch_macro_range("2026-01-05", "2026-01-06")
        # Verify the yf_end passed to fetch_indicators is 2026-01-07 (exclusive)
        call_kwargs = mock_fetch.call_args
        self.assertEqual(call_kwargs.kwargs.get("end_date"), "2026-01-07")

    @mock.patch("macro_daily.fetch_indicators")
    def test_rows_have_schema_columns(self, mock_fetch):
        """Each row has the fetcher-provided schema columns (not score/verdict which are pipeline-computed)."""
        mock_fetch.return_value = self._mock_yf_data([
            ("2026-01-05", 4.20),
        ])
        rows, status = fetch_macro_range("2026-01-05", "2026-01-05")
        self.assertEqual(status, "OK")
        # fetch_macro_range produces these columns; score/verdict/signals_json/created_at
        # are NOT produced by the historical fetcher — they are computed by the daily pipeline.
        expected_cols = {"date", "us10y", "us2y", "us13w", "dxy", "vix", "usdtwd", "yield_spread"}
        for row in rows:
            for col in expected_cols:
                self.assertIn(col, row)

    @mock.patch("macro_daily.fetch_indicators")
    def test_dates_sorted_ascending(self, mock_fetch):
        """Returned rows are sorted by date ascending."""
        mock_fetch.return_value = self._mock_yf_data([
            ("2026-01-07", 4.30),
            ("2026-01-05", 4.20),
            ("2026-01-06", 4.25),
        ])
        rows, status = fetch_macro_range("2026-01-05", "2026-01-07")
        self.assertEqual(status, "OK")
        dates = [r["date"] for r in rows]
        self.assertEqual(dates, sorted(dates))

    @mock.patch("macro_daily.fetch_indicators")
    def test_no_duplicate_dates(self, mock_fetch):
        """No duplicate dates in returned rows."""
        mock_fetch.return_value = self._mock_yf_data([
            ("2026-01-05", 4.20),
            ("2026-01-05", 4.25),  # Same date, different value (should not happen but test)
            ("2026-01-06", 4.30),
        ])
        rows, status = fetch_macro_range("2026-01-05", "2026-01-06")
        # all_dates is a set, so duplicates collapse
        dates = [r["date"] for r in rows]
        self.assertEqual(len(dates), len(set(dates)))


class TestMacroLegacyDefault(TestCase):
    """Test that fetch_indicators() preserves legacy behavior when no range args given."""

    def test_no_args_uses_period_5d(self):
        """When called with no args, uses period='5d' (legacy)."""
        with mock.patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker = mock.MagicMock()
            mock_ticker_cls.return_value = mock_ticker
            mock_ticker.history.return_value = _mock_hist_df([
                ("2026-01-05", 4.20),
                ("2026-01-06", 4.25),
            ])
            import macro_daily
            data = macro_daily.fetch_indicators()
            # Should have called history with period='5d'
            mock_ticker.history.assert_called_with(period="5d")

    def test_legacy_returns_value_and_change(self):
        """Legacy mode returns value, change, pct, date fields."""
        with mock.patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker = mock.MagicMock()
            mock_ticker_cls.return_value = mock_ticker
            mock_ticker.history.return_value = _mock_hist_df([
                ("2026-01-05", 4.20),
                ("2026-01-06", 4.25),
            ])
            import macro_daily
            data = macro_daily.fetch_indicators()
            us10y = data.get("US10Y", {})
            self.assertIn("value", us10y)
            self.assertIn("change", us10y)
            self.assertIn("pct", us10y)
            self.assertIn("date", us10y)
            self.assertNotIn("history", us10y)

    def test_range_mode_returns_history(self):
        """Range mode returns 'history' field."""
        with mock.patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker = mock.MagicMock()
            mock_ticker_cls.return_value = mock_ticker
            mock_ticker.history.return_value = _mock_hist_df([
                ("2026-01-05", 4.20),
                ("2026-01-06", 4.25),
            ])
            import macro_daily
            data = macro_daily.fetch_indicators(start_date="2026-01-05", end_date="2026-01-07")
            us10y = data.get("US10Y", {})
            self.assertIn("history", us10y)
            self.assertEqual(len(us10y["history"]), 2)
            self.assertIn("count", us10y)

    def test_partial_range_args_uses_legacy(self):
        """Only start_date without end_date should use legacy mode."""
        with mock.patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker = mock.MagicMock()
            mock_ticker_cls.return_value = mock_ticker
            mock_ticker.history.return_value = _mock_hist_df([
                ("2026-01-05", 4.20),
            ])
            import macro_daily
            data = macro_daily.fetch_indicators(start_date="2026-01-05")
            # Should fall back to legacy (period="5d")
            mock_ticker.history.assert_called_with(period="5d")


class TestCompanyHistoricalRejection(TestCase):
    """Test that company/stock_monthly historical fetch is truthfully BLOCKED."""

    def test_fetch_company_as_of_returns_empty(self):
        """fetch_company_as_of returns empty list and BLOCKED status."""
        rows, status = fetch_company_as_of("2026-01-05")
        self.assertEqual(len(rows), 0)
        self.assertTrue(status.startswith("BLOCKED"))

    def test_fetch_company_as_of_does_not_fabricate(self):
        """No fabricated data — returns truly empty list."""
        rows, status = fetch_company_as_of("2025-06-01")
        self.assertEqual(rows, [])
        self.assertIn("BLOCKED", status)

    def test_stock_monthly_historical_status_blocked(self):
        """HISTORICAL_FETCH_STATUS marks stock_monthly as BLOCKED."""
        self.assertEqual(HISTORICAL_FETCH_STATUS[SOURCE_STOCK_MONTHLY], "BLOCKED")

    def test_reason_mentions_yfinance_info(self):
        """BLOCKED reason mentions yfinance .info limitation."""
        reason = HISTORICAL_FETCH_REASONS[SOURCE_STOCK_MONTHLY]
        self.assertIn("info", reason.lower())
        self.assertIn("current-snapshot", reason.lower())


class TestInstitutionalRegression(TestCase):
    """Regression: institutional_daily behavior unchanged."""

    @mock.patch("institutional.fetch_t86_daily")
    def test_institutional_range_calls_t86_per_date(self, mock_t86):
        """fetch_institutional_range calls T86 for each weekday."""
        mock_t86.return_value = {"2330": {"foreign": 100, "prop": 50, "total": 150}}
        rows, status = fetch_institutional_range(
            "2026-01-05", "2026-01-07",
            target_codes=["2330"],
        )
        self.assertEqual(status, "OK")
        # 3 weekdays (Jan 5=Mon, 6=Tue, 7=Wed)
        self.assertEqual(mock_t86.call_count, 3)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["code"], "2330")
            self.assertEqual(row["foreign_net"], 100)
            self.assertEqual(row["prop_net"], 50)

    @mock.patch("institutional.fetch_t86_daily")
    def test_institutional_skips_weekends(self, mock_t86):
        """fetch_institutional_range skips weekends."""
        mock_t86.return_value = {"2330": {"foreign": 100, "prop": 50, "total": 150}}
        # Jan 4 = Sunday, Jan 5 = Monday
        rows, status = fetch_institutional_range(
            "2026-01-04", "2026-01-05",
            target_codes=["2330"],
        )
        self.assertEqual(status, "OK")
        # Only 1 weekday call (Monday)
        self.assertEqual(mock_t86.call_count, 1)
        self.assertEqual(len(rows), 1)

    @mock.patch("institutional.fetch_t86_daily")
    def test_institutional_no_data_returns_no_data(self, mock_t86):
        """T86 returns None for all dates → NO_DATA."""
        mock_t86.return_value = None
        rows, status = fetch_institutional_range(
            "2026-01-05", "2026-01-06",
            target_codes=["2330"],
        )
        self.assertEqual(status, "NO_DATA")
        self.assertEqual(len(rows), 0)


class TestIndustryBlocked(TestCase):
    """Test that industry_derived remains blocked."""

    def test_industry_in_blocked_sources(self):
        self.assertIn(SOURCE_INDUSTRY_DERIVED, BLOCKED_SOURCES)

    def test_industry_not_in_supported_sources(self):
        self.assertNotIn(SOURCE_INDUSTRY_DERIVED, SUPPORTED_SOURCES)

    def test_plan_backfill_blocks_industry(self):
        """plan_backfill returns blocked=True for industry_derived."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = os.path.join(tmpdir, "test.db")
            create_temp_db(tmp_db)
            plan = plan_backfill(
                source_db=tmp_db,
                target_db=tmp_db,
                source_class=SOURCE_INDUSTRY_DERIVED,
                start_date="2026-01-01",
                end_date="2026-01-31",
            )
            self.assertTrue(plan.blocked)
            self.assertIn("derived", plan.block_reason.lower())

    def test_industry_historical_status_blocked(self):
        self.assertEqual(HISTORICAL_FETCH_STATUS[SOURCE_INDUSTRY_DERIVED], "BLOCKED")


class TestBackfillIntegration(TestCase):
    """Test integration of range fetch with backfill execute."""

    @mock.patch("macro_daily.fetch_indicators")
    def test_macro_range_to_backfill_dry_run(self, mock_fetch):
        """fetch_macro_range → execute_backfill(dry_run=True) works."""
        mock_fetch.return_value = {
            "US10Y": {"history": [{"date": "2026-01-05", "value": 4.20}], "name": "t", "unit": "%", "count": 1, "value": 4.20, "date": "2026-01-05"},
            "US2Y": {"history": [{"date": "2026-01-05", "value": 3.80}], "name": "t", "unit": "%", "count": 1, "value": 3.80, "date": "2026-01-05"},
            "US13W": {"history": [{"date": "2026-01-05", "value": 4.00}], "name": "t", "unit": "%", "count": 1, "value": 4.00, "date": "2026-01-05"},
            "DXY": {"history": [{"date": "2026-01-05", "value": 105.0}], "name": "t", "unit": "", "count": 1, "value": 105.0, "date": "2026-01-05"},
            "VIX": {"history": [{"date": "2026-01-05", "value": 15.0}], "name": "t", "unit": "", "count": 1, "value": 15.0, "date": "2026-01-05"},
            "USDTWD": {"history": [{"date": "2026-01-05", "value": 32.5}], "name": "t", "unit": "", "count": 1, "value": 32.5, "date": "2026-01-05"},
        }
        rows, status = fetch_macro_range("2026-01-05", "2026-01-05")
        self.assertEqual(status, "OK")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = os.path.join(tmpdir, "backfill_test.db")
            create_temp_db(tmp_db)
            result = execute_backfill(
                source_db=tmp_db,
                target_db=tmp_db,
                source_class=SOURCE_MACRO_DAILY,
                start_date="2026-01-05",
                end_date="2026-01-05",
                rows=rows,
                dry_run=False,
            )
            self.assertEqual(result.rows_inserted, 1)
            self.assertEqual(count_rows(tmp_db, "macro_daily"), 1)

    @mock.patch("macro_daily.fetch_indicators")
    def test_macro_range_to_backfill_idempotent(self, mock_fetch):
        """Second run with same rows is idempotent (update, not insert)."""
        mock_fetch.return_value = {
            "US10Y": {"history": [{"date": "2026-01-05", "value": 4.20}], "name": "t", "unit": "%", "count": 1, "value": 4.20, "date": "2026-01-05"},
            "US2Y": {"history": [{"date": "2026-01-05", "value": 3.80}], "name": "t", "unit": "%", "count": 1, "value": 3.80, "date": "2026-01-05"},
            "US13W": {"history": [{"date": "2026-01-05", "value": 4.00}], "name": "t", "unit": "%", "count": 1, "value": 4.00, "date": "2026-01-05"},
            "DXY": {"history": [{"date": "2026-01-05", "value": 105.0}], "name": "t", "unit": "", "count": 1, "value": 105.0, "date": "2026-01-05"},
            "VIX": {"history": [{"date": "2026-01-05", "value": 15.0}], "name": "t", "unit": "", "count": 1, "value": 15.0, "date": "2026-01-05"},
            "USDTWD": {"history": [{"date": "2026-01-05", "value": 32.5}], "name": "t", "unit": "", "count": 1, "value": 32.5, "date": "2026-01-05"},
        }
        rows, _ = fetch_macro_range("2026-01-05", "2026-01-05")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = os.path.join(tmpdir, "backfill_test.db")
            create_temp_db(tmp_db)

            # First run
            r1 = execute_backfill(tmp_db, tmp_db, SOURCE_MACRO_DAILY,
                                  "2026-01-05", "2026-01-05", rows=rows, dry_run=False)
            self.assertEqual(r1.rows_inserted, 1)

            # Second run (same rows) → idempotent
            r2 = execute_backfill(tmp_db, tmp_db, SOURCE_MACRO_DAILY,
                                  "2026-01-05", "2026-01-05", rows=rows, dry_run=False)
            self.assertEqual(r2.rows_inserted, 0)
            self.assertEqual(r2.rows_updated, 1)
            self.assertEqual(count_rows(tmp_db, "macro_daily"), 1)

    @mock.patch("macro_daily.fetch_indicators")
    def test_no_date_drift(self, mock_fetch):
        """Dates in fetched rows match the requested interval — no drift."""
        mock_fetch.return_value = {
            "US10Y": {"history": [
                {"date": "2026-01-05", "value": 4.20},
                {"date": "2026-01-06", "value": 4.25},
            ], "name": "t", "unit": "%", "count": 2, "value": 4.25, "date": "2026-01-06"},
            "US2Y": {"history": [
                {"date": "2026-01-05", "value": 3.80},
                {"date": "2026-01-06", "value": 3.85},
            ], "name": "t", "unit": "%", "count": 2, "value": 3.85, "date": "2026-01-06"},
            "US13W": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "DXY": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
            "VIX": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
            "USDTWD": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
        }
        rows, status = fetch_macro_range("2026-01-05", "2026-01-06")
        self.assertEqual(status, "OK")
        # Only 2 unique dates
        dates = [r["date"] for r in rows]
        self.assertEqual(set(dates), {"2026-01-05", "2026-01-06"})

    def test_production_db_rejected_in_backfill(self):
        """execute_backfill with production DB path raises ValueError."""
        with self.assertRaises(ValueError):
            execute_backfill(
                source_db="/tmp/fake.db",
                target_db=str(Path(__file__).resolve().parents[2] / "macro_history.db"),
                source_class=SOURCE_MACRO_DAILY,
                start_date="2026-01-05",
                end_date="2026-01-06",
                rows=[],
                dry_run=False,
            )


class TestIntervalBoundarySemantics(TestCase):
    """Test interval boundary semantics for macro range fetch."""

    @mock.patch("macro_daily.fetch_indicators")
    def test_single_day_interval(self, mock_fetch):
        """Single-day interval [2026-01-05, 2026-01-05] returns 1 row."""
        mock_fetch.return_value = {
            "US10Y": {"history": [{"date": "2026-01-05", "value": 4.20}], "name": "t", "unit": "%", "count": 1, "value": 4.20, "date": "2026-01-05"},
            "US2Y": {"history": [{"date": "2026-01-05", "value": 3.80}], "name": "t", "unit": "%", "count": 1, "value": 3.80, "date": "2026-01-05"},
            "US13W": {"history": [{"date": "2026-01-05", "value": 4.00}], "name": "t", "unit": "%", "count": 1, "value": 4.00, "date": "2026-01-05"},
            "DXY": {"history": [{"date": "2026-01-05", "value": 105.0}], "name": "t", "unit": "", "count": 1, "value": 105.0, "date": "2026-01-05"},
            "VIX": {"history": [{"date": "2026-01-05", "value": 15.0}], "name": "t", "unit": "", "count": 1, "value": 15.0, "date": "2026-01-05"},
            "USDTWD": {"history": [{"date": "2026-01-05", "value": 32.5}], "name": "t", "unit": "", "count": 1, "value": 32.5, "date": "2026-01-05"},
        }
        rows, status = fetch_macro_range("2026-01-05", "2026-01-05")
        self.assertEqual(status, "OK")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-01-05")

    @mock.patch("macro_daily.fetch_indicators")
    def test_end_date_inclusive(self, mock_fetch):
        """end_date is inclusive in fetch_macro_range semantics."""
        mock_fetch.return_value = {
            "US10Y": {"history": [
                {"date": "2026-01-05", "value": 4.20},
                {"date": "2026-01-06", "value": 4.25},
                {"date": "2026-01-07", "value": 4.30},
            ], "name": "t", "unit": "%", "count": 3, "value": 4.30, "date": "2026-01-07"},
            "US2Y": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "US13W": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "DXY": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
            "VIX": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
            "USDTWD": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
        }
        rows, status = fetch_macro_range("2026-01-05", "2026-01-07")
        self.assertEqual(status, "OK")
        self.assertEqual(len(rows), 3)
        # All dates within [start, end]
        for r in rows:
            self.assertGreaterEqual(r["date"], "2026-01-05")
            self.assertLessEqual(r["date"], "2026-01-07")

    @mock.patch("macro_daily.fetch_indicators")
    def test_yfinance_end_adjusted_by_one_day(self, mock_fetch):
        """Verify yfinance end is end_date + 1 (exclusive)."""
        mock_fetch.return_value = {
            "US10Y": {"history": [{"date": "2026-01-05", "value": 4.20}], "name": "t", "unit": "%", "count": 1, "value": 4.20, "date": "2026-01-05"},
            "US2Y": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "US13W": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "DXY": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
            "VIX": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
            "USDTWD": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
        }
        fetch_macro_range("2026-01-05", "2026-01-10")
        call_kwargs = mock_fetch.call_args
        # end_date should be 2026-01-11 (exclusive for yfinance)
        self.assertEqual(call_kwargs.kwargs.get("end_date"), "2026-01-11")
        self.assertEqual(call_kwargs.kwargs.get("start_date"), "2026-01-05")


class TestMissingValues(TestCase):
    """Test handling of missing ticker values in range fetch."""

    @mock.patch("macro_daily.fetch_indicators")
    def test_partial_ticker_data(self, mock_fetch):
        """When some tickers have data and others don't, rows still built."""
        mock_fetch.return_value = {
            "US10Y": {"history": [{"date": "2026-01-05", "value": 4.20}], "name": "t", "unit": "%", "count": 1, "value": 4.20, "date": "2026-01-05"},
            "US2Y": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "US13W": {"history": [], "name": "t", "unit": "%", "count": 0, "value": None, "date": None},
            "DXY": {"history": [{"date": "2026-01-05", "value": 105.0}], "name": "t", "unit": "", "count": 1, "value": 105.0, "date": "2026-01-05"},
            "VIX": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
            "USDTWD": {"history": [], "name": "t", "unit": "", "count": 0, "value": None, "date": None},
        }
        rows, status = fetch_macro_range("2026-01-05", "2026-01-05")
        self.assertEqual(status, "OK")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["us10y"], 4.20)
        self.assertEqual(rows[0]["dxy"], 105.0)
        self.assertIsNone(rows[0]["us2y"])
        self.assertIsNone(rows[0]["vix"])
        self.assertIsNone(rows[0]["yield_spread"])  # us2y missing