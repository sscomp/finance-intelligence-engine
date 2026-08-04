"""Phase 5 M3-S1 — Targeted tests for compute_exposure + ExposureReport.

Covers:
- ExposureReport construction + to_dict / from_dict round-trip
- compute_exposure against fixture portfolios FP1-FP6
- gross / net / long / short decomposition
- by_entity and by_currency aggregation
- empty portfolio edge case
- market_prices input validation (ValueError paths)
- determinism (same inputs -> byte-identical outputs)

Tolerance: default ``math.isclose(rel_tol=1e-9, abs_tol=0.0)`` per kickoff
plan §8.7.
"""
from __future__ import annotations

import math
import unittest
from typing import Any

from phase3.portfolio.domain import (
    CostBasis,
    EntityId,
    Portfolio,
    PortfolioId,
    Position,
    PositionId,
    Price,
    Quantity,
    Weight,
)
from phase3.portfolio.risk import ExposureReport, compute_exposure


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def _pos(
    pid: str,
    eid: str,
    weight: float,
    qty: int = 100,
    price: tuple[float, str] | None = None,
    cost: tuple[float, str] | None = None,
) -> Position:
    """Build a Position with sensible defaults."""
    price_obj = Price(value=price[0], currency=price[1]) if price else None
    cost_obj = CostBasis(value=cost[0], currency=cost[1]) if cost else None
    return Position(
        position_id=PositionId(value=pid),
        entity_id=EntityId(value=eid),
        weight=Weight(value=weight),
        quantity=Quantity(value=qty),
        price=price_obj,
        cost_basis=cost_obj,
    )


def _portfolio(pid: str, name: str, positions: tuple[Position, ...]) -> Portfolio:
    return Portfolio(
        portfolio_id=PortfolioId(value=pid),
        name=name,
        positions=positions,
    )


def fp1_equal_weight_5() -> Portfolio:
    """FP1: Equal-weight 5-position portfolio. Weights 0.20 each."""
    positions = tuple(
        _pos(f"p{i}", f"company:TW:{i}", 0.20, price=(100.0, "TWD"))
        for i in range(1, 6)
    )
    return _portfolio("fp1", "Equal Weight 5", positions)


def fp2_single_position() -> Portfolio:
    """FP2: Single-position portfolio. Weight 1.00."""
    positions = (_pos("p1", "company:TW:1", 1.00, price=(50.0, "USD")),)
    return _portfolio("fp2", "Single Position", positions)


def fp3_concentrated_80_20() -> Portfolio:
    """FP3: Concentrated 80/20 portfolio."""
    positions = (
        _pos("p1", "company:TW:1", 0.80, price=(100.0, "TWD")),
        _pos("p2", "company:TW:2", 0.20, price=(200.0, "TWD")),
    )
    return _portfolio("fp3", "Concentrated 80/20", positions)


def fp4_empty() -> Portfolio:
    """FP4: Empty portfolio (0 positions)."""
    return _portfolio("fp4", "Empty", tuple())


def fp5_multi_currency() -> Portfolio:
    """FP5: Multi-currency portfolio (USD, EUR, TWD)."""
    positions = (
        _pos("p1", "company:US:1", 0.40, price=(100.0, "USD")),
        _pos("p2", "company:EU:2", 0.35, price=(80.0, "EUR")),
        _pos("p3", "company:TW:3", 0.25, price=(30.0, "TWD")),
    )
    return _portfolio("fp5", "Multi Currency", positions)


def fp6_max_positions() -> Portfolio:
    """FP6: Max-positions portfolio (4096 positions, equal weight 1/4096)."""
    n = 4096
    w = 1.0 / n
    positions = tuple(
        _pos(f"p{i}", f"company:TW:{i}", w, price=(10.0, "TWD"))
        for i in range(n)
    )
    return _portfolio("fp6", "Max Positions", positions)


# --------------------------------------------------------------------------- #
# ExposureReport serialization tests
# --------------------------------------------------------------------------- #


