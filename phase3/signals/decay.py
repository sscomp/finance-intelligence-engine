"""Time-decay functions for signal weighting.

Four functions per docs/phase3/02_signal_engine.md §1.4:
  - linear:      weight *= max(0, 1 - age / half_life)
  - exponential: weight *= 0.5 ** (age / half_life)
  - step:        weight = 1.0 if age <= step_threshold else 0.0
  - none:        weight stays 1.0

All functions take `age_days: float` (non-negative) and `half_life_days`
or `step_threshold_days` (both non-negative). They never raise on
non-negative inputs; negative ages are treated as 0 (defensive — clocks
shouldn't go backward, but if they do we don't want NaN).
"""
from __future__ import annotations

import math
from typing import Callable

DecayFn = Callable[[float, float, float], float]


def _safe_age(age_days: float) -> float:
    """Coerce negative or NaN ages to 0."""
    if age_days is None or (isinstance(age_days, float) and math.isnan(age_days)):
        return 0.0
    if age_days < 0:
        return 0.0
    return float(age_days)


def decay_linear(age_days: float, half_life_days: float, _unused: float = 0.0) -> float:
    """weight *= max(0, 1 - age / half_life).

    At age == half_life → 0. At age == 2*half_life → 0 (clamped)."""
    age = _safe_age(age_days)
    if half_life_days <= 0:
        return 0.0
    return max(0.0, 1.0 - age / half_life_days)


def decay_exponential(
    age_days: float, half_life_days: float, _unused: float = 0.0
) -> float:
    """weight *= 0.5 ** (age / half_life).

    At age == half_life → 0.5. At age == 2*half_life → 0.25. Asymptotic
    to 0 but never reaches it (good for "decaying but not gone")."""
    age = _safe_age(age_days)
    if half_life_days <= 0:
        return 0.0
    return 0.5 ** (age / half_life_days)


def decay_step(age_days: float, _unused: float, step_threshold_days: float) -> float:
    """weight = 1.0 if age <= step_threshold else 0.0.

    _unused is the half_life_days slot. The dispatch helper in apply_decay
    always passes three positional args; we ignore the half_life here.
    """
    age = _safe_age(age_days)
    if step_threshold_days < 0:
        step_threshold_days = 0.0
    return 1.0 if age <= step_threshold_days else 0.0


def decay_none(age_days: float, _h: float = 0.0, _s: float = 0.0) -> float:
    """weight stays 1.0 — signal never decays."""
    _ = _safe_age(age_days)  # validation only
    return 1.0


DECAY_FUNCTIONS: dict[str, DecayFn] = {
    "linear": decay_linear,
    "exponential": decay_exponential,
    "step": decay_step,
    "none": decay_none,
}


def apply_decay(
    function: str,
    age_days: float,
    half_life_days: float,
    step_threshold_days: float = 0.0,
) -> float:
    """Dispatch a decay call by name. Returns the new weight in [0, 1].

    Raises:
        ValueError: if `function` is not one of the four supported.
    """
    fn = DECAY_FUNCTIONS.get(function)
    if fn is None:
        raise ValueError(
            f"Unknown decay function {function!r}; expected one of {sorted(DECAY_FUNCTIONS)}"
        )
    return float(fn(age_days, half_life_days, step_threshold_days))
