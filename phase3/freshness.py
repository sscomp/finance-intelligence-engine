"""Freshness / staleness classification for historical-source consumption.

Implements the authoritative freshness policy from the governance document
``fie_historical_data_retention_freshness_backfill_governance.md`` §5.

Per-data-class policy (§5.2):

| Data Class | Freshness Window | Warning/Stale | Hard-Expiry | Day Type |
|---|---|---|---|---|
| DC-1 macro_daily   | <= 1 trading day  | > 1 to <= 3 trading days | > 3 trading days  | Trading day |
| DC-2 stock_monthly | <= 35 calendar    | > 35 to <= 65 calendar   | > 65 calendar    | Calendar day |
| DC-3 institutional | <= 35 calendar    | > 35 to <= 65 calendar   | > 65 calendar    | Calendar day |
| DC-4 industry      | same as DC-3      | same as DC-3             | same as DC-3     | Calendar day |

Freshness status enum (§5.4):

    FRESH       — age <= freshness window
    STALE       — age > freshness window but <= hard-expiry
    EXPIRED     — age > hard-expiry
    UNAVAILABLE — no data exists at all

This module is **pure**: no I/O, no database access, no side effects.
Callers supply the dates; this module classifies.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any


class FreshnessStatus(str, Enum):
    """Freshness classification per governance §5.4."""

    FRESH = "FRESH"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Policy constants — governance §5.2
# ---------------------------------------------------------------------------

# Data class identifiers matching governance §3.1
DATA_CLASS_MACRO = "macro_daily"
DATA_CLASS_COMPANY = "stock_monthly"
DATA_CLASS_INSTITUTIONAL = "institutional_daily"
DATA_CLASS_INDUSTRY = "industry_derived"

# Per-data-class thresholds.
# Values are in the unit appropriate to the data class:
#   trading days for macro_daily (DC-1)
#   calendar days for stock_monthly (DC-2), institutional_daily (DC-3), industry (DC-4)
FRESHNESS_POLICY: dict[str, dict[str, int | str]] = {
    DATA_CLASS_MACRO: {
        "freshness_window": 1,
        "stale_threshold": 3,
        "hard_expiry": 3,
        "day_type": "trading",
    },
    DATA_CLASS_COMPANY: {
        "freshness_window": 35,
        "stale_threshold": 65,
        "hard_expiry": 65,
        "day_type": "calendar",
    },
    DATA_CLASS_INSTITUTIONAL: {
        "freshness_window": 35,
        "stale_threshold": 65,
        "hard_expiry": 65,
        "day_type": "calendar",
    },
    DATA_CLASS_INDUSTRY: {
        "freshness_window": 35,
        "stale_threshold": 65,
        "hard_expiry": 65,
        "day_type": "calendar",
    },
}

# Mapping from source_table (as stored in SignalRecord.metadata) to data class.
SOURCE_TABLE_TO_DATA_CLASS: dict[str, str] = {
    "macro_daily": DATA_CLASS_MACRO,
    "stock_monthly": DATA_CLASS_COMPANY,
    "institutional_daily": DATA_CLASS_INSTITUTIONAL,
}

# Industry signals have source_table="institutional_daily" (they're derived
# from it) but are classified under DATA_CLASS_INDUSTRY. The caller must
# pass the correct data_class explicitly when classifying industry signals.


# ---------------------------------------------------------------------------
# Holiday calendar
# ---------------------------------------------------------------------------

def load_holidays(config_path: str | Path | None = None) -> set[str]:
    """Load TWSE holiday dates from config/holidays.json.

    Returns a set of ISO date strings ("YYYY-MM-DD"). If the file does not
    exist or is not provided, returns an empty set (no holidays known).

    The holidays file is a simple JSON object:
        {"holidays": ["2026-01-01", "2026-02-28", ...]}
    """
    if config_path is None:
        # Default: config/holidays.json relative to the macro-report repo root
        repo_root = Path(__file__).resolve().parent.parent
        config_path = repo_root / "config" / "holidays.json"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        return set()

    try:
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()

    holidays = raw.get("holidays", []) if isinstance(raw, dict) else []
    return {str(h) for h in holidays}


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_date(d: str | date | datetime) -> date:
    """Parse a date-like value to a date object."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    # String: expect ISO format YYYY-MM-DD
    return datetime.strptime(str(d), "%Y-%m-%d").date()