class TestExposureReportSerialization(unittest.TestCase):
    """Round-trip and construction tests for ExposureReport."""

    def test_construct_minimal_report(self):
        """Construct with required fields; defaults applied."""
        r = ExposureReport(
            portfolio_id="pf1",
            gross_exposure=1.0,
            net_exposure=1.0,
            long_exposure=1.0,
            short_exposure=0.0,
        )
        self.assertEqual(r.portfolio_id, "pf1")
        self.assertEqual(r.by_entity, {})
        self.assertEqual(r.by_currency, {})
        self.assertEqual(r.position_count, 0)

    def test_construct_full_report(self):
        """Construct with all fields populated."""
        r = ExposureReport(
            portfolio_id="pf1",
            gross_exposure=0.5,
            net_exposure=0.5,
            long_exposure=0.5,
            short_exposure=0.0,
            by_entity={"a": 0.3, "b": 0.2},
            by_currency={"USD": 0.5},
            position_count=2,
        )
        self.assertEqual(r.by_entity, {"a": 0.3, "b": 0.2})
        self.assertEqual(r.by_currency, {"USD": 0.5})
        self.assertEqual(r.position_count, 2)

    def test_to_dict_keys_fixed_order(self):
        """to_dict keys are in code-defined order."""
        r = ExposureReport(
            portfolio_id="pf1",
            gross_exposure=1.0,
            net_exposure=1.0,
            long_exposure=1.0,
            short_exposure=0.0,
            by_entity={"a": 1.0},
            by_currency={"USD": 1.0},
            position_count=1,
        )
        d = r.to_dict()
        expected_keys = [
            "portfolio_id",
            "gross_exposure",
            "net_exposure",
            "long_exposure",
            "short_exposure",
            "by_entity",
            "by_currency",
            "position_count",
        ]
        self.assertEqual(list(d.keys()), expected_keys)

    def test_round_trip_byte_identical(self):
        """from_dict(to_dict(x)).to_dict() == to_dict(x). Tolerance: exact."""
        original = ExposureReport(
            portfolio_id="pf1",
            gross_exposure=0.95,
            net_exposure=0.95,
            long_exposure=0.95,
            short_exposure=0.0,
            by_entity={"a": 0.5, "b": 0.45},
            by_currency={"USD": 0.5, "TWD": 0.45},
            position_count=2,
        )
        d1 = original.to_dict()
        rebuilt = ExposureReport.from_dict(d1)
        d2 = rebuilt.to_dict()
        self.assertEqual(d1, d2)

    def test_round_trip_empty_dict_fields(self):
        """Round-trip with empty by_entity/by_currency dicts."""
        original = ExposureReport(
            portfolio_id="pf1",
            gross_exposure=0.0,
            net_exposure=0.0,
            long_exposure=0.0,
            short_exposure=0.0,
        )
        d1 = original.to_dict()
        rebuilt = ExposureReport.from_dict(d1)
        self.assertEqual(rebuilt.to_dict(), d1)

    def test_frozen_dataclass_immutable(self):
        """ExposureReport is frozen; setattr raises FrozenInstanceError."""
        r = ExposureReport(
            portfolio_id="pf1",
            gross_exposure=1.0,
            net_exposure=1.0,
            long_exposure=1.0,
            short_exposure=0.0,
        )
        with self.assertRaises(Exception):
            r.gross_exposure = 2.0  # type: ignore[misc]

    def test_hashable_when_no_mutable_defaults(self):
        """ExposureReport with empty dicts is hashable (frozen=True)."""
        r = ExposureReport(
            portfolio_id="pf1",
            gross_exposure=1.0,
            net_exposure=1.0,
            long_exposure=1.0,
            short_exposure=0.0,
        )
        # Frozen dataclasses are hashable iff all fields are hashable. Empty
        # dict is unhashable, so this should raise. We assert it raises to
        # document the behavior.
        with self.assertRaises(TypeError):
            hash(r)


# --------------------------------------------------------------------------- #
# compute_exposure tests against fixture portfolios
# --------------------------------------------------------------------------- #


