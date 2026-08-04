"""Phase 5 M4-S1 — Allocation DTO targeted tests.

Covers construction, invariants, deterministic serialization round-trip,
edge cases (empty allocation, single position, multi-position, metadata).
Mirrors the M2 test pattern (``test_portfolio_domain.py``) and the M4
kickoff plan §9.6 deterministic-serialization test pattern.

FP tolerance: ``math.isclose(rel_tol=1e-9, abs_tol=1e-12)`` (pinned by
§5.3 of the M4 kickoff plan; documented per test).
"""
from __future__ import annotations

import math
import unittest

from phase3.portfolio.allocation import Allocation
from phase3.portfolio.domain import (
    EntityId,
    Position,
    PositionId,
    Weight,
)


def _make_position(
    pid: str = "pos-1",
    eid: str = "company:TW:2330",
    weight: float = 0.50,
) -> Position:
    """Build a minimal Position for tests."""
    return Position(
        position_id=PositionId(pid),
        entity_id=EntityId(eid),
        weight=Weight(weight),
        quantity=__import__("phase3.portfolio.domain", fromlist=["Quantity"]).Quantity(0),
    )


class TestAllocationConstruction(unittest.TestCase):
    """Allocation construction + invariants."""

    def test_empty_allocation_with_zero_target_total(self):
        """Empty allocation (positions=()) requires target_total=0.0."""
        alloc = Allocation(allocation_id="empty-1", positions=(), target_total=0.0)
        self.assertEqual(alloc.position_count, 0)
        self.assertEqual(alloc.total_weight, 0.0)
        self.assertEqual(alloc.target_total, 0.0)

    def test_single_position_allocation(self):
        """Single position with weight 1.0, target_total 1.0."""
        pos = _make_position(weight=1.0)
        alloc = Allocation(
            allocation_id="single-1",
            positions=(pos,),
            target_total=1.0,
        )
        self.assertEqual(alloc.position_count, 1)
        self.assertTrue(math.isclose(alloc.total_weight, 1.0, rel_tol=1e-9, abs_tol=1e-12))

    def test_multi_position_allocation_sums_to_target(self):
        """Two positions 0.6 + 0.4 sum to target_total 1.0."""
        pos_a = _make_position(pid="a", eid="company:TW:2330", weight=0.6)
        pos_b = _make_position(pid="b", eid="company:TW:2317", weight=0.4)
        alloc = Allocation(
            allocation_id="multi-1",
            positions=(pos_a, pos_b),
            target_total=1.0,
        )
        self.assertEqual(alloc.position_count, 2)
        self.assertTrue(math.isclose(alloc.total_weight, 1.0, rel_tol=1e-9, abs_tol=1e-12))

    def test_list_positions_defensively_converted_to_tuple(self):
        """A list of positions is normalized to a tuple."""
        pos_a = _make_position(pid="a", weight=0.5)
        pos_b = _make_position(pid="b", eid="company:TW:2317", weight=0.5)
        alloc = Allocation(
            allocation_id="list-1",
            positions=[pos_a, pos_b],  # list, not tuple
            target_total=1.0,
        )
        self.assertIsInstance(alloc.positions, tuple)
        self.assertEqual(alloc.position_count, 2)