def _is_weekend(d: date) -> bool:
    """Saturday=5, Sunday=6."""
    return d.weekday() >= 5


def _is_trading_day(d: date, holidays: set[str] | None = None) -> bool:
    """Check if a date is a trading day (not weekend, not holiday)."""
    if _is_weekend(d):
        return False
    if holidays and d.isoformat() in holidays:
        return False
    return True


def count_trading_days(start: date, end: date, holidays: set[str] | None = None) -> int:
    """Count trading days between start (exclusive) and end (inclusive).

    Counts days where the market is open (weekday, not holiday) that fall
    in the half-open interval (start, end]. If start >= end, returns 0.

    This is used for DC-1 (macro_daily) freshness age calculation:
    age = number of trading days the data is "behind" the requested date.
    """
    if start >= end:
        return 0
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if _is_trading_day(current, holidays):
            count += 1
        current += timedelta(days=1)
    return count


def count_calendar_days(start: date, end: date) -> int:
    """Count calendar days between start (exclusive) and end (inclusive).

    If start >= end, returns 0.
    """
    if start >= end:
        return 0
    return (end - start).days


# ---------------------------------------------------------------------------
# Freshness classification
# ---------------------------------------------------------------------------

def compute_age(
    source_date: str | date | datetime | None,
    requested_date: str | date | datetime,
    data_class: str,
    holidays: set[str] | None = None,
) -> int | None:
    """Compute the age of data in the unit appropriate for the data class.

    For DC-1 (macro_daily): age in trading days (skipping weekends/holidays).
    For DC-2/DC-3/DC-4: age in calendar days.

    Returns None if source_date is None (no data available).
    """
    if source_date is None:
        return None

    src = _parse_date(source_date)
    req = _parse_date(requested_date)

    policy = FRESHNESS_POLICY.get(data_class)
    if policy is None:
        # Unknown data class: default to calendar days
        return count_calendar_days(src, req)

    if policy["day_type"] == "trading":
        return count_trading_days(src, req, holidays)
    else:
        return count_calendar_days(src, req)


def classify_freshness(
    source_date: str | date | datetime | None,
    requested_date: str | date | datetime,
    data_class: str,
    holidays: set[str] | None = None,
) -> tuple[FreshnessStatus, int | None]:
    """Classify freshness of a data source given its source_date and the
    requested_date it's being consumed for.

    Returns (FreshnessStatus, age) where age is in the unit appropriate for
    the data class (trading days for macro, calendar days for others).
    age is None when status is UNAVAILABLE.

    Implements governance §5.2 and §5.4 exactly:
    - FRESH: age <= freshness_window
    - STALE: freshness_window < age <= hard_expiry (stale_threshold)
    - EXPIRED: age > hard_expiry
    - UNAVAILABLE: source_date is None
    """
    if source_date is None:
        return FreshnessStatus.UNAVAILABLE, None

    age = compute_age(source_date, requested_date, data_class, holidays)
    if age is None:
        return FreshnessStatus.UNAVAILABLE, None

    policy = FRESHNESS_POLICY.get(data_class)
    if policy is None:
        # Unknown data class: treat as FRESH if any data exists
        return FreshnessStatus.FRESH, age

    freshness_window: int = int(policy["freshness_window"])  # type: ignore[arg-type]
    hard_expiry: int = int(policy["hard_expiry"])  # type: ignore[arg-type]

    if age <= freshness_window:
        return FreshnessStatus.FRESH, age
    elif age <= hard_expiry:
        return FreshnessStatus.STALE, age
    else:
        return FreshnessStatus.EXPIRED, age


