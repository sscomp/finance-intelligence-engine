"""Phase 5 M5 — Execution Planning: targeted tests + safety guards (TD9).

Tests for ``phase3.portfolio.execution``:

- SuggestedOrder construction, invariants, serialization round-trip
- ExecutionQueue construction, invariants, serialization round-trip
- ReviewQueue construction, invariants, serialization round-trip
- ExecutionPlanConfig construction, invariants, serialization round-trip
- ExecutionPlanResult construction, invariants, serialization round-trip
- plan_execution algorithm: delta computation, action assignment, priority
- plan_execution partitioning: execution vs review queue
- plan_execution edge cases: empty portfolio, empty allocation, all-hold

Safety guards (TD9, AG-M5-1 through AG-M5-8):
- AG-M5-1: AST scan — 0 forbidden imports in execution.py
- AG-M5-2: AST scan — 0 IntelligencePipeline() instantiations
- AG-M5-3: AST scan — 0 requests.post / broker / place_order / network calls
- AG-M5-4: File-level sha256 — 0 modifications to files outside phase3.portfolio.*
- AG-M5-5: macro_history.db before/after unchanged
- AG-M5-6: intelligence.db* count = 0
- AG-M5-7: jobs.json unchanged
- AG-M5-8: M2 + M3 + M4 regression (via separate test files, verified by suite)
"""
from __future__ import annotations

import ast
import hashlib
import math
import os
import unittest
from dataclasses import FrozenInstanceError

# We import from phase3.portfolio.execution AFTER the stdlib imports.
from phase3.portfolio.domain import (
    EntityId,
    Portfolio,
    PortfolioId,
    Position,
    PositionId,
    Quantity,
    Weight,
)
from phase3.portfolio.allocation import Allocation
from phase3.portfolio.decision import PortfolioDecision
from phase3.portfolio.execution import (
    ExecutionPlanConfig,
    ExecutionQueue,
    ExecutionPlanResult,
    ReviewQueue,
    SuggestedOrder,
    plan_execution,
)


# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #

_EXECUTION_MODULE_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..",
        "phase3", "portfolio", "execution.py",
    )
)

MACRO_DB_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..",
        "macro_history.db",
    )
)

JOBS_JSON_PATH = os.path.expanduser("~/.hermes/cron/jobs.json")


def _make_entity_id(value: str = "company:TW:2330") -> EntityId:
    return EntityId(value)


def _make_position(
    entity_id: str = "company:TW:2330",
    weight: float = 0.10,
    quantity: int = 1000,
    position_id: str | None = None,
) -> Position:
    return Position(
        position_id=PositionId(position_id or f"pos-{entity_id}"),
        entity_id=EntityId(entity_id),
        weight=Weight(weight),
        quantity=Quantity(quantity),
    )


def _make_portfolio(
    positions: list[tuple[str, float, int]] | None = None,
    portfolio_id: str = "portfolio-001",
) -> Portfolio:
    """Build a Portfolio from (entity_id, weight, quantity) tuples."""
    if positions is None:
        positions = [
            ("company:TW:2330", 0.30, 1000),
            ("company:TW:2317", 0.20, 2000),
            ("company:TW:2454", 0.15, 500),
        ]
    pos_list = []
    for eid, w, q in positions:
        pos_list.append(_make_position(eid, w, q))
    return Portfolio(
        portfolio_id=PortfolioId(portfolio_id),
        name="Test Portfolio",
        positions=tuple(pos_list),
    )


def _make_allocation(
    positions: list[tuple[str, float]] | None = None,
    allocation_id: str = "alloc-001",
    target_total: float = 1.0,
) -> Allocation:
    """Build an Allocation from (entity_id, weight) tuples."""
    if positions is None:
        positions = [
            ("company:TW:2330", 0.40),
            ("company:TW:2317", 0.30),
            ("company:TW:2454", 0.30),
        ]
    pos_list = []
    for eid, w in positions:
        pos_list.append(Position(
            position_id=PositionId(f"alloc-pos-{eid}"),
            entity_id=EntityId(eid),
            weight=Weight(w),
            quantity=Quantity(0),
        ))
    return Allocation(
        allocation_id=allocation_id,
        positions=tuple(pos_list),
        target_total=target_total,
    )


def _make_decision(
    allocation_positions: list[tuple[str, float]] | None = None,
    decision_id: str = "decision-001",
    portfolio_id: str = "portfolio-001",
    generated_at: str = "2026-08-08T12:00:00Z",
) -> PortfolioDecision:
    if allocation_positions is None:
        allocation_positions = [
            ("company:TW:2330", 0.50),
            ("company:TW:2317", 0.30),
            ("company:TW:2454", 0.20),
        ]
    target_total = sum(w for _, w in allocation_positions) if allocation_positions else 0.0
    alloc = _make_allocation(allocation_positions, allocation_id=f"{decision_id}-alloc", target_total=target_total)
    return PortfolioDecision(
        decision_id=decision_id,
        portfolio_id=portfolio_id,
        allocation=alloc,
        rationale="Test decision for M5 execution planning",
        evidence_refs=("sig-001", "sig-002"),
        generated_at=generated_at,
        risk_summary={},
    )


