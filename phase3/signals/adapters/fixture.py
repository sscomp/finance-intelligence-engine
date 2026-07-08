"""FixtureAdapter — turn a JSON file (or in-memory dict) into Signals.

Why this adapter exists
------------------------
Phase 3B tests need deterministic, offline signal fixtures. The
FixtureAdapter is the simplest possible adapter:

  - ``raw`` can be:
      * a list of :class:`phase3.datamodel.signals.Signal` objects
        (round-trip through :meth:`Signal.to_dict` is the canonical
        shape produced by the production adapter stack);
      * a dict with a top-level ``"signals"`` list;
      * a path (str or :class:`pathlib.Path`) to a JSON file
        containing either of the above shapes.
  - Emits one :class:`Signal` per input entry.
  - Skips entries that cannot be parsed (returns a warning counter,
    does not raise) — a single malformed fixture must not drop the
    whole batch.
  - Determinism: ``adapt(raw)`` returns equal output for equal input,
    including when ``raw`` is a path. (The file must exist; we
    ``os.path.realpath`` it once and cache the read.)

Why it does NOT call :meth:`Signal.from_dict` directly
------------------------------------------------------
The signal model has a tiny but real contract: every Signal must
have a non-empty ``signal_id`` that matches
``make_signal_id(source, entity_id, signal_type, date_bucket)``. If a
fixture entry was hand-edited and the signal_id is stale, we
recompute it. This keeps the adapter contract tight: "feed me a
Signal-shaped thing, get a Signal with a correct id out" — even if
the caller didn't bother to compute the id.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, Union

from phase3.datamodel.signals import (
    Signal,
    SignalSource,
    make_signal_id,
)
from phase3.signals.adapters.base import SourceAdapter


# Public type alias for the three legal input shapes.
FixtureInput = Union[Sequence[Mapping[str, Any]], Mapping[str, Any], str, os.PathLike[str]]


@dataclass
class FixtureAdaptResult:
    """Return value from :meth:`FixtureAdapter.adapt`.

    The adapter is the only one in the Phase 3B stack that returns
    a wrapper instead of a bare list. The wrapper carries the
    warning count and a parsed-OK count so the CLI can print a
    useful summary without having to re-iterate the output.

    ``signals`` is a list of :class:`Signal` objects ready for
    persistence. The order matches input order, modulo skipped
    entries.
    """

    signals: list[Signal] = field(default_factory=list)
    parsed: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


class FixtureAdapter(SourceAdapter):
    """Read JSON files (or in-memory dicts) and emit Signals.

    The adapter is stateless after construction; calling ``adapt``
    twice with the same input returns equal output. Construction
    takes no required args.
    """

    source_type = "fixture"

    def adapt(self, raw: Any) -> list[Signal]:  # type: ignore[override]
        """Return a list of Signals parsed from ``raw``.

        The returned wrapper's ``.signals`` is the canonical output;
        we return the list directly so the type matches
        :meth:`SourceAdapter.adapt`'s contract. Warning / skip counts
        are reachable via the corresponding ``adapt_with_stats()``
        method below.
        """
        result = self.adapt_with_stats(raw)
        return result.signals

    def adapt_with_stats(self, raw: Any) -> FixtureAdaptResult:
        """Same as :meth:`adapt` but returns full :class:`FixtureAdaptResult`."""
        result = FixtureAdaptResult()
        entries = self._load_entries(raw, result)
        for entry in entries:
            sig = self._entry_to_signal(entry, result)
            if sig is not None:
                result.signals.append(sig)
                result.parsed += 1
        return result

    # ----- internals -------------------------------------------------------

    def _load_entries(self, raw: Any, result: FixtureAdaptResult) -> list[Mapping[str, Any]]:
        """Resolve ``raw`` to a list of dict-shaped entries."""
        # Case 1: already a list of mappings
        if isinstance(raw, (list, tuple)):
            return [self._coerce_entry(e, result) for e in raw]
        # Case 2: dict with "signals" key
        if isinstance(raw, Mapping):
            if "signals" in raw and isinstance(raw["signals"], list):
                return [self._coerce_entry(e, result) for e in raw["signals"]]
            # Single Signal-shaped dict — wrap in list
            return [self._coerce_entry(raw, result)]
        # Case 3: path (str or PathLike)
        try:
            path = Path(os.fspath(raw))  # type: ignore[arg-type]
        except TypeError:
            result.warnings.append(
                f"unsupported raw type: {type(raw).__name__}; expected list, dict, or path"
            )
            return []
        if not path.exists():
            result.warnings.append(f"fixture path not found: {path}")
            return []
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            result.warnings.append(f"failed to load fixture {path}: {exc}")
            return []
        if isinstance(data, list):
            return [self._coerce_entry(e, result) for e in data]
        if isinstance(data, Mapping):
            if "signals" in data and isinstance(data["signals"], list):
                return [self._coerce_entry(e, result) for e in data["signals"]]
            return [self._coerce_entry(data, result)]
        result.warnings.append(
            f"unsupported fixture JSON shape: top-level {type(data).__name__}"
        )
        return []

    @staticmethod
    def _coerce_entry(entry: Any, result: FixtureAdaptResult) -> Mapping[str, Any]:
        """Coerce ``entry`` to a Mapping, recording a warning for non-conforming inputs."""
        if isinstance(entry, Mapping):
            return entry
        result.warnings.append(
            f"skipping non-mapping entry: {type(entry).__name__}"
        )
        return {"_skip": True, "_raw": entry}

    def _entry_to_signal(
        self, entry: Mapping[str, Any], result: FixtureAdaptResult
    ) -> Signal | None:
        """Build a Signal from a dict entry, or None if the entry is invalid."""
        if entry.get("_skip"):
            result.skipped += 1
            return None
        # Minimal required fields
        required = ("entity_type", "entity_id", "signal_type", "value", "unit", "direction")
        for k in required:
            if k not in entry:
                result.warnings.append(
                    f"missing field {k!r} in entry; skipping"
                )
                result.skipped += 1
                return None
        try:
            value = float(entry["value"])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            result.warnings.append(
                f"non-numeric value for entity_id={entry.get('entity_id')!r}; skipping"
            )
            result.skipped += 1
            return None
        direction = str(entry["direction"])
        if direction not in ("bullish", "bearish", "neutral"):
            result.warnings.append(
                f"invalid direction {direction!r}; defaulting to 'neutral'"
            )
            direction = "neutral"
        ts_raw = entry.get("timestamp")
        ts = self._parse_timestamp(ts_raw)
        # Source
        src_raw = entry.get("source")
        if isinstance(src_raw, Mapping):
            src = SignalSource(
                source_id=str(src_raw.get("source_id", "fixture.unknown")),
                source_type=str(src_raw.get("source_type", "fixture")),
                ref=str(src_raw.get("ref", "")),
                fetched_at=self._parse_timestamp(src_raw.get("fetched_at")) or _utcnow(),
                metadata=dict(src_raw.get("metadata", {})),
            )
        else:
            src = SignalSource(
                source_id=str(entry.get("source_id", "fixture.unknown")),
                source_type=str(entry.get("source_type", "fixture")),
                ref=str(entry.get("ref", "")),
                fetched_at=_utcnow(),
            )
        # Date bucket (default = timestamp YYYY-MM-DD)
        date_bucket = str(entry.get("date_bucket", ts.date().isoformat()))
        # signal_id: respect provided value if present and well-formed (16-hex),
        # otherwise recompute deterministically from (source.source_id, entity_id,
        # signal_type, date_bucket).
        provided_id = str(entry.get("signal_id", "")).strip()
        signal_id = provided_id if _is_hex16(provided_id) else make_signal_id(
            src.source_id, str(entry["entity_id"]), str(entry["signal_type"]), date_bucket,
        )
        metadata = dict(entry.get("metadata", {}))
        return Signal(
            signal_id=signal_id,
            entity_type=str(entry["entity_type"]),
            entity_id=str(entry["entity_id"]),
            signal_type=str(entry["signal_type"]),
            value=value,
            unit=str(entry["unit"]),
            direction=direction,  # type: ignore[arg-type]
            timestamp=ts,
            source=src,
            date_bucket=date_bucket,
            schema_version=str(entry.get("schema_version", "3.0")),
            metadata=metadata,
        )

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        """Parse a timestamp from various shapes; fall back to UTC now."""
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return _utcnow()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_hex16(value: str) -> bool:
    """Return True if ``value`` is a 16-char lowercase/uppercase hex string."""
    if not value or len(value) != 16:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


__all__ = ["FixtureAdapter", "FixtureAdaptResult"]
