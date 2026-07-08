"""Confidence scoring for weighted signals.

Confidence = completeness * recency_factor * consensus_factor

Each factor is a 0..1 multiplier; the final confidence is the product.
The three components are reported separately (ConfidenceBreakdown) so
Phase 3B dashboards can show "low confidence because consensus is
mixed", not just "low confidence".

Inputs:
  - n_signals:        how many sources covered this (entity, signal_type)
  - n_expected_keys:  how many canonical keys the source normally provides
  - n_present_keys:   how many of those are actually present
  - age_days:         how old the latest data is
  - direction_dispersion: 0..1 (0 = all sources agree, 1 = all disagree)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from phase3.datamodel.signals import Direction

DirectionLike = Direction | str


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """Auditable confidence components. Final = completeness * recency * consensus."""

    completeness: float
    recency: float
    consensus: float
    final: float


def _clip01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


def completeness_factor(n_present: int, n_expected: int) -> float:
    """Fraction of expected fields present. 1.0 = all present, 0.0 = none.

    If n_expected == 0 (e.g. unknown expected field count), return 1.0
    rather than 0 — we'd rather be slightly over-confident than 0 in
    a way that permanently zeros out a signal.
    """
    if n_expected <= 0:
        return 1.0
    return _clip01(n_present / n_expected)


def recency_factor(age_days: float, half_life_days: float = 14.0) -> float:
    """Decay-shaped recency score.

    24h → ~0.99, 14d → 0.5, 30d → ~0.22. half_life_days=0 collapses to 1
    (treat as "no time degradation").
    """
    if age_days is None or age_days < 0:
        age_days = 0.0
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def consensus_factor(directions: Iterable[DirectionLike]) -> float:
    """How much the directions agree.

    Returns 1.0 if all agree on the same direction, 0.0 if perfectly split
    between bullish and bearish, intermediate otherwise. "neutral" is
    treated as a weak signal and reduces consensus slightly.

    Empty list → 0.5 (we have no opinion, neither confident nor skeptical).
    Single direction → 1.0.
    """
    dirs = list(directions)
    if not dirs:
        return 0.5
    if len(dirs) == 1:
        return 1.0
    bull = sum(1 for d in dirs if d == "bullish")
    bear = sum(1 for d in dirs if d == "bearish")
    neut = sum(1 for d in dirs if d == "neutral")
    n = bull + bear + neut
    if n == 0:
        return 0.5
    # Treat neutral as a half-vote for whichever non-neutral side has
    # more votes. This lets a 1-bull + 1-bear + 1-neut set tilt slightly
    # without claiming perfect consensus.
    half_neut = neut / 2.0
    bull_eff = bull + half_neut
    bear_eff = bear + half_neut
    diff = abs(bull_eff - bear_eff)
    max_eff = max(bull_eff, bear_eff)
    if max_eff == 0:
        return 0.5
    return _clip01(diff / max_eff)


def compute_confidence(
    n_present_keys: int = 0,
    n_expected_keys: int = 0,
    age_days: float = 0.0,
    recency_half_life_days: float = 14.0,
    directions: Iterable[DirectionLike] | None = None,
) -> ConfidenceBreakdown:
    """Compute the final confidence score with full breakdown.

    All three factors default to 1.0 if their input is not supplied
    (callers can opt in to each factor independently).
    """
    c = completeness_factor(n_present_keys, n_expected_keys)
    r = recency_factor(age_days, recency_half_life_days)
    cn = consensus_factor(directions or [])
    return ConfidenceBreakdown(
        completeness=c,
        recency=r,
        consensus=cn,
        final=_clip01(c * r * cn),
    )