# --------------------------------------------------------------------------- #
# SuggestedOrder tests
# --------------------------------------------------------------------------- #


class TestSuggestedOrderConstruction(unittest.TestCase):
    """SuggestedOrder construction and invariant enforcement."""

    def test_minimal_valid_construction(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="buy",
            target_weight=0.10,
            current_weight=0.05,
            delta_weight=0.05,
        )
        self.assertEqual(order.order_id, "order-0001")
        self.assertEqual(order.action, "buy")
        self.assertEqual(order.priority, 3)  # default
        self.assertEqual(order.quantity_estimate, 0)  # default
        self.assertEqual(order.metadata, {})  # default

    def test_full_valid_construction(self):
        order = SuggestedOrder(
            order_id="order-0002",
            entity_id=_make_entity_id("company:TW:2317"),
            action="sell",
            target_weight=0.00,
            current_weight=0.15,
            delta_weight=-0.15,
            priority=4,
            quantity_estimate=500,
            metadata={"reason": "rebalance"},
        )
        self.assertEqual(order.action, "sell")
        self.assertEqual(order.priority, 4)
        self.assertEqual(order.quantity_estimate, 500)
        self.assertEqual(order.metadata, {"reason": "rebalance"})

    def test_hold_action(self):
        order = SuggestedOrder(
            order_id="order-0003",
            entity_id=_make_entity_id(),
            action="hold",
            target_weight=0.10,
            current_weight=0.10,
            delta_weight=0.0,
        )
        self.assertEqual(order.action, "hold")

    def test_frozen_dataclass(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="buy",
            target_weight=0.10,
            current_weight=0.05,
            delta_weight=0.05,
        )
        with self.assertRaises(FrozenInstanceError):
            order.action = "sell"

    def test_invalid_action_rejected(self):
        with self.assertRaisesRegex(ValueError, "action must be one of"):
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="execute",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
            )

    def test_invalid_order_id_rejected(self):
        with self.assertRaisesRegex(ValueError, "order_id must be non-empty"):
            SuggestedOrder(
                order_id="",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
            )

    def test_invalid_order_id_whitespace_rejected(self):
        with self.assertRaisesRegex(ValueError, "order_id must be non-empty"):
            SuggestedOrder(
                order_id="order 0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
            )

    def test_entity_id_must_be_entityid_instance(self):
        with self.assertRaisesRegex(ValueError, "entity_id must be an EntityId"):
            SuggestedOrder(
                order_id="order-0001",
                entity_id="company:TW:2330",  # raw string, not EntityId
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
            )

    def test_target_weight_out_of_range_rejected(self):
        with self.assertRaisesRegex(ValueError, "target_weight must be in"):
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=1.5,
                current_weight=0.05,
                delta_weight=0.05,
            )

    def test_current_weight_negative_rejected(self):
        with self.assertRaisesRegex(ValueError, "current_weight must be in"):
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=-0.05,
                delta_weight=0.15,
            )

    def test_delta_weight_out_of_range_rejected(self):
        with self.assertRaisesRegex(ValueError, "delta_weight must be in"):
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=1.5,
            )

    def test_invalid_priority_rejected(self):
        with self.assertRaisesRegex(ValueError, "priority must be in"):
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
                priority=6,
            )

    def test_negative_quantity_rejected(self):
        with self.assertRaisesRegex(ValueError, "quantity_estimate must be >= 0"):
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
                quantity_estimate=-1,
            )

    def test_metadata_must_be_dict(self):
        with self.assertRaisesRegex(ValueError, "metadata must be a dict"):
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
                metadata="not a dict",
            )


class TestSuggestedOrderSerialization(unittest.TestCase):
    """SuggestedOrder to_dict / from_dict round-trip."""

    def test_round_trip_basic(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id("company:TW:2330"),
            action="buy",
            target_weight=0.10,
            current_weight=0.05,
            delta_weight=0.05,
            priority=3,
            quantity_estimate=100,
            metadata={"sector": "semiconductor"},
        )
        d = order.to_dict()
        restored = SuggestedOrder.from_dict(d)
        self.assertEqual(restored.to_dict(), d)

    def test_round_trip_with_defaults(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="hold",
            target_weight=0.10,
            current_weight=0.10,
            delta_weight=0.0,
        )
        d = order.to_dict()
        restored = SuggestedOrder.from_dict(d)
        self.assertEqual(restored.to_dict(), d)

    def test_to_dict_key_order(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="buy",
            target_weight=0.10,
            current_weight=0.05,
            delta_weight=0.05,
        )
        d = order.to_dict()
        expected_keys = [
            "order_id", "entity_id", "action", "target_weight",
            "current_weight", "delta_weight", "priority",
            "quantity_estimate", "metadata",
        ]
        self.assertEqual(list(d.keys()), expected_keys)

    def test_is_review_candidate_hold(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="hold",
            target_weight=0.10,
            current_weight=0.10,
            delta_weight=0.0,
        )
        self.assertFalse(order.is_review_candidate)

    def test_is_review_candidate_large_delta(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="buy",
            target_weight=0.20,
            current_weight=0.05,
            delta_weight=0.15,
        )
        self.assertTrue(order.is_review_candidate)

    def test_is_review_candidate_new_position(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="buy",
            target_weight=0.10,
            current_weight=0.0,
            delta_weight=0.10,
        )
        self.assertTrue(order.is_review_candidate)

    def test_is_review_candidate_full_exit(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="sell",
            target_weight=0.0,
            current_weight=0.15,
            delta_weight=-0.15,
        )
        self.assertTrue(order.is_review_candidate)


