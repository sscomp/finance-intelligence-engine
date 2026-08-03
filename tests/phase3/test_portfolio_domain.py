"""M2 — Portfolio Domain Model targeted tests.

Coverage (per phase5_kickoff_plan.md §12.4):
- DTO construction + invariant enforcement
- ``to_dict()`` round-trip (serialization + ``from_dict()`` deserialization)
- JSON serializability (json.dumps on to_dict output)
- Deterministic representation
- Boundary discipline (no macro_history.db / no phase3.pipeline / etc.)

NOT covered here (separate files, later milestones):
- ``test_portfolio_safety_guards.py`` — production-safety tripwires
- ``Allocation`` / ``PortfolioDecision`` — deferred per work order scope
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from phase3.portfolio import (
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
from phase3.portfolio.domain import portfolio_repr


# ---------------------------------------------------------------------------
# Value-object construction + invariant enforcement
# ---------------------------------------------------------------------------


class TestEntityIdConstruction(unittest.TestCase):
    def test_accepts_canonical_entity_ids(self):
        for v in (
            "company:TW:2330",
            "industry:TW:semiconductor",
            "macro:global:fed_rate",
            "a",
            "x" * 128,
        ):
            with self.subTest(value=v):
                eid = EntityId(value=v)
                self.assertEqual(eid.value, v)

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            EntityId(value="")

    def test_rejects_whitespace(self):
        for v in (" has_leading", "has_trailing ", "has space", "\ttab"):
            with self.subTest(value=v):
                with self.assertRaises(ValueError):
                    EntityId(value=v)

    def test_rejects_too_long(self):
        with self.assertRaises(ValueError):
            EntityId(value="x" * 129)


class TestPortfolioIdConstruction(unittest.TestCase):
    def test_accepts_canonical(self):
        pid = PortfolioId(value="portfolio:default:v1")
        self.assertEqual(pid.value, "portfolio:default:v1")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            PortfolioId(value="")


class TestPositionIdConstruction(unittest.TestCase):
    def test_accepts_canonical(self):
        posid = PositionId(value="pos:2330:001")
        self.assertEqual(posid.value, "pos:2330:001")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            PositionId(value="")


class TestWeightInvariant(unittest.TestCase):
    def test_accepts_zero(self):
        self.assertEqual(Weight(value=0.0).value, 0.0)

    def test_accepts_one(self):
        self.assertEqual(Weight(value=1.0).value, 1.0)

    def test_accepts_midrange(self):
        self.assertEqual(Weight(value=0.05).value, 0.05)

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            Weight(value=-0.01)

    def test_rejects_above_one(self):
        with self.assertRaises(ValueError):
            Weight(value=1.01)

    def test_rejects_nan(self):
        import math
        with self.assertRaises(ValueError):
            Weight(value=float("nan"))


class TestQuantityInvariant(unittest.TestCase):
    def test_accepts_zero(self):
        self.assertEqual(Quantity(value=0).value, 0)

    def test_accepts_positive(self):
        self.assertEqual(Quantity(value=1000).value, 1000)

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            Quantity(value=-1)


class TestPriceInvariant(unittest.TestCase):
    def test_accepts_zero_with_valid_currency(self):
        p = Price(value=0.0, currency="TWD")
        self.assertEqual(p.value, 0.0)
        self.assertEqual(p.currency, "TWD")

    def test_accepts_positive(self):
        p = Price(value=525.0, currency="TWD")
        self.assertEqual(p.value, 525.0)

    def test_rejects_negative_value(self):
        with self.assertRaises(ValueError):
            Price(value=-1.0, currency="TWD")

    def test_rejects_invalid_currency_lowercase(self):
        with self.assertRaises(ValueError):
            Price(value=100.0, currency="twd")

    def test_rejects_invalid_currency_too_short(self):
        with self.assertRaises(ValueError):
            Price(value=100.0, currency="TW")

    def test_rejects_invalid_currency_too_long(self):
        with self.assertRaises(ValueError):
            Price(value=100.0, currency="TWDD")

    def test_rejects_invalid_currency_with_digits(self):
        with self.assertRaises(ValueError):
            Price(value=100.0, currency="TW1")


class TestCostBasisInvariant(unittest.TestCase):
    def test_accepts_zero(self):
        cb = CostBasis(value=0.0, currency="USD")
        self.assertEqual(cb.value, 0.0)

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            CostBasis(value=-1.0, currency="USD")

    def test_rejects_invalid_currency(self):
        with self.assertRaises(ValueError):
            CostBasis(value=100.0, currency="usd")


# ---------------------------------------------------------------------------
# Position construction + cross-value-object invariant
# ---------------------------------------------------------------------------


def _make_position(
    position_id: str = "pos:001",
    entity_id: str = "company:TW:2330",
    weight: float = 0.1,
    quantity: int = 100,
    price: tuple[float, str] | None = (525.0, "TWD"),
    cost_basis: tuple[float, str] | None = (480.0, "TWD"),
    metadata: dict[str, Any] | None = None,
) -> Position:
    return Position(
        position_id=PositionId(value=position_id),
        entity_id=EntityId(value=entity_id),
        weight=Weight(value=weight),
        quantity=Quantity(value=quantity),
        price=Price(value=price[0], currency=price[1]) if price is not None else None,
        cost_basis=(
            CostBasis(value=cost_basis[0], currency=cost_basis[1])
            if cost_basis is not None
            else None
        ),
        metadata=metadata or {},
    )


class TestPositionConstruction(unittest.TestCase):
    def test_canonical_construction(self):
        pos = _make_position()
        self.assertEqual(pos.position_id.value, "pos:001")
        self.assertEqual(pos.entity_id.value, "company:TW:2330")
        self.assertEqual(pos.weight.value, 0.1)
        self.assertEqual(pos.quantity.value, 100)
        self.assertEqual(pos.price.value, 525.0)
        self.assertEqual(pos.price.currency, "TWD")
        self.assertEqual(pos.cost_basis.value, 480.0)

    def test_optional_price_none(self):
        pos = _make_position(price=None)
        self.assertIsNone(pos.price)
        self.assertIsNotNone(pos.cost_basis)

    def test_optional_cost_basis_none(self):
        pos = _make_position(cost_basis=None)
        self.assertIsNotNone(pos.price)
        self.assertIsNone(pos.cost_basis)

    def test_both_none_allowed(self):
        pos = _make_position(price=None, cost_basis=None)
        self.assertIsNone(pos.price)
        self.assertIsNone(pos.cost_basis)

    def test_currency_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            _make_position(price=(525.0, "TWD"), cost_basis=(480.0, "USD"))

    def test_currency_match_accepted(self):
        pos = _make_position(price=(525.0, "USD"), cost_basis=(480.0, "USD"))
        self.assertEqual(pos.price.currency, "USD")

    def test_position_is_frozen(self):
        pos = _make_position()
        with self.assertRaises(Exception):
            pos.weight = Weight(value=0.2)  # type: ignore[misc]

    def test_metadata_default_empty(self):
        pos = _make_position(metadata=None)
        self.assertEqual(pos.metadata, {})


# ---------------------------------------------------------------------------
# Position serialization round-trip
# ---------------------------------------------------------------------------


class TestPositionToDict(unittest.TestCase):
    def test_to_dict_keys_and_shape(self):
        pos = _make_position(metadata={"note": "test"})
        d = pos.to_dict()
        self.assertEqual(
            set(d.keys()),
            {
                "position_id",
                "entity_id",
                "weight",
                "quantity",
                "price",
                "cost_basis",
                "metadata",
            },
        )
        self.assertEqual(d["position_id"], {"value": "pos:001"})
        self.assertEqual(d["entity_id"], {"value": "company:TW:2330"})
        self.assertEqual(d["weight"], {"value": 0.1})
        self.assertEqual(d["quantity"], {"value": 100})
        self.assertEqual(d["price"], {"value": 525.0, "currency": "TWD"})
        self.assertEqual(d["cost_basis"], {"value": 480.0, "currency": "TWD"})
        self.assertEqual(d["metadata"], {"note": "test"})

    def test_to_dict_price_none(self):
        pos = _make_position(price=None)
        d = pos.to_dict()
        self.assertIsNone(d["price"])

    def test_to_dict_cost_basis_none(self):
        pos = _make_position(cost_basis=None)
        d = pos.to_dict()
        self.assertIsNone(d["cost_basis"])

    def test_to_dict_json_serializable(self):
        pos = _make_position(metadata={"k": "v"})
        json.dumps(pos.to_dict())

    def test_from_dict_round_trip(self):
        pos = _make_position()
        d = pos.to_dict()
        pos2 = Position.from_dict(d)
        self.assertEqual(pos2.to_dict(), d)

    def test_from_dict_round_trip_with_none_price_cost(self):
        pos = _make_position(price=None, cost_basis=None)
        d = pos.to_dict()
        pos2 = Position.from_dict(d)
        self.assertEqual(pos2.to_dict(), d)

    def test_to_dict_does_not_share_mutable_state(self):
        metadata = {"k": "v"}
        pos = _make_position(metadata=metadata)
        d = pos.to_dict()
        d["metadata"]["k"] = "MUTATED"
        # Original Position metadata must be untouched.
        self.assertEqual(pos.metadata, {"k": "v"})


# ---------------------------------------------------------------------------
# Portfolio construction + invariants
# ---------------------------------------------------------------------------


class TestPortfolioConstruction(unittest.TestCase):
    def test_canonical_construction(self):
        pos = _make_position()
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:default:v1"),
            name="Default Portfolio",
            positions=(pos,),
        )
        self.assertEqual(portfolio.portfolio_id.value, "pf:default:v1")
        self.assertEqual(portfolio.name, "Default Portfolio")
        self.assertEqual(portfolio.position_count, 1)
        self.assertEqual(len(portfolio.positions), 1)

    def test_empty_portfolio_allowed(self):
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:empty"),
            name="Empty",
            positions=(),
        )
        self.assertEqual(portfolio.position_count, 0)
        self.assertEqual(portfolio.allocation_total, 0.0)

    def test_list_input_converted_to_tuple(self):
        pos = _make_position()
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="P",
            positions=[pos],  # list input
        )
        self.assertIsInstance(portfolio.positions, tuple)
        self.assertEqual(portfolio.position_count, 1)

    def test_name_stripped(self):
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="  Spaced  ",
            positions=(),
        )
        self.assertEqual(portfolio.name, "Spaced")

    def test_name_empty_after_strip_rejected(self):
        with self.assertRaises(ValueError):
            Portfolio(
                portfolio_id=PortfolioId(value="pf:1"),
                name="   ",
                positions=(),
            )

    def test_name_too_long_rejected(self):
        with self.assertRaises(ValueError):
            Portfolio(
                portfolio_id=PortfolioId(value="pf:1"),
                name="x" * 257,
                positions=(),
            )

    def test_name_must_be_str(self):
        with self.assertRaises(ValueError):
            Portfolio(
                portfolio_id=PortfolioId(value="pf:1"),
                name=123,  # type: ignore[arg-type]
                positions=(),
            )

    def test_duplicate_position_id_rejected(self):
        pos1 = _make_position(position_id="pos:001", entity_id="company:TW:2330")
        pos2 = _make_position(position_id="pos:001", entity_id="company:TW:2317")
        with self.assertRaises(ValueError):
            Portfolio(
                portfolio_id=PortfolioId(value="pf:1"),
                name="P",
                positions=(pos1, pos2),
            )

    def test_duplicate_entity_id_rejected(self):
        pos1 = _make_position(position_id="pos:001", entity_id="company:TW:2330")
        pos2 = _make_position(position_id="pos:002", entity_id="company:TW:2330")
        with self.assertRaises(ValueError):
            Portfolio(
                portfolio_id=PortfolioId(value="pf:1"),
                name="P",
                positions=(pos1, pos2),
            )

    def test_non_position_element_rejected(self):
        with self.assertRaises(ValueError):
            Portfolio(
                portfolio_id=PortfolioId(value="pf:1"),
                name="P",
                positions=("not a position",),  # type: ignore[arg-type]
            )

    def test_portfolio_is_frozen(self):
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="P",
            positions=(),
        )
        with self.assertRaises(Exception):
            portfolio.name = "Other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Portfolio derived properties
# ---------------------------------------------------------------------------


class TestPortfolioDerivedProperties(unittest.TestCase):
    def test_allocation_total_zero_for_empty(self):
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="P",
            positions=(),
        )
        self.assertEqual(portfolio.allocation_total, 0.0)

    def test_allocation_total_sum_of_weights(self):
        p1 = _make_position(position_id="pos:1", entity_id="e:1", weight=0.3)
        p2 = _make_position(position_id="pos:2", entity_id="e:2", weight=0.3)
        p3 = _make_position(position_id="pos:3", entity_id="e:3", weight=0.4)
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="P",
            positions=(p1, p2, p3),
        )
        self.assertAlmostEqual(portfolio.allocation_total, 1.0, places=9)

    def test_allocation_total_below_one(self):
        p1 = _make_position(position_id="pos:1", entity_id="e:1", weight=0.3)
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="P",
            positions=(p1,),
        )
        self.assertAlmostEqual(portfolio.allocation_total, 0.3, places=9)

    def test_position_count(self):
        p1 = _make_position(position_id="pos:1", entity_id="e:1")
        p2 = _make_position(position_id="pos:2", entity_id="e:2")
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="P",
            positions=(p1, p2),
        )
        self.assertEqual(portfolio.position_count, 2)


# ---------------------------------------------------------------------------
# Portfolio serialization round-trip
# ---------------------------------------------------------------------------


class TestPortfolioToDict(unittest.TestCase):
    def test_empty_portfolio_to_dict(self):
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:empty"),
            name="Empty",
            positions=(),
        )
        d = portfolio.to_dict()
        self.assertEqual(set(d.keys()), {"portfolio_id", "name", "positions"})
        self.assertEqual(d["portfolio_id"], {"value": "pf:empty"})
        self.assertEqual(d["name"], "Empty")
        self.assertEqual(d["positions"], ())

    def test_portfolio_with_positions_to_dict(self):
        p1 = _make_position(position_id="pos:1", entity_id="e:1", weight=0.5)
        p2 = _make_position(position_id="pos:2", entity_id="e:2", weight=0.5)
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="Two",
            positions=(p1, p2),
        )
        d = portfolio.to_dict()
        self.assertEqual(d["portfolio_id"], {"value": "pf:1"})
        self.assertEqual(d["name"], "Two")
        self.assertEqual(len(d["positions"]), 2)
        self.assertEqual(d["positions"][0]["position_id"], {"value": "pos:1"})

    def test_to_dict_json_serializable(self):
        p1 = _make_position(
            position_id="pos:1",
            entity_id="e:1",
            metadata={"k": "v", "n": 42},
        )
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="P",
            positions=(p1,),
        )
        # json.dumps must succeed on the to_dict output. Note: positions is a
        # tuple in to_dict — json.dumps serializes tuples as JSON arrays.
        s = json.dumps(portfolio.to_dict())
        self.assertIn('"portfolio_id"', s)
        self.assertIn('"positions"', s)

    def test_from_dict_round_trip(self):
        p1 = _make_position(position_id="pos:1", entity_id="e:1", weight=0.5)
        p2 = _make_position(position_id="pos:2", entity_id="e:2", weight=0.5)
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="Two",
            positions=(p1, p2),
        )
        d = portfolio.to_dict()
        portfolio2 = Portfolio.from_dict(d)
        self.assertEqual(portfolio2.to_dict(), d)

    def test_from_dict_empty_portfolio(self):
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:empty"),
            name="Empty",
            positions=(),
        )
        d = portfolio.to_dict()
        portfolio2 = Portfolio.from_dict(d)
        self.assertEqual(portfolio2.to_dict(), d)
        self.assertEqual(portfolio2.position_count, 0)


# ---------------------------------------------------------------------------
# Deterministic representation
# ---------------------------------------------------------------------------


class TestPortfolioRepr(unittest.TestCase):
    def test_repr_is_deterministic(self):
        p1 = _make_position(position_id="pos:1", entity_id="e:1", weight=0.5)
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="P",
            positions=(p1,),
        )
        r1 = portfolio_repr(portfolio)
        r2 = portfolio_repr(portfolio)
        self.assertEqual(r1, r2)

    def test_repr_includes_key_fields(self):
        p1 = _make_position(position_id="pos:1", entity_id="e:1", weight=0.5)
        portfolio = Portfolio(
            portfolio_id=PortfolioId(value="pf:1"),
            name="P",
            positions=(p1,),
        )
        r = portfolio_repr(portfolio)
        self.assertIn("Portfolio(", r)
        self.assertIn("pf:1", r)
        self.assertIn("name='P'", r)
        self.assertIn("positions=1", r)
        self.assertIn("pos:1", r)
        self.assertIn("e:1", r)


# ---------------------------------------------------------------------------
# Boundary discipline: this module imports no forbidden surfaces
# ---------------------------------------------------------------------------


class TestNoForbiddenImports(unittest.TestCase):
    """Statically verify that ``phase3.portfolio.domain`` does not import
    any forbidden surfaces. This is a cheap module-level guard; the full
    production-safety tripwires live in ``test_portfolio_safety_guards.py``.
    """

    def test_domain_module_has_no_sqlite_import(self):
        # Verify that the module does not import sqlite3 or open the
        # macro_history database. We check the module's own globals (not the
        # raw source text) to avoid false positives from docstrings that
        # mention the forbidden surface as a boundary contract.
        import phase3.portfolio.domain as dom
        self.assertNotIn("sqlite3", dir(dom))
        self.assertNotIn("macro_history", dir(dom))

    def test_domain_module_has_no_db_connection_objects(self):
        # Defense in depth: confirm no sqlite3.Connection or cursor leaked
        # into the module namespace at import time.
        import phase3.portfolio.domain as dom
        for name in dir(dom):
            obj = getattr(dom, name)
            self.assertNotEqual(type(obj).__name__, "Connection")

    def test_domain_module_has_no_phase3_pipeline_import(self):
        import phase3.portfolio.domain as dom
        import inspect
        src = inspect.getsource(dom)
        self.assertNotIn("from phase3.pipeline", src)
        self.assertNotIn("import phase3.pipeline", src)


if __name__ == "__main__":
    unittest.main()