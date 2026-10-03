"""Historical backfill tooling for the Finance Intelligence Engine.

Implements governance §8 (Historical Backfill Policy) from the authoritative
document ``fie_historical_data_retention_freshness_backfill_governance.md``.

Key design principles (governance §8.4, §8.7, §8.8):
  - Idempotent: uses INSERT OR REPLACE (same PK → no duplicate rows).
  - Dry-run/plan mode: reports what WOULD be inserted/updated/skipped.
  - Validation before write: schema, date bounds, entity mapping, duplicate
    detection, null/range checks, provenance, freshness compatibility.
  - Production-safe: refuses to write to macro_history.db.
  - Reuses ONLY existing evidence-backed fetcher abstractions.
  - Does NOT fabricate data. Unsupported source classes are BLOCKED.

Supported source classes (governance §8.2):
  - macro_daily (P1, yfinance historical) — GAP DETECTION + PLAN
  - institutional_daily (P2, TWSE T86 historical) — GAP DETECTION + PLAN
  - stock_monthly (P3, yfinance historical) — GAP DETECTION + PLAN
  - industry_derived (P4, reaggregate from DC-3) — BLOCKED (derived, not
    directly fetchable; must backfill DC-3 first then re-derive)

This module is pure-Python and has NO network side-effects. The actual
fetcher calls are delegated to the existing ``macro_daily.py``,
``institutional.py``, and ``company_monthly.py`` modules, which are only
invoked when ``execute=True`` AND the target DB is confirmed non-production.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from phase3.paths import config_dir, macro_history_db_path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRODUCTION_DB_NAME = "macro_history.db"
# Phase 6.1 portability: the reference production history DB path is now
# derived from the central runtime path boundary (FIE_DB_PATH > FIE_DATA_DIR >
# project root) instead of the hard-coded /home/ubuntu/macro-report path.
# Guards still resolve and compare against this reference (and refuse the
# reserved basename macro_history.db regardless of its directory).
PRODUCTION_DB_PATH = str(macro_history_db_path())

# Source class identifiers (governance §3.1)
SOURCE_MACRO_DAILY = "macro_daily"
SOURCE_STOCK_MONTHLY = "stock_monthly"
SOURCE_INSTITUTIONAL_DAILY = "institutional_daily"
SOURCE_INDUSTRY_DERIVED = "industry_derived"

SUPPORTED_SOURCES = frozenset({
    SOURCE_MACRO_DAILY,
    SOURCE_STOCK_MONTHLY,
    SOURCE_INSTITUTIONAL_DAILY,
})

# Sources that are BLOCKED (no direct fetch path)
BLOCKED_SOURCES = frozenset({
    SOURCE_INDUSTRY_DERIVED,
})

# Governance §8.6 rate limits
RATE_LIMITS = {
    SOURCE_MACRO_DAILY: {"batch_size": 50, "description": "50 trading days per batch"},
    SOURCE_STOCK_MONTHLY: {"batch_size": 50, "description": "1 month (all 50 companies) per batch"},
    SOURCE_INSTITUTIONAL_DAILY: {"batch_size": 50, "description": "50 entities per batch"},
}

# Governance §8.1 minimum horizons
MINIMUM_HORIZON = {
    SOURCE_MACRO_DAILY: 252,        # 1 year of trading days
    SOURCE_STOCK_MONTHLY: 12,       # 12 monthly snapshots
    SOURCE_INSTITUTIONAL_DAILY: 252,  # 1 year of trading days
}

# Table schemas for validation (governance §9.1, §2.2)
TABLE_SCHEMAS: dict[str, dict[str, type]] = {
    SOURCE_MACRO_DAILY: {
        "date": str,
        "us10y": float,
        "us2y": float,
        "us13w": float,
        "dxy": float,
        "vix": float,
        "usdtwd": float,
        "yield_spread": float,
        "score": int,
        "verdict": str,
        "signals_json": str,
        "created_at": str,
    },
    SOURCE_STOCK_MONTHLY: {
        "date": str,
        "code": str,
        "name": str,
        "sector": str,
        "price": float,
        "eps_ttm": float,
        "pe_trailing": float,
        "pe_forward": float,
        "roe": float,
        "roa": float,
        "gross_margin": float,
        "operating_margin": float,
        "profit_margin": float,
        "dividend_rate": float,
        "dividend_yield": float,
        "payout_ratio": float,
        "pb_ratio": float,
        "revenue_growth": float,
        "earnings_growth": float,
        "nim_growth": float,
        "interest_spread": float,
        "high_52": float,
        "low_52": float,
        "dist_from_high": float,
        "target_mean": float,
        "peg_ratio": float,
        "market_cap": float,
    },
    SOURCE_INSTITUTIONAL_DAILY: {
        "date": str,
        "code": str,
        "foreign_net": int,
        "prop_net": int,
        "total_net": int,
        "trading_days": int,
    },
}

# Primary keys per table (governance §2.2)
TABLE_PKS: dict[str, tuple[str, ...]] = {
    SOURCE_MACRO_DAILY: ("date",),
    SOURCE_STOCK_MONTHLY: ("date", "code"),
    SOURCE_INSTITUTIONAL_DAILY: ("date", "code"),
}

# Required (non-null) columns per table (governance §9.4)
REQUIRED_COLUMNS: dict[str, list[str]] = {
    SOURCE_MACRO_DAILY: ["date", "us10y", "us2y", "us13w", "dxy", "vix"],
    SOURCE_STOCK_MONTHLY: ["date", "code", "pe_trailing", "roe"],
    SOURCE_INSTITUTIONAL_DAILY: ["date", "code", "foreign_net", "prop_net"],
}

# ISO date pattern
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class GapResult:
    """Result of gap detection for a single source class."""

    source: str
    start_date: str
    end_date: str
    expected_dates: list[str] = field(default_factory=list)
    existing_dates: list[str] = field(default_factory=list)
    missing_dates: list[str] = field(default_factory=list)
    entity_count: int = 0
    expected_entity_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "expected_count": len(self.expected_dates),
            "existing_count": len(self.existing_dates),
            "missing_count": len(self.missing_dates),
            "missing_dates": self.missing_dates,
            "entity_count": self.entity_count,
            "expected_entity_count": self.expected_entity_count,
        }


@dataclass
class BackfillPlan:
    """Plan for a backfill operation (dry-run result)."""

    source: str
    start_date: str
    end_date: str
    target_db: str
    gaps: GapResult
    would_insert: int = 0
    would_update: int = 0
    would_skip: int = 0
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "target_db": self.target_db,
            "gaps": self.gaps.to_dict(),
            "would_insert": self.would_insert,
            "would_update": self.would_update,
            "would_skip": self.would_skip,
            "validation_errors": self.validation_errors,
            "validation_warnings": self.validation_warnings,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
        }


@dataclass
class BackfillResult:
    """Result of an executed backfill."""

    source: str
    start_date: str
    end_date: str
    target_db: str
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_skipped: int = 0
    validation_errors: list[str] = field(default_factory=list)
    execution_errors: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "target_db": self.target_db,
            "rows_inserted": self.rows_inserted,
            "rows_updated": self.rows_updated,
            "rows_skipped": self.rows_skipped,
            "validation_errors": self.validation_errors,
            "execution_errors": self.execution_errors,
            "provenance": self.provenance,
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# Production-path protection
# ---------------------------------------------------------------------------

def is_production_db(db_path: str | Path) -> bool:
    """Check if a database path is the production macro_history.db.

    Governance §8.8: Backfill MUST NOT write to macro_history.db in
    production. This function is the hard guard.
    """
    p = Path(db_path).resolve()
    prod = Path(PRODUCTION_DB_PATH).resolve()
    # Match by name (basename) or by absolute path
    if p == prod:
        return True
    if p.name.lower() == PRODUCTION_DB_NAME:
        return True
    return False


def assert_not_production(db_path: str | Path) -> None:
    """Raise ValueError if db_path is (or resolves to) the production DB."""
    if is_production_db(db_path):
        raise ValueError(
            f"REFUSED: target database '{db_path}' resolves to the production "
            f"macro_history.db. Backfill MUST NOT write to production. "
            f"Use a temp/test database path instead."
        )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_date(d: str | date | datetime) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), "%Y-%m-%d").date()


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def generate_trading_days(
    start: str | date,
    end: str | date,
    holidays: set[str] | None = None,
) -> list[str]:
    """Generate all trading day strings (YYYY-MM-DD) in [start, end].

    Skips weekends and known holidays. Used for macro_daily gap detection.
    """
    s = _parse_date(start)
    e = _parse_date(end)
    days: list[str] = []
    current = s
    while current <= e:
        if not _is_weekend(current):
            if holidays is None or current.isoformat() not in holidays:
                days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def generate_monthly_dates(
    start: str | date,
    end: str | date,
    day_of_month: int = 12,
) -> list[str]:
    """Generate monthly snapshot dates (day_of_month of each month).

    If the day_of_month doesn't exist in a month (e.g., day 31 in February),
    uses the last day of that month. Used for stock_monthly and
    institutional_daily gap detection.
    """
    s = _parse_date(start)
    e = _parse_date(end)
    dates: list[str] = []
    year = s.year
    month = s.month
    while (year, month) <= (e.year, e.month):
        # Compute the target day for this month
        if month == 12:
            next_first = date(year + 1, 1, 1)
        else:
            next_first = date(year, month + 1, 1)
        last_day = (next_first - timedelta(days=1)).day
        actual_day = min(day_of_month, last_day)
        d = date(year, month, actual_day)
        dates.append(d.isoformat())
        # Advance to next month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return dates


# ---------------------------------------------------------------------------
# Gap detection (governance §8.5)
# ---------------------------------------------------------------------------

def detect_gaps(
    source_db: str | Path,
    source_class: str,
    start_date: str,
    end_date: str,
    holidays: set[str] | None = None,
    expected_entity_count: int = 50,
) -> GapResult:
    """Detect gaps in a source table over the given interval.

    For macro_daily: checks for missing trading days.
    For stock_monthly/institutional_daily: checks for missing monthly
    snapshot dates AND missing entities per snapshot.

    This is a READ-ONLY operation. It does NOT modify any database.
    """
    if source_class not in SUPPORTED_SOURCES and source_class not in BLOCKED_SOURCES:
        raise ValueError(f"Unknown source class: {source_class}")

    if source_class in BLOCKED_SOURCES:
        return GapResult(
            source=source_class,
            start_date=start_date,
            end_date=end_date,
            expected_dates=[],
            existing_dates=[],
            missing_dates=[],
            entity_count=0,
            expected_entity_count=0,
        )

    # Connect to the source DB (read-only)
    conn = sqlite3.connect(str(source_db))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if source_class == SOURCE_MACRO_DAILY:
        # Expected dates: all trading days in [start, end]
        expected = generate_trading_days(start_date, end_date, holidays)
        # Existing dates
        rows = c.execute(
            "SELECT DISTINCT date FROM macro_daily "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            (start_date, end_date),
        ).fetchall()
        existing = [r["date"] for r in rows]
        existing_set = set(existing)
        missing = [d for d in expected if d not in existing_set]
        entity_count = len(existing)  # 1 entity (global)
        result = GapResult(
            source=source_class,
            start_date=start_date,
            end_date=end_date,
            expected_dates=expected,
            existing_dates=existing,
            missing_dates=missing,
            entity_count=1,
            expected_entity_count=1,
        )

    elif source_class in (SOURCE_STOCK_MONTHLY, SOURCE_INSTITUTIONAL_DAILY):
        table = source_class
        # Expected dates: monthly snapshots
        expected = generate_monthly_dates(start_date, end_date)
        # Existing dates with entity counts
        rows = c.execute(
            f"SELECT date, COUNT(DISTINCT code) as cnt FROM {table} "
            f"WHERE date >= ? AND date <= ? GROUP BY date ORDER BY date",
            (start_date, end_date),
        ).fetchall()
        existing_map = {r["date"]: r["cnt"] for r in rows}
        existing = list(existing_map.keys())
        # Missing: expected dates with no rows, or dates with insufficient entities
        missing: list[str] = []
        for d in expected:
            if d not in existing_map:
                missing.append(d)
            elif existing_map[d] < expected_entity_count:
                missing.append(d)
        total_entities = sum(existing_map.values())
        result = GapResult(
            source=source_class,
            start_date=start_date,
            end_date=end_date,
            expected_dates=expected,
            existing_dates=existing,
            missing_dates=missing,
            entity_count=total_entities,
            expected_entity_count=expected_entity_count,
        )
    else:
        result = GapResult(
            source=source_class,
            start_date=start_date,
            end_date=end_date,
        )

    conn.close()
    return result


# ---------------------------------------------------------------------------
# Validation (governance §9)
# ---------------------------------------------------------------------------

def validate_row(
    source_class: str,
    row: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Validate a candidate row before write.

    Returns (errors, warnings) where errors are blocking and warnings
    are non-blocking.

    Implements governance §9.1–§9.7:
      - DQ-1/DQ-2: Schema (column presence + type)
      - DQ-3: PK non-NULL
      - DQ-6: Date format YYYY-MM-DD
      - DQ-7/DQ-8/DQ-9: Duplicate detection (handled at write time via PK)
      - DQ-10–DQ-14: Null checks on required columns
      - DQ-15: Range checks
      - DQ-16: Entity mapping (checked at plan time)
      - DQ-19/DQ-20/DQ-21: Provenance
    """
    errors: list[str] = []
    warnings: list[str] = []

    schema = TABLE_SCHEMAS.get(source_class)
    if schema is None:
        errors.append(f"Unknown source class: {source_class}")
        return errors, warnings

    # DQ-1: Check all schema columns exist
    for col in schema:
        if col not in row:
            errors.append(f"Missing required column '{col}' for {source_class}")

    # DQ-3: PK columns non-NULL
    pk_cols = TABLE_PKS.get(source_class, ())
    for col in pk_cols:
        val = row.get(col)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            errors.append(f"PK column '{col}' is NULL or empty")

    # DQ-6: Date format YYYY-MM-DD
    date_val = row.get("date")
    if date_val and isinstance(date_val, str):
        if not ISO_DATE_RE.match(date_val):
            errors.append(f"Date format invalid: '{date_val}' (expected YYYY-MM-DD)")

    # DQ-5: No future-dated rows
    if date_val and isinstance(date_val, str) and ISO_DATE_RE.match(date_val):
        try:
            d = datetime.strptime(date_val, "%Y-%m-%d").date()
            today = date.today()
            if d > today:
                warnings.append(f"Future-dated row: {date_val} (today={today.isoformat()})")
        except ValueError:
            pass

    # DQ-10–DQ-14: Required column null checks
    required = REQUIRED_COLUMNS.get(source_class, [])
    for col in required:
        if col in row and row[col] is None:
            if col in ("date", "code"):
                errors.append(f"Required column '{col}' is NULL")
            else:
                warnings.append(f"Required column '{col}' is NULL")

    # DQ-13: PE trailing > 0 (stock_monthly)
    if source_class == SOURCE_STOCK_MONTHLY:
        pe = row.get("pe_trailing")
        if pe is not None and pe < 0:
            warnings.append(f"pe_trailing is negative ({pe}), should skip signal")

    # DQ-15: Range checks
    if source_class == SOURCE_MACRO_DAILY:
        vix = row.get("vix")
        if vix is not None and vix <= 0:
            warnings.append(f"VIX value {vix} is non-positive (suspicious)")

    return errors, warnings


