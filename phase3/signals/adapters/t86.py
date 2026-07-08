"""T86Adapter — turn TWSE T86 institutional flow rows into Signals.

Input shape
-----------
``raw`` is one of:

  1. A single row dict with at least ``code`` (or ``entity_id``);
     optional fields: ``foreign_net``, ``prop_net`` (or
     ``investment_trust_net``), ``dealer_net``, ``date_bucket``
     (defaults to today), ``industry_id`` (optional — see
     industry rollup below).
  2. A list of such dicts.
  3. A path to a JSON file containing shape 1 or 2.

This is offline-only. The T86 HTTP fetch is a separate concern;
this adapter is the pure normalizer that the production caller
fills in.

Field name normalization
------------------------
TWSE/T86 data sources use different field names in different
years and wrappers. We accept any of:

  - ``foreign_net``     (primary)
  - ``prop_net``        (primary)
  - ``investment_trust_net`` (alias for prop_net)
  - ``dealer_net``      (primary)

A row may include any subset; missing fields are skipped (a
warning is recorded on the result wrapper, not raised). All
missing → row is silently skipped (nothing to emit).

Emitted signal types
--------------------
  - ``foreign_net``  (zhang)
  - ``prop_net``     (zhang, normalized from investment_trust_net if needed)
  - ``dealer_net``   (zhang)

Industry rollup
---------------
If a row has ``industry_id`` and ``industry_rollup=true`` (or, by
default, the constructor was called with ``emit_industry_rollup=True``),
an additional industry-level signal is emitted for each present
flow type. The industry entity_type is ``"industry"`` and the
entity_id is the row's ``industry_id``.

Direction heuristic
-------------------
foreign_net/prop_net/dealer_net have no fixed "good" direction at
the company level — the sign of the number is the signal
direction. Positive = bullish, negative = bearish, zero = neutral.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from phase3.datamodel.signals import (
    Direction,
    Signal,
    SignalSource,
    make_signal_id,
)
from phase3.signals.adapters.base import SourceAdapter


# Field → canonical signal_type
_FLOW_FIELDS: tuple[tuple[str, str], ...] = (
    ("foreign_net", "foreign_net"),
    ("prop_net", "prop_net"),
    ("dealer_net", "dealer_net"),
)


@dataclass
class T86AdaptResult:
    """Return value from :meth:`T86Adapter.adapt_with_stats`."""

    signals: list[Signal] = field(default_factory=list)
    rows_processed: int = 0
    rows_skipped: int = 0
    warnings: list[str] = field(default_factory=list)


class T86Adapter(SourceAdapter):
    """Offline normalizer for TWSE T86 institutional-flow rows."""

    source_type = "t86"

    def __init__(self, emit_industry_rollup: bool = True) -> None:
        # Default: emit industry rollup when a row carries industry_id.
        # Set False to disable (some reports only want company signals).
        self._emit_industry_rollup = bool(emit_industry_rollup)

    def adapt(self, raw: Any) -> list[Signal]:  # type: ignore[override]
        return self.adapt_with_stats(raw).signals

    def adapt_with_stats(self, raw: Any) -> T86AdaptResult:
        result = T86AdaptResult()
        rows = self._load_rows(raw, result)
        for row in rows:
            self._emit_for_row(row, result)
        return result

    # ----- emission --------------------------------------------------------

    def _emit_for_row(self, row: Mapping[str, Any], result: T86AdaptResult) -> None:
        entity_id = str(row.get("code", row.get("entity_id", "")) or "").strip()
        if not entity_id:
            result.warnings.append("row missing 'code' / 'entity_id'; skipping")
            result.rows_skipped += 1
            return
        # prop_net <-> investment_trust_net aliasing
        if "prop_net" not in row and "investment_trust_net" in row:
            row = {**row, "prop_net": row["investment_trust_net"]}
        date_bucket = str(
            row.get("date_bucket")
            or row.get("date")
            or datetime.now(timezone.utc).date().isoformat()
        )
        timestamp = self._parse_timestamp(row.get("timestamp")) or self._parse_date(date_bucket)
        source_id = f"t86.{entity_id}.{date_bucket}"
        present_fields = [
            (fname, stype) for fname, stype in _FLOW_FIELDS
            if fname in row and row[fname] is not None
        ]
        if not present_fields:
            result.warnings.append(
                f"row code={entity_id} has no flow fields; skipping"
            )
            result.rows_skipped += 1
            return
        # Emit company-level signals
        for fname, stype in present_fields:
            value = self._clean_value(row[fname], entity_id, stype, result)
            if value is None:
                continue
            sig_id = make_signal_id(source_id, entity_id, stype, date_bucket)
            sig = Signal(
                signal_id=sig_id,
                entity_type="company",
                entity_id=entity_id,
                signal_type=stype,
                value=value,
                unit="zhang",
                direction=self._direction_for(value),
                timestamp=timestamp,
                source=SignalSource(
                    source_id=source_id,
                    source_type="t86",
                    ref=f"t86://{entity_id}",
                    fetched_at=_utcnow(),
                    metadata={"code": entity_id, "field": fname},
                ),
                date_bucket=date_bucket,
                metadata={"code": entity_id, "field": fname, "raw": row[fname]},
            )
            result.signals.append(sig)
        # Optional industry rollup
        if self._emit_industry_rollup:
            industry_id = str(row.get("industry_id", "") or "").strip()
            if industry_id and row.get("industry_rollup", True):
                for fname, stype in present_fields:
                    value = row.get(fname)
                    if value is None:
                        continue
                    cleaned = self._clean_value(value, industry_id, stype, result)
                    if cleaned is None:
                        continue
                    ind_source_id = f"t86.industry.{industry_id}.{date_bucket}"
                    sig_id = make_signal_id(
                        ind_source_id, industry_id, stype, date_bucket
                    )
                    sig = Signal(
                        signal_id=sig_id,
                        entity_type="industry",
                        entity_id=industry_id,
                        signal_type=stype,
                        value=cleaned,
                        unit="zhang",
                        direction=self._direction_for(cleaned),
                        timestamp=timestamp,
                        source=SignalSource(
                            source_id=ind_source_id,
                            source_type="t86",
                            ref=f"t86://industry/{industry_id}",
                            fetched_at=_utcnow(),
                            metadata={
                                "industry_id": industry_id,
                                "constituent_code": entity_id,
                                "field": fname,
                            },
                        ),
                        date_bucket=date_bucket,
                        metadata={
                            "industry_id": industry_id,
                            "constituent_code": entity_id,
                            "field": fname,
                            "raw": value,
                        },
                    )
                    result.signals.append(sig)
        result.rows_processed += 1

    # ----- helpers ---------------------------------------------------------

    @staticmethod
    def _clean_value(
        value: Any, entity_id: str, signal_type: str, result: T86AdaptResult,
    ) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            result.warnings.append(
                f"entity_id={entity_id}: {signal_type} value {value!r} not numeric; skipping"
            )
            return None
        if f != f:  # NaN
            result.warnings.append(
                f"entity_id={entity_id}: {signal_type} is NaN; skipping"
            )
            return None
        return f

    @staticmethod
    def _direction_for(value: float) -> Direction:
        if value > 0:
            return "bullish"
        if value < 0:
            return "bearish"
        return "neutral"

    @staticmethod
    def _load_rows(raw: Any, result: T86AdaptResult) -> list[Mapping[str, Any]]:
        if isinstance(raw, Mapping):
            if "rows" in raw and isinstance(raw["rows"], list):
                return [r for r in raw["rows"] if isinstance(r, Mapping)]
            return [raw]
        if isinstance(raw, (list, tuple)):
            return [r for r in raw if isinstance(r, Mapping)]
        try:
            path = Path(os.fspath(raw))  # type: ignore[arg-type]
        except TypeError:
            result.warnings.append(f"unsupported raw type: {type(raw).__name__}")
            return []
        if not path.exists():
            result.warnings.append(f"t86 fixture path not found: {path}")
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

    @staticmethod
    def _parse_date(date_str: str) -> datetime:
        try:
            return datetime.fromisoformat(date_str)
        except ValueError:
            return _utcnow()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["T86Adapter", "T86AdaptResult"]
