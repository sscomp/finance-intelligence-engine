"""Aggregating multiple WeightedSignals for the same (entity, signal_type)
into a single AggregatedSignal.

Aggregation policy (Phase 3A MVP, fixed):
  - weighted_sum = sum(weighted.value * weighted.final_weight)
  - If the user passes a `score_sign` callback, values are signed before
    summing (e.g. PE ratio → lower is better, so sign is flipped).
    Default is identity (higher value → higher contribution).
  - direction_consensus is "bullish" / "bearish" / "neutral" if all
    WeightedSignals agree, otherwise "mixed".
  - aggregate_confidence = mean(weighted.confidence) * consensus_factor
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from phase3.datamodel.signals import Direction, Signal


# A WeightedSignal is what the engine produces; we don't have a dedicated
# frozen dataclass for it in Phase 3A (it's a documented shape, not a
# hard schema). We define it loosely here to avoid a circular dep.
@dataclass(frozen=True)
class WeightedSignal:
    """Engine-internal weighted-signal view.

    A separate shape from the public Signal so the public schema can
    stay focused on identity / provenance while this internal shape
    adds runtime-computed weights. Phase 3B will consider promoting
    this to a public frozen dataclass if dashboards need it.
    """

    signal: Signal
    source_weight: float
    type_weight: float
    recency_weight: float
    final_weight: float
    confidence: float
    decay_function: str
    half_life_days: float | None = None

    @property
    def value(self) -> float:
        return self.signal.value

    @property
    def direction(self) -> Direction:
        return self.signal.direction


@dataclass(frozen=True)
class AggregatedSignal:
    """The merged view over a (entity_type, entity_id, signal_type) bucket.

    `contributors` is preserved so evidence tracing has the full chain
    back to each source.
    """

    entity_type: str
    entity_id: str
    signal_type: str
    weighted_sum: float
    contributors: list[WeightedSignal]
    direction_consensus: Direction | str  # "bullish" | "bearish" | "neutral" | "mixed"
    aggregate_confidence: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = "3.0"

    def to_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "signal_type": self.signal_type,
            "weighted_sum": self.weighted_sum,
            "contributors": [
                {
                    "signal_id": ws.signal.signal_id,
                    "value": ws.signal.value,
                    "direction": ws.signal.direction,
                    "source_weight": ws.source_weight,
                    "type_weight": ws.type_weight,
                    "recency_weight": ws.recency_weight,
                    "final_weight": ws.final_weight,
                    "confidence": ws.confidence,
                }
                for ws in self.contributors
            ],
            "direction_consensus": self.direction_consensus,
            "aggregate_confidence": self.aggregate_confidence,
            "timestamp": self.timestamp.isoformat(),
            "schema_version": self.schema_version,
        }


ScoreSign = Callable[[Signal], float]


def _identity_sign(sig: Signal) -> float:
    return 1.0


class SignalAggregator:
    """Pure-function aggregator. No I/O. Deterministic."""

    def __init__(self, score_sign: ScoreSign | None = None) -> None:
        self._sign = score_sign or _identity_sign

    def aggregate(
        self, signals: Iterable[WeightedSignal]
    ) -> AggregatedSignal:
        sigs = list(signals)
        if not sigs:
            raise ValueError("Cannot aggregate an empty signal list")

        first = sigs[0].signal
        # Bucket consistency check — every contributor must share the
        # same (entity_type, entity_id, signal_type) or aggregation
        # becomes meaningless.
        for ws in sigs[1:]:
            s = ws.signal
            if (
                s.entity_type != first.entity_type
                or s.entity_id != first.entity_id
                or s.signal_type != first.signal_type
            ):
                raise ValueError(
                    "Signals in one aggregation must share (entity_type, entity_id, signal_type); "
                    f"got {first.entity_type}/{first.entity_id}/{first.signal_type} and "
                    f"{s.entity_type}/{s.entity_id}/{s.signal_type}"
                )

        weighted_sum = sum(
            self._sign(ws.signal) * ws.signal.value * ws.final_weight for ws in sigs
        )

        # Direction consensus
        directions = [ws.signal.direction for ws in sigs]
        if all(d == "bullish" for d in directions):
            consensus: str = "bullish"
        elif all(d == "bearish" for d in directions):
            consensus = "bearish"
        elif all(d == "neutral" for d in directions):
            consensus = "neutral"
        else:
            consensus = "mixed"

        # Aggregate confidence: mean confidence × consensus factor
        mean_conf = sum(ws.confidence for ws in sigs) / len(sigs)
        if consensus == "mixed":
            consensus_factor = 0.5
        elif consensus == "neutral":
            consensus_factor = 0.7
        else:
            consensus_factor = 1.0
        agg_conf = max(0.0, min(1.0, mean_conf * consensus_factor))

        return AggregatedSignal(
            entity_type=first.entity_type,
            entity_id=first.entity_id,
            signal_type=first.signal_type,
            weighted_sum=weighted_sum,
            contributors=sigs,
            direction_consensus=consensus,
            aggregate_confidence=agg_conf,
        )
