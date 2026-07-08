"""Tests for Phase 3A signal decay functions.

Covers the four decay functions: linear, exponential, step, none.
Verifies:
  - Half-life behavior (age=half_life → 0.5 for exp, 0.0 for linear)
  - Step threshold (age <= threshold → 1.0, else 0.0)
  - Bounds are [0, 1] (inclusive)
  - apply_decay dispatch and ValueError on unknown function
  - Defensive handling of negative ages and zero half_life
"""
from __future__ import annotations

import math
import unittest

from phase3.signals.decay import (
    DECAY_FUNCTIONS,
    apply_decay,
    decay_exponential,
    decay_linear,
    decay_none,
    decay_step,
)


class TestDecayLinear(unittest.TestCase):
    def test_at_zero_age(self):
        # age=0 → weight = 1 - 0 = 1.0
        self.assertAlmostEqual(decay_linear(0.0, 10.0), 1.0)

    def test_at_half_life(self):
        # age=half_life → 0.0 (linear ramps to 0 at half-life)
        self.assertAlmostEqual(decay_linear(10.0, 10.0), 0.0)

    def test_at_quarter_life(self):
        # age=half_life/4 → 1 - 0.25 = 0.75
        self.assertAlmostEqual(decay_linear(2.5, 10.0), 0.75)

    def test_past_half_life_clamps_to_zero(self):
        # age > half_life → max(0, 1 - age/half) = 0
        self.assertAlmostEqual(decay_linear(20.0, 10.0), 0.0)
        self.assertAlmostEqual(decay_linear(100.0, 10.0), 0.0)

    def test_zero_half_life_returns_zero(self):
        # half_life=0 → defensive 0.0
        self.assertAlmostEqual(decay_linear(5.0, 0.0), 0.0)

    def test_negative_age_treated_as_zero(self):
        # negative age → 0 → 1.0
        self.assertAlmostEqual(decay_linear(-5.0, 10.0), 1.0)

    def test_within_zero_to_one(self):
        for age in [0.0, 1.0, 5.0, 9.99, 10.0, 10.01, 100.0]:
            w = decay_linear(age, 10.0)
            self.assertGreaterEqual(w, 0.0)
            self.assertLessEqual(w, 1.0)


class TestDecayExponential(unittest.TestCase):
    def test_at_zero_age(self):
        # 0.5 ** 0 = 1.0
        self.assertAlmostEqual(decay_exponential(0.0, 10.0), 1.0)

    def test_at_half_life(self):
        # 0.5 ** 1 = 0.5
        self.assertAlmostEqual(decay_exponential(10.0, 10.0), 0.5)

    def test_at_two_half_lives(self):
        # 0.5 ** 2 = 0.25
        self.assertAlmostEqual(decay_exponential(20.0, 10.0), 0.25)

    def test_at_three_half_lives(self):
        # 0.5 ** 3 = 0.125
        self.assertAlmostEqual(decay_exponential(30.0, 10.0), 0.125)

    def test_at_very_large_age_underflows_to_zero(self):
        # 0.5^(1e9) underflows to 0.0 in IEEE 754 double — that's OK.
        # The contract is "non-negative", not "strictly positive".
        self.assertGreaterEqual(decay_exponential(1e9, 10.0), 0.0)
        self.assertEqual(decay_exponential(1e9, 10.0), 0.0)

    def test_within_zero_to_one(self):
        for age in [0.0, 1.0, 10.0, 100.0, 1000.0]:
            w = decay_exponential(age, 10.0)
            self.assertGreaterEqual(w, 0.0)
            self.assertLessEqual(w, 1.0)

    def test_zero_half_life_returns_zero(self):
        self.assertAlmostEqual(decay_exponential(5.0, 0.0), 0.0)

    def test_negative_age_treated_as_zero(self):
        self.assertAlmostEqual(decay_exponential(-5.0, 10.0), 1.0)

    def test_nan_age_treated_as_zero(self):
        # NaN should be coerced to 0 → 1.0
        self.assertAlmostEqual(decay_exponential(float("nan"), 10.0), 1.0)


