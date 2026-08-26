"""Tests for freshness / staleness metadata and guards.

Tests the governance policy defined in:
  fie_historical_data_retention_freshness_backfill_governance.md section 5.

Covers:
  - Fresh data classification
  - Stale data classification
  - Expired/unavailable data classification
  - requested_date / source_date provenance
  - Weekend/holiday edge cases
  - Deterministic as-of selection
  - Company monthly cadence
  - Macro/institutional daily cadence
  - Industry partial/derived behavior
  - No fabricated score fallback
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from phase3.freshness import (
    DATA_CLASS_COMPANY,
    DATA_CLASS_INDUSTRY,
    DATA_CLASS_INSTITUTIONAL,
    DATA_CLASS_MACRO,
    FRESHNESS_POLICY,
    FreshnessStatus,
    classify_freshness,
    compute_age,
    count_calendar_days,
    count_trading_days,
    data_class_for_signal,
    freshness_metadata,
    freshness_warnings,
    load_holidays,
)


class TestFreshnessPolicyConstants(unittest.TestCase):
    """Verify the policy constants match governance section 5.2."""

    def test_macro_policy(self):
        p = FRESHNESS_POLICY[DATA_CLASS_MACRO]
        self.assertEqual(p["freshness_window"], 1)
        self.assertEqual(p["stale_threshold"], 3)
        self.assertEqual(p["hard_expiry"], 3)
        self.assertEqual(p["day_type"], "trading")

    def test_company_policy(self):
        p = FRESHNESS_POLICY[DATA_CLASS_COMPANY]
        self.assertEqual(p["freshness_window"], 35)
        self.assertEqual(p["stale_threshold"], 65)
        self.assertEqual(p["hard_expiry"], 65)
        self.assertEqual(p["day_type"], "calendar")

    def test_institutional_policy(self):
        p = FRESHNESS_POLICY[DATA_CLASS_INSTITUTIONAL]
        self.assertEqual(p["freshness_window"], 35)
        self.assertEqual(p["stale_threshold"], 65)
        self.assertEqual(p["hard_expiry"], 65)
        self.assertEqual(p["day_type"], "calendar")

    def test_industry_policy_matches_institutional(self):
        p_ind = FRESHNESS_POLICY[DATA_CLASS_INDUSTRY]
        p_inst = FRESHNESS_POLICY[DATA_CLASS_INSTITUTIONAL]
        self.assertEqual(p_ind["freshness_window"], p_inst["freshness_window"])
        self.assertEqual(p_ind["stale_threshold"], p_inst["stale_threshold"])
        self.assertEqual(p_ind["hard_expiry"], p_inst["hard_expiry"])
        self.assertEqual(p_ind["day_type"], p_inst["day_type"])


class TestFreshDataClassification(unittest.TestCase):
    """Test that fresh data is classified as FRESH."""

    def test_macro_fresh_1_trading_day(self):
        # Source date 1 trading day before requested = FRESH (age=1 <= 1)
        # Thu Aug 6 -> Fri Aug 7 = 1 trading day
        st, age = classify_freshness("2026-08-06", "2026-08-07", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 1)

    def test_macro_fresh_same_day(self):
        st, age = classify_freshness("2026-08-07", "2026-08-07", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 0)

    def test_company_fresh_within_35_days(self):
        st, age = classify_freshness("2026-07-01", "2026-08-05", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 35)

    def test_company_fresh_0_days(self):
        st, age = classify_freshness("2026-07-01", "2026-07-01", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 0)

    def test_institutional_fresh(self):
        st, age = classify_freshness("2026-07-29", "2026-08-10", DATA_CLASS_INSTITUTIONAL)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 12)

    def test_industry_fresh(self):
        st, age = classify_freshness("2026-07-29", "2026-08-10", DATA_CLASS_INDUSTRY)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 12)


class TestStaleDataClassification(unittest.TestCase):
    """Test that stale data (warning threshold) is classified as STALE."""

    def test_macro_stale_2_trading_days(self):
        # Thu Aug 6 -> Mon Aug 10 = 2 trading days (Fri, Mon)
        st, age = classify_freshness("2026-08-06", "2026-08-10", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 2)

    def test_macro_stale_3_trading_days(self):
        # Thu Aug 6 -> Tue Aug 11 = 3 trading days (Fri, Mon, Tue)
        st, age = classify_freshness("2026-08-06", "2026-08-11", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 3)

    def test_company_stale_36_calendar_days(self):
        st, age = classify_freshness("2026-07-01", "2026-08-06", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 36)

    def test_company_stale_65_calendar_days(self):
        st, age = classify_freshness("2026-07-01", "2026-09-04", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 65)

    def test_institutional_stale(self):
        st, age = classify_freshness("2026-07-01", "2026-08-10", DATA_CLASS_INSTITUTIONAL)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 40)

    def test_industry_stale(self):
        st, age = classify_freshness("2026-07-01", "2026-08-10", DATA_CLASS_INDUSTRY)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 40)


class TestExpiredDataClassification(unittest.TestCase):
    """Test that expired data (hard-expiry exceeded) is classified as EXPIRED."""

    def test_macro_expired_4_trading_days(self):
        # Thu Aug 6 -> Wed Aug 12 = 4 trading days (Fri, Mon, Tue, Wed)
        st, age = classify_freshness("2026-08-06", "2026-08-12", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.EXPIRED)
        self.assertEqual(age, 4)

    def test_company_expired_66_calendar_days(self):
        st, age = classify_freshness("2026-07-01", "2026-09-05", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.EXPIRED)
        self.assertEqual(age, 66)

    def test_institutional_expired(self):
        st, age = classify_freshness("2026-06-01", "2026-08-10", DATA_CLASS_INSTITUTIONAL)
        self.assertEqual(st, FreshnessStatus.EXPIRED)
        self.assertEqual(age, 70)

    def test_industry_expired(self):
        st, age = classify_freshness("2026-06-01", "2026-08-10", DATA_CLASS_INDUSTRY)
        self.assertEqual(st, FreshnessStatus.EXPIRED)
        self.assertEqual(age, 70)


class TestUnavailableDataClassification(unittest.TestCase):
    """Test that None source_date is classified as UNAVAILABLE."""

    def test_macro_unavailable(self):
        st, age = classify_freshness(None, "2026-08-07", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.UNAVAILABLE)
        self.assertIsNone(age)

    def test_company_unavailable(self):
        st, age = classify_freshness(None, "2026-08-07", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.UNAVAILABLE)
        self.assertIsNone(age)

    def test_institutional_unavailable(self):
        st, age = classify_freshness(None, "2026-08-07", DATA_CLASS_INSTITUTIONAL)
        self.assertEqual(st, FreshnessStatus.UNAVAILABLE)
        self.assertIsNone(age)

    def test_industry_unavailable(self):
        st, age = classify_freshness(None, "2026-08-07", DATA_CLASS_INDUSTRY)
        self.assertEqual(st, FreshnessStatus.UNAVAILABLE)
        self.assertIsNone(age)


class TestProvenanceFields(unittest.TestCase):
    """Test that freshness_metadata includes requested_date and source_date."""

    def test_metadata_contains_requested_date(self):
        m = freshness_metadata("2026-08-07", "2026-08-08", DATA_CLASS_MACRO)
        self.assertIn("requested_date", m)
        self.assertEqual(m["requested_date"], "2026-08-08")

    def test_metadata_contains_source_date(self):
        m = freshness_metadata("2026-08-07", "2026-08-08", DATA_CLASS_MACRO)
        self.assertIn("source_date", m)
        self.assertEqual(m["source_date"], "2026-08-07")

    def test_metadata_source_date_none_when_unavailable(self):
        m = freshness_metadata(None, "2026-08-08", DATA_CLASS_MACRO)
        self.assertIsNone(m["source_date"])
        self.assertEqual(m["freshness_status"], "UNAVAILABLE")

    def test_metadata_contains_data_class(self):
        m = freshness_metadata("2026-08-07", "2026-08-08", DATA_CLASS_MACRO)
        self.assertIn("data_class", m)
        self.assertEqual(m["data_class"], "macro_daily")

    def test_metadata_contains_day_type(self):
        m = freshness_metadata("2026-08-07", "2026-08-08", DATA_CLASS_MACRO)
        self.assertEqual(m["day_type"], "trading")

        m2 = freshness_metadata("2026-07-01", "2026-08-08", DATA_CLASS_COMPANY)
        self.assertEqual(m2["day_type"], "calendar")

    def test_metadata_contains_freshness_age(self):
        # Aug 7 is Friday, Aug 10 is Monday = 1 trading day
        m = freshness_metadata("2026-08-07", "2026-08-10", DATA_CLASS_MACRO)
        self.assertIn("freshness_age", m)
        self.assertEqual(m["freshness_age"], 1)


class TestWeekendHolidayEdgeCases(unittest.TestCase):
    """Test trading-day age calculation skips weekends and holidays."""

    def test_friday_to_monday_is_1_trading_day(self):
        # Fri Aug 7 -> Mon Aug 10 = 1 trading day (Mon)
        st, age = classify_freshness("2026-08-07", "2026-08-10", DATA_CLASS_MACRO)
        self.assertEqual(age, 1)
        self.assertEqual(st, FreshnessStatus.FRESH)

    def test_friday_to_tuesday_is_2_trading_days(self):
        # Fri Aug 7 -> Tue Aug 11 = 2 trading days (Mon, Tue)
        st, age = classify_freshness("2026-08-07", "2026-08-11", DATA_CLASS_MACRO)
        self.assertEqual(age, 2)
        self.assertEqual(st, FreshnessStatus.STALE)

    def test_thursday_to_monday_is_1_trading_day(self):
        # Thu Aug 6 -> Mon Aug 10 = 1 trading day (Mon; Fri is a trading day too!)
        # Actually Thu->Fri=1, Fri->Sat=0, Sat->Sun=0, Sun->Mon=0
        # count_trading_days(Thu, Mon) = Fri + Mon = 2
        # Wait: start=Thu, end=Mon, current=Fri(trading)=1, Sat(no), Sun(no), Mon(trading)=2
        st, age = classify_freshness("2026-08-06", "2026-08-10", DATA_CLASS_MACRO)
        self.assertEqual(age, 2)
        self.assertEqual(st, FreshnessStatus.STALE)

    def test_holiday_skipped_in_trading_days(self):
        # With holidays containing 2026-08-10 (Monday), Fri->Tue = 1 trading day (Tue only)
        holidays = {"2026-08-10"}
        st, age = classify_freshness(
            "2026-08-07", "2026-08-11", DATA_CLASS_MACRO, holidays=holidays)
        # Fri->Tue with Mon=holiday: Tue only = 1 trading day
        self.assertEqual(age, 1)
        self.assertEqual(st, FreshnessStatus.FRESH)

    def test_weekend_not_counted_as_trading_day(self):
        # Wed Aug 5 -> Sat Aug 8: trading days = Thu, Fri = 2 (Sat is weekend)
        st, age = classify_freshness("2026-08-05", "2026-08-08", DATA_CLASS_MACRO)
        self.assertEqual(age, 2)

    def test_long_weekend_with_holiday(self):
        # Fri -> Wed with Mon as holiday = 2 trading days (Tue, Wed)
        holidays = {"2026-08-10"}
        st, age = classify_freshness(
            "2026-08-07", "2026-08-12", DATA_CLASS_MACRO, holidays=holidays)
        self.assertEqual(age, 2)
        self.assertEqual(st, FreshnessStatus.STALE)


class TestDeterministicAsOfSelection(unittest.TestCase):
    """Test that freshness classification is deterministic given same inputs."""

    def test_same_inputs_same_output(self):
        r1 = classify_freshness("2026-08-07", "2026-08-10", DATA_CLASS_MACRO)
        r2 = classify_freshness("2026-08-07", "2026-08-10", DATA_CLASS_MACRO)
        self.assertEqual(r1, r2)

    def test_different_requested_date_different_output(self):
        r1 = classify_freshness("2026-08-07", "2026-08-10", DATA_CLASS_MACRO)
        r2 = classify_freshness("2026-08-07", "2026-08-11", DATA_CLASS_MACRO)
        self.assertNotEqual(r1[0], r2[0])

    def test_same_inputs_with_holidays_deterministic(self):
        holidays = {"2026-08-10"}
        r1 = classify_freshness(
            "2026-08-07", "2026-08-11", DATA_CLASS_MACRO, holidays=holidays)
        r2 = classify_freshness(
            "2026-08-07", "2026-08-11", DATA_CLASS_MACRO, holidays=holidays)
        self.assertEqual(r1, r2)


class TestCompanyMonthlyCadence(unittest.TestCase):
    """Test company monthly freshness with calendar-day thresholds."""

    def test_30_days_fresh(self):
        st, age = classify_freshness("2026-07-01", "2026-07-31", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 30)

    def test_35_days_fresh_boundary(self):
        st, age = classify_freshness("2026-07-01", "2026-08-05", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 35)

    def test_36_days_stale(self):
        st, age = classify_freshness("2026-07-01", "2026-08-06", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 36)

    def test_65_days_stale_boundary(self):
        st, age = classify_freshness("2026-07-01", "2026-09-04", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 65)

    def test_66_days_expired(self):
        st, age = classify_freshness("2026-07-01", "2026-09-05", DATA_CLASS_COMPANY)
        self.assertEqual(st, FreshnessStatus.EXPIRED)
        self.assertEqual(age, 66)


class TestMacroInstitutionalDailyCadence(unittest.TestCase):
    """Test macro (trading-day) and institutional (calendar-day) cadence."""

    def test_macro_fresh_within_1_trading_day(self):
        # Wed Aug 5 -> Thu Aug 6 = 1 trading day
        st, age = classify_freshness("2026-08-05", "2026-08-06", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 1)

    def test_macro_stale_2_trading_days(self):
        # Wed Aug 5 -> Fri Aug 7 = 2 trading days
        st, age = classify_freshness("2026-08-05", "2026-08-07", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 2)

    def test_macro_expired_4_trading_days(self):
        # Wed Aug 5 -> Wed Aug 12 = 5 trading days (Thu, Fri, Mon, Tue, Wed)
        st, age = classify_freshness("2026-08-05", "2026-08-12", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.EXPIRED)
        self.assertEqual(age, 5)

    def test_institutional_uses_calendar_days(self):
        # Institutional: 35 calendar days fresh, 36-65 stale, 66+ expired
        st, age = classify_freshness("2026-07-01", "2026-08-05", DATA_CLASS_INSTITUTIONAL)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 35)

    def test_institutional_stale_36(self):
        st, age = classify_freshness("2026-07-01", "2026-08-06", DATA_CLASS_INSTITUTIONAL)
        self.assertEqual(st, FreshnessStatus.STALE)
        self.assertEqual(age, 36)

    def test_institutional_expired_66(self):
        st, age = classify_freshness("2026-07-01", "2026-09-05", DATA_CLASS_INSTITUTIONAL)
        self.assertEqual(st, FreshnessStatus.EXPIRED)
        self.assertEqual(age, 66)


class TestIndustryPartialDerivedBehavior(unittest.TestCase):
    """Test industry (DC-4) which is derived from institutional (DC-3)."""

    def test_industry_same_thresholds_as_institutional(self):
        """DC-4 must match DC-3 per governance section 5.2."""
        for req in ["2026-08-05", "2026-08-10", "2026-09-05", "2026-10-01"]:
            st_ind, age_ind = classify_freshness(
                "2026-07-01", req, DATA_CLASS_INDUSTRY)
            st_inst, age_inst = classify_freshness(
                "2026-07-01", req, DATA_CLASS_INSTITUTIONAL)
            self.assertEqual(st_ind, st_inst, f"status mismatch for {req}")
            self.assertEqual(age_ind, age_inst, f"age mismatch for {req}")

    def test_industry_unavailable_when_no_source(self):
        st, age = classify_freshness(None, "2026-08-10", DATA_CLASS_INDUSTRY)
        self.assertEqual(st, FreshnessStatus.UNAVAILABLE)

    def test_industry_fresh(self):
        st, age = classify_freshness("2026-07-29", "2026-08-10", DATA_CLASS_INDUSTRY)
        self.assertEqual(st, FreshnessStatus.FRESH)
        self.assertEqual(age, 12)


class TestNoFabricatedScoreFallback(unittest.TestCase):
    """Test that the classification system does not fabricate scores.

    The freshness module is pure metadata — it classifies and reports,
    it never invents data. UNAVAILABLE means no data exists, and the
    score should fall back to neutral (0.0), NOT a fabricated value.
    """

    def test_unavailable_does_not_produce_age(self):
        st, age = classify_freshness(None, "2026-08-07", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.UNAVAILABLE)
        self.assertIsNone(age)

    def test_expired_returns_age_not_none(self):
        """Expired data still reports its age — the caller decides to use it or not."""
        st, age = classify_freshness("2026-01-01", "2026-08-07", DATA_CLASS_MACRO)
        self.assertEqual(st, FreshnessStatus.EXPIRED)
        self.assertIsNotNone(age)
        self.assertGreater(age, 3)

    def test_freshness_metadata_does_not_invent_source_date(self):
        """When source_date is None, metadata must not invent one."""
        m = freshness_metadata(None, "2026-08-07", DATA_CLASS_MACRO)
        self.assertIsNone(m["source_date"])
        self.assertEqual(m["freshness_status"], "UNAVAILABLE")
        self.assertIsNone(m["freshness_age"])


class TestFreshnessWarnings(unittest.TestCase):
    """Test the freshness_warnings helper for CLI output."""

    def test_all_fresh_no_warnings(self):
        cls = {
            "macro_daily": FreshnessStatus.FRESH,
            "stock_monthly": FreshnessStatus.FRESH,
        }
        self.assertEqual(freshness_warnings(cls), [])

    def test_stale_produces_warning(self):
        cls = {"macro_daily": FreshnessStatus.STALE}
        warnings = freshness_warnings(cls)
        self.assertEqual(len(warnings), 1)
        self.assertIn("STALE", warnings[0])

    def test_expired_produces_error(self):
        cls = {"macro_daily": FreshnessStatus.EXPIRED}
        warnings = freshness_warnings(cls)
        self.assertEqual(len(warnings), 1)
        self.assertIn("EXPIRED", warnings[0])

    def test_unavailable_produces_error(self):
        cls = {"macro_daily": FreshnessStatus.UNAVAILABLE}
        warnings = freshness_warnings(cls)
        self.assertEqual(len(warnings), 1)
        self.assertIn("UNAVAILABLE", warnings[0])

    def test_string_values_accepted(self):
        cls = {"macro_daily": "STALE"}
        warnings = freshness_warnings(cls)
        self.assertEqual(len(warnings), 1)


class TestHolidayLoading(unittest.TestCase):
    """Test loading holidays from config file."""

    def test_load_holidays_from_config(self):
        # Create a temp holidays file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"holidays": ["2026-01-01", "2026-02-28"]}, f)
            path = f.name
        try:
            holidays = load_holidays(path)
            self.assertIn("2026-01-01", holidays)
            self.assertIn("2026-02-28", holidays)
            self.assertEqual(len(holidays), 2)
        finally:
            os.unlink(path)

    def test_load_holidays_missing_file_returns_empty(self):
        holidays = load_holidays("/nonexistent/path/holidays.json")
        self.assertEqual(holidays, set())

    def test_load_holidays_invalid_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("invalid json")
            path = f.name
        try:
            holidays = load_holidays(path)
            self.assertEqual(holidays, set())
        finally:
            os.unlink(path)


class TestDataClassMapping(unittest.TestCase):
    """Test data_class_for_signal helper."""

    def test_macro_signal(self):
        self.assertEqual(
            data_class_for_signal("macro", "macro_daily", "macro"),
            DATA_CLASS_MACRO,
        )

    def test_company_stock_monthly(self):
        self.assertEqual(
            data_class_for_signal("company", "stock_monthly", "yfinance"),
            DATA_CLASS_COMPANY,
        )

    def test_company_institutional(self):
        self.assertEqual(
            data_class_for_signal("company", "institutional_daily", "t86"),
            DATA_CLASS_INSTITUTIONAL,
        )

    def test_industry(self):
        self.assertEqual(
            data_class_for_signal("industry", "institutional_daily", "industry_aggregate"),
            DATA_CLASS_INDUSTRY,
        )


class TestTradingDayCount(unittest.TestCase):
    """Test count_trading_days directly."""

    def test_zero_when_same_day(self):
        d = date(2026, 8, 7)
        self.assertEqual(count_trading_days(d, d), 0)

    def test_zero_when_start_after_end(self):
        self.assertEqual(
            count_trading_days(date(2026, 8, 10), date(2026, 8, 7)), 0)

    def test_friday_to_monday(self):
        # Fri Aug 7 -> Mon Aug 10: only Mon is a trading day after Fri
        self.assertEqual(
            count_trading_days(date(2026, 8, 7), date(2026, 8, 10)), 1)

    def test_with_holiday(self):
        holidays = {"2026-08-10"}
        # Fri Aug 7 -> Tue Aug 11 with Mon=holiday: Tue only = 1
        self.assertEqual(
            count_trading_days(
                date(2026, 8, 7), date(2026, 8, 11), holidays), 1)


class TestCalendarDayCount(unittest.TestCase):
    """Test count_calendar_days directly."""

    def test_zero_when_same_day(self):
        d = date(2026, 8, 7)
        self.assertEqual(count_calendar_days(d, d), 0)

    def test_one_day(self):
        self.assertEqual(
            count_calendar_days(date(2026, 8, 7), date(2026, 8, 8)), 1)

    def test_thirty_days(self):
        self.assertEqual(
            count_calendar_days(date(2026, 7, 1), date(2026, 7, 31)), 30)

    def test_zero_when_start_after_end(self):
        self.assertEqual(
            count_calendar_days(date(2026, 8, 10), date(2026, 8, 7)), 0)


class TestComputeAge(unittest.TestCase):
    """Test compute_age for each data class."""

    def test_macro_trading_days(self):
        age = compute_age("2026-08-07", "2026-08-10", DATA_CLASS_MACRO)
        self.assertEqual(age, 1)

    def test_company_calendar_days(self):
        age = compute_age("2026-07-01", "2026-08-05", DATA_CLASS_COMPANY)
        self.assertEqual(age, 35)

    def test_none_source_returns_none(self):
        age = compute_age(None, "2026-08-07", DATA_CLASS_MACRO)
        self.assertIsNone(age)

    def test_institutional_calendar_days(self):
        age = compute_age("2026-07-01", "2026-08-06", DATA_CLASS_INSTITUTIONAL)
        self.assertEqual(age, 36)


if __name__ == "__main__":
    unittest.main()