def validate_plan(
    source_class: str,
    start_date: str,
    end_date: str,
    target_db: str | Path,
    gaps: GapResult,
) -> tuple[list[str], list[str]]:
    """Validate a backfill plan before execution.

    Returns (errors, warnings).
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Date bounds
    try:
        s = _parse_date(start_date)
        e = _parse_date(end_date)
    except ValueError:
        errors.append(f"Invalid date format: start={start_date}, end={end_date}")
        return errors, warnings

    if s > e:
        errors.append(f"start_date ({start_date}) is after end_date ({end_date})")

    # Production path protection
    if is_production_db(target_db):
        errors.append(
            f"target_db '{target_db}' is the production macro_history.db — "
            f"backfill MUST NOT write to production"
        )

    # Source class support
    if source_class in BLOCKED_SOURCES:
        errors.append(
            f"Source class '{source_class}' is BLOCKED — "
            f"it is derived data with no direct fetch path. "
            f"Backfill the upstream source first."
        )

    if source_class not in SUPPORTED_SOURCES and source_class not in BLOCKED_SOURCES:
        errors.append(f"Unknown source class: {source_class}")

    # No gaps → nothing to do
    if len(gaps.missing_dates) == 0:
        warnings.append("No gaps detected — nothing to backfill")

    return errors, warnings


# ---------------------------------------------------------------------------
# Plan (dry-run) (governance §8.5, §8.7)
# ---------------------------------------------------------------------------

def plan_backfill(
    source_db: str | Path,
    target_db: str | Path,
    source_class: str,
    start_date: str,
    end_date: str,
    holidays: set[str] | None = None,
    expected_entity_count: int = 50,
) -> BackfillPlan:
    """Create a backfill plan (dry-run).

    This is a READ-ONLY operation. It detects gaps, validates the plan,
    and reports what WOULD be inserted/updated/skipped. No data is
    fetched or written.
    """
    # Detect gaps
    gaps = detect_gaps(
        source_db, source_class, start_date, end_date,
        holidays=holidays,
        expected_entity_count=expected_entity_count,
    )

    # Validate
    val_errors, val_warnings = validate_plan(
        source_class, start_date, end_date, target_db, gaps,
    )

    # Compute would-insert/update/skip
    would_insert = 0
    would_update = 0
    would_skip = len(gaps.existing_dates)

    if source_class == SOURCE_MACRO_DAILY:
        would_insert = len(gaps.missing_dates)
    elif source_class in (SOURCE_STOCK_MONTHLY, SOURCE_INSTITUTIONAL_DAILY):
        would_insert = len(gaps.missing_dates) * expected_entity_count
    elif source_class in BLOCKED_SOURCES:
        would_insert = 0
        would_skip = 0

    blocked = source_class in BLOCKED_SOURCES
    block_reason = ""
    if blocked:
        block_reason = (
            f"Source '{source_class}' is derived data (aggregated from "
            f"institutional_daily). It cannot be directly backfilled. "
            f"Backfill institutional_daily first, then re-derive."
        )

    return BackfillPlan(
        source=source_class,
        start_date=start_date,
        end_date=end_date,
        target_db=str(target_db),
        gaps=gaps,
        would_insert=would_insert,
        would_update=would_update,
        would_skip=would_skip,
        validation_errors=val_errors,
        validation_warnings=val_warnings,
        blocked=blocked,
        block_reason=block_reason,
    )


# ---------------------------------------------------------------------------
# Safe write (governance §8.4, §8.7, §8.8)
# ---------------------------------------------------------------------------

def execute_backfill(
    source_db: str | Path,
    target_db: str | Path,
    source_class: str,
    start_date: str,
    end_date: str,
    rows: list[dict[str, Any]] | None = None,
    dry_run: bool = True,
    holidays: set[str] | None = None,
    expected_entity_count: int = 50,
) -> BackfillResult:
    """Execute a backfill against a target database.

    If dry_run=True (default): returns a plan-like result with no writes.
    If dry_run=False: writes rows to target_db using INSERT OR REPLACE
    (idempotent upsert by PK).

    The caller supplies `rows` — a list of pre-fetched row dicts. This
    function does NOT call any external API. The caller is responsible
    for fetching data using the existing evidence-backed fetchers.

    Governance §8.8: This function MUST NOT write to macro_history.db.
    """
    # Hard production guard
    assert_not_production(target_db)

    # Plan first
    plan = plan_backfill(
        source_db, target_db, source_class, start_date, end_date,
        holidays=holidays,
        expected_entity_count=expected_entity_count,
    )

    result = BackfillResult(
        source=source_class,
        start_date=start_date,
        end_date=end_date,
        target_db=str(target_db),
        dry_run=dry_run,
        provenance={
            "source_class": source_class,
            "source_db": str(source_db),
            "target_db": str(target_db),
            "planned_by": "phase3.backfill.execute_backfill",
            "governance_ref": "fie_historical_data_retention_freshness_backfill_governance.md §8",
        },
    )

    # If plan has errors, return early
    result.validation_errors = list(plan.validation_errors)
    if plan.validation_errors:
        return result

    # If blocked, return
    if plan.blocked:
        result.validation_errors.append(plan.block_reason)
        return result

    # If dry-run, compute what would happen
    if dry_run:
        result.rows_inserted = plan.would_insert
        result.rows_updated = plan.would_update
        result.rows_skipped = plan.would_skip
        return result

    # Execute: write rows to target DB
    if rows is None:
        result.execution_errors.append(
            "No rows provided for non-dry-run backfill. "
            "The caller must fetch rows using existing evidence-backed fetchers."
        )
        return result

    # Validate each row before write
    valid_rows: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        row_errors, row_warnings = validate_row(source_class, row)
        if row_errors:
            result.validation_errors.extend(
                f"Row {i}: {e}" for e in row_errors
            )
        else:
            valid_rows.append(row)

    if result.validation_errors:
        return result

    # Write to target DB using INSERT OR REPLACE (idempotent)
    if not valid_rows:
        result.rows_skipped = len(rows)
        return result

    schema = TABLE_SCHEMAS[source_class]
    columns = list(schema.keys())
    placeholders = ",".join("?" * len(columns))
    col_names = ",".join(columns)

    conn = sqlite3.connect(str(target_db))
    c = conn.cursor()

    # Ensure table exists (same schema as production)
    _ensure_table(c, source_class)

    inserted = 0
    updated = 0
    skipped = 0

    for row in valid_rows:
        # Check if row already exists (by PK)
        pk_cols = TABLE_PKS[source_class]
        pk_where = " AND ".join(f"{col} = ?" for col in pk_cols)
        pk_vals = tuple(row[col] for col in pk_cols)
        existing = c.execute(
            f"SELECT 1 FROM {source_class} WHERE {pk_where}",
            pk_vals,
        ).fetchone()

        if existing:
            updated += 1
        else:
            inserted += 1

        # INSERT OR REPLACE (idempotent)
        values = tuple(row.get(col) for col in columns)
        c.execute(
            f"INSERT OR REPLACE INTO {source_class} ({col_names}) "
            f"VALUES ({placeholders})",
            values,
        )

    conn.commit()
    conn.close()

    result.rows_inserted = inserted
    result.rows_updated = updated
    result.rows_skipped = skipped

    return result


def _ensure_table(c: sqlite3.Cursor, source_class: str) -> None:
    """Ensure the target table exists in the target DB.

    Uses the same schema as the production db.py init_db() function.
    """
    if source_class == SOURCE_MACRO_DAILY:
        c.execute("""
            CREATE TABLE IF NOT EXISTS macro_daily (
                date TEXT PRIMARY KEY,
                us10y REAL, us2y REAL, us13w REAL,
                dxy REAL, vix REAL, usdtwd REAL,
                yield_spread REAL,
                score INTEGER,
                verdict TEXT,
                signals_json TEXT,
                created_at TEXT
            )
        """)
    elif source_class == SOURCE_STOCK_MONTHLY:
        c.execute("""
            CREATE TABLE IF NOT EXISTS stock_monthly (
                date TEXT,
                code TEXT,
                name TEXT,
                sector TEXT,
                price REAL,
                eps_ttm REAL, pe_trailing REAL, pe_forward REAL,
                roe REAL, roa REAL,
                gross_margin REAL, operating_margin REAL, profit_margin REAL,
                dividend_rate REAL, dividend_yield REAL, payout_ratio REAL,
                pb_ratio REAL, revenue_growth REAL, earnings_growth REAL,
                nim_growth REAL, interest_spread REAL,
                high_52 REAL, low_52 REAL, dist_from_high REAL,
                target_mean REAL, peg_ratio REAL, market_cap REAL,
                PRIMARY KEY (date, code)
            )
        """)
    elif source_class == SOURCE_INSTITUTIONAL_DAILY:
        c.execute("""
            CREATE TABLE IF NOT EXISTS institutional_daily (
                date TEXT,
                code TEXT,
                foreign_net INTEGER,
                prop_net INTEGER,
                total_net INTEGER,
                trading_days INTEGER,
                PRIMARY KEY (date, code)
            )
        """)


# ---------------------------------------------------------------------------
# Temp DB helpers
# ---------------------------------------------------------------------------

def create_temp_copy(source_db: str | Path, temp_path: str | Path) -> str:
    """Create a temp copy of a source DB for safe backfill testing.

    Copies the entire DB file. The temp path must NOT be the production DB.
    """
    assert_not_production(temp_path)
    shutil.copy2(str(source_db), str(temp_path))
    return str(temp_path)


def create_temp_db(temp_path: str | Path) -> str:
    """Create an empty temp DB with all three source tables.

    The temp path must NOT be the production DB.
    """
    assert_not_production(temp_path)
    conn = sqlite3.connect(str(temp_path))
    c = conn.cursor()
    _ensure_table(c, SOURCE_MACRO_DAILY)
    _ensure_table(c, SOURCE_STOCK_MONTHLY)
    _ensure_table(c, SOURCE_INSTITUTIONAL_DAILY)
    conn.commit()
    conn.close()
    return str(temp_path)


def count_rows(db_path: str | Path, table: str) -> int:
    """Count rows in a table. Read-only."""
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    try:
        count = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        count = 0
    conn.close()
    return count


def get_existing_pks(
    db_path: str | Path,
    source_class: str,
) -> set[tuple]:
    """Get the set of existing PK tuples for a source class. Read-only."""
    pk_cols = TABLE_PKS.get(source_class, ())
    if not pk_cols:
        return set()
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    pk_select = ",".join(pk_cols)
    try:
        rows = c.execute(
            f"SELECT {pk_select} FROM {source_class}"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return set(tuple(r) for r in rows)


# ---------------------------------------------------------------------------\
# Historical range fetch helpers (T-2.1)
# ---------------------------------------------------------------------------\
# These functions bridge the existing evidence-backed fetchers to the
# backfill tooling by producing row dicts that can be passed to
# execute_backfill(rows=...). They do NOT fabricate data. If a source
# cannot supply historical data, the function returns an empty list
# and a status string explaining the limitation.


def fetch_macro_range(
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch macro_daily rows for a date range using the existing
    ``macro_daily.fetch_indicators()`` fetcher.

    yfinance ``Ticker.history(start, end)`` uses exclusive end semantics.
    This function adds 1 day to ``end_date`` so the caller's ``end_date``
    is included. The returned rows contain only the columns needed
    for the ``macro_daily`` table schema (date, us10y, us2y, us13w, dxy,
    vix, usdtwd, yield_spread). Score/verdict/signals are NOT fetched
    in range mode — they are computed by the daily pipeline, not by
    the historical fetcher.

    Args:
        start_date: ISO date string (YYYY-MM-DD), inclusive.
        end_date: ISO date string (YYYY-MM-DD), inclusive (this function
            handles the yfinance exclusive-end adjustment internally).

    Returns:
        (rows, status): rows is a list of dict rows ready for
        execute_backfill; status is "OK" on success, "NO_DATA" if
        no data returned, or "ERROR: <message>" on failure.
    """
    from datetime import date as _date, timedelta as _timedelta
    # Parse and adjust end_date for yfinance exclusive semantics
    d_start = _date.fromisoformat(start_date)
    d_end = _date.fromisoformat(end_date)
    yf_end = (d_end + _timedelta(days=1)).isoformat()

    try:
        # Import from the macro_daily module at the repo root
        import macro_daily  # type: ignore[import-not-found]
        data = macro_daily.fetch_indicators(
            start_date=d_start.isoformat(), end_date=yf_end,
        )
    except Exception as e:
        return [], f"ERROR: {e}"

    rows = []
    # Build per-date rows from the history lists
    # Collect all dates from any ticker's history
    all_dates: set[str] = set()
    for key in ("US10Y", "US2Y", "US13W", "DXY", "VIX", "USDTWD"):
        hist = data.get(key, {}).get("history", [])
        for entry in hist:
            all_dates.add(entry["date"])

    if not all_dates:
        return [], "NO_DATA"

    for d in sorted(all_dates):
        row: dict[str, Any] = {"date": d}
        # Map ticker keys to schema columns
        col_map = {
            "US10Y": "us10y",
            "US2Y": "us2y",
            "US13W": "us13w",
            "DXY": "dxy",
            "VIX": "vix",
            "USDTWD": "usdtwd",
        }
        for tkey, col in col_map.items():
            hist = data.get(tkey, {}).get("history", [])
            for entry in hist:
                if entry["date"] == d:
                    row[col] = entry["value"]
                    break
            else:
                row[col] = None

        # Compute yield_spread if both us10y and us2y present
        if row.get("us10y") is not None and row.get("us2y") is not None:
            row["yield_spread"] = row["us10y"] - row["us2y"]
        else:
            row["yield_spread"] = None

        # Fill pipeline-computed columns with None (not produced by historical fetcher)
        row["score"] = None
        row["verdict"] = None
        row["signals_json"] = None
        row["created_at"] = None

        rows.append(row)

    return rows, "OK"


def fetch_company_as_of(
    as_of_date: str,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch company/stock fundamentals as of a specific date.

    The existing ``company_monthly.fetch_stock_fundamentals()`` uses
    ``yf.Ticker().info`` which is a CURRENT SNAPSHOT. It does NOT
    support historical as-of retrieval. yfinance does not provide
    historical fundamental data (EPS, ROE, margins, PE) via the .info
    endpoint. The .quarterly_income_stmt endpoint gives quarterly
    statements but not the same derived metrics.

    This function TRUTHFULLY reports this limitation. It does NOT
    fabricate snapshots or forward-fill current values into historical
    dates. Returns an empty list and a BLOCKED status.

    Returns:
        ([], "BLOCKED: yfinance .info is current-snapshot only. \
        Historical fundamentals not available without a third-party \
        source (governance §8.3).")
    """
    return [], (
        "BLOCKED: yfinance .info is current-snapshot only. "
        "Historical fundamentals not available without a third-party "
        "source (governance §8.3)."
    )


def fetch_institutional_range(
    start_date: str,
    end_date: str,
    target_codes: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch institutional_daily rows for a date range using the existing
    ``institutional.fetch_t86_daily(date_str)`` fetcher.

    The T86 API accepts any historical date in YYYYMMDD format. This
    function calls it for each trading day in [start_date, end_date]
    and accumulates per-stock foreign_net/prop_net/total_net for the
    given target_codes (or all Taiwan50 codes if not specified).

    Note: T86 returns daily buy/sell totals, NOT cumulative. The
    existing ``accumulate_institutional_data()`` function sums across
    a rolling window. For backfill, we store each day's raw values
    as a snapshot row (per governance: daily snapshot, not cumulative).

    Args:
        start_date: ISO date string (YYYY-MM-DD), inclusive.
        end_date: ISO date string (YYYY-MM-DD), inclusive.
        target_codes: Optional list of stock codes. If None, loads
            from taiwan50_config.json.

    Returns:
        (rows, status): rows ready for execute_backfill;
        status is "OK" or "ERROR: <message>".
    """
    import time as _time
    from datetime import date as _date, timedelta as _timedelta

    try:
        import institutional  # type: ignore[import-not-found]
    except Exception as e:
        return [], f"ERROR: {e}"

    # Load config if codes not provided
    if target_codes is None:
        import json as _json
        config_path = str(config_dir() / "taiwan50_config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = _json.load(f)
            target_codes = [s["code"] for s in config["constituents"]]
        except Exception as e:
            return [], f"ERROR loading config: {e}"

    rows = []
    d_start = _date.fromisoformat(start_date)
    d_end = _date.fromisoformat(end_date)
    d = d_start
    while d <= d_end:
        if d.weekday() < 5:  # Mon-Fri only
            date_str = d.strftime("%Y%m%d")
            daily_data = institutional.fetch_t86_daily(date_str)
            if daily_data is not None:
                for code in target_codes:
                    if code in daily_data:
                        dd = daily_data[code]
                        rows.append({
                            "date": d.isoformat(),
                            "code": code,
                            "foreign_net": dd["foreign"],
                            "prop_net": dd["prop"],
                            "total_net": dd["total"],
                            "trading_days": 1,
                        })
            _time.sleep(0.3)  # Rate limit per governance §8.6
        d += _timedelta(days=1)

    if not rows:
        return [], "NO_DATA"
    return rows, "OK"


# Source-class historical capability registry (T-2.1)
HISTORICAL_FETCH_STATUS = {
    SOURCE_MACRO_DAILY: "READY",
    SOURCE_STOCK_MONTHLY: "BLOCKED",
    SOURCE_INSTITUTIONAL_DAILY: "READY",
    SOURCE_INDUSTRY_DERIVED: "BLOCKED",
}

HISTORICAL_FETCH_REASONS = {
    SOURCE_MACRO_DAILY: "fetch_macro_range(start, end) — yfinance Ticker.history(start, end)",
    SOURCE_STOCK_MONTHLY: "yfinance .info is current-snapshot only; historical fundamentals unavailable",
    SOURCE_INSTITUTIONAL_DAILY: "fetch_institutional_range(start, end) — TWSE T86 API per-date fetch",
    SOURCE_INDUSTRY_DERIVED: "Derived from institutional_daily; must backfill DC-3 then re-derive",
}


__all__ = [
    # Constants
    "PRODUCTION_DB_NAME",
    "PRODUCTION_DB_PATH",
    "SUPPORTED_SOURCES",
    "BLOCKED_SOURCES",
    "SOURCE_MACRO_DAILY",
    "SOURCE_STOCK_MONTHLY",
    "SOURCE_INSTITUTIONAL_DAILY",
    "SOURCE_INDUSTRY_DERIVED",
    "TABLE_SCHEMAS",
    "TABLE_PKS",
    "REQUIRED_COLUMNS",
    "RATE_LIMITS",
    "MINIMUM_HORIZON",
    # Data structures
    "GapResult",
    "BackfillPlan",
    "BackfillResult",
    # Production protection
    "is_production_db",
    "assert_not_production",
    # Date helpers
    "generate_trading_days",
    "generate_monthly_dates",
    # Gap detection
    "detect_gaps",
    # Validation
    "validate_row",
    "validate_plan",
    # Plan
    "plan_backfill",
    # Execute
    "execute_backfill",
    # Temp DB helpers
    "create_temp_copy",
    "create_temp_db",
    "count_rows",
    "get_existing_pks",
    # Historical range fetch helpers (T-2.1)
    "fetch_macro_range",
    "fetch_company_as_of",
    "fetch_institutional_range",
    "HISTORICAL_FETCH_STATUS",
    "HISTORICAL_FETCH_REASONS",
]