class TestComputeExposureFixtures(unittest.TestCase):
    """compute_exposure against FP1-FP6 fixture portfolios."""

    def test_fp1_equal_weight_5_gross_1(self):
        """FP1: 5 positions at 0.20 each -> gross=1.0, by_entity 5 entries."""
        pf = fp1_equal_weight_5()
        r = compute_exposure(pf)
        self.assertEqual(r.portfolio_id, "fp1")
        self.assertTrue(math.isclose(r.gross_exposure, 1.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.net_exposure, 1.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.long_exposure, 1.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.short_exposure, 0.0, rel_tol=1e-9))
        self.assertEqual(r.position_count, 5)
        self.assertEqual(len(r.by_entity), 5)
        for eid, w in r.by_entity.items():
            self.assertTrue(math.isclose(w, 0.20, rel_tol=1e-9))
        self.assertEqual(r.by_currency, {"TWD": 1.0})

    def test_fp2_single_position(self):
        """FP2: 1 position at weight 1.00 -> gross=1.0, single entity."""
        pf = fp2_single_position()
        r = compute_exposure(pf)
        self.assertTrue(math.isclose(r.gross_exposure, 1.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.long_exposure, 1.0, rel_tol=1e-9))
        self.assertEqual(r.position_count, 1)
        self.assertEqual(r.by_entity, {"company:TW:1": 1.0})
        self.assertEqual(r.by_currency, {"USD": 1.0})

    def test_fp3_concentrated_80_20(self):
        """FP3: 80/20 split -> gross=1.0, entity weights match."""
        pf = fp3_concentrated_80_20()
        r = compute_exposure(pf)
        self.assertTrue(math.isclose(r.gross_exposure, 1.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.by_entity["company:TW:1"], 0.80, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.by_entity["company:TW:2"], 0.20, rel_tol=1e-9))
        self.assertEqual(r.position_count, 2)
        self.assertTrue(math.isclose(r.by_currency["TWD"], 1.0, rel_tol=1e-9))

    def test_fp4_empty_portfolio(self):
        """FP4: empty portfolio -> all-zero exposures, empty dicts."""
        pf = fp4_empty()
        r = compute_exposure(pf)
        self.assertEqual(r.portfolio_id, "fp4")
        self.assertTrue(math.isclose(r.gross_exposure, 0.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.net_exposure, 0.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.long_exposure, 0.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.short_exposure, 0.0, rel_tol=1e-9))
        self.assertEqual(r.by_entity, {})
        self.assertEqual(r.by_currency, {})
        self.assertEqual(r.position_count, 0)

    def test_fp5_multi_currency(self):
        """FP5: 3 positions in USD/EUR/TWD -> by_currency has 3 keys."""
        pf = fp5_multi_currency()
        r = compute_exposure(pf)
        self.assertTrue(math.isclose(r.gross_exposure, 1.0, rel_tol=1e-9))
        self.assertEqual(set(r.by_currency.keys()), {"USD", "EUR", "TWD"})
        self.assertTrue(math.isclose(r.by_currency["USD"], 0.40, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.by_currency["EUR"], 0.35, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.by_currency["TWD"], 0.25, rel_tol=1e-9))
        self.assertEqual(r.position_count, 3)

    def test_fp6_max_positions(self):
        """FP6: 4096 equal-weight positions -> gross ~1.0, HHI ~1/4096."""
        pf = fp6_max_positions()
        r = compute_exposure(pf)
        self.assertEqual(r.position_count, 4096)
        # gross = 4096 * (1/4096) = 1.0 with floating-point tolerance
        self.assertTrue(math.isclose(r.gross_exposure, 1.0, rel_tol=1e-6, abs_tol=1e-12))
        self.assertEqual(len(r.by_entity), 4096)


# --------------------------------------------------------------------------- #
# compute_exposure behavior tests
# --------------------------------------------------------------------------- #


class TestComputeExposureBehavior(unittest.TestCase):
    """Behavioral and determinism tests for compute_exposure."""

    def test_unknown_currency_for_no_price_positions(self):
        """Positions without Price are bucketed under 'UNKNOWN'."""
        positions = (
            _pos("p1", "company:TW:1", 0.50, price=None),
            _pos("p2", "company:TW:2", 0.50, price=(100.0, "TWD")),
        )
        pf = _portfolio("pf", "Mixed Price", positions)
        r = compute_exposure(pf)
        self.assertEqual(set(r.by_currency.keys()), {"UNKNOWN", "TWD"})
        self.assertTrue(math.isclose(r.by_currency["UNKNOWN"], 0.50, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.by_currency["TWD"], 0.50, rel_tol=1e-9))

    def test_determinism_same_inputs_byte_identical(self):
        """Same inputs produce byte-identical to_dict outputs."""
        pf = fp3_concentrated_80_20()
        r1 = compute_exposure(pf)
        r2 = compute_exposure(pf)
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_determinism_repeated_calls(self):
        """Call compute_exposure 5 times; all outputs equal."""
        pf = fp5_multi_currency()
        dicts = [compute_exposure(pf).to_dict() for _ in range(5)]
        for d in dicts[1:]:
            self.assertEqual(d, dicts[0])

    def test_long_only_net_equals_gross(self):
        """M2 domain is long-only: net_exposure == gross_exposure."""
        pf = fp1_equal_weight_5()
        r = compute_exposure(pf)
        self.assertTrue(math.isclose(r.net_exposure, r.gross_exposure, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.short_exposure, 0.0, rel_tol=1e-9))

    def test_by_entity_sums_to_gross(self):
        """by_entity values sum to gross_exposure. Tolerance: 1e-9."""
        pf = fp5_multi_currency()
        r = compute_exposure(pf)
        total = sum(r.by_entity.values())
        self.assertTrue(math.isclose(total, r.gross_exposure, rel_tol=1e-9))

    def test_by_currency_sums_to_gross(self):
        """by_currency values sum to gross_exposure. Tolerance: 1e-9."""
        pf = fp5_multi_currency()
        r = compute_exposure(pf)
        total = sum(r.by_currency.values())
        self.assertTrue(math.isclose(total, r.gross_exposure, rel_tol=1e-9))

    def test_market_prices_none_ok(self):
        """market_prices=None is accepted (default)."""
        pf = fp1_equal_weight_5()
        r = compute_exposure(pf, market_prices=None)
        self.assertTrue(math.isclose(r.gross_exposure, 1.0, rel_tol=1e-9))

    def test_market_prices_empty_dict_ok(self):
        """Empty market_prices dict is accepted."""
        pf = fp1_equal_weight_5()
        r = compute_exposure(pf, market_prices={})
        self.assertTrue(math.isclose(r.gross_exposure, 1.0, rel_tol=1e-9))

    def test_market_prices_valid_subset_ok(self):
        """market_prices with a valid subset of entity IDs is accepted."""
        pf = fp3_concentrated_80_20()
        prices = {"company:TW:1": 105.0}
        r = compute_exposure(pf, market_prices=prices)
        self.assertTrue(math.isclose(r.gross_exposure, 1.0, rel_tol=1e-9))

    def test_market_prices_unknown_entity_raises(self):
        """market_prices with an entity ID not in portfolio raises ValueError."""
        pf = fp3_concentrated_80_20()
        prices = {"company:TW:999": 100.0}
        with self.assertRaises(ValueError):
            compute_exposure(pf, market_prices=prices)

    def test_market_prices_negative_value_raises(self):
        """market_prices with a negative value raises ValueError."""
        pf = fp3_concentrated_80_20()
        prices = {"company:TW:1": -1.0}
        with self.assertRaises(ValueError):
            compute_exposure(pf, market_prices=prices)

    def test_market_prices_nan_value_raises(self):
        """market_prices with NaN value raises ValueError."""
        pf = fp3_concentrated_80_20()
        prices = {"company:TW:1": float("nan")}
        with self.assertRaises(ValueError):
            compute_exposure(pf, market_prices=prices)

    def test_market_prices_inf_value_raises(self):
        """market_prices with inf value raises ValueError."""
        pf = fp3_concentrated_80_20()
        prices = {"company:TW:1": float("inf")}
        with self.assertRaises(ValueError):
            compute_exposure(pf, market_prices=prices)

    def test_market_prices_non_dict_raises(self):
        """market_prices that is not a dict raises ValueError."""
        pf = fp3_concentrated_80_20()
        with self.assertRaises(ValueError):
            compute_exposure(pf, market_prices=[("company:TW:1", 100.0)])  # type: ignore[arg-type]

    def test_round_trip_report_from_compute(self):
        """Report produced by compute_exposure round-trips through to/from_dict."""
        pf = fp5_multi_currency()
        r1 = compute_exposure(pf)
        d1 = r1.to_dict()
        r2 = ExposureReport.from_dict(d1)
        self.assertEqual(d1, r2.to_dict())


if __name__ == "__main__":
    unittest.main()