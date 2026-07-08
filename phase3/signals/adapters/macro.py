"""MacroAdapter — turn a daily macro indicator dict into macro Signals.

Input shape
-----------
``raw`` is one of:

  1. A dict whose keys are macro field names. Recognized fields
     include:

       - fed_rate, us10y, us2y, us13w, dxy, vix, cpi_yoy,
         core_cpi_yoy, m2_yoy, credit_spread_bp, yield_spread,
         rate_hike_flag

     All values are numeric; ``None`` / missing fields are skipped
     (with a per-field warning).

  2. A list of such dicts (one row per day / per source).

  3. A path to a JSON file containing shape 1 or 2.

Signal_type mapping
-------------------
We align with the existing ``config/phase3/source_weights.yaml``
``macro_yfinance`` source type and the ``config/phase3/decay.yaml``
rule names:

    fed_rate        → fed_rate
    us10y           → us10y
    vix             → vix
    dxy             → dxy
    cpi_yoy         → cpi_yoy
    core_cpi_yoy    → core_cpi_yoy
    m2_yoy          → m2_yoy
    credit_spread_bp → credit_spread
    yield_spread    → yield_spread
    rate_hike_flag  → rate_hike        (when value > 0, signalled as +1)

For each emitted signal, entity_type is ``"macro"`` and entity_id
is the canonical Phase 3 macro entity id (``MACRO_ENTITY_ID`` from
``phase3.datamodel.scores_macro``). Source id is
``macro_yfinance.<date_bucket>`` so the scorer can group by date.

Direction heuristic
-------------------
Most macro fields are not directionally interpretable in isolation
(us10y = 4.5% is neither bullish nor bearish in itself — context
matters). We follow the convention:

  - fed_rate, us10y, dxy, cpi_yoy, core_cpi_yoy, m2_yoy,
    credit_spread_bp, yield_spread: direction = "neutral" by
    default. The scorer can override.
  - vix: >25 → bearish, <15 → bullish, else neutral.
  - rate_hike_flag: >0 → bearish, <0 → bullish, ==0 → neutral.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from phase3.datamodel import MACRO_ENTITY_ID, MACRO_ENTITY_TYPE
from phase3.datamodel.signals import (
    Direction,
    Signal,
    SignalSource,
    make_signal_id,
)
from phase3.signals.adapters.base import SourceAdapter


# Field → (signal_type, unit)
_MACRO_FIELDS: dict[str, tuple[str, str]] = {
    "fed_rate":         ("fed_rate",         "pct"),
    "us10y":            ("us10y",            "pct"),
    "us2y":             ("us2y",             "pct"),
    "us13w":            ("us13w",            "pct"),
    "dxy":              ("dxy",              "index"),
    "vix":              ("vix",              "index"),
    "cpi_yoy":          ("cpi_yoy",          "pct"),
    "core_cpi_yoy":     ("core_cpi_yoy",     "pct"),
    "m2_yoy":           ("m2_yoy",           "pct"),
    "credit_spread_bp": ("credit_spread",    "bp"),
    "yield_spread":     ("yield_spread",     "pp"),
    "rate_hike_flag":   ("rate_hike",        "count"),
}


@dataclass
class MacroAdaptResult:
    """Return value from :meth:`MacroAdapter.adapt_with_stats`."""

    signals: list[Signal] = field(default_factory=list)
    rows_processed: int = 0
    rows_skipped: int = 0
    warnings: list[str] = field(default_factory=list)


class MacroAdapter(SourceAdapter):
    """Offline normalizer for daily macro indicator dicts."""

    source_type = "macro"

    def adapt(self, raw: Any) -> list[Signal]:  # type: ignore[override]
        return self.adapt_with_stats(raw).signals

    def adapt_with_stats(self, raw: Any) -> MacroAdaptResult:
        result = MacroAdaptResult()
        rows = self._load_rows(raw, result)
        for row in rows:
            self._emit_for_row(row, result)
        return result

    def _emit_for_row(self, row: Mapping[str, Any], result: MacroAdaptResult) -> None:
        timestamp = self._parse_timestamp(row.get("timestamp")) or _utcnow()
        date_bucket = str(
            row.get("date_bucket") or timestamp.date().isoformat()
        )
        source_id = f"macro_yfinance.{date_bucket}"
        emitted = 0
        for fname, (stype, unit) in _MACRO_FIELDS.items():
            if fname not in row:
                continue
            value = self._clean_value(row[fname], fname, result)
            if value is None:
                continue
            sig_id = make_signal_id(source_id, MACRO_ENTITY_ID, stype, date_bucket)
            sig = Signal(
                signal_id=sig_id,
                entity_type=MACRO_ENTITY_TYPE,
                entity_id=MACRO_ENTITY_ID,
                signal_type=stype,
                value=value,
                unit=unit,
                direction=self._direction_for(stype, value),
                timestamp=timestamp,
                source=SignalSource(
                    source_id=source_id,
                    source_type="macro",
                    ref="macro://daily",
                    fetched_at=_utcnow(),
                    metadata={"field": fname},
                ),
                date_bucket=date_bucket,
                metadata={"field": fname, "raw": row[fname]},
            )
            result.signals.append(sig)
            emitted += 1
        if emitted == 0:
            result.warnings.append(
                f"macro row with no recognized fields (date_bucket={date_bucket}); skipping"
            )
            result.rows_skipped += 1
        else:
            result.rows_processed += 1

    # ----- helpers ---------------------------------------------------------

    @staticmethod
    def _clean_value(
        value: Any, field_name: str, result: MacroAdaptResult
    ) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            result.warnings.append(
                f"macro field {field_name!r} value {value!r} not numeric; skipping"
            )
            return None
        if math.isnan(f) or math.isinf(f):
            result.warnings.append(
                f"macro field {field_name!r} is NaN/Inf; skipping"
            )
            return None
        return f

    @staticmethod
    def _direction_for(signal_type: str, value: float) -> Direction:
        if signal_type == "vix":
            if value > 25.0:
                return "bearish"
            if value < 15.0:
                return "bullish"
            return "neutral"
        if signal_type == "rate_hike":
            if value > 0:
                return "bearish"
            if value < 0:
                return "bullish"
            return "neutral"
        return "neutral"

    @staticmethod
    def _load_rows(raw: Any, result: MacroAdaptResult) -> list[Mapping[str, Any]]:
        if isinstance(raw, Mapping):
            if "rows" in raw and isinstance(raw["rows"], list):
                return [r for r in raw["rows"] if isinstance(r, Mapping)]
            return [raw]
        if isinstance(raw, (list, tuple)):
            return [r for r in raw if isinstance(r, Mapping)]
        try:
            path = Path(os.fspath(raw))  # type: ignore[arg-type]
        except TypeError:
            result.warnings.append(
                f"unsupported raw type: {type(raw).__name__}"
            )
            return []
        if not path.exists():
            result.warnings.append(f"macro fixture path not found: {path}")
            return []
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            result.warnings.append(f"failed to load {path}: {exc}")
            return []
        if isinstance(data, list):
            return [r for r in data if isinstance(r, Mapping)]
        if isinstance(data, Mapping):
            if "rows" in data and isinstance(data["rows"], list):
                return [r for r in data["rows"] if isinstance(r, Mapping)]
            return [data]
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


__all__ = ["MacroAdapter", "MacroAdaptResult"]
