"""SignalEngine — the orchestrator that takes a Signal and returns a
WeightedSignal, applying lookup tables for source/type weights and
time-decay for recency.

Phase 3A scope: works in-memory from pre-built Signal objects. The
SourceAdapter ABC is shipped for the Phase 3B network-fed adapters.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from phase3.datamodel.signals import DEFAULT_DECAY_CONFIG, Signal
from phase3.signals.aggregator import WeightedSignal
from phase3.signals.confidence import compute_confidence
from phase3.signals.decay import apply_decay
from phase3.signals.weighting import (
    combine_weights,
    lookup_source_weight,
    resolve_decay_rule,
)


def _age_days(value_ts: datetime, as_of: datetime) -> float:
    if value_ts is None or as_of is None:
        return 0.0
    delta = (as_of - value_ts).total_seconds() / 86400.0
    if delta < 0:
        return 0.0
    return delta


@dataclass(frozen=True)
class EngineConfig:
    """In-memory engine config. The ConfigLoader builds this from YAML.

    Attributes:
        source_weights: maps source_type → {source_weight, type_weights{...}}
        decay_config:   maps signal_type → {function, half_life_days, step_threshold_days?}
        default_decay:  fallback rule if signal_type not in decay_config
    """

    source_weights: dict[str, dict[str, Any]]
    decay_config: dict[str, dict[str, Any]]
    default_decay: dict[str, Any]


class SignalEngine:
    """Compute final_weight, recency_weight, and confidence for a Signal.

    Stateless after construction — multiple threads can call weight()
    concurrently. No I/O; no logging side-effects.
    """

    def __init__(self, config: EngineConfig | None = None) -> None:
        self._config = config or EngineConfig(
            source_weights={},
            decay_config=dict(DEFAULT_DECAY_CONFIG),
            default_decay=dict(DEFAULT_DECAY_CONFIG["default"]),
        )

    @property
    def config(self) -> EngineConfig:
        return self._config

    def _resolve_decay(self, signal_type: str) -> dict[str, Any]:
        return resolve_decay_rule(signal_type, self._config.decay_config)

    def weight(
        self, signal: Signal, as_of: datetime | None = None
    ) -> WeightedSignal:
        """Compute weights for a single Signal.

        Args:
            signal: the normalized Signal to weight
            as_of:  reference time for decay (defaults to "now" UTC)
        """
        if as_of is None:
            as_of = datetime.now(timezone.utc)
        decay = self._resolve_decay(signal.signal_type)
        age = _age_days(signal.timestamp, as_of)
        recency = apply_decay(
            function=decay.get("function", "exponential"),
            age_days=age,
            half_life_days=float(decay.get("half_life_days", 0.0) or 0.0),
            step_threshold_days=float(decay.get("step_threshold_days", 0.0) or 0.0),
        )
        type_w = lookup_source_weight(
            self._config.source_weights, signal.source.source_type, signal.signal_type
        )
        # Use the source_weight from source_weights lookup as `source_weight`
        # here, and the type-specific lookup as `type_weight`. The lookup
        # helper does both internally — we split them for the breakdown.
        entry = self._config.source_weights.get(signal.source.source_type) or {}
        sw_base = entry.get("source_weight", 0.5)
        sw_base = float(sw_base) if isinstance(sw_base, (int, float)) else 0.5
        breakdown = combine_weights(
            source_weight=sw_base,
            type_weight=type_w,
            recency_weight=recency,
        )
        cb = compute_confidence(
            n_present_keys=1,
            n_expected_keys=1,
            age_days=age,
            recency_half_life_days=float(decay.get("half_life_days", 14.0) or 14.0),
            directions=[signal.direction],
        )
        return WeightedSignal(
            signal=signal,
            source_weight=breakdown.source_weight,
            type_weight=breakdown.type_weight,
            recency_weight=breakdown.recency_weight,
            final_weight=breakdown.final_weight,
            confidence=cb.final,
            decay_function=decay.get("function", "exponential"),
            half_life_days=float(decay.get("half_life_days", 0.0) or 0.0),
        )

    def weight_all(
        self, signals: Iterable[Signal], as_of: datetime | None = None
    ) -> list[WeightedSignal]:
        return [self.weight(s, as_of=as_of) for s in signals]
