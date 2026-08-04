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
from phase3.portfolio.risk import (
    ConcentrationReport,
    DrawdownReport,
    ExposureReport,
    compute_concentration,
    compute_drawdown,
    compute_exposure,
)


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


# --------------------------------------------------------------------------- #
# M3-S2: Concentration tests
# --------------------------------------------------------------------------- #


class TestConcentrationReportSerialization(unittest.TestCase):
    """Round-trip and construction tests for ConcentrationReport."""

    def test_construct_minimal_report(self):
        """Construct with required fields; defaults applied."""
        r = ConcentrationReport(
            portfolio_id="pf1",
            hhi=0.2,
            top_n_concentration=0.6,
            top_n=5,
            effective_position_count=5.0,
            position_count=5,
        )
        self.assertEqual(r.portfolio_id, "pf1")
        self.assertIsNone(r.top_entity_concentration)
        self.assertEqual(r.by_entity, {})

    def test_construct_full_report(self):
        """Construct with all fields populated including by_entity."""
        r = ConcentrationReport(
            portfolio_id="pf1",
            hhi=0.5,
            top_n_concentration=0.9,
            top_n=3,
            effective_position_count=2.0,
            position_count=5,
            top_entity_concentration=0.9,
            by_entity={"a": 0.5, "b": 0.4},
        )
        self.assertEqual(r.by_entity, {"a": 0.5, "b": 0.4})
        self.assertEqual(r.top_entity_concentration, 0.9)

    def test_to_dict_keys_fixed_order(self):
        """to_dict keys are in code-defined order."""
        r = ConcentrationReport(
            portfolio_id="pf1",
            hhi=0.2,
            top_n_concentration=0.6,
            top_n=5,
            effective_position_count=5.0,
            position_count=5,
            by_entity={"a": 0.2},
        )
        d = r.to_dict()
        expected_keys = [
            "portfolio_id",
            "hhi",
            "top_n_concentration",
            "top_n",
            "effective_position_count",
            "position_count",
            "top_entity_concentration",
            "by_entity",
        ]
        self.assertEqual(list(d.keys()), expected_keys)

    def test_round_trip_byte_identical(self):
        """from_dict(to_dict(x)).to_dict() == to_dict(x). Tolerance: exact."""
        original = ConcentrationReport(
            portfolio_id="pf1",
            hhi=0.345,
            top_n_concentration=0.75,
            top_n=2,
            effective_position_count=2.898,
            position_count=3,
            top_entity_concentration=None,
            by_entity={"a": 0.4, "b": 0.35, "c": 0.25},
        )
        d1 = original.to_dict()
        rebuilt = ConcentrationReport.from_dict(d1)
        d2 = rebuilt.to_dict()
        self.assertEqual(d1, d2)

    def test_round_trip_with_top_entity_concentration(self):
        """Round-trip with top_entity_concentration set."""
        original = ConcentrationReport(
            portfolio_id="pf1",
            hhi=0.5,
            top_n_concentration=0.9,
            top_n=2,
            effective_position_count=2.0,
            position_count=3,
            top_entity_concentration=0.85,
            by_entity={"a": 0.5, "b": 0.35, "c": 0.15},
        )
        d1 = original.to_dict()
        rebuilt = ConcentrationReport.from_dict(d1)
        self.assertEqual(d1, rebuilt.to_dict())

    def test_frozen_dataclass_immutable(self):
        """ConcentrationReport is frozen; setattr raises FrozenInstanceError."""
        r = ConcentrationReport(
            portfolio_id="pf1",
            hhi=0.2,
            top_n_concentration=0.6,
            top_n=5,
            effective_position_count=5.0,
            position_count=5,
        )
        with self.assertRaises(Exception):
            r.hhi = 0.5  # type: ignore[misc]