class TestAllocationInvariants(unittest.TestCase):
    """Allocation invariants enforced at construction."""

    def test_invalid_allocation_id_whitespace_rejected(self):
        """allocation_id with whitespace is rejected."""
        with self.assertRaises(ValueError):
            Allocation(allocation_id="has space", positions=(), target_total=0.0)

    def test_invalid_allocation_id_empty_rejected(self):
        """Empty allocation_id is rejected."""
        with self.assertRaises(ValueError):
            Allocation(allocation_id="", positions=(), target_total=0.0)

    def test_target_total_out_of_range_rejected(self):
        """target_total > 1.0 is rejected."""
        with self.assertRaises(ValueError):
            Allocation(allocation_id="bad-target", positions=(), target_total=1.5)

    def test_target_total_negative_rejected(self):
        """Negative target_total is rejected."""
        with self.assertRaises(ValueError):
            Allocation(allocation_id="neg-target", positions=(), target_total=-0.1)

    def test_sum_mismatch_rejected(self):
        """Positions summing to 0.5 with target_total=1.0 is rejected."""
        pos = _make_position(weight=0.5)
        with self.assertRaises(ValueError):
            Allocation(
                allocation_id="mismatch-1",
                positions=(pos,),
                target_total=1.0,
            )

    def test_empty_allocation_nonzero_target_rejected(self):
        """Empty allocation with target_total=1.0 is rejected (0 != 1.0)."""
        with self.assertRaises(ValueError):
            Allocation(
                allocation_id="empty-bad",
                positions=(),
                target_total=1.0,
            )

    def test_duplicate_position_id_rejected(self):
        """Duplicate PositionId within an allocation is rejected."""
        pos_a = _make_position(pid="dup", weight=0.5)
        pos_b = _make_position(pid="dup", eid="company:TW:2317", weight=0.5)
        with self.assertRaises(ValueError):
            Allocation(
                allocation_id="dup-pid",
                positions=(pos_a, pos_b),
                target_total=1.0,
            )

    def test_non_position_in_positions_rejected(self):
        """A non-Position object in positions is rejected."""
        with self.assertRaises(ValueError):
            Allocation(
                allocation_id="bad-type",
                positions=("not-a-position",),  # type: ignore[arg-type]
                target_total=0.0,
            )


class TestAllocationSerialization(unittest.TestCase):
    """Allocation deterministic serialization round-trip (AG-M4-8)."""

    def test_round_trip_empty_allocation(self):
        """Round-trip: from_dict(to_dict(x)).to_dict() == to_dict(x). Empty allocation."""
        alloc = Allocation(allocation_id="rt-empty", positions=(), target_total=0.0, metadata={"k": "v"})
        d1 = alloc.to_dict()
        alloc2 = Allocation.from_dict(d1)
        d2 = alloc2.to_dict()
        self.assertEqual(d1, d2)

    def test_round_trip_single_position(self):
        """Round-trip: single-position allocation is byte-identical."""
        pos = _make_position(weight=1.0)
        alloc = Allocation(
            allocation_id="rt-single",
            positions=(pos,),
            target_total=1.0,
            metadata={"source": "test"},
        )
        d1 = alloc.to_dict()
        alloc2 = Allocation.from_dict(d1)
        d2 = alloc2.to_dict()
        self.assertEqual(d1, d2)

    def test_round_trip_multi_position_with_metadata(self):
        """Round-trip: multi-position allocation with metadata is byte-identical."""
        pos_a = _make_position(pid="a", eid="company:TW:2330", weight=0.6)
        pos_b = _make_position(pid="b", eid="company:TW:2317", weight=0.4)
        alloc = Allocation(
            allocation_id="rt-multi",
            positions=(pos_a, pos_b),
            target_total=1.0,
            metadata={"policy": "score-weighted", "version": "m4-s1"},
        )
        d1 = alloc.to_dict()
        alloc2 = Allocation.from_dict(d1)
        d2 = alloc2.to_dict()
        self.assertEqual(d1, d2)

    def test_to_dict_keys_fixed_order(self):
        """to_dict() keys are emitted in a fixed, code-defined order."""
        pos = _make_position(weight=1.0)
        alloc = Allocation(
            allocation_id="order-1",
            positions=(pos,),
            target_total=1.0,
        )
        d = alloc.to_dict()
        self.assertEqual(
            list(d.keys()),
            ["allocation_id", "positions", "target_total", "metadata"],
        )

    def test_from_dict_accepts_list_positions(self):
        """from_dict accepts positions as a list (defensive normalization)."""
        pos = _make_position(weight=1.0)
        alloc = Allocation(
            allocation_id="list-in",
            positions=(pos,),
            target_total=1.0,
        )
        d = alloc.to_dict()
        # Tamper: positions as a list.
        d_list = dict(d)
        d_list["positions"] = list(d["positions"])
        alloc2 = Allocation.from_dict(d_list)
        self.assertIsInstance(alloc2.positions, tuple)
        self.assertEqual(alloc2.to_dict(), alloc.to_dict())


if __name__ == "__main__":
    unittest.main()