# --------------------------------------------------------------------------- #
# ExecutionQueue tests
# --------------------------------------------------------------------------- #


class TestExecutionQueueConstruction(unittest.TestCase):
    """ExecutionQueue construction and invariant enforcement."""

    def test_minimal_valid_construction(self):
        q = ExecutionQueue(
            queue_id="exec-001",
            portfolio_id="portfolio-001",
        )
        self.assertEqual(q.queue_id, "exec-001")
        self.assertEqual(q.order_count, 0)
        self.assertEqual(q.orders, ())

    def test_with_orders(self):
        orders = (
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
            ),
            SuggestedOrder(
                order_id="order-0002",
                entity_id=_make_entity_id("company:TW:2317"),
                action="sell",
                target_weight=0.05,
                current_weight=0.10,
                delta_weight=-0.05,
            ),
        )
        q = ExecutionQueue(
            queue_id="exec-001",
            portfolio_id="portfolio-001",
            orders=orders,
        )
        self.assertEqual(q.order_count, 2)
        self.assertEqual(q.buy_count, 1)
        self.assertEqual(q.sell_count, 1)
        self.assertEqual(q.hold_count, 0)

    def test_list_defensively_converted_to_tuple(self):
        orders = [
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
            ),
        ]
        q = ExecutionQueue(
            queue_id="exec-001",
            portfolio_id="portfolio-001",
            orders=orders,  # list, not tuple
        )
        self.assertIsInstance(q.orders, tuple)

    def test_invalid_queue_id_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionQueue(queue_id="", portfolio_id="portfolio-001")

    def test_invalid_portfolio_id_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionQueue(queue_id="exec-001", portfolio_id="")

    def test_non_suggested_order_rejected(self):
        with self.assertRaisesRegex(ValueError, "must contain SuggestedOrder"):
            ExecutionQueue(
                queue_id="exec-001",
                portfolio_id="portfolio-001",
                orders=("not an order",),
            )

    def test_frozen(self):
        q = ExecutionQueue(queue_id="exec-001", portfolio_id="portfolio-001")
        with self.assertRaises(FrozenInstanceError):
            q.queue_id = "exec-002"


class TestExecutionQueueSerialization(unittest.TestCase):

    def test_round_trip(self):
        orders = (
            SuggestedOrder(
                order_id="order-0001",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
            ),
        )
        q = ExecutionQueue(
            queue_id="exec-001",
            portfolio_id="portfolio-001",
            orders=orders,
            generated_at="2026-08-08T12:00:00Z",
            metadata={"source": "test"},
        )
        d = q.to_dict()
        restored = ExecutionQueue.from_dict(d)
        self.assertEqual(restored.to_dict(), d)

    def test_empty_round_trip(self):
        q = ExecutionQueue(
            queue_id="exec-001",
            portfolio_id="portfolio-001",
        )
        d = q.to_dict()
        restored = ExecutionQueue.from_dict(d)
        self.assertEqual(restored.to_dict(), d)


# --------------------------------------------------------------------------- #
# ReviewQueue tests
# --------------------------------------------------------------------------- #


class TestReviewQueueConstruction(unittest.TestCase):

    def test_minimal_valid_construction(self):
        q = ReviewQueue(
            queue_id="review-001",
            portfolio_id="portfolio-001",
        )
        self.assertEqual(q.queue_id, "review-001")
        self.assertEqual(q.order_count, 0)
        self.assertEqual(q.review_reasons, {})

    def test_with_orders_and_reasons(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="buy",
            target_weight=0.20,
            current_weight=0.0,
            delta_weight=0.20,
        )
        q = ReviewQueue(
            queue_id="review-001",
            portfolio_id="portfolio-001",
            orders=(order,),
            review_reasons={"order-0001": ["new position", "large delta"]},
        )
        self.assertEqual(q.order_count, 1)
        self.assertEqual(q.review_reasons["order-0001"], ["new position", "large delta"])

    def test_invalid_queue_id_rejected(self):
        with self.assertRaises(ValueError):
            ReviewQueue(queue_id="", portfolio_id="portfolio-001")

    def test_frozen(self):
        q = ReviewQueue(queue_id="review-001", portfolio_id="portfolio-001")
        with self.assertRaises(FrozenInstanceError):
            q.queue_id = "review-002"


