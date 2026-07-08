"""Signal family: Direction enum, SignalSource, Signal, default decay config."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from phase3.datamodel._version import SCHEMA_VERSION

Direction = Literal["bullish", "bearish", "neutral"]


class DirectionEnum(str, Enum):
    """String enum mirror of the Direction literal for places that need an enum
    (serialization, validation). The string literal type is the source of truth."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


def make_signal_id(
    source: str, entity_id: str, signal_type: str, date_bucket: str
) -> str:
    """sha256-derived 16-char signal id. Deterministic → same input = same id.

    Matches docs/phase3/07_data_model.md §5.1. We re-implement locally (no
    Phase 2B dependency) to keep datamodel stdlib-only.
    """
    import hashlib

    payload = f"{source}:{entity_id}:{signal_type}:{date_bucket}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SignalSource:
    """Where a Signal came from. Plain identifier + provenance metadata.

    Distinct from GraphNode/Source to keep the Signal engine free of graph
    dependencies (graph can *reference* sources by source_id but does not
    need to know Signal internals).
    """

    source_id: str  # e.g. "yfinance.2330.TW", "t86.fed.2026-07-08"
    source_type: str  # "yfinance" | "rss" | "t86" | "macro" | "custom"
    ref: str = ""  # URL / API endpoint / RSS feed (debug aid)
    fetched_at: datetime = field(default_factory=_utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    """A normalized, source-attributed data point ready to be weighted.

    The Signal engine is responsible for turning raw source data into Signals.
    Scorers consume Signals (typically in the form of WeightedSignal wrappers)
    never raw source data.

    signal_id is content-derived (source + entity + type + date bucket) so
    re-ingestion of identical data yields the same id — important for
    idempotency and replay.
    """

    signal_id: str
    entity_type: str  # "company" | "industry" | "macro" | "news"
    entity_id: str  # "2330" | "AI" | "FED_RATE" | "news:abc123"
    signal_type: str  # "pe_ratio" | "roe" | "rate_hike" | "sentiment" | ...
    value: float
    unit: str  # "ratio" | "pct" | "twd" | "usd" | "score" | "count" | "z_score"
    direction: Direction
    timestamp: datetime  # value-as-of (NOT fetch time)
    source: SignalSource
    date_bucket: str = ""  # YYYY-MM-DD; used to dedupe same-day re-ingest
    schema_version: str = SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "signal_type": self.signal_type,
            "value": self.value,
            "unit": self.unit,
            "direction": self.direction,
            "timestamp": self.timestamp.isoformat(),
            "source": {
                "source_id": self.source.source_id,
                "source_type": self.source.source_type,
                "ref": self.source.ref,
                "fetched_at": self.source.fetched_at.isoformat(),
                "metadata": dict(self.source.metadata),
            },
            "date_bucket": self.date_bucket,
            "schema_version": self.schema_version,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Signal":
        src = data["source"]
        return cls(
            signal_id=data["signal_id"],
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            signal_type=data["signal_type"],
            value=float(data["value"]),
            unit=data["unit"],
            direction=data["direction"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=SignalSource(
                source_id=src["source_id"],
                source_type=src["source_type"],
                ref=src.get("ref", ""),
                fetched_at=datetime.fromisoformat(src["fetched_at"]),
                metadata=dict(src.get("metadata", {})),
            ),
            date_bucket=data.get("date_bucket", ""),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            metadata=dict(data.get("metadata", {})),
        )


# Default decay config used by the Signal engine when no YAML override is
# loaded. Kept here (not in config/loader) so the engine has a sane fallback
# without touching disk.
DEFAULT_DECAY_CONFIG: dict[str, dict[str, Any]] = {
    # signal_type -> {function, half_life_days, step_threshold_days?}
    "fed_rate": {"function": "linear", "half_life_days": 14.0},
    "monthly_revenue": {"function": "exponential", "half_life_days": 30.0},
    "news_headline": {"function": "exponential", "half_life_days": 1.0},
    "institutional_flow": {"function": "step", "step_threshold_days": 5.0},
    "pe_ratio": {"function": "none", "half_life_days": 0.0},
    "roe": {"function": "none", "half_life_days": 0.0},
    "pb_ratio": {"function": "none", "half_life_days": 0.0},
    "revenue_growth": {"function": "exponential", "half_life_days": 30.0},
    "earnings_growth": {"function": "exponential", "half_life_days": 30.0},
    "rate_hike": {"function": "linear", "half_life_days": 14.0},
    "sentiment": {"function": "exponential", "half_life_days": 1.0},
    "default": {"function": "exponential", "half_life_days": 7.0},
}