def freshness_metadata(
    source_date: str | date | datetime | None,
    requested_date: str | date | datetime,
    data_class: str,
    holidays: set[str] | None = None,
) -> dict[str, Any]:
    """Build the freshness metadata dict to attach to a SignalRecord.

    Returns a dict with:
        freshness_status: str (FRESH/STALE/EXPIRED/UNAVAILABLE)
        freshness_age: int | None
        requested_date: str (ISO)
        source_date: str | None (ISO)
        data_class: str
        day_type: str ("trading" | "calendar")
    """
    status, age = classify_freshness(
        source_date, requested_date, data_class, holidays
    )
    req_str = _parse_date(requested_date).isoformat()
    src_str = None
    if source_date is not None:
        src_str = _parse_date(source_date).isoformat()

    policy = FRESHNESS_POLICY.get(data_class, {})
    day_type = policy.get("day_type", "calendar")

    return {
        "freshness_status": status.value,
        "freshness_age": age,
        "requested_date": req_str,
        "source_date": src_str,
        "data_class": data_class,
        "day_type": day_type,
    }


def freshness_warnings(
    classifications: dict[str, FreshnessStatus | str],
) -> list[str]:
    """Given a mapping of data_class -> freshness_status (str or enum),
    return a list of human-readable warning strings for any class that is
    not FRESH and not UNAVAILABLE (which is reported separately).

    Used by the CLI --freshness-check flag to emit warnings.
    """
    warnings: list[str] = []
    for data_class, status in classifications.items():
        if isinstance(status, FreshnessStatus):
            s = status.value
        else:
            s = str(status)
        if s == FreshnessStatus.STALE.value:
            warnings.append(
                f"WARNING: {data_class} data is STALE "
                f"(exceeds freshness window but within hard-expiry)"
            )
        elif s == FreshnessStatus.EXPIRED.value:
            warnings.append(
                f"ERROR: {data_class} data is EXPIRED "
                f"(exceeds hard-expiry threshold — should not be used for scoring)"
            )
        elif s == FreshnessStatus.UNAVAILABLE.value:
            warnings.append(
                f"ERROR: {data_class} data is UNAVAILABLE "
                f"(no data exists for this data class)"
            )
    return warnings


# ---------------------------------------------------------------------------
# Data-class mapping for bridge integration
# ---------------------------------------------------------------------------

# Map from the bridge's entity_type + source_table to the governance data class.
def data_class_for_signal(
    entity_type: str,
    source_table: str | None = None,
    source_type: str | None = None,
) -> str:
    """Determine the governance data class for a signal.

    Industry signals (entity_type="industry") are DC-4.
    Company signals from stock_monthly are DC-2.
    Company signals from institutional_daily are DC-3.
    Macro signals (entity_type="macro") are DC-1.
    """
    if entity_type == "macro":
        return DATA_CLASS_MACRO
    if entity_type == "industry":
        return DATA_CLASS_INDUSTRY
    if entity_type == "company":
        if source_table == "institutional_daily" or source_type == "t86":
            return DATA_CLASS_INSTITUTIONAL
        return DATA_CLASS_COMPANY
    # Unknown entity type: default to calendar-day classification
    return DATA_CLASS_COMPANY


__all__ = [
    "FreshnessStatus",
    "FRESHNESS_POLICY",
    "DATA_CLASS_MACRO",
    "DATA_CLASS_COMPANY",
    "DATA_CLASS_INSTITUTIONAL",
    "DATA_CLASS_INDUSTRY",
    "SOURCE_TABLE_TO_DATA_CLASS",
    "load_holidays",
    "compute_age",
    "classify_freshness",
    "freshness_metadata",
    "freshness_warnings",
    "data_class_for_signal",
    "count_trading_days",
    "count_calendar_days",
]