class TestReviewQueueSerialization(unittest.TestCase):

    def test_round_trip(self):
        order = SuggestedOrder(
            order_id="order-0001",
            entity_id=_make_entity_id(),
            action="buy",
            target_weight=0.20,
            current_weight=0.0,
            delta_weight=0.20,
        )
        q = ReviewQueue(
            queue_id="review-001",
            portfolio_id="portfolio-001",
            orders=(order,),
            review_reasons={"order-0001": ["new position"]},
            generated_at="2026-08-08T12:00:00Z",
        )
        d = q.to_dict()
        restored = ReviewQueue.from_dict(d)
        self.assertEqual(restored.to_dict(), d)

    def test_empty_round_trip(self):
        q = ReviewQueue(
            queue_id="review-001",
            portfolio_id="portfolio-001",
        )
        d = q.to_dict()
        restored = ReviewQueue.from_dict(d)
        self.assertEqual(restored.to_dict(), d)


# --------------------------------------------------------------------------- #
# ExecutionPlanConfig tests
# --------------------------------------------------------------------------- #


class TestExecutionPlanConfig(unittest.TestCase):

    def test_defaults(self):
        c = ExecutionPlanConfig()
        self.assertAlmostEqual(c.review_delta_threshold, 0.05)
        self.assertTrue(c.review_new_positions)
        self.assertTrue(c.review_full_exits)
        self.assertAlmostEqual(c.max_order_weight, 0.25)
        self.assertEqual(c.generated_at, "")
        self.assertEqual(c.queue_id, "")

    def test_custom_values(self):
        c = ExecutionPlanConfig(
            review_delta_threshold=0.10,
            review_new_positions=False,
            review_full_exits=False,
            max_order_weight=0.30,
            generated_at="2026-08-08T12:00:00Z",
            queue_id="custom-queue",
        )
        self.assertAlmostEqual(c.review_delta_threshold, 0.10)
        self.assertFalse(c.review_new_positions)
        self.assertFalse(c.review_full_exits)
        self.assertAlmostEqual(c.max_order_weight, 0.30)

    def test_invalid_threshold_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionPlanConfig(review_delta_threshold=1.5)

    def test_invalid_threshold_negative_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionPlanConfig(review_delta_threshold=-0.1)

    def test_invalid_max_order_weight_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionPlanConfig(max_order_weight=2.0)

    def test_invalid_queue_id_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionPlanConfig(queue_id="has space")

    def test_frozen(self):
        c = ExecutionPlanConfig()
        with self.assertRaises(FrozenInstanceError):
            c.review_delta_threshold = 0.10

    def test_round_trip(self):
        c = ExecutionPlanConfig(
            review_delta_threshold=0.08,
            review_new_positions=True,
            review_full_exits=False,
            max_order_weight=0.20,
            generated_at="2026-08-08T12:00:00Z",
            queue_id="test-q",
        )
        d = c.to_dict()
        restored = ExecutionPlanConfig.from_dict(d)
        self.assertEqual(restored.to_dict(), d)


# --------------------------------------------------------------------------- #
# ExecutionPlanResult tests
# --------------------------------------------------------------------------- #


class TestExecutionPlanResult(unittest.TestCase):

    def test_construction(self):
        eq = ExecutionQueue(queue_id="q-exec", portfolio_id="p1")
        rq = ReviewQueue(queue_id="q-review", portfolio_id="p1")
        result = ExecutionPlanResult(
            plan_id="plan-001",
            execution_queue=eq,
            review_queue=rq,
        )
        self.assertEqual(result.plan_id, "plan-001")
        self.assertEqual(result.total_order_count, 0)

    def test_invalid_plan_id_rejected(self):
        eq = ExecutionQueue(queue_id="q-exec", portfolio_id="p1")
        rq = ReviewQueue(queue_id="q-review", portfolio_id="p1")
        with self.assertRaises(ValueError):
            ExecutionPlanResult(plan_id="", execution_queue=eq, review_queue=rq)

    def test_invalid_exec_queue_type(self):
        rq = ReviewQueue(queue_id="q-review", portfolio_id="p1")
        with self.assertRaises(ValueError):
            ExecutionPlanResult(plan_id="p1", execution_queue="not a queue", review_queue=rq)

    def test_round_trip(self):
        eq = ExecutionQueue(
            queue_id="q-exec",
            portfolio_id="p1",
            orders=(SuggestedOrder(
                order_id="o1",
                entity_id=_make_entity_id(),
                action="buy",
                target_weight=0.10,
                current_weight=0.05,
                delta_weight=0.05,
            ),),
            generated_at="2026-08-08T12:00:00Z",
        )
        rq = ReviewQueue(
            queue_id="q-review",
            portfolio_id="p1",
            generated_at="2026-08-08T12:00:00Z",
        )
        result = ExecutionPlanResult(
            plan_id="plan-001",
            execution_queue=eq,
            review_queue=rq,
            generated_at="2026-08-08T12:00:00Z",
        )
        d = result.to_dict()
        restored = ExecutionPlanResult.from_dict(d)
        self.assertEqual(restored.to_dict(), d)


# --------------------------------------------------------------------------- #
# plan_execution tests
# --------------------------------------------------------------------------- #