class TestDecayStep(unittest.TestCase):
    def test_within_threshold(self):
        # age <= threshold → 1.0
        self.assertAlmostEqual(decay_step(0.0, 0.0, 5.0), 1.0)
        self.assertAlmostEqual(decay_step(2.0, 0.0, 5.0), 1.0)
        self.assertAlmostEqual(decay_step(5.0, 0.0, 5.0), 1.0)  # exactly at boundary

    def test_above_threshold(self):
        self.assertAlmostEqual(decay_step(5.01, 0.0, 5.0), 0.0)
        self.assertAlmostEqual(decay_step(10.0, 0.0, 5.0), 0.0)

    def test_zero_threshold(self):
        # threshold=0 → only age=0 yields 1.0
        self.assertAlmostEqual(decay_step(0.0, 0.0, 0.0), 1.0)
        self.assertAlmostEqual(decay_step(0.5, 0.0, 0.0), 0.0)

    def test_negative_threshold_clamped(self):
        # negative threshold treated as 0
        self.assertAlmostEqual(decay_step(0.0, 0.0, -5.0), 1.0)
        self.assertAlmostEqual(decay_step(1.0, 0.0, -5.0), 0.0)

    def test_negative_age_treated_as_zero(self):
        # negative age → 0 → 1.0 if threshold >= 0
        self.assertAlmostEqual(decay_step(-1.0, 0.0, 5.0), 1.0)

    def test_within_zero_to_one(self):
        for age in [-1.0, 0.0, 1.0, 5.0, 10.0, 100.0]:
            w = decay_step(age, 0.0, 5.0)
            self.assertIn(w, (0.0, 1.0))


class TestDecayNone(unittest.TestCase):
    def test_always_one(self):
        for age in [0.0, 1.0, 10.0, 100.0, 1000.0]:
            self.assertAlmostEqual(decay_none(age), 1.0)

    def test_within_zero_to_one(self):
        self.assertEqual(decay_none(1e9), 1.0)


class TestApplyDecayDispatch(unittest.TestCase):
    def test_dispatch_linear(self):
        self.assertAlmostEqual(apply_decay("linear", 5.0, 10.0), 0.5)

    def test_dispatch_exponential(self):
        self.assertAlmostEqual(apply_decay("exponential", 10.0, 10.0), 0.5)

    def test_dispatch_step(self):
        self.assertAlmostEqual(apply_decay("step", 3.0, 0.0, 5.0), 1.0)
        self.assertAlmostEqual(apply_decay("step", 6.0, 0.0, 5.0), 0.0)

    def test_dispatch_none(self):
        self.assertAlmostEqual(apply_decay("none", 100.0, 0.0, 0.0), 1.0)

    def test_unknown_function_raises(self):
        with self.assertRaises(ValueError) as ctx:
            apply_decay("bogus", 0.0, 10.0)
        # The error message should mention the bad function name
        self.assertIn("bogus", str(ctx.exception))

    def test_decay_functions_registry_keys(self):
        self.assertEqual(
            set(DECAY_FUNCTIONS.keys()),
            {"linear", "exponential", "step", "none"},
        )

    def test_apply_decay_returns_float(self):
        for fn in ("linear", "exponential", "step", "none"):
            w = apply_decay(fn, 5.0, 10.0, 5.0)
            self.assertIsInstance(w, float)


class TestDecayBounds(unittest.TestCase):
    """All four functions should output strictly in [0, 1] for valid inputs."""

    def test_all_functions_within_bounds(self):
        test_cases = [
            (0.0, 10.0, 0.0),
            (1.0, 10.0, 0.0),
            (5.0, 10.0, 0.0),
            (10.0, 10.0, 0.0),
            (15.0, 10.0, 0.0),
            (100.0, 10.0, 0.0),
        ]
        for age, half_life, step_th in test_cases:
            for fn in ("linear", "exponential", "step", "none"):
                w = apply_decay(fn, age, half_life, step_th)
                self.assertGreaterEqual(
                    w, 0.0, f"{fn} at age={age} produced {w} < 0"
                )
                self.assertLessEqual(
                    w, 1.0, f"{fn} at age={age} produced {w} > 1"
                )


if __name__ == "__main__":
    unittest.main()
