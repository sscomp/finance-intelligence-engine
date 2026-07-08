"""YFinanceAdapter — map a yfinance .info-style dict to per-metric Signals.

Input shape
-----------
``raw`` is one of:

  1. A single dict ``{"ticker": "2330.TW", "info": {...yfinance .info...}}``
  2. A list of such dicts
  3. A path to a JSON file containing shape 1 or 2

The adapter does NOT call yfinance itself. The production caller is
responsible for invoking yfinance (offline for tests, live in
production); this adapter is a pure normalizer. The yfinance
``Ticker.info`` payload is a flat dict of named fields — the
adapter extracts the subset that maps cleanly to Signal types
defined in ``config/phase3/decay.yaml`` and emits one Signal per
present metric.

Emitted signal types
--------------------
For each present .info field, we emit exactly one Signal:

    trailingPE             → pe_ratio   (unit: ratio)
    priceToBook            → pb_ratio   (unit: ratio)
    pegRatio               → peg_ratio  (unit: ratio)
    returnOnEquity         → roe        (unit: ratio)
    earningsGrowth         → earnings_growth  (unit: ratio)
    revenueGrowth          → revenue_growth   (unit: ratio)
    beta                   → beta       (unit: ratio)
    dividendYield          → dividend_yield   (unit: ratio)
    marketCap              → market_cap (unit: usd)
    grossMargins           → gross_margin     (unit: ratio)

Missing/None/NaN fields are skipped with a per-metric warning.
Negative PE is skipped (PE <= 0 means loss-making; we don't want
to compare -5 PE to +20 PE in the same signal bucket).
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, Union

from phase3.datamodel.signals import (
    Direction,
    Signal,
    SignalSource,
    make_signal_id,
)

DirectionType = Literal["bullish", "bearish", "neutral"]

from phase3.signals.adapters.base import SourceAdapter


# Map from yfinance .info field → (signal_type, unit)
_INFO_FIELD_MAP: dict[str, tuple[str, str]] = {
    "trailingPE":         ("pe_ratio",        "ratio"),
    "priceToBook":        ("pb_ratio",        "ratio"),
    "pegRatio":           ("peg_ratio",       "ratio"),
    "returnOnEquity":     ("roe",             "ratio"),
    "earningsGrowth":     ("earnings_growth", "ratio"),
    "revenueGrowth":      ("revenue_growth",  "ratio"),
    "beta":               ("beta",            "ratio"),
    "dividendYield":      ("dividend_yield",  "ratio"),
    "marketCap":          ("market_cap",      "usd"),
    "grossMargins":       ("gross_margin",    "ratio"),
}


# Fields where a negative value is a no-go (e.g. PE = -5 means loss-making;
# we don't want it compared to PE = 20 in the same signal bucket).
_NON_NEGATIVE_FIELDS = frozenset({"pe_ratio", "pe", "priceToBook", "trailingPE"})


@dataclass
class YFinanceAdaptResult:
    """Return value from :meth:`YFinanceAdapter.adapt_with_stats`."""

    signals: list[Signal] = field(default_factory=list)
    tickers_processed: int = 0
    tickers_skipped: int = 0
    warnings: list[str] = field(default_factory=list)


class YFinanceAdapter(SourceAdapter):
    """Offline normalizer for yfinance .info payloads."""

    source_type = "yfinance"

    # Direction heuristic thresholds for ratio signals.
    # These are NOT scoring rules — they just let the Signal.direction
    # field carry a sensible default for the scorer. The scorer is
    # allowed to override.
    _RATIO_DIRECTION_HINTS: dict[str, tuple[float, float]] = {
        # signal_type: (low_threshold_for_bearish, high_threshold_for_bullish)
        "pe_ratio":        (0.0, 30.0),    # <0 skip, 0-30 neutral, >30 bearish
        "pb_ratio":        (0.0, 5.0),
        "peg_ratio":       (0.0, 2.0),
        "roe":             (0.10, 0.20),   # ROE >20% bullish, <10% bearish
        "earnings_growth": (0.0, 0.20),
        "revenue_growth":  (0.0, 0.20),
        "beta":            (0.0, 1.5),
        "dividend_yield":  (0.02, 0.05),
        "gross_margin":    (0.20, 0.40),
    }

    def adapt(self, raw: Any) -> list[Signal]:  # type: ignore[override]
        result = self.adapt_with_stats(raw)
        return result.signals

    def adapt_with_stats(self, raw: Any) -> YFinanceAdaptResult:
        result = YFinanceAdaptResult()
        records = self._load_records(raw, result)
        for rec in records:
            ticker = rec.get("ticker", "<unknown>")
            info = rec.get("info", {}) or {}
            if not isinstance(info, Mapping):
                result.warnings.append(
                    f"ticker={ticker}: 'info' must be a dict, got {type(info).__name__}"
                )
                result.tickers_skipped += 1
                continue
            ts_raw = rec.get("timestamp")
            timestamp = self._parse_timestamp(ts_raw) or _utcnow()
            date_bucket = str(rec.get("date_bucket") or timestamp.date().isoformat())
            # entity_id: callers may pass either a raw ticker like "2330.TW"
            # or a code-only id like "2330". We strip the .XX suffix for
            # the entity_id (the scorer groups by entity_id) but keep
            # the full ticker in the source_id and metadata so the
            # signal stays traceable to the original yfinance symbol.
            ticker = str(ticker)
            entity_id = self._entity_id_from_ticker(ticker, rec)
            source_id = f"yfinance.{ticker}.{date_bucket}"
            src = SignalSource(
                source_id=source_id,
                source_type="yfinance",
                ref=f"yfinance://{ticker}",
                fetched_at=_utcnow(),
                metadata={"ticker": ticker, "entity_id": entity_id},
            )
            for field_name, (signal_type, unit) in _INFO_FIELD_MAP.items():
                if field_name not in info:
                    continue
                value = info[field_name]
                cleaned = self._clean_value(value, signal_type, ticker, result)
                if cleaned is None:
                    continue
                signal_id = make_signal_id(source_id, entity_id, signal_type, date_bucket)
                direction = self._direction_for(signal_type, cleaned)
                sig = Signal(
                    signal_id=signal_id,
                    entity_type="company",
                    entity_id=entity_id,
                    signal_type=signal_type,
                    value=cleaned,
                    unit=unit,
                    direction=direction,
                    timestamp=timestamp,
                    source=SignalSource(
                        source_id=source_id,
                        source_type="yfinance",
                        ref=f"yfinance://{ticker}",
                        fetched_at=_utcnow(),
                        metadata={"ticker": ticker, "field": field_name},
                    ),
                    date_bucket=date_bucket,
                    metadata={"ticker": ticker, "field": field_name, "raw": value},
                )
                result.signals.append(sig)
            result.tickers_processed += 1
        return result

    # ----- helpers --------------------------------------------------------

    @staticmethod
    def _clean_value(
        value: Any, signal_type: str, ticker: str, result: YFinanceAdaptResult,
    ) -> float | None:
        """Return a float value, or None if the input should be skipped."""
        if value is None:
            return None
        if isinstance(value, bool):
            # bool is an int subclass; explicit reject.
            result.warnings.append(
                f"ticker={ticker}: {signal_type} value is bool ({value!r}); skipping"
            )
            return None
        if isinstance(value, (int, float)):
            f = float(value)
        else:
            try:
                f = float(value)
            except (TypeError, ValueError):
                result.warnings.append(
                    f"ticker={ticker}: {signal_type} value {value!r} not numeric; skipping"
                )
                return None
        if math.isnan(f) or math.isinf(f):
            result.warnings.append(
                f"ticker={ticker}: {signal_type} is NaN/Inf; skipping"
            )
            return None
        # Negative PE: skip per the module docstring
        if signal_type in _NON_NEGATIVE_FIELDS and f < 0:
            result.warnings.append(
                f"ticker={ticker}: {signal_type}={f} is negative; skipping"
            )
            return None
        return f

    def _direction_for(self, signal_type: str, value: float) -> DirectionType:
        hints = self._RATIO_DIRECTION_HINTS.get(signal_type)
        if hints is None:
            # market_cap, etc. — neutral by default
            return "neutral"
        low, high = hints
        if value < low:
            return "bearish"
        if value > high:
            return "bullish"
        return "neutral"

    @staticmethod
    def _entity_id_from_ticker(ticker: str, rec: Mapping[str, Any]) -> str:
        """Return the entity_id for a ticker.

        Preference order:
          1. ``rec["entity_id"]`` if provided.
          2. Strip the exchange suffix from the ticker: ``2330.TW``
             → ``2330``, ``AAPL`` → ``AAPL``.
          3. If stripping leaves an empty string, fall back to the
             full ticker.
        """
        explicit = rec.get("entity_id")
        if explicit:
            return str(explicit)
        # Strip a single trailing .XX / .XX exchange suffix
        if "." in ticker:
            head = ticker.rsplit(".", 1)[0]
            if head:
                return head
        return ticker

    @staticmethod
    def _load_records(raw: Any, result: YFinanceAdaptResult) -> list[Mapping[str, Any]]:
        """Resolve raw input to a list of {"ticker": ..., "info": ...} dicts."""
        if isinstance(raw, Mapping):
            return [raw]
        if isinstance(raw, (list, tuple)):
            return [r for r in raw if isinstance(r, Mapping)]
        # Try as path
        try:
            path = Path(os.fspath(raw))  # type: ignore[arg-type]
        except TypeError:
            result.warnings.append(
                f"unsupported raw type: {type(raw).__name__}"
            )
            return []
        if not path.exists():
            result.warnings.append(f"yfinance fixture path not found: {path}")
            return []
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            result.warnings.append(f"failed to load {path}: {exc}")
            return []
        if isinstance(data, Mapping):
            return [data]
        if isinstance(data, list):
            return [r for r in data if isinstance(r, Mapping)]
        return []

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["YFinanceAdapter", "YFinanceAdaptResult"]