class TestPlanExecution(unittest.TestCase):
    """Tests for the plan_execution pure function."""

    def test_basic_rebalance(self):
        """Current portfolio has 3 positions; decision shifts weights."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.30, 1000),
            ("company:TW:2317", 0.20, 2000),
            ("company:TW:2454", 0.15, 500),
        ])
        decision = _make_decision([
            ("company:TW:2330", 0.50),
            ("company:TW:2317", 0.30),
            ("company:TW:2454", 0.20),
        ])
        result = plan_execution(decision, portfolio)
        self.assertIsInstance(result, ExecutionPlanResult)
        # 3 entities, 2 changed (2330 buy 0.10, 2454 buy 0.15), 1 hold (2317).
        self.assertEqual(result.total_order_count, 3)
        # Delta 0.15 > 0.05 threshold → review.
        self.assertGreater(result.review_queue.order_count, 0)

    def test_all_hold(self):
        """Target equals current → all orders are hold."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.40, 1000),
            ("company:TW:2317", 0.30, 2000),
            ("company:TW:2454", 0.30, 500),
        ])
        decision = _make_decision([
            ("company:TW:2330", 0.40),
            ("company:TW:2317", 0.30),
            ("company:TW:2454", 0.30),
        ])
        result = plan_execution(decision, portfolio)
        self.assertEqual(result.execution_queue.order_count, 3)
        self.assertEqual(result.review_queue.order_count, 0)
        for order in result.execution_queue.orders:
            self.assertEqual(order.action, "hold")

    def test_new_position_triggers_review(self):
        """Target includes an entity not in current portfolio → buy from zero → review."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.50, 1000),
            ("company:TW:2317", 0.50, 2000),
        ])
        decision = _make_decision([
            ("company:TW:2330", 0.40),
            ("company:TW:2317", 0.40),
            ("company:TW:2454", 0.20),
        ])
        result = plan_execution(decision, portfolio)
        # 2454 is new (buy from 0 → 0.20).
        review_orders_by_entity = {
            o.entity_id.value: o for o in result.review_queue.orders
        }
        self.assertIn("company:TW:2454", review_orders_by_entity)
        self.assertEqual(review_orders_by_entity["company:TW:2454"].action, "buy")

    def test_full_exit_triggers_review(self):
        """Current has position, target does not → sell to zero → review."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.30, 1000),
            ("company:TW:2317", 0.30, 2000),
            ("company:TW:2454", 0.40, 500),
        ])
        decision = _make_decision([
            ("company:TW:2330", 0.50),
            ("company:TW:2317", 0.50),
        ])
        result = plan_execution(decision, portfolio)
        # 2454 is full exit (sell from 0.40 → 0.0).
        review_orders_by_entity = {
            o.entity_id.value: o for o in result.review_queue.orders
        }
        self.assertIn("company:TW:2454", review_orders_by_entity)
        self.assertEqual(review_orders_by_entity["company:TW:2454"].action, "sell")

    def test_small_delta_goes_to_execution(self):
        """Small delta below threshold goes to execution queue."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.50, 1000),
            ("company:TW:2317", 0.50, 2000),
        ])
        decision = _make_decision([
            ("company:TW:2330", 0.52),
            ("company:TW:2317", 0.48),
        ])
        config = ExecutionPlanConfig(
            review_delta_threshold=0.10,
            review_new_positions=False,
            review_full_exits=False,
            max_order_weight=0.50,
        )
        result = plan_execution(decision, portfolio, config)
        # Deltas are 0.02 and -0.02, both < 0.10 threshold.
        self.assertEqual(result.execution_queue.order_count, 2)
        self.assertEqual(result.review_queue.order_count, 0)

    def test_large_delta_triggers_review(self):
        """Delta above threshold triggers review."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.20, 1000),
            ("company:TW:2317", 0.80, 2000),
        ])
        decision = _make_decision([
            ("company:TW:2330", 0.50),
            ("company:TW:2317", 0.50),
        ])
        config = ExecutionPlanConfig(
            review_delta_threshold=0.10,
            review_new_positions=False,
            review_full_exits=False,
            max_order_weight=0.50,
        )
        result = plan_execution(decision, portfolio, config)
        # Both deltas are 0.30 and -0.30, both > 0.10.
        self.assertEqual(result.review_queue.order_count, 2)

    def test_priority_assignment(self):
        """Priority is assigned based on delta magnitude."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.50, 1000),
            ("company:TW:2317", 0.50, 2000),
        ])
        decision = _make_decision([
            ("company:TW:2330", 0.75),
            ("company:TW:2317", 0.25),
        ])
        config = ExecutionPlanConfig(
            review_delta_threshold=1.0,  # very high, so all go to exec
            review_new_positions=False,
            review_full_exits=False,
            max_order_weight=1.0,
        )
        result = plan_execution(decision, portfolio, config)
        orders_by_entity = {
            o.entity_id.value: o for o in result.execution_queue.orders
        }
        # 2330 delta = 0.25 → priority 5
        self.assertEqual(orders_by_entity["company:TW:2330"].priority, 5)
        # 2317 delta = -0.25 → priority 5
        self.assertEqual(orders_by_entity["company:TW:2317"].priority, 5)

    def test_empty_portfolio(self):
        """Empty portfolio with all-new positions."""
        portfolio = Portfolio(
            portfolio_id=PortfolioId("portfolio-empty"),
            name="Empty",
            positions=(),
        )
        decision = _make_decision([
            ("company:TW:2330", 0.50),
            ("company:TW:2317", 0.50),
        ])
        result = plan_execution(decision, portfolio)
        self.assertEqual(result.total_order_count, 2)
        # Both are new positions → review.
        self.assertEqual(result.review_queue.order_count, 2)

    def test_empty_allocation(self):
        """Decision with empty allocation → all sells to zero."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.50, 1000),
            ("company:TW:2317", 0.50, 2000),
        ])
        # Allocation with positions=() requires target_total=0.0.
        alloc = Allocation(allocation_id="alloc-empty", positions=(), target_total=0.0)
        decision = PortfolioDecision(
            decision_id="decision-empty",
            portfolio_id="portfolio-001",
            allocation=alloc,
            rationale="Empty allocation test",
            generated_at="2026-08-08T12:00:00Z",
        )
        result = plan_execution(decision, portfolio)
        self.assertEqual(result.total_order_count, 2)
        # Both are full exits → review.
        self.assertEqual(result.review_queue.order_count, 2)

    def test_config_none_uses_defaults(self):
        """Passing config=None uses default ExecutionPlanConfig."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.50, 1000),
            ("company:TW:2317", 0.50, 2000),
        ])
        decision = _make_decision([
            ("company:TW:2330", 0.52),
            ("company:TW:2317", 0.48),
        ])
        result = plan_execution(decision, portfolio, config=None)
        # Deltas 0.02 < 0.05 threshold → execution.
        self.assertEqual(result.execution_queue.order_count, 2)

    def test_generated_at_propagated(self):
        """generated_at from config is propagated to queues."""
        portfolio = _make_portfolio()
        decision = _make_decision()
        config = ExecutionPlanConfig(generated_at="2026-01-01T00:00:00Z")
        result = plan_execution(decision, portfolio, config)
        self.assertEqual(result.generated_at, "2026-01-01T00:00:00Z")
        self.assertEqual(result.execution_queue.generated_at, "2026-01-01T00:00:00Z")
        self.assertEqual(result.review_queue.generated_at, "2026-01-01T00:00:00Z")

    def test_metadata_contains_decision_id(self):
        """Orders carry decision_id in metadata."""
        portfolio = _make_portfolio()
        decision = _make_decision(decision_id="dec-xyz")
        result = plan_execution(decision, portfolio)
        for order in result.execution_queue.orders:
            self.assertEqual(order.metadata["decision_id"], "dec-xyz")
        for order in result.review_queue.orders:
            self.assertEqual(order.metadata["decision_id"], "dec-xyz")

    def test_review_reasons_populated(self):
        """Review queue has reasons for each reviewed order."""
        portfolio = _make_portfolio([
            ("company:TW:2330", 0.10, 1000),
            ("company:TW:2317", 0.90, 2000),
        ])
        decision = _make_decision([
            ("company:TW:2330", 0.50),
            ("company:TW:2317", 0.50),
        ])
        result = plan_execution(decision, portfolio)
        for order in result.review_queue.orders:
            self.assertIn(order.order_id, result.review_queue.review_reasons)
            self.assertGreater(len(result.review_queue.review_reasons[order.order_id]), 0)

    def test_deterministic_output(self):
        """Same inputs produce same output (modulo generated_at)."""
        portfolio = _make_portfolio()
        decision = _make_decision()
        config = ExecutionPlanConfig(generated_at="2026-01-01T00:00:00Z")
        result1 = plan_execution(decision, portfolio, config)
        result2 = plan_execution(decision, portfolio, config)
        self.assertEqual(result1.to_dict(), result2.to_dict())

    def test_result_round_trip(self):
        """Full ExecutionPlanResult round-trip."""
        portfolio = _make_portfolio()
        decision = _make_decision()
        config = ExecutionPlanConfig(generated_at="2026-01-01T00:00:00Z")
        result = plan_execution(decision, portfolio, config)
        d = result.to_dict()
        restored = ExecutionPlanResult.from_dict(d)
        self.assertEqual(restored.to_dict(), d)


# --------------------------------------------------------------------------- #
# Safety Guards (TD9: AG-M5-1 through AG-M5-8)
# --------------------------------------------------------------------------- #


class TestSafetyGuardAGM5_1_ForbiddenImports(unittest.TestCase):
    """AG-M5-1: AST scan — 0 forbidden imports in execution.py."""

    FORBIDDEN_MODULES = {
        "sqlite3",
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "macro_history",
        "phase3.pipeline",
        "phase3.datamodel",
        "phase3.graph",
        "broker",
    }

    ALLOWED_MODULES = {
        "phase3.portfolio.domain",
        "phase3.portfolio.allocation",
        "phase3.portfolio.decision",
        "phase3.portfolio.risk",
        "math",
        "re",
        "dataclasses",
        "typing",
        "__future__",
    }

    def test_no_forbidden_imports(self):
        with open(_EXECUTION_MODULE_PATH, "r") as f:
            source = f.read()
        tree = ast.parse(source, filename=_EXECUTION_MODULE_PATH)

        found_imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found_imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    found_imports.add(node.module.split(".")[0])
                    found_imports.add(node.module)

        for forbidden in self.FORBIDDEN_MODULES:
            self.assertNotIn(
                forbidden, found_imports,
                f"AG-M5-1 FAIL: forbidden import '{forbidden}' found in execution.py"
            )

    def test_only_allowed_imports(self):
        with open(_EXECUTION_MODULE_PATH, "r") as f:
            source = f.read()
        tree = ast.parse(source, filename=_EXECUTION_MODULE_PATH)

        found_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    found_modules.add(node.module)

        for mod in found_modules:
            top = mod.split(".")[0]
            self.assertTrue(
                top in {a.split(".")[0] for a in self.ALLOWED_MODULES},
                f"AG-M5-1 FAIL: import '{mod}' not in allowed set"
            )


class TestSafetyGuardAGM5_2_NoIntelligencePipeline(unittest.TestCase):
    """AG-M5-2: AST scan — 0 IntelligencePipeline() instantiations."""

    def test_no_intelligence_pipeline_instantiation(self):
        with open(_EXECUTION_MODULE_PATH, "r") as f:
            source = f.read()
        tree = ast.parse(source, filename=_EXECUTION_MODULE_PATH)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "IntelligencePipeline":
                    self.fail(
                        "AG-M5-2 FAIL: IntelligencePipeline() instantiated in execution.py"
                    )
                if isinstance(func, ast.Attribute) and func.attr == "IntelligencePipeline":
                    self.fail(
                        "AG-M5-2 FAIL: IntelligencePipeline() instantiated in execution.py"
                    )


class TestSafetyGuardAGM5_3_NoNetworkCalls(unittest.TestCase):
    """AG-M5-3: AST scan — 0 requests.post / broker / place_order / network calls."""

    FORBIDDEN_NAMES = {
        "requests", "broker", "place_order", "httpx",
        "aiohttp", "urlopen", "http_client", "websocket",
    }

    def test_no_forbidden_function_calls(self):
        with open(_EXECUTION_MODULE_PATH, "r") as f:
            source = f.read()
        tree = ast.parse(source, filename=_EXECUTION_MODULE_PATH)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    if func.attr in self.FORBIDDEN_NAMES:
                        self.fail(
                            f"AG-M5-3 FAIL: forbidden call '{func.attr}' in execution.py"
                        )
                if isinstance(func, ast.Name) and func.id in self.FORBIDDEN_NAMES:
                    self.fail(
                        f"AG-M5-3 FAIL: forbidden call '{func.id}' in execution.py"
                    )

    def test_no_forbidden_name_loads(self):
        """Check that forbidden names are never loaded (Name nodes)."""
        with open(_EXECUTION_MODULE_PATH, "r") as f:
            source = f.read()
        tree = ast.parse(source, filename=_EXECUTION_MODULE_PATH)

        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                if node.id in self.FORBIDDEN_NAMES and isinstance(node.ctx, ast.Load):
                    self.fail(
                        f"AG-M5-3 FAIL: forbidden name '{node.id}' loaded in execution.py"
                    )

    def test_grep_static_tripwire(self):
        """Static grep tripwire: 0 hits for requests.post / broker / place_order."""
        with open(_EXECUTION_MODULE_PATH, "r") as f:
            source = f.read()
        forbidden_patterns = ["requests.post", "broker", "place_order"]
        for pattern in forbidden_patterns:
            count = source.count(pattern)
            self.assertEqual(
                count, 0,
                f"AG-M5-3 FAIL: '{pattern}' found {count} times in execution.py"
            )


class TestSafetyGuardAGM5_4_NoExternalFileModifications(unittest.TestCase):
    """AG-M5-4: File-level sha256 — 0 modifications to files outside phase3.portfolio.*."""

    # Protected files (must not be modified by M5).
    PROTECTED_FILES = [
        "phase3/portfolio/domain.py",
        "phase3/portfolio/risk.py",
        "phase3/portfolio/decision.py",
        "phase3/portfolio/allocation.py",
        "phase3/pipeline/scoring_pipeline.py",
        "phase3/datamodel/scores.py",
    ]

    # Expected SHA-256 hashes (captured at baseline, 2026-08-08).
    EXPECTED_HASHES = {
        "phase3/portfolio/domain.py": "d358fcdc683788341960f1a266129f1a12bee4ca609c886d09016fd92c7c7741",
        "phase3/portfolio/risk.py": "5f1e5ddab0b274254fdcb9f735cd4644da0ea2fa213073d38f5621b4ab96bb05",
        "phase3/portfolio/decision.py": "2bbcb209dd189ca1e76213b9fcea3015661fad13c333c1011c968a727d22679f",
        "phase3/portfolio/allocation.py": "9133f66d25342c0ccfbcdf2809de3388f1f1cc79fd26a8c9f952047c5a7a6e08",
        "phase3/pipeline/scoring_pipeline.py": "5b778e00c99cec04c7d90965f291d70acada9507d5a99fb938f94fa03fb4dd59",
        "phase3/datamodel/scores.py": "cc934edd423b1cea8a51cd0554547546a5bc2a34bac169d0ba154332599a090c",
    }

    def test_protected_files_unchanged(self):
        repo_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        for rel_path, expected_sha in self.EXPECTED_HASHES.items():
            full_path = os.path.join(repo_root, rel_path)
            self.assertTrue(
                os.path.isfile(full_path),
                f"AG-M5-4 FAIL: protected file missing: {rel_path}"
            )
            with open(full_path, "rb") as f:
                actual_sha = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(
                actual_sha, expected_sha,
                f"AG-M5-4 FAIL: protected file modified: {rel_path}\n"
                f"  expected: {expected_sha}\n"
                f"  actual:   {actual_sha}"
            )


class TestSafetyGuardAGM5_5_MacroHistoryDB(unittest.TestCase):
    """AG-M5-5: macro_history.db before/after unchanged."""

    def setUp(self):
        if os.path.isfile(MACRO_DB_PATH):
            with open(MACRO_DB_PATH, "rb") as f:
                self._pre_hash = hashlib.sha256(f.read()).hexdigest()
        else:
            self._pre_hash = None

    def test_macro_history_db_not_mutated_by_test(self):
        """Import and execution of plan_execution does not mutate macro_history.db."""
        # Simply importing and running plan_execution should not touch the DB.
        portfolio = _make_portfolio()
        decision = _make_decision()
        plan_execution(decision, portfolio)

        if self._pre_hash is not None:
            with open(MACRO_DB_PATH, "rb") as f:
                post_hash = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(
                self._pre_hash, post_hash,
                "AG-M5-5 FAIL: macro_history.db was mutated"
            )


class TestSafetyGuardAGM5_6_NoIntelligenceDB(unittest.TestCase):
    """AG-M5-6: intelligence.db* count = 0."""

    def test_no_intelligence_db_created(self):
        repo_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        # Run plan_execution (in-memory, should not create any DB).
        portfolio = _make_portfolio()
        decision = _make_decision()
        plan_execution(decision, portfolio)

        # Check no intelligence.db* files exist.
        import glob
        pattern = os.path.join(repo_root, "intelligence.db*")
        matches = glob.glob(pattern)
        self.assertEqual(
            len(matches), 0,
            f"AG-M5-6 FAIL: intelligence.db* files found: {matches}"
        )


class TestSafetyGuardAGM5_7_JobsJsonUnchanged(unittest.TestCase):
    """AG-M5-7: jobs.json unchanged."""

    def setUp(self):
        if os.path.isfile(JOBS_JSON_PATH):
            with open(JOBS_JSON_PATH, "rb") as f:
                self._pre_hash = hashlib.sha256(f.read()).hexdigest()
        else:
            self._pre_hash = None

    def test_jobs_json_not_mutated(self):
        if self._pre_hash is not None:
            with open(JOBS_JSON_PATH, "rb") as f:
                post_hash = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(
                self._pre_hash, post_hash,
                "AG-M5-7 FAIL: jobs.json was mutated"
            )


class TestSafetyGuardAGM5_8_RegressionModulesExist(unittest.TestCase):
    """AG-M5-8: M2 + M3 + M4 regression test files exist on disk."""

    EXPECTED_TEST_FILES = [
        "tests/phase3/test_portfolio_domain.py",
        "tests/phase3/test_portfolio_risk.py",
        "tests/phase3/test_portfolio_allocation.py",
        "tests/phase3/test_portfolio_decision.py",
        "tests/phase3/test_portfolio_safety_guards.py",
        "tests/phase3/test_portfolio_decision_safety_guards.py",
    ]

    def test_regression_test_files_exist(self):
        repo_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        for rel_path in self.EXPECTED_TEST_FILES:
            full_path = os.path.join(repo_root, rel_path)
            self.assertTrue(
                os.path.isfile(full_path),
                f"AG-M5-8 FAIL: regression test file missing: {rel_path}"
            )


# --------------------------------------------------------------------------- #
# Module __all__ surface test
# --------------------------------------------------------------------------- #


class TestModuleSurface(unittest.TestCase):

    def test_all_exports(self):
        import phase3.portfolio.execution as exec_mod
        expected = {
            "ExecutionPlanConfig",
            "SuggestedOrder",
            "ExecutionQueue",
            "ReviewQueue",
            "ExecutionPlanResult",
            "plan_execution",
        }
        self.assertEqual(set(exec_mod.__all__), expected)

    def test_all_symbols_importable(self):
        from phase3.portfolio.execution import (
            ExecutionPlanConfig,
            SuggestedOrder,
            ExecutionQueue,
            ReviewQueue,
            ExecutionPlanResult,
            plan_execution,
        )
        # Just touching the names to ensure no import error.
        self.assertIsNotNone(ExecutionPlanConfig)
        self.assertIsNotNone(SuggestedOrder)
        self.assertIsNotNone(ExecutionQueue)
        self.assertIsNotNone(ReviewQueue)
        self.assertIsNotNone(ExecutionPlanResult)
        self.assertIsNotNone(plan_execution)


if __name__ == "__main__":
    unittest.main()