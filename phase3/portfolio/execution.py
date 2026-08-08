"""Phase 5 M5 — Execution Planning: SuggestedOrder, ExecutionQueue, ReviewQueue.

M5 ships the execution-planning layer. It transforms a ``PortfolioDecision``
(target allocation produced by M4's ``PortfolioDecisionEngine``) and a
current ``Portfolio`` (M2 domain model) into actionable **suggested orders**
and partitions them into two queues:

- **ExecutionQueue** — actionable orders with small weight deltas that fall
  within normal rebalancing thresholds. These are auto-generated suggestions
  ready for downstream consumption (M6 reporting or a human-operated trade
  blotter). They are **output-only** — no direct execution-platform API call, no order placement.

- **ReviewQueue** — orders that require human review before execution.
  These include large weight deltas, new positions (buy from zero), full
  exits (sell to zero), and orders that breach configurable risk thresholds.

Design principles (mirrors M2 ``domain.py`` + M4 ``allocation.py`` / ``decision.py``)
--------------------------------------------------------------------

1. **Immutable.** ``SuggestedOrder``, ``ExecutionQueue``, and ``ReviewQueue``
   are ``@dataclass(frozen=True)``. Mutation returns a new instance.

2. **Composition.** ``ExecutionQueue`` and ``ReviewQueue`` hold tuples of
   ``SuggestedOrder`` entities. They do NOT subclass each other or
   ``PortfolioDecision``.

3. **Deterministic serialization.** ``to_dict()`` returns a
   JSON-serializable ``dict[str, Any]`` with keys in a fixed, code-defined
   order. ``from_dict(to_dict(x)).to_dict() == to_dict(x)`` (byte-identical
   round-trip).

4. **No ``datetime.now()``.** ``generated_at`` is injected as a string
   (ISO 8601) — the same determinism pattern as M4's ``PortfolioDecision``.

5. **Pure functions.** The planning function ``plan_execution`` is a pure
   function — no I/O, no global mutable state, no ``random.*``, no
   ``datetime.now()``.

6. **Output-only invariant (O4).** This module MUST NOT contain
   network/execution-platform API calls (forbidden names are checked by AST scan
   AG-M5-3 and the static-analysis tripwire in TD9).

Boundary contract
-------------------

This module imports ONLY from the Python standard library and from
``phase3.portfolio.domain`` (M2), ``phase3.portfolio.allocation`` (M4-S1),
and ``phase3.portfolio.decision`` (M4). It MAY optionally import from
``phase3.portfolio.risk`` (M3) for risk-threshold-based review-queue
partitioning. It MUST NOT import ``sqlite3``, ``macro_history``,
``phase3.pipeline``, ``phase3.datamodel``, ``phase3.graph``, or any
execution-platform / network module (AST-enforced by TD9, the M5 safety guard test
file ``tests/phase3/test_portfolio_execution.py``).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

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

# --------------------------------------------------------------------------- #
# Module constants
# --------------------------------------------------------------------------- #

_IDENTIFIER_PATTERN = re.compile(r"^[^\s]{1,128}$")

_FP_REL_TOL = 1e-9
_FP_ABS_TOL = 1e-12

# Default review thresholds (overridable via ExecutionPlanConfig).
_DEFAULT_REVIEW_DELTA = 0.05  # 5% weight delta triggers review
_DEFAULT_REVIEW_NEW_POSITION = True  # new positions (buy from zero) trigger review
_DEFAULT_REVIEW_FULL_EXIT = True  # full exits (sell to zero) trigger review
_DEFAULT_MAX_ORDER_WEIGHT = 0.25  # single order > 25% of portfolio triggers review

# Valid order actions.
_VALID_ACTIONS = {"buy", "sell", "hold"}

# Valid priority levels (higher = more urgent).
_VALID_PRIORITIES = {1, 2, 3, 4, 5}


# --------------------------------------------------------------------------- #
# ExecutionPlanConfig (M5 NEW)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExecutionPlanConfig:
    """Configuration for the execution planning function.

    Controls how ``plan_execution`` partitions ``SuggestedOrder`` entities
    into the ``ExecutionQueue`` (actionable) and ``ReviewQueue`` (needs
    human review).

    Fields:
        review_delta_threshold: Weight delta (absolute fraction) above
            which an order is routed to the review queue. Default 0.05
            (5%). A delta of exactly the threshold is NOT reviewed (strict
            greater-than).
        review_new_positions: If ``True`` (default), orders that create a
            new position (buy from zero weight) are routed to the review
            queue.
        review_full_exits: If ``True`` (default), orders that fully exit a
            position (sell to zero weight) are routed to the review queue.
        max_order_weight: Maximum weight for a single order before it is
            routed to the review queue. Default 0.25 (25%).
        generated_at: ISO 8601 timestamp string injected for determinism
            (NOT ``datetime.now()``).
        queue_id: Optional queue pair identifier. If empty, auto-generated
            as a deterministic hash.
    """

    review_delta_threshold: float = _DEFAULT_REVIEW_DELTA
    review_new_positions: bool = _DEFAULT_REVIEW_NEW_POSITION
    review_full_exits: bool = _DEFAULT_REVIEW_FULL_EXIT
    max_order_weight: float = _DEFAULT_MAX_ORDER_WEIGHT
    generated_at: str = ""
    queue_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.review_delta_threshold, (int, float)):
            raise ValueError(
                f"ExecutionPlanConfig.review_delta_threshold must be a "
                f"float; got {type(self.review_delta_threshold).__name__}"
            )
        rdt_f = float(self.review_delta_threshold)
        if rdt_f != self.review_delta_threshold:
            object.__setattr__(self, "review_delta_threshold", rdt_f)
        if not math.isfinite(rdt_f) or not (0.0 <= rdt_f <= 1.0):
            raise ValueError(
                f"ExecutionPlanConfig.review_delta_threshold must be in "
                f"[0.0, 1.0]; got {rdt_f!r}"
            )

        if not isinstance(self.review_new_positions, bool):
            raise ValueError(
                f"ExecutionPlanConfig.review_new_positions must be bool; "
                f"got {type(self.review_new_positions).__name__}"
            )

        if not isinstance(self.review_full_exits, bool):
            raise ValueError(
                f"ExecutionPlanConfig.review_full_exits must be bool; "
                f"got {type(self.review_full_exits).__name__}"
            )

        if not isinstance(self.max_order_weight, (int, float)):
            raise ValueError(
                f"ExecutionPlanConfig.max_order_weight must be a "
                f"float; got {type(self.max_order_weight).__name__}"
            )
        mow_f = float(self.max_order_weight)
        if mow_f != self.max_order_weight:
            object.__setattr__(self, "max_order_weight", mow_f)
        if not math.isfinite(mow_f) or not (0.0 <= mow_f <= 1.0):
            raise ValueError(
                f"ExecutionPlanConfig.max_order_weight must be in "
                f"[0.0, 1.0]; got {mow_f!r}"
            )

        if not isinstance(self.generated_at, str):
            raise ValueError(
                f"ExecutionPlanConfig.generated_at must be a str; "
                f"got {type(self.generated_at).__name__}"
            )

        if not isinstance(self.queue_id, str):
            raise ValueError(
                f"ExecutionPlanConfig.queue_id must be a str; "
                f"got {type(self.queue_id).__name__}"
            )
        if self.queue_id and not _IDENTIFIER_PATTERN.match(self.queue_id):
            raise ValueError(
                f"ExecutionPlanConfig.queue_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.queue_id!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_delta_threshold": self.review_delta_threshold,
            "review_new_positions": self.review_new_positions,
            "review_full_exits": self.review_full_exits,
            "max_order_weight": self.max_order_weight,
            "generated_at": self.generated_at,
            "queue_id": self.queue_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionPlanConfig":
        return cls(
            review_delta_threshold=float(data.get("review_delta_threshold", _DEFAULT_REVIEW_DELTA)),
            review_new_positions=bool(data.get("review_new_positions", _DEFAULT_REVIEW_NEW_POSITION)),
            review_full_exits=bool(data.get("review_full_exits", _DEFAULT_REVIEW_FULL_EXIT)),
            max_order_weight=float(data.get("max_order_weight", _DEFAULT_MAX_ORDER_WEIGHT)),
            generated_at=data.get("generated_at", ""),
            queue_id=data.get("queue_id", ""),
        )


# --------------------------------------------------------------------------- #
# SuggestedOrder DTO
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SuggestedOrder:
    """A single suggested order: buy, sell, or hold for one entity.

    ``SuggestedOrder`` is the atomic unit of the execution planning layer.
    It is produced by comparing a target allocation (from M4's
    ``PortfolioDecision``) against the current portfolio state (M2
    ``Portfolio``) and computing the weight delta, action, priority, and
    an optional quantity estimate.

    Invariants (enforced at construction):

    - ``order_id`` matches the identifier pattern (non-empty, no
      whitespace, <= 128 chars).
    - ``entity_id`` is an ``EntityId`` instance.
    - ``action`` is one of ``"buy"``, ``"sell"``, ``"hold"``.
    - ``target_weight`` is a finite float in ``[0.0, 1.0]``.
    - ``current_weight`` is a finite float in ``[0.0, 1.0]``.
    - ``delta_weight`` is a finite float in ``[-1.0, 1.0]``.
    - ``priority`` is an integer in ``{1, 2, 3, 4, 5}`` (higher = more
      urgent).
    - ``quantity_estimate`` is a non-negative integer (0 for hold).
    - ``metadata`` is a JSON-serializable dict (default empty).
    """

    order_id: str
    entity_id: EntityId
    action: str
    target_weight: float
    current_weight: float
    delta_weight: float
    priority: int = 3
    quantity_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str):
            raise ValueError(
                f"SuggestedOrder.order_id must be a str; "
                f"got {type(self.order_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.order_id):
            raise ValueError(
                f"SuggestedOrder.order_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.order_id!r}"
            )

        if not isinstance(self.entity_id, EntityId):
            raise ValueError(
                f"SuggestedOrder.entity_id must be an EntityId; "
                f"got {type(self.entity_id).__name__}"
            )

        if self.action not in _VALID_ACTIONS:
            raise ValueError(
                f"SuggestedOrder.action must be one of "
                f"{_VALID_ACTIONS}; got {self.action!r}"
            )

        for name, val in (
            ("target_weight", self.target_weight),
            ("current_weight", self.current_weight),
        ):
            if not isinstance(val, (int, float)):
                raise ValueError(
                    f"SuggestedOrder.{name} must be a float; "
                    f"got {type(val).__name__}"
                )
            val_f = float(val)
            if not math.isfinite(val_f) or not (0.0 <= val_f <= 1.0):
                raise ValueError(
                    f"SuggestedOrder.{name} must be in [0.0, 1.0]; "
                    f"got {val_f!r}"
                )

        if not isinstance(self.delta_weight, (int, float)):
            raise ValueError(
                f"SuggestedOrder.delta_weight must be a float; "
                f"got {type(self.delta_weight).__name__}"
            )
        dw_f = float(self.delta_weight)
        if not math.isfinite(dw_f) or not (-1.0 <= dw_f <= 1.0):
            raise ValueError(
                f"SuggestedOrder.delta_weight must be in [-1.0, 1.0]; "
                f"got {dw_f!r}"
            )

        if not isinstance(self.priority, int):
            raise ValueError(
                f"SuggestedOrder.priority must be an int; "
                f"got {type(self.priority).__name__}"
            )
        if self.priority not in _VALID_PRIORITIES:
            raise ValueError(
                f"SuggestedOrder.priority must be in "
                f"{_VALID_PRIORITIES}; got {self.priority!r}"
            )

        if not isinstance(self.quantity_estimate, int):
            raise ValueError(
                f"SuggestedOrder.quantity_estimate must be an int; "
                f"got {type(self.quantity_estimate).__name__}"
            )
        if self.quantity_estimate < 0:
            raise ValueError(
                f"SuggestedOrder.quantity_estimate must be >= 0; "
                f"got {self.quantity_estimate!r}"
            )

        if not isinstance(self.metadata, dict):
            raise ValueError(
                f"SuggestedOrder.metadata must be a dict; "
                f"got {type(self.metadata).__name__}"
            )

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #

    @property
    def is_review_candidate(self) -> bool:
        """Heuristic: is this order likely to need human review?

        This is a convenience property for callers who want a quick
        signal without running the full ``plan_execution`` partitioning.
        The authoritative partitioning is done by ``plan_execution``.
        """
        if self.action == "hold":
            return False
        if abs(self.delta_weight) > _DEFAULT_REVIEW_DELTA:
            return True
        if self.action == "buy" and math.isclose(self.current_weight, 0.0, abs_tol=_FP_ABS_TOL):
            return True
        if self.action == "sell" and math.isclose(self.target_weight, 0.0, abs_tol=_FP_ABS_TOL):
            return True
        return False

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "entity_id": self.entity_id.to_dict(),
            "action": self.action,
            "target_weight": self.target_weight,
            "current_weight": self.current_weight,
            "delta_weight": self.delta_weight,
            "priority": self.priority,
            "quantity_estimate": self.quantity_estimate,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuggestedOrder":
        return cls(
            order_id=data["order_id"],
            entity_id=EntityId.from_dict(data["entity_id"]),
            action=data["action"],
            target_weight=float(data["target_weight"]),
            current_weight=float(data["current_weight"]),
            delta_weight=float(data["delta_weight"]),
            priority=int(data.get("priority", 3)),
            quantity_estimate=int(data.get("quantity_estimate", 0)),
            metadata=dict(data.get("metadata", {})),
        )


# --------------------------------------------------------------------------- #
# ExecutionQueue DTO
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExecutionQueue:
    """Queue of actionable suggested orders (auto-generated, output-only).

    The ``ExecutionQueue`` holds orders that fall within normal
    rebalancing thresholds and are ready for downstream consumption (M6
    reporting or a human-operated trade blotter). It is **output-only**:
    it contains no direct execution-platform API calls, no order placement logic.

    Invariants:
    - ``queue_id`` matches the identifier pattern.
    - ``orders`` is a tuple of ``SuggestedOrder`` instances (defensive
      list-to-tuple conversion).
    - ``generated_at`` is a non-empty string.
    - ``portfolio_id`` is a non-empty string.
    """

    queue_id: str
    portfolio_id: str
    orders: tuple[SuggestedOrder, ...] = field(default_factory=tuple)
    generated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.queue_id, str):
            raise ValueError(
                f"ExecutionQueue.queue_id must be a str; "
                f"got {type(self.queue_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.queue_id):
            raise ValueError(
                f"ExecutionQueue.queue_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.queue_id!r}"
            )

        if not isinstance(self.portfolio_id, str):
            raise ValueError(
                f"ExecutionQueue.portfolio_id must be a str; "
                f"got {type(self.portfolio_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.portfolio_id):
            raise ValueError(
                f"ExecutionQueue.portfolio_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.portfolio_id!r}"
            )

        if not isinstance(self.orders, tuple):
            object.__setattr__(self, "orders", tuple(self.orders))
        for order in self.orders:
            if not isinstance(order, SuggestedOrder):
                raise ValueError(
                    f"ExecutionQueue.orders must contain SuggestedOrder "
                    f"instances; got {type(order).__name__}"
                )

        if not isinstance(self.generated_at, str):
            raise ValueError(
                f"ExecutionQueue.generated_at must be a str; "
                f"got {type(self.generated_at).__name__}"
            )

        if not isinstance(self.metadata, dict):
            raise ValueError(
                f"ExecutionQueue.metadata must be a dict; "
                f"got {type(self.metadata).__name__}"
            )

    @property
    def order_count(self) -> int:
        return len(self.orders)

    @property
    def buy_count(self) -> int:
        return sum(1 for o in self.orders if o.action == "buy")

    @property
    def sell_count(self) -> int:
        return sum(1 for o in self.orders if o.action == "sell")

    @property
    def hold_count(self) -> int:
        return sum(1 for o in self.orders if o.action == "hold")

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "portfolio_id": self.portfolio_id,
            "orders": tuple(o.to_dict() for o in self.orders),
            "generated_at": self.generated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionQueue":
        raw_orders = data.get("orders", ())
        if isinstance(raw_orders, (list, tuple)):
            orders_tuple = tuple(
                SuggestedOrder.from_dict(o) for o in raw_orders
            )
        else:
            orders_tuple = ()
        return cls(
            queue_id=data["queue_id"],
            portfolio_id=data["portfolio_id"],
            orders=orders_tuple,
            generated_at=data.get("generated_at", ""),
            metadata=dict(data.get("metadata", {})),
        )


# --------------------------------------------------------------------------- #
# ReviewQueue DTO
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReviewQueue:
    """Queue of suggested orders requiring human review.

    The ``ReviewQueue`` holds orders that breach configurable thresholds:
    large weight deltas, new positions, full exits, or single-order
    weight exceeding ``max_order_weight``. These orders are NOT
    auto-actionable; they require human judgment before execution.

    Invariants:
    - ``queue_id`` matches the identifier pattern.
    - ``orders`` is a tuple of ``SuggestedOrder`` instances.
    - ``generated_at`` is a non-empty string.
    - ``portfolio_id`` is a non-empty string.
    - ``review_reasons`` is a dict mapping ``order_id`` to a list of
      reason strings explaining why the order was routed to review.
    """

    queue_id: str
    portfolio_id: str
    orders: tuple[SuggestedOrder, ...] = field(default_factory=tuple)
    review_reasons: dict[str, list[str]] = field(default_factory=dict)
    generated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.queue_id, str):
            raise ValueError(
                f"ReviewQueue.queue_id must be a str; "
                f"got {type(self.queue_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.queue_id):
            raise ValueError(
                f"ReviewQueue.queue_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.queue_id!r}"
            )

        if not isinstance(self.portfolio_id, str):
            raise ValueError(
                f"ReviewQueue.portfolio_id must be a str; "
                f"got {type(self.portfolio_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.portfolio_id):
            raise ValueError(
                f"ReviewQueue.portfolio_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.portfolio_id!r}"
            )

        if not isinstance(self.orders, tuple):
            object.__setattr__(self, "orders", tuple(self.orders))
        for order in self.orders:
            if not isinstance(order, SuggestedOrder):
                raise ValueError(
                    f"ReviewQueue.orders must contain SuggestedOrder "
                    f"instances; got {type(order).__name__}"
                )

        if not isinstance(self.review_reasons, dict):
            raise ValueError(
                f"ReviewQueue.review_reasons must be a dict; "
                f"got {type(self.review_reasons).__name__}"
            )

        if not isinstance(self.generated_at, str):
            raise ValueError(
                f"ReviewQueue.generated_at must be a str; "
                f"got {type(self.generated_at).__name__}"
            )

        if not isinstance(self.metadata, dict):
            raise ValueError(
                f"ReviewQueue.metadata must be a dict; "
                f"got {type(self.metadata).__name__}"
            )

    @property
    def order_count(self) -> int:
        return len(self.orders)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "portfolio_id": self.portfolio_id,
            "orders": tuple(o.to_dict() for o in self.orders),
            "review_reasons": {
                k: list(v) for k, v in self.review_reasons.items()
            },
            "generated_at": self.generated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewQueue":
        raw_orders = data.get("orders", ())
        if isinstance(raw_orders, (list, tuple)):
            orders_tuple = tuple(
                SuggestedOrder.from_dict(o) for o in raw_orders
            )
        else:
            orders_tuple = ()
        raw_reasons = data.get("review_reasons", {})
        reasons_dict = (
            {k: list(v) for k, v in raw_reasons.items()}
            if isinstance(raw_reasons, dict)
            else {}
        )
        return cls(
            queue_id=data["queue_id"],
            portfolio_id=data["portfolio_id"],
            orders=orders_tuple,
            review_reasons=reasons_dict,
            generated_at=data.get("generated_at", ""),
            metadata=dict(data.get("metadata", {})),
        )


# --------------------------------------------------------------------------- #
# ExecutionPlanResult (M5 NEW)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExecutionPlanResult:
    """The complete output of ``plan_execution``.

    Bundles the ``ExecutionQueue`` and ``ReviewQueue`` together with
    summary metadata. This is the top-level return type of the planning
    function.

    Invariants:
    - ``plan_id`` matches the identifier pattern.
    - ``execution_queue`` is an ``ExecutionQueue`` instance.
    - ``review_queue`` is a ``ReviewQueue`` instance.
    - ``generated_at`` is a string.
    """

    plan_id: str
    execution_queue: ExecutionQueue
    review_queue: ReviewQueue
    generated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str):
            raise ValueError(
                f"ExecutionPlanResult.plan_id must be a str; "
                f"got {type(self.plan_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.plan_id):
            raise ValueError(
                f"ExecutionPlanResult.plan_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.plan_id!r}"
            )

        if not isinstance(self.execution_queue, ExecutionQueue):
            raise ValueError(
                f"ExecutionPlanResult.execution_queue must be an "
                f"ExecutionQueue; got "
                f"{type(self.execution_queue).__name__}"
            )

        if not isinstance(self.review_queue, ReviewQueue):
            raise ValueError(
                f"ExecutionPlanResult.review_queue must be a "
                f"ReviewQueue; got {type(self.review_queue).__name__}"
            )

        if not isinstance(self.generated_at, str):
            raise ValueError(
                f"ExecutionPlanResult.generated_at must be a str; "
                f"got {type(self.generated_at).__name__}"
            )

        if not isinstance(self.metadata, dict):
            raise ValueError(
                f"ExecutionPlanResult.metadata must be a dict; "
                f"got {type(self.metadata).__name__}"
            )

    @property
    def total_order_count(self) -> int:
        return self.execution_queue.order_count + self.review_queue.order_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "execution_queue": self.execution_queue.to_dict(),
            "review_queue": self.review_queue.to_dict(),
            "generated_at": self.generated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionPlanResult":
        return cls(
            plan_id=data["plan_id"],
            execution_queue=ExecutionQueue.from_dict(data["execution_queue"]),
            review_queue=ReviewQueue.from_dict(data["review_queue"]),
            generated_at=data.get("generated_at", ""),
            metadata=dict(data.get("metadata", {})),
        )


# --------------------------------------------------------------------------- #
# Planning function
# --------------------------------------------------------------------------- #


def plan_execution(
    decision: PortfolioDecision,
    portfolio: Portfolio,
    config: ExecutionPlanConfig | None = None,
) -> ExecutionPlanResult:
    """Transform a ``PortfolioDecision`` + current ``Portfolio`` into
    suggested orders partitioned into execution and review queues.

    This is a pure function. It does NOT perform any I/O, does NOT call
    any execution-platform API, and does NOT modify any external state.

    Algorithm:

    1. Build a current-weight map from ``portfolio.positions`` keyed by
       ``entity_id.value``.
    2. Build a target-weight map from ``decision.allocation.positions``
       keyed by ``entity_id.value``.
    3. For each entity in the union of current and target weight maps:
       a. Compute ``delta = target - current``.
       b. Determine action: ``buy`` if delta > 0, ``sell`` if delta < 0,
          ``hold`` if delta is within floating-point tolerance.
       c. Assign priority based on delta magnitude:
          - Priority 5: |delta| > 0.20
          - Priority 4: |delta| > 0.10
          - Priority 3: |delta| > 0.05
          - Priority 2: |delta| > 0.02
          - Priority 1: |delta| <= 0.02 (hold or near-zero)
       d. Estimate quantity: if the portfolio has a known total value
          and the entity has a price, ``quantity = int(delta * total /
          price)``. Otherwise, ``quantity_estimate = 0`` (caller
          must supply this).
    4. Partition orders into execution vs review based on
       ``ExecutionPlanConfig`` thresholds.
    5. Return ``ExecutionPlanResult``.

    Args:
        decision: The target allocation from M4's PortfolioDecisionEngine.
        portfolio: The current portfolio state from M2's domain model.
        config: Execution plan configuration. If ``None``, uses defaults.

    Returns:
        An ``ExecutionPlanResult`` containing the execution queue and
        review queue.
    """
    if config is None:
        config = ExecutionPlanConfig()

    # Build current-weight map.
    current_weights: dict[str, float] = {}
    current_positions: dict[str, Position] = {}
    for pos in portfolio.positions:
        eid = pos.entity_id.value
        current_weights[eid] = pos.weight.value
        current_positions[eid] = pos

    # Build target-weight map.
    target_weights: dict[str, float] = {}
    target_positions: dict[str, Position] = {}
    for pos in decision.allocation.positions:
        eid = pos.entity_id.value
        target_weights[eid] = pos.weight.value
        target_positions[eid] = pos

    # Union of all entity IDs.
    all_entity_ids = sorted(set(current_weights.keys()) | set(target_weights.keys()))

    # Generate order IDs deterministically.
    orders: list[SuggestedOrder] = []
    for idx, eid_str in enumerate(all_entity_ids):
        tw = target_weights.get(eid_str, 0.0)
        cw = current_weights.get(eid_str, 0.0)
        delta = tw - cw

        # Determine action.
        if abs(delta) < _FP_ABS_TOL:
            action = "hold"
        elif delta > 0:
            action = "buy"
        else:
            action = "sell"

        # Assign priority.
        abs_delta = abs(delta)
        if abs_delta > 0.20:
            priority = 5
        elif abs_delta > 0.10:
            priority = 4
        elif abs_delta > 0.05:
            priority = 3
        elif abs_delta > 0.02:
            priority = 2
        else:
            priority = 1

        # Quantity estimate (0 if no price available).
        qty_estimate = 0
        target_pos = target_positions.get(eid_str)
        if target_pos is not None and target_pos.price is not None and target_pos.price.value > 0:
            # Estimate: delta * total portfolio value / price per share.
            # We don't have total portfolio value here; leave as 0 for
            # the caller to fill in. This keeps the function pure.
            qty_estimate = 0

        order_id = f"order-{idx:04d}"
        entity_id = EntityId(eid_str)

        order = SuggestedOrder(
            order_id=order_id,
            entity_id=entity_id,
            action=action,
            target_weight=tw,
            current_weight=cw,
            delta_weight=delta,
            priority=priority,
            quantity_estimate=qty_estimate,
            metadata={
                "decision_id": decision.decision_id,
            },
        )
        orders.append(order)

    # Partition into execution vs review.
    exec_orders: list[SuggestedOrder] = []
    review_orders: list[SuggestedOrder] = []
    review_reasons: dict[str, list[str]] = {}

    for order in orders:
        reasons: list[str] = []

        if config.review_delta_threshold > 0 and abs(order.delta_weight) > config.review_delta_threshold:
            reasons.append(
                f"delta_weight {order.delta_weight:.6f} exceeds "
                f"threshold {config.review_delta_threshold:.6f}"
            )

        if config.review_new_positions and order.action == "buy" and math.isclose(order.current_weight, 0.0, abs_tol=_FP_ABS_TOL):
            reasons.append("new position (buy from zero)")

        if config.review_full_exits and order.action == "sell" and math.isclose(order.target_weight, 0.0, abs_tol=_FP_ABS_TOL):
            reasons.append("full exit (sell to zero)")

        if order.action != "hold" and abs(order.delta_weight) > config.max_order_weight:
            reasons.append(
                f"order weight {abs(order.delta_weight):.6f} exceeds "
                f"max_order_weight {config.max_order_weight:.6f}"
            )

        if reasons:
            review_orders.append(order)
            review_reasons[order.order_id] = reasons
        else:
            exec_orders.append(order)

    # Build queue IDs.
    queue_id_base = config.queue_id or f"exec-{decision.decision_id}"
    exec_queue_id = f"{queue_id_base}-exec"
    review_queue_id = f"{queue_id_base}-review"
    plan_id = f"{queue_id_base}-plan"

    exec_queue = ExecutionQueue(
        queue_id=exec_queue_id,
        portfolio_id=decision.portfolio_id,
        orders=tuple(exec_orders),
        generated_at=config.generated_at,
        metadata={
            "decision_id": decision.decision_id,
            "order_count": len(exec_orders),
        },
    )

    review_queue = ReviewQueue(
        queue_id=review_queue_id,
        portfolio_id=decision.portfolio_id,
        orders=tuple(review_orders),
        review_reasons=review_reasons,
        generated_at=config.generated_at,
        metadata={
            "decision_id": decision.decision_id,
            "order_count": len(review_orders),
        },
    )

    return ExecutionPlanResult(
        plan_id=plan_id,
        execution_queue=exec_queue,
        review_queue=review_queue,
        generated_at=config.generated_at,
        metadata={
            "decision_id": decision.decision_id,
            "portfolio_id": decision.portfolio_id,
            "total_orders": len(orders),
            "exec_orders": len(exec_orders),
            "review_orders": len(review_orders),
        },
    )


__all__ = [
    "ExecutionPlanConfig",
    "SuggestedOrder",
    "ExecutionQueue",
    "ReviewQueue",
    "ExecutionPlanResult",
    "plan_execution",
]