class TestComputeConcentrationFixtures(unittest.TestCase):
    """compute_concentration against FP1-FP6 fixture portfolios."""

    def test_fp1_equal_weight_5_hhi(self):
        """FP1: 5 positions at 0.20 each -> HHI = 0.20, effective N = 5."""
        pf = fp1_equal_weight_5()
        r = compute_concentration(pf)
        self.assertEqual(r.portfolio_id, "fp1")
        # HHI = 5 * 0.04 = 0.20
        self.assertTrue(math.isclose(r.hhi, 0.20, rel_tol=1e-9, abs_tol=1e-12))
        # effective N = 1 / 0.20 = 5.0
        self.assertTrue(math.isclose(r.effective_position_count, 5.0, rel_tol=1e-9))
        self.assertEqual(r.position_count, 5)
        self.assertEqual(r.top_n, 5)  # default top_n=5, position_count=5
        # top-5 = sum of all = 1.0
        self.assertTrue(math.isclose(r.top_n_concentration, 1.0, rel_tol=1e-9))
        self.assertIsNone(r.top_entity_concentration)  # M2: one pos per entity
        self.assertEqual(len(r.by_entity), 5)

    def test_fp2_single_position_max_concentration(self):
        """FP2: 1 position at weight 1.00 -> HHI = 1.0, effective N = 1.0."""
        pf = fp2_single_position()
        r = compute_concentration(pf)
        self.assertTrue(math.isclose(r.hhi, 1.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.effective_position_count, 1.0, rel_tol=1e-9))
        self.assertEqual(r.position_count, 1)
        self.assertEqual(r.top_n, 1)  # clamped from 5 to 1
        self.assertTrue(math.isclose(r.top_n_concentration, 1.0, rel_tol=1e-9))
        self.assertIsNone(r.top_entity_concentration)

    def test_fp3_concentrated_80_20(self):
        """FP3: 80/20 split -> HHI = 0.68, top-1 = 0.80, effective N ~ 1.47."""
        pf = fp3_concentrated_80_20()
        r = compute_concentration(pf)
        # HHI = 0.64 + 0.04 = 0.68
        self.assertTrue(math.isclose(r.hhi, 0.68, rel_tol=1e-9, abs_tol=1e-12))
        # top-1 = 0.80
        r_top1 = compute_concentration(pf, top_n=1)
        self.assertTrue(math.isclose(r_top1.top_n_concentration, 0.80, rel_tol=1e-9))
        # effective N = 1 / 0.68 ≈ 1.470588
        self.assertTrue(
            math.isclose(r.effective_position_count, 1.0 / 0.68, rel_tol=1e-6, abs_tol=1e-12)
        )
        self.assertEqual(r.position_count, 2)
        self.assertEqual(r.top_n, 2)  # clamped from 5 to 2
        self.assertIsNone(r.top_entity_concentration)

    def test_fp4_empty_portfolio_zero_concentration(self):
        """FP4: empty portfolio -> HHI=0.0, effective N=0.0, top-N=0.0."""
        pf = fp4_empty()
        r = compute_concentration(pf)
        self.assertTrue(math.isclose(r.hhi, 0.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.effective_position_count, 0.0, rel_tol=1e-9))
        self.assertEqual(r.position_count, 0)
        self.assertEqual(r.top_n, 0)  # clamped from 5 to 0
        self.assertTrue(math.isclose(r.top_n_concentration, 0.0, rel_tol=1e-9))
        self.assertEqual(r.by_entity, {})
        self.assertIsNone(r.top_entity_concentration)

    def test_fp5_multi_currency_concentration(self):
        """FP5: 3 positions 0.40/0.35/0.25 -> HHI = 0.345, top-2 = 0.75."""
        pf = fp5_multi_currency()
        r = compute_concentration(pf)
        # HHI = 0.16 + 0.1225 + 0.0625 = 0.345
        self.assertTrue(math.isclose(r.hhi, 0.345, rel_tol=1e-9, abs_tol=1e-12))
        # effective N ≈ 2.898
        self.assertTrue(
            math.isclose(r.effective_position_count, 1.0 / 0.345, rel_tol=1e-6, abs_tol=1e-12)
        )
        # top-2 = 0.40 + 0.35 = 0.75
        r_top2 = compute_concentration(pf, top_n=2)
        self.assertTrue(math.isclose(r_top2.top_n_concentration, 0.75, rel_tol=1e-9))
        self.assertEqual(r.position_count, 3)
        self.assertEqual(len(r.by_entity), 3)
        self.assertIsNone(r.top_entity_concentration)

    def test_fp6_max_positions_minimal_concentration(self):
        """FP6: 4096 equal-weight -> HHI ≈ 1/4096, effective N ≈ 4096."""
        pf = fp6_max_positions()
        r = compute_concentration(pf)
        self.assertEqual(r.position_count, 4096)
        expected_hhi = 1.0 / 4096  # each w = 1/4096, HHI = 4096 * (1/4096)^2 = 1/4096
        self.assertTrue(math.isclose(r.hhi, expected_hhi, rel_tol=1e-6, abs_tol=1e-12))
        self.assertTrue(
            math.isclose(r.effective_position_count, 4096.0, rel_tol=1e-6, abs_tol=1e-12)
        )
        self.assertEqual(r.top_n, 5)  # default
        # top-5 = 5/4096
        self.assertTrue(
            math.isclose(r.top_n_concentration, 5.0 / 4096, rel_tol=1e-6, abs_tol=1e-12)
        )


