"""Bridge: read production macro-history tables and seed Phase 3 signal_log.

This module bridges the gap between the production data tables
(``macro_daily``, ``stock_monthly``, ``institutional_daily``) in the
raw layer (``macro_history.db`` or its PostgreSQL twin) and the Phase 3
``signal_log`` table that the IntelligencePipeline reads via
``SignalLoader``.

The bridge is a pure offline mapping — no network calls, no new data
sources. It reads rows that production scripts (macro_daily.py,
company_monthly.py, institutional.py) already fetched via yfinance (or
wrote to the PostgreSQL raw layer) and converts them to the input
format expected by the existing Phase 3 adapters (MacroAdapter,
YFinanceAdapter, T86Adapter), then writes the resulting Signals into
the target store's ``signal_log`` table via
``SignalRepository.upsert_many``.

Backend awareness (WO C1 — PostgreSQL Production Readiness)
-----------------------------------------------------------
Both the **source** (raw layer) and the **target** (intelligence
store) are backend-aware specifier strings:

* a specifier starting with ``postgres://`` / ``postgresql://``
  (case-insensitive) selects the PostgreSQL backend;
* anything else selects the SQLite file path backend.

A PostgreSQL DSN is therefore NEVER interpreted as a filesystem path:
the target goes through :func:`phase3.persistence.backend.open_store`
(the same selector the Phase 3B service surface uses), and the source
is opened by the matching driver. Unsupported backends fail closed —
they raise instead of silently producing an empty seed.

Seed accounting (machine-verifiable)
------------------------------------
The returned accounting dict exposes (WO C1 requirements):

``SOURCE_COUNT``
    Per-source row counts as *selected* by the seeding predicate —
    the explicit/auto-resolved date window and the requested code
    filter. Selection (e.g. the macro window keeps the 5 most recent
    macro rows when no date is given) is a documented domain filter,
    not a rejection: the explained delta between SOURCE_COUNT and
    SEEDED_COUNT comes from (a) field expansion (each source row can
    yield one signal per mapped field) and (b) the date-window
    selection above.
``REJECTED_COUNT``
    Field-value level rejections — the row was read but a mapped
    value was absent/non-numeric (plus negative ``pe_ratio``, which
    the domain intentionally skips). Per-source breakdown + total.
``SEEDED_COUNT``
    The number of :class:`~phase3.datamodel.signals.SignalRecord`
    objects constructed and handed to ``SignalRepository.upsert_many``
    (equals ``total``).

``rc == 0`` alone must never be read as "the seed succeeded" — use the
accounting (and the CLI ``--min-seed-total`` assertion).

Usage:
    from phase3.bridge.seed_signals import seed_from_macro_history
    seed_from_macro_history(
        source_db="postgres://.../raw",        # or an explicit SQLite path
        target_db="postgres://.../intel",      # or an explicit SQLite path
    )

The target_db must NOT be macro_history.db (guarded upstream).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from phase3.datamodel.signals import Signal, SignalSource, make_signal_id
from phase3.freshness import (
    FreshnessStatus,
    data_class_for_signal,
    freshness_metadata,
    load_holidays,
)
from phase3.persistence.signal_repo import SignalRecord, SignalRepository


# ---------------------------------------------------------------------------
# Backend-aware source/target plumbing (WO C1)
# ---------------------------------------------------------------------------

PG_URL_PREFIXES = ("postgres://", "postgresql://")


def _is_pg_spec(specifier: str | None) -> bool:
    """True when ``specifier`` selects the PostgreSQL backend."""
    return bool(specifier) and str(specifier).strip().lower().startswith(PG_URL_PREFIXES)


def _pg_source_sql(sql: str) -> str:
    """Translate SQLite-style ``?`` placeholders to psycopg ``%s``.

    The bridge SQL contains no literal ``?`` characters — every one of
    them is a placeholder, so the translation is a plain replacement
    (the same contract the persistence layer documents).
    """
    return sql.replace("?", "%s")


class _SourceConnection:
    """Backend-dispatched, read-only source connection wrapper.

    ``execute(sql, params)`` returns a cursor-like object supporting
    ``fetchone()`` / ``fetchall()`` and mapping-style rows for both
    SQLite and PostgreSQL, so the ``_seed_*`` readers are unchanged.
    """

    def __init__(self, backend: str, conn: Any) -> None:
        self.backend = backend
        self._conn = conn

    def execute(self, sql: str, params: tuple | list | None = None):
        if self.backend == "postgres":
            return self._conn.execute(
                _pg_source_sql(sql), params if params else None
            )
        return self._conn.execute(sql, tuple(params) if params else ())

    def close(self) -> None:
        if self.backend == "postgres":
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - close is best-effort
                pass
            return
        self._conn.close()


def _open_source(source_db: str) -> _SourceConnection:
    """Open the seed source with the backend its spec selects.

    PostgreSQL DSNs are handed to :class:`PostgresStore` (bounded
    connect/statement timeouts, no SQLite fallback — a failure raises
    and the caller fails closed). SQLite specs open ``sqlite3.connect``
    with mapping rows, exactly as before. Malformed/unsupported specs
    are treated as SQLite paths by design (the same selection rule as
    :data:`phase3.persistence.backend.PG_URL_PREFIXES`), whose failures
    surface as the driver error — never as a silent empty seed.
    """
    if _is_pg_spec(source_db):
        from phase3.persistence.postgres import PostgresStore

        # Store is a DatabaseStore facade (execute/transaction/close);
        # used purely as a read connection here.
        return _SourceConnection("postgres", PostgresStore(source_db.strip()))
    conn = sqlite3.connect(str(os.fspath(source_db)) if source_db else source_db)
    conn.row_factory = sqlite3.Row
    return _SourceConnection("sqlite", conn)


# ---------------------------------------------------------------------------
# Macro: macro_daily table -> MacroAdapter-compatible Signals
# ---------------------------------------------------------------------------

# Fields from macro_daily that map directly to MacroAdapter signal types.
# See phase3/signals/adapters/macro.py _MACRO_FIELDS for the canonical mapping.
def _enrich_with_freshness(
    metadata: dict[str, Any],
    source_date: str | None,
    requested_date: str | None,
    data_class: str,
    holidays: set[str] | None = None,
) -> dict[str, Any]:
    """Add freshness metadata to a signal's metadata dict (additive).

    Uses ``requested_date`` (the date the analysis is FOR) and
    ``source_date`` (the date the data is FROM) to compute freshness
    status and age per the governance policy.

    When ``requested_date`` is None, freshness is not computed and the
    metadata is returned unchanged (no freshness fields added).
    """
    if requested_date is None or source_date is None:
        return metadata
    fresh = freshness_metadata(source_date, requested_date, data_class, holidays)
    result = dict(metadata)
    result.update(fresh)
    return result


_MACRO_FIELD_MAP: dict[str, tuple[str, str]] = {
    # column_name -> (signal_type, unit)
    "us10y": ("us10y", "pct"),
    "us2y": ("us2y", "pct"),
    "us13w": ("us13w", "pct"),
    "dxy": ("dxy", "index"),
    "vix": ("vix", "index"),
    "yield_spread": ("yield_spread", "pp"),
}


def _macro_direction(signal_type: str, value: float) -> str:
    if signal_type == "vix":
        if value > 25:
            return "bearish"
        if value < 15:
            return "bullish"
        return "neutral"
    return "neutral"


def _seed_macro_signals(
    conn: Any,
    date_bucket: str | None = None,
    target_date_bucket: str | None = None,
    requested_date: str | None = None,
    holidays: set[str] | None = None,
) -> tuple[list[SignalRecord], int, int]:
    """Read macro_daily rows and convert to SignalRecord objects.

    Returns ``(records, source_count, rejected_count)`` — the counts
    that feed the machine-verifiable seed accounting (WO C1).

    When ``target_date_bucket`` is provided, all signals are stamped
    with that date_bucket (not the source row's date). This allows
    the pipeline to query a single consistent date_bucket across
    all signal types even when the source tables have different
    cadences.

    When ``requested_date`` is provided, freshness metadata is
    computed and added to each signal's metadata dict.
    """
    if date_bucket:
        cur = conn.execute(
            "SELECT * FROM macro_daily WHERE date = ? ORDER BY date DESC",
            (date_bucket,),
        )
    else:
        cur = conn.execute("SELECT * FROM macro_daily ORDER BY date DESC LIMIT 5")

    rows = cur.fetchall()
    records: list[SignalRecord] = []
    rejected = 0
    for row in rows:
        row_dict = dict(row) if not isinstance(row, dict) else row
        date_str = str(row_dict["date"])
        effective_bucket = target_date_bucket or date_str
        source_id = f"macro_yfinance.{date_str}"
        for col, (signal_type, unit) in _MACRO_FIELD_MAP.items():
            val = row_dict.get(col)
            if val is None:
                rejected += 1
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                rejected += 1
                continue
            sig_id = make_signal_id(source_id, "global", signal_type, effective_bucket)
            records.append(SignalRecord(
                signal_id=sig_id,
                entity_type="macro",
                entity_id="global",
                signal_type=signal_type,
                value=fval,
                unit=unit,
                direction=_macro_direction(signal_type, fval),
                timestamp=f"{date_str}T00:00:00Z",
                date_bucket=effective_bucket,
                source_id=source_id,
                source_type="macro",
                ref="macro_history.db/macro_daily",
                fetched_at=row_dict.get("created_at", ""),
                fetch_id="",
                schema_version="3.0",
                metadata=_enrich_with_freshness(
                    {"source_table": "macro_daily", "column": col, "source_date": date_str},
                    date_str,
                    requested_date,
                    data_class_for_signal("macro", "macro_daily", "macro"),
                    holidays,
                ),
                raw_payload=dict(row_dict),
            ))
    return records, len(rows), rejected


# ---------------------------------------------------------------------------
# Company: stock_monthly table -> YFinanceAdapter-compatible Signals
# ---------------------------------------------------------------------------

# Fields from stock_monthly that map to YFinanceAdapter signal types.
# See phase3/signals/adapters/yfinance.py _INFO_FIELD_MAP for the canonical mapping.
_COMPANY_FIELD_MAP: dict[str, tuple[str, str]] = {
    # column_name -> (signal_type, unit)
    "pe_trailing": ("pe_ratio", "ratio"),
    "pb_ratio": ("pb_ratio", "ratio"),
    "peg_ratio": ("peg_ratio", "ratio"),
    "roe": ("roe", "ratio"),
    "earnings_growth": ("earnings_growth", "ratio"),
    "revenue_growth": ("revenue_growth", "ratio"),
    "dividend_yield": ("dividend_yield", "ratio"),
    "market_cap": ("market_cap", "usd"),
    "gross_margin": ("gross_margin", "ratio"),
}


def _company_direction(signal_type: str, value: float) -> str:
    hints: dict[str, tuple[float, float]] = {
        "pe_ratio": (0.0, 30.0),
        "pb_ratio": (0.0, 5.0),
        "peg_ratio": (0.0, 2.0),
        "roe": (0.10, 0.20),
        "earnings_growth": (0.0, 0.20),
        "revenue_growth": (0.0, 0.20),
        "dividend_yield": (0.02, 0.05),
        "gross_margin": (0.20, 0.40),
    }
    h = hints.get(signal_type)
    if h is None:
        return "neutral"
    low, high = h
    if value < low:
        return "bearish"
    if value > high:
        return "bullish"
    return "neutral"


def _seed_company_signals(
    conn: Any,
    codes: list[str] | None = None,
    date_bucket: str | None = None,
    target_date_bucket: str | None = None,
    requested_date: str | None = None,
    holidays: set[str] | None = None,
) -> tuple[list[SignalRecord], int, int]:
    """Read stock_monthly rows and convert to SignalRecord objects.

    Returns ``(records, source_count, rejected_count)`` (WO C1).

    When ``target_date_bucket`` is provided, all signals are stamped
    with that date_bucket (not the source row's date).

    When ``requested_date`` is provided, freshness metadata is
    computed and added to each signal's metadata dict.
    """
    if date_bucket and codes:
        placeholders = ",".join("?" * len(codes))
        cur = conn.execute(
            f"SELECT * FROM stock_monthly WHERE date = ? AND code IN ({placeholders})",
            [date_bucket] + codes,
        )
    elif date_bucket:
        cur = conn.execute(
            "SELECT * FROM stock_monthly WHERE date = ? ORDER BY code",
            (date_bucket,),
        )
    elif codes:
        placeholders = ",".join("?" * len(codes))
        cur = conn.execute(
            f"SELECT * FROM stock_monthly WHERE code IN ({placeholders}) ORDER BY date DESC",
            codes,
        )
    else:
        cur = conn.execute("SELECT * FROM stock_monthly ORDER BY date DESC")

    rows = cur.fetchall()
    records: list[SignalRecord] = []
    rejected = 0
    for row in rows:
        row_dict = dict(row) if not isinstance(row, dict) else row
        code = str(row_dict["code"])
        date_str = str(row_dict["date"])
        effective_bucket = target_date_bucket or date_str
        source_id = f"yfinance.{code}.TW.{date_str}"
        for col, (signal_type, unit) in _COMPANY_FIELD_MAP.items():
            val = row_dict.get(col)
            if val is None:
                rejected += 1
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                rejected += 1
                continue
            # Skip negative PE
            if signal_type == "pe_ratio" and fval <= 0:
                rejected += 1
                continue
            sig_id = make_signal_id(source_id, code, signal_type, effective_bucket)
            records.append(SignalRecord(
                signal_id=sig_id,
                entity_type="company",
                entity_id=code,
                signal_type=signal_type,
                value=fval,
                unit=unit,
                direction=_company_direction(signal_type, fval),
                timestamp=f"{date_str}T00:00:00Z",
                date_bucket=effective_bucket,
                source_id=source_id,
                source_type="yfinance",
                ref=f"yfinance://{code}.TW",
                fetched_at="",
                fetch_id="",
                schema_version="3.0",
                metadata=_enrich_with_freshness(
                    {
                        "source_table": "stock_monthly",
                        "column": col,
                        "name": row_dict.get("name", ""),
                        "sector": row_dict.get("sector", ""),
                    },
                    date_str,
                    requested_date,
                    data_class_for_signal("company", "stock_monthly", "yfinance"),
                    holidays,
                ),
                raw_payload=dict(row_dict),
            ))
    return records, len(rows), rejected


# ---------------------------------------------------------------------------
# Institutional: institutional_daily table -> T86Adapter-compatible Signals
# ---------------------------------------------------------------------------

_INST_FIELD_MAP: dict[str, str] = {
    "foreign_net": "foreign_net",
    "prop_net": "prop_net",
}


def _inst_direction(value: float) -> str:
    if value > 0:
        return "bullish"
    if value < 0:
        return "bearish"
    return "neutral"


def _seed_institutional_signals(
    conn: Any,
    codes: list[str] | None = None,
    date_bucket: str | None = None,
    target_date_bucket: str | None = None,
    requested_date: str | None = None,
    holidays: set[str] | None = None,
) -> tuple[list[SignalRecord], int, int]:
    """Read institutional_daily rows and convert to SignalRecord objects.

    Returns ``(records, source_count, rejected_count)`` (WO C1).

    When ``target_date_bucket`` is provided, all signals are stamped
    with that date_bucket (not the source row's date).

    When ``requested_date`` is provided, freshness metadata is
    computed and added to each signal's metadata dict.
    """
    if date_bucket and codes:
        placeholders = ",".join("?" * len(codes))
        cur = conn.execute(
            f"SELECT * FROM institutional_daily WHERE date = ? AND code IN ({placeholders})",
            [date_bucket] + codes,
        )
    elif date_bucket:
        cur = conn.execute(
            "SELECT * FROM institutional_daily WHERE date = ? ORDER BY code",
            (date_bucket,),
        )
    elif codes:
        placeholders = ",".join("?" * len(codes))
        cur = conn.execute(
            f"SELECT * FROM institutional_daily WHERE code IN ({placeholders}) ORDER BY date DESC",
            codes,
        )
    else:
        cur = conn.execute("SELECT * FROM institutional_daily ORDER BY date DESC")

    rows = cur.fetchall()
    records: list[SignalRecord] = []
    rejected = 0
    for row in rows:
        row_dict = dict(row) if not isinstance(row, dict) else row
        code = str(row_dict["code"])
        date_str = str(row_dict["date"])
        effective_bucket = target_date_bucket or date_str
        source_id = f"t86.{code}.{date_str}"
        for col, signal_type in _INST_FIELD_MAP.items():
            val = row_dict.get(col)
            if val is None:
                rejected += 1
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                rejected += 1
                continue
            sig_id = make_signal_id(source_id, code, signal_type, effective_bucket)
            records.append(SignalRecord(
                signal_id=sig_id,
                entity_type="company",
                entity_id=code,
                signal_type=signal_type,
                value=fval,
                unit="zhang",
                direction=_inst_direction(fval),
                timestamp=f"{date_str}T00:00:00Z",
                date_bucket=effective_bucket,
                source_id=source_id,
                source_type="t86",
                ref=f"t86://{code}",
                fetched_at="",
                fetch_id="",
                schema_version="3.0",
                metadata=_enrich_with_freshness(
                    {
                        "source_table": "institutional_daily",
                        "column": col,
                        "trading_days": row_dict.get("trading_days", 0),
                    },
                    date_str,
                    requested_date,
                    data_class_for_signal("company", "institutional_daily", "t86"),
                    holidays,
                ),
                raw_payload=dict(row_dict),
            ))
    return records, len(rows), rejected


# ---------------------------------------------------------------------------
# Industry: aggregate institutional_daily by industry_config.json mapping
# ---------------------------------------------------------------------------
#
# The IndustryScorer (phase3/scoring/industry.py) expects Signals with
# entity_type="industry" for these signal types (mapped to dimensions via
# InputBuilder._assign_industry_signal):
#   - foreign_net  -> capital_flow
#   - prop_net     -> capital_flow
#
# Other dimensions (rotation, relative_strength, cyclicality,
# industry_news, macro_sensitivity) require data sources that do NOT
# exist in the local persisted tables (sector ETF perf, PMI, book-to-bill,
# RSS sentiment, etc). Those dimensions will fire the "no signals;
# defaulting to neutral inputs" warning in InputBuilder and produce
# default/neutral sub-scores — which is the correct, evidence-backed
# fallback behaviour (not fabricated data).
#
# The mapping is read from industry_config.json which defines
# supply_chain: { company_name: stock_code } per industry.


def _load_industry_mapping(config_path: str | None = None) -> dict[str, list[str]]:
    """Load industry_config.json and return {industry_id: [stock_codes]}."""
    import os
    if config_path is None:
        # Default path relative to the macro-report repo root
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        config_path = os.path.join(repo_root, "industry_config.json")
    if not os.path.exists(config_path):
        return {}
    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)
    mapping: dict[str, list[str]] = {}
    for industry_id, info in raw.items():
        supply_chain = info.get("supply_chain", {})
        codes = list(supply_chain.values())
        if codes:
            mapping[industry_id] = codes
    return mapping


def _seed_industry_signals(
    conn: Any,
    industry_config_path: str | None = None,
    industry_ids: list[str] | None = None,
    date_bucket: str | None = None,
    target_date_bucket: str | None = None,
    requested_date: str | None = None,
    holidays: set[str] | None = None,
) -> tuple[list[SignalRecord], int, int]:
    """Aggregate institutional_daily into industry-level capital_flow signals.

    Returns ``(records, source_count, rejected_count)`` where
    ``source_count`` is the number of per-industry aggregates actually
    processed (each emits two signals) (WO C1).

    For each industry defined in industry_config.json, sum foreign_net and
    prop_net across all constituent companies for the given date_bucket.
    Emit two Signals per industry: foreign_net and prop_net.

    When ``target_date_bucket`` is provided, all signals are stamped
    with that date_bucket (not the source row's date).

    When ``requested_date`` is provided, freshness metadata is
    computed and added to each signal's metadata dict.

    Only the capital_flow dimension can be populated from local persisted
    data. Other industry dimensions (rotation, relative_strength, cyclicality,
    industry_news, macro_sensitivity) have no local authoritative source and
    will default to neutral — this is correct behaviour, not a bug.
    """
    mapping = _load_industry_mapping(industry_config_path)
    if not mapping:
        return [], 0, 0

    if industry_ids:
        mapping = {k: v for k, v in mapping.items() if k in industry_ids}

    records: list[SignalRecord] = []
    aggregates = 0
    for industry_id, codes in mapping.items():
        if not codes:
            continue
        placeholders = ",".join("?" * len(codes))
        if date_bucket:
            cur = conn.execute(
                f"SELECT "
                f"  COALESCE(SUM(foreign_net), 0) as sum_foreign, "
                f"  COALESCE(SUM(prop_net), 0) as sum_prop, "
                f"  COUNT(*) as row_count "
                f"FROM institutional_daily WHERE date = ? AND code IN ({placeholders})",
                [date_bucket] + codes,
            )
        else:
            # Use the latest date available
            cur = conn.execute(
                f"SELECT "
                f"  COALESCE(SUM(foreign_net), 0) as sum_foreign, "
                f"  COALESCE(SUM(prop_net), 0) as sum_prop, "
                f"  COUNT(*) as row_count, "
                f"  MAX(date) as max_date "
                f"FROM institutional_daily WHERE code IN ({placeholders})",
                codes,
            )
        row = cur.fetchone()
        if row is None:
            continue
        row_dict = dict(row) if not isinstance(row, dict) else row
        row_count = row_dict.get("row_count", 0)
        if row_count == 0:
            continue
        aggregates += 1
        if date_bucket:
            effective_date = date_bucket
        else:
            effective_date = str(row_dict.get("max_date", ""))
            if not effective_date:
                continue

        stamped_bucket = target_date_bucket or effective_date
        source_id = f"industry_aggregate.{industry_id}.{effective_date}"
        sum_foreign = float(row_dict.get("sum_foreign", 0))
        sum_prop = float(row_dict.get("sum_prop", 0))

        for signal_type, val in [("foreign_net", sum_foreign), ("prop_net", sum_prop)]:
            sig_id = make_signal_id(source_id, industry_id, signal_type, stamped_bucket)
            records.append(SignalRecord(
                signal_id=sig_id,
                entity_type="industry",
                entity_id=industry_id,
                signal_type=signal_type,
                value=val,
                unit="zhang",
                direction=_inst_direction(val),
                timestamp=f"{effective_date}T00:00:00Z",
                date_bucket=stamped_bucket,
                source_id=source_id,
                source_type="industry_aggregate",
                ref=f"industry_aggregate://{industry_id}",
                fetched_at="",
                fetch_id="",
                schema_version="3.0",
                metadata=_enrich_with_freshness(
                    {
                        "source_table": "institutional_daily",
                        "aggregation": "sum",
                        "constituent_count": len(codes),
                        "rows_found": row_count,
                        "config_source": "industry_config.json",
                    },
                    effective_date,
                    requested_date,
                    data_class_for_signal("industry", "institutional_daily", "industry_aggregate"),
                    holidays,
                ),
                raw_payload={
                    "industry_id": industry_id,
                    "date": effective_date,
                    signal_type: val,
                    "constituent_codes": codes,
                },
            ))
    return records, aggregates, 0


# ---------------------------------------------------------------------------
# As-of date resolution: deterministic freshness strategy
# ---------------------------------------------------------------------------


def resolve_as_of_dates(
    source_db: str,
    requested_date: str | None = None,
) -> dict[str, str | None]:
    """Resolve the as-of date for each data source given a requested date.

    The macro_daily, stock_monthly, and institutional_daily tables have
    different cadences (daily, monthly, daily) and may not share the
    same date_bucket. This function returns the closest available date
    <= requested_date for each table, or the latest available date if
    requested_date is None or is after the latest data.

    Semantics:
    - macro_date: most recent macro_daily.date <= requested_date
      (or latest if requested_date is None / future)
    - company_date: most recent stock_monthly.date <= requested_date
      (or latest if requested_date is None / future)
    - institutional_date: most recent institutional_daily.date <= requested_date
      (or latest if requested_date is None / future)

    This is a "latest-available-as-of" strategy: the pipeline uses the
    most recent data that existed on or before the requested date. This
    is deterministic and avoids blending arbitrary future data.
    """
    conn = _open_source(source_db)
    result: dict[str, str | None] = {}
    for table, key in [
        ("macro_daily", "macro_date"),
        ("stock_monthly", "company_date"),
        ("institutional_daily", "institutional_date"),
    ]:
        if requested_date:
            cur = conn.execute(
                f"SELECT MAX(date) FROM {table} WHERE date <= ?",
                (requested_date,),
            )
        else:
            cur = conn.execute(f"SELECT MAX(date) FROM {table}")
        row = cur.fetchone()
        result[key] = row[0] if row and row[0] else None
    conn.close()
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def seed_from_macro_history(
    source_db: str | None = None,
    target_db: str | None = None,
    *,
    macro_date: str | None = None,
    company_codes: list[str] | None = None,
    company_date: str | None = None,
    institutional_codes: list[str] | None = None,
    institutional_date: str | None = None,
    industry_ids: list[str] | None = None,
    industry_config_path: str | None = None,
    auto_resolve_dates: bool = False,
    requested_date: str | None = None,
) -> dict[str, Any]:
    """Bridge production data tables into Phase 3 signal_log.

    Reads from source_db (macro_history.db) and writes SignalRecords
    into target_db's signal_log table via SignalRepository.

    When ``auto_resolve_dates=True``, the function calls
    :func:`resolve_as_of_dates` with ``requested_date`` to
    deterministically pick the latest-available date for each table.
    Explicitly passed dates (macro_date, company_date,
    institutional_date) override the auto-resolved values.

    Returns a machine-verifiable accounting dict:
    {macro: N, company: N, institutional: N, industry: N, total: N,
     new_rows: N, resolved_dates: {...},
     SOURCE_COUNT: {...}, SEEDED_COUNT: N, REJECTED_COUNT: {...}}
    """
    # Phase 6.8A: both targets are required (no implicit CWD defaults —
    # the historical "macro_history.db" / "intelligence.db" literals have
    # been abolished).
    if not source_db or not target_db:
        raise ValueError(
            "seed_from_macro_history: source_db and target_db are both "
            "required (no implicit CWD default per runtime contract)")
    # Auto-resolve dates if requested
    resolved_dates: dict[str, str | None] = {}
    if auto_resolve_dates:
        resolved_dates = resolve_as_of_dates(source_db, requested_date)
        # Explicit dates override auto-resolved
        if macro_date is None:
            macro_date = resolved_dates.get("macro_date")
        if company_date is None:
            company_date = resolved_dates.get("company_date")
        if institutional_date is None:
            institutional_date = resolved_dates.get("institutional_date")
    else:
        resolved_dates = {
            "macro_date": macro_date,
            "company_date": company_date,
            "institutional_date": institutional_date,
        }

    # When auto_resolve_dates is used, stamp all signals with the
    # requested_date so the pipeline can query a single consistent
    # date_bucket. This is the "as-of alignment" — data from different
    # cadences (daily macro, monthly company) is unified under one
    # observation date. The original source_date is preserved in
    # each signal's metadata for provenance.
    target_date_bucket = requested_date if auto_resolve_dates and requested_date else None

    # Load TWSE holiday calendar for freshness age calculation
    holidays = load_holidays()

    src_conn = _open_source(source_db)
    source_counts: dict[str, int] = {}
    rejected_counts: dict[str, int] = {}

    # Collect all signal records
    macro_records, macro_src, macro_rej = _seed_macro_signals(
        src_conn, date_bucket=macro_date, target_date_bucket=target_date_bucket,
        requested_date=requested_date, holidays=holidays,
    )
    company_records, company_src, company_rej = _seed_company_signals(
        src_conn, codes=company_codes, date_bucket=company_date,
        target_date_bucket=target_date_bucket,
        requested_date=requested_date, holidays=holidays,
    )
    inst_records, inst_src, inst_rej = _seed_institutional_signals(
        src_conn, codes=institutional_codes, date_bucket=institutional_date,
        target_date_bucket=target_date_bucket,
        requested_date=requested_date, holidays=holidays,
    )
    industry_records, industry_src, industry_rej = _seed_industry_signals(
        src_conn,
        industry_config_path=industry_config_path,
        industry_ids=industry_ids,
        date_bucket=institutional_date,
        target_date_bucket=target_date_bucket,
        requested_date=requested_date, holidays=holidays,
    )
    src_conn.close()
    source_counts = {
        "macro": macro_src,
        "company": company_src,
        "institutional": inst_src,
        "industry_aggregate": industry_src,
    }
    rejected_counts = {
        "macro": macro_rej,
        "company": company_rej,
        "institutional": inst_rej,
        "industry_aggregate": industry_rej,
    }

    all_records = macro_records + company_records + inst_records + industry_records

    # Open the target store and write signals. The store is opened (and
    # its schema ensured) even when the seed is empty: an empty seed must
    # still validate the target (backend reachability/schema) so the
    # accounting can never be confused with an untested no-op (WO C1:
    # fail-closed on unreachable/malformed target configuration).
    from phase3.persistence.backend import resolve_spec, open_store
    from phase3.persistence.migrations import MigrationManager, default_migrations_for

    spec = resolve_spec(target_db)
    # open_store applies the selection rule for BOTH backends: SQLite
    # targets keep the historical SQLiteStore construction (path guard,
    # sqlite:// scheme handling); PostgreSQL DSNs go to PostgresStore.
    # Unreachable/unsupported targets raise here — never a silent no-op.
    store = open_store(spec)
    mgr = MigrationManager(store, default_migrations_for(store))
    mgr.apply()

    new_count = 0
    if all_records:
        repo = SignalRepository(store)
        new_count = repo.upsert_many(all_records)
    else:
        # Readiness probe of the (already valid) store so an unreachable
        # or mis-specified target still fails closed even with nothing
        # to seed.
        store.execute("SELECT 1").fetchone()
    store.close()

    return {
        "macro": len(macro_records),
        "company": len(company_records),
        "institutional": len(inst_records),
        "industry": len(industry_records),
        "total": len(all_records),
        "new_rows": new_count,
        "resolved_dates": resolved_dates,
        "SOURCE_COUNT": source_counts,
        "SOURCE_COUNT_TOTAL": sum(source_counts.values()),
        "SEEDED_COUNT": len(all_records),
        "REJECTED_COUNT": rejected_counts,
        "REJECTED_COUNT_TOTAL": sum(rejected_counts.values()),
        "backend_target": spec.backend,
        "source_kind": "postgres" if _is_pg_spec(source_db) else "sqlite",
    }


__all__ = ["seed_from_macro_history", "resolve_as_of_dates", "PG_URL_PREFIXES"]