"""Combine source weight, type weight, and recency weight into a final weight.

Final weight = source_weight * type_weight * recency_weight, with a
defensive clamp to [0, 1]. Individual inputs are also clamped to [0, 1]
so a misconfigured weight file (e.g. 1.2) cannot leak >1.0 into the
final score.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from phase3.datamodel.signals import DEFAULT_DECAY_CONFIG


@dataclass(frozen=True)
class WeightBreakdown:
    """Auditable record of how `final_weight` was computed.

    Stored alongside the weighted signal in tests; not (yet) part of the
    public frozen-dataclass contract. Future Phase 3B will promote this
    into the WeightedSignal wrapper.
    """

    source_weight: float
    type_weight: float
    recency_weight: float
    final_weight: float


def _clip01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


def combine_weights(
    source_weight: float,
    type_weight: float,
    recency_weight: float,
) -> WeightBreakdown:
    """Multiply three weights, each clipped to [0, 1].

    Why clip instead of raise: the YAML config system already validates
    weight ranges, but a future adapter might pass an out-of-range
    value. Clipping keeps the engine's invariants intact and is easy to
    trace in tests.
    """
    sw = _clip01(source_weight)
    tw = _clip01(type_weight)
    rw = _clip01(recency_weight)
    return WeightBreakdown(
        source_weight=sw,
        type_weight=tw,
        recency_weight=rw,
        final_weight=_clip01(sw * tw * rw),
    )


def lookup_source_weight(
    source_weights: dict[str, dict[str, Any]],
    source_type: str,
    signal_type: str,
) -> float:
    """Resolve the (source_type, signal_type) → weight fallback chain.

    Precedence:
      1. source_weights[source_type]['type_weights'][signal_type]
      2. source_weights[source_type]['source_weight']
      3. 0.5  (defensive default; never return None)

    Used by the SignalEngine to look up a weight without binding the
    engine to a specific SourceWeightEntry type (so a YAML loader can
    supply plain dicts during Phase 3A scaffold).
    """
    if not source_weights:
        return 0.5
    entry = source_weights.get(source_type) or {}
    type_map = entry.get("type_weights") or {}
    if signal_type in type_map:
        v = type_map[signal_type]
        if isinstance(v, (int, float)):
            return _clip01(float(v))
    base = entry.get("source_weight")
    if isinstance(base, (int, float)):
        return _clip01(float(base))
    return 0.5


def resolve_decay_rule(
    signal_type: str,
    decay_config: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the decay rule for a signal type, falling back to defaults.

    Order of lookup:
      1. decay_config[signal_type] (if supplied by caller/YAML)
      2. DEFAULT_DECAY_CONFIG[signal_type]
      3. DEFAULT_DECAY_CONFIG['default']
    """
    cfg = decay_config if decay_config is not None else DEFAULT_DECAY_CONFIG
    if signal_type in cfg:
        return dict(cfg[signal_type])
    if signal_type in DEFAULT_DECAY_CONFIG:
        return dict(DEFAULT_DECAY_CONFIG[signal_type])
    return dict(DEFAULT_DECAY_CONFIG["default"])