class TestComputeConcentrationBehavior(unittest.TestCase):
    """Behavioral, validation, and determinism tests for compute_concentration."""

    def test_top_n_clamped_to_position_count(self):
        """top_n > position_count is clamped, no error."""
        pf = fp3_concentrated_80_20()
        r = compute_concentration(pf, top_n=100)
        self.assertEqual(r.top_n, 2)
        self.assertTrue(math.isclose(r.top_n_concentration, 1.0, rel_tol=1e-9))

    def test_top_n_zero_raises(self):
        """top_n = 0 raises ValueError."""
        pf = fp1_equal_weight_5()
        with self.assertRaises(ValueError):
            compute_concentration(pf, top_n=0)

    def test_top_n_negative_raises(self):
        """top_n < 0 raises ValueError."""
        pf = fp1_equal_weight_5()
        with self.assertRaises(ValueError):
            compute_concentration(pf, top_n=-1)

    def test_top_n_must_be_int_not_bool(self):
        """top_n as bool raises ValueError (bool is not int for this guard)."""
        pf = fp1_equal_weight_5()
        with self.assertRaises(ValueError):
            compute_concentration(pf, top_n=True)  # type: ignore[arg-type]

    def test_determinism_same_inputs_byte_identical(self):
        """Same inputs produce byte-identical to_dict outputs."""
        pf = fp5_multi_currency()
        r1 = compute_concentration(pf)
        r2 = compute_concentration(pf)
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_determinism_repeated_calls(self):
        """Call compute_concentration 5 times; all outputs equal."""
        pf = fp3_concentrated_80_20()
        dicts = [compute_concentration(pf).to_dict() for _ in range(5)]
        for d in dicts[1:]:
            self.assertEqual(d, dicts[0])

    def test_hhi_on_raw_weights_not_normalized(self):
        """HHI is on raw weights; weights summing to 0.5 produce HHI on 0.5."""
        positions = (
            _pos("p1", "company:TW:1", 0.40, price=(100.0, "TWD")),
            _pos("p2", "company:TW:2", 0.10, price=(100.0, "TWD")),
        )
        pf = _portfolio("pf_half", "Half Invested", positions)
        r = compute_concentration(pf)
        # HHI = 0.16 + 0.01 = 0.17 (NOT normalized to 1.0)
        self.assertTrue(math.isclose(r.hhi, 0.17, rel_tol=1e-9, abs_tol=1e-12))
        # top-1 = 0.40
        r_top1 = compute_concentration(pf, top_n=1)
        self.assertTrue(math.isclose(r_top1.top_n_concentration, 0.40, rel_tol=1e-9))

    def test_round_trip_report_from_compute(self):
        """Report produced by compute_concentration round-trips through to/from_dict."""
        pf = fp5_multi_currency()
        r1 = compute_concentration(pf)
        d1 = r1.to_dict()
        r2 = ConcentrationReport.from_dict(d1)
        self.assertEqual(d1, r2.to_dict())


# --------------------------------------------------------------------------- #
# M3-S2: Drawdown tests
# --------------------------------------------------------------------------- #


class TestDrawdownReportSerialization(unittest.TestCase):
    """Round-trip and construction tests for DrawdownReport."""

    def test_construct_minimal_report(self):
        """Construct with required fields."""
        r = DrawdownReport(
            max_drawdown=0.5,
            average_drawdown=0.2,
            drawdown_duration=3,
            peak_index=0,
            trough_index=3,
            drawdown_series=(0.0, 0.1, 0.2, 0.5),
            series_length=4,
        )
        self.assertEqual(r.series_length, 4)
        self.assertEqual(r.drawdown_series, (0.0, 0.1, 0.2, 0.5))

    def test_to_dict_keys_fixed_order(self):
        """to_dict keys are in code-defined order."""
        r = DrawdownReport(
            max_drawdown=0.5,
            average_drawdown=0.2,
            drawdown_duration=3,
            peak_index=0,
            trough_index=3,
            drawdown_series=(0.0, 0.1, 0.2, 0.5),
            series_length=4,
        )
        d = r.to_dict()
        expected_keys = [
            "max_drawdown",
            "average_drawdown",
            "drawdown_duration",
            "peak_index",
            "trough_index",
            "drawdown_series",
            "series_length",
        ]
        self.assertEqual(list(d.keys()), expected_keys)

    def test_round_trip_tuple_to_list_to_tuple(self):
        """from_dict(to_dict(x)).to_dict() == to_dict(x). tuple <-> list."""
        original = DrawdownReport(
            max_drawdown=0.7,
            average_drawdown=0.389,
            drawdown_duration=5,
            peak_index=0,
            trough_index=5,
            drawdown_series=(0.0, 0.05, 0.2, 0.4, 0.6, 0.7),
            series_length=6,
        )
        d1 = original.to_dict()
        # drawdown_series serialized as list
        self.assertIsInstance(d1["drawdown_series"], list)
        self.assertEqual(d1["drawdown_series"], [0.0, 0.05, 0.2, 0.4, 0.6, 0.7])
        rebuilt = DrawdownReport.from_dict(d1)
        # drawdown_series restored as tuple
        self.assertIsInstance(rebuilt.drawdown_series, tuple)
        d2 = rebuilt.to_dict()
        self.assertEqual(d1, d2)

    def test_round_trip_empty_series_field(self):
        """Round-trip with single-point drawdown_series (no drawdown)."""
        original = DrawdownReport(
            max_drawdown=0.0,
            average_drawdown=0.0,
            drawdown_duration=0,
            peak_index=0,
            trough_index=0,
            drawdown_series=(0.0, 0.0),
            series_length=2,
        )
        d1 = original.to_dict()
        rebuilt = DrawdownReport.from_dict(d1)
        self.assertEqual(d1, rebuilt.to_dict())

    def test_frozen_dataclass_immutable(self):
        """DrawdownReport is frozen; setattr raises FrozenInstanceError."""
        r = DrawdownReport(
            max_drawdown=0.5,
            average_drawdown=0.2,
            drawdown_duration=3,
            peak_index=0,
            trough_index=3,
            drawdown_series=(0.0, 0.1, 0.2, 0.5),
            series_length=4,
        )
        with self.assertRaises(Exception):
            r.max_drawdown = 0.9  # type: ignore[misc]


class TestComputeDrawdownFixtures(unittest.TestCase):
    """compute_drawdown against FM4-FM6 + FM7/FM8 fixture value series."""

    def test_fm4_crash_scenario(self):
        """FM4: [100, 95, 80, 60, 40, 30] -> max DD = 70%, duration = 5."""
        series = [100.0, 95.0, 80.0, 60.0, 40.0, 30.0]
        r = compute_drawdown(series)
        # max drawdown = (100 - 30) / 100 = 0.70
        self.assertTrue(math.isclose(r.max_drawdown, 0.70, rel_tol=1e-9))
        self.assertEqual(r.series_length, 6)
        self.assertEqual(r.peak_index, 0)
        self.assertEqual(r.trough_index, 5)
        # drawdown series: all 5 points after peak are in drawdown
        self.assertEqual(r.drawdown_duration, 5)
        # average drawdown = mean of [0, 0.05, 0.2, 0.4, 0.6, 0.7]
        expected_avg = (0.0 + 0.05 + 0.20 + 0.40 + 0.60 + 0.70) / 6
        self.assertTrue(math.isclose(r.average_drawdown, expected_avg, rel_tol=1e-9))

    def test_fm5_monotonic_increase_no_drawdown(self):
        """FM5: [100, 110, 120, 130] -> max DD = 0%, duration = 0."""
        series = [100.0, 110.0, 120.0, 130.0]
        r = compute_drawdown(series)
        self.assertTrue(math.isclose(r.max_drawdown, 0.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.average_drawdown, 0.0, rel_tol=1e-9))
        self.assertEqual(r.drawdown_duration, 0)
        self.assertEqual(r.peak_index, 0)
        self.assertEqual(r.trough_index, 0)
        self.assertEqual(r.series_length, 4)
        self.assertEqual(r.drawdown_series, (0.0, 0.0, 0.0, 0.0))

    def test_fm6_flat_no_drawdown(self):
        """FM6: [100, 100, 100, 100] -> max DD = 0%, duration = 0."""
        series = [100.0, 100.0, 100.0, 100.0]
        r = compute_drawdown(series)
        self.assertTrue(math.isclose(r.max_drawdown, 0.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.average_drawdown, 0.0, rel_tol=1e-9))
        self.assertEqual(r.drawdown_duration, 0)
        self.assertEqual(r.series_length, 4)
        self.assertEqual(r.drawdown_series, (0.0, 0.0, 0.0, 0.0))

    def test_fm7_recovery_scenario(self):
        """FM7: [100, 80, 90, 110, 105, 120] -> max DD = 20%."""
        series = [100.0, 80.0, 90.0, 110.0, 105.0, 120.0]
        r = compute_drawdown(series)
        # max drawdown = (100 - 80) / 100 = 0.20 at index 1
        self.assertTrue(math.isclose(r.max_drawdown, 0.20, rel_tol=1e-9))
        self.assertEqual(r.peak_index, 0)
        self.assertEqual(r.trough_index, 1)
        # drawdown at 105 (index 4): peak 110, dd = (110-105)/110 ≈ 0.04545
        # duration: index 1 (dd>0), index 2 (dd>0), index 3 (dd=0, new peak)
        # index 4 (dd>0), index 5 (dd=0, new peak). Longest run = 2 (indices 1-2)
        self.assertEqual(r.drawdown_duration, 2)
        self.assertEqual(r.series_length, 6)

    def test_fm8_all_zero_peak_guard(self):
        """FM8: [0, 0, 0] -> max DD = 0% (peak=0 division guard)."""
        series = [0.0, 0.0, 0.0]
        r = compute_drawdown(series)
        self.assertTrue(math.isclose(r.max_drawdown, 0.0, rel_tol=1e-9))
        self.assertTrue(math.isclose(r.average_drawdown, 0.0, rel_tol=1e-9))
        self.assertEqual(r.drawdown_duration, 0)
        self.assertEqual(r.series_length, 3)
        self.assertEqual(r.drawdown_series, (0.0, 0.0, 0.0))


class TestComputeDrawdownBehavior(unittest.TestCase):
    """Behavioral, validation, and determinism tests for compute_drawdown."""

    def test_empty_series_raises(self):
        """Empty value series raises ValueError (length >= 2 required)."""
        with self.assertRaises(ValueError):
            compute_drawdown([])

    def test_single_element_series_raises(self):
        """Single-element series raises ValueError (length >= 2 required)."""
        with self.assertRaises(ValueError):
            compute_drawdown([100.0])

    def test_nan_in_series_raises(self):
        """NaN in value series raises ValueError."""
        with self.assertRaises(ValueError):
            compute_drawdown([100.0, float("nan"), 90.0])

    def test_inf_in_series_raises(self):
        """Inf in value series raises ValueError."""
        with self.assertRaises(ValueError):
            compute_drawdown([100.0, float("inf"), 90.0])

    def test_negative_value_raises(self):
        """Negative value in series raises ValueError."""
        with self.assertRaises(ValueError):
            compute_drawdown([100.0, -10.0, 90.0])

    def test_non_list_raises(self):
        """Non-list input (tuple) raises ValueError."""
        with self.assertRaises(ValueError):
            compute_drawdown((100.0, 90.0, 80.0))  # type: ignore[arg-type]

    def test_determinism_same_inputs_byte_identical(self):
        """Same inputs produce byte-identical to_dict outputs."""
        series = [100.0, 95.0, 80.0, 60.0, 40.0, 30.0]
        r1 = compute_drawdown(series)
        r2 = compute_drawdown(series)
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_determinism_repeated_calls(self):
        """Call compute_drawdown 5 times; all outputs equal."""
        series = [100.0, 80.0, 90.0, 110.0, 105.0, 120.0]
        dicts = [compute_drawdown(series).to_dict() for _ in range(5)]
        for d in dicts[1:]:
            self.assertEqual(d, dicts[0])

    def test_round_trip_report_from_compute(self):
        """Report produced by compute_drawdown round-trips through to/from_dict."""
        series = [100.0, 95.0, 80.0, 60.0, 40.0, 30.0]
        r1 = compute_drawdown(series)
        d1 = r1.to_dict()
        r2 = DrawdownReport.from_dict(d1)
        self.assertEqual(d1, r2.to_dict())

    def test_two_element_minimal_series(self):
        """Minimal length-2 series: [100, 90] -> max DD = 10%, duration = 1."""
        r = compute_drawdown([100.0, 90.0])
        self.assertTrue(math.isclose(r.max_drawdown, 0.10, rel_tol=1e-9))
        self.assertEqual(r.drawdown_duration, 1)
        self.assertEqual(r.peak_index, 0)
        self.assertEqual(r.trough_index, 1)
        self.assertEqual(r.series_length, 2)
        self.assertEqual(r.drawdown_series, (0.0, 0.10))

    def test_drawdown_duration_strict_greater_than_zero(self):
        """Duration uses strict > 0.0; flat series after drawdown breaks run."""
        # peak at 100, drop to 90 (dd=0.1), flat at 90 (dd=0.1), then back to 100
        series = [100.0, 90.0, 90.0, 100.0]
        r = compute_drawdown(series)
        # drawdown series: [0.0, 0.1, 0.1, 0.0]
        # duration: run of dd > 0.0 = indices 1,2 = 2
        self.assertEqual(r.drawdown_duration, 2)
        self.assertTrue(math.isclose(r.max_drawdown, 0.10, rel_tol=1e-9))


if __name__ == "__main__":
    unittest.main()