"""Phase 5 M2 — Portfolio Domain Model.

This module defines the foundational portfolio domain entities and value
objects for Phase 5 of the Finance Intelligence Engine. It is the lowest-risk
work item in Phase 5: pure data classes with deterministic serialization, no
I/O, no DB access, no broker integration.

Design principles
-----------------

1. **Value objects are immutable.** All value objects are ``@dataclass(frozen=True)``
   with ``__post_init__`` invariants enforced once at construction time. A
   constructed value object is always valid; an invalid construction raises
   ``ValueError`` and never produces a partially-constructed instance.

2. **Domain entities reference value objects by composition, not inheritance.**
   ``Position`` holds an ``EntityId`` plus a ``Weight`` / ``Quantity`` /
   ``CostBasis`` / ``Price`` value object triplet. ``Portfolio`` holds a tuple
   of ``Position`` entities plus an immutable ``PortfolioId``.

3. **Deterministic serialization.** Every public class implements ``to_dict()``
   returning a JSON-serializable ``dict[str, Any]``. The output is stable: the
   same object serialized twice produces byte-identical output (modulo
   ``generated_at``-style timestamps, which are NOT part of M2). No dict
   ordering dependency — keys are emitted in a fixed, code-defined order.

4. **No floats as weights except via ``Weight``.** A bare ``float`` weight is
   ambiguous (fraction? percentage? basis points?). The ``Weight`` value
   object normalizes to a fraction in ``[0.0, 1.0]`` and exposes ``.value``.

5. **No money types mixed.** ``Price`` and ``CostBasis`` carry an ISO 4217
   currency code. Arithmetic across currencies is rejected at the type level
   (no implicit conversion).

6. **``Portfolio.allocation_total`` is derived, not stored.** It is the sum of
   ``Position.weight.value`` across positions; M2 exposes it as a read-only
   property. M3+ will enforce the sum-to-1.0 invariant via the risk engine.

Invariants (enforced at construction)
-------------------------------------

- ``Weight.value`` in ``[0.0, 1.0]``
- ``Quantity.value`` >= 0
- ``Price.value`` >= 0
- ``CostBasis.value`` >= 0
- ``EntityId.value`` non-empty, no whitespace, max 128 chars
- ``PortfolioId.value`` non-empty, no whitespace, max 128 chars
- ``PositionId.value`` non-empty, no whitespace, max 128 chars
- ``Position.cost_basis.currency`` == ``Position.price.currency`` when both
  are present (defensive — M2 allows cost_basis=None for untracked positions)
- ``Portfolio.positions`` is a tuple (immutable); duplicate
  ``Position.position_id`` values are rejected
- ``Portfolio.positions`` cannot contain two ``Position`` entities with the
  same ``entity_id`` (one position per entity per portfolio — M3+ risk engine
  enforces concentration on top of this)

Boundary contract
-----------------

This module imports ONLY from the standard library and from
``phase3.datamodel`` types that are already public (e.g. ``ScoreBreakdown``
is referenced only in docstrings — M2 does not import it). It MUST NOT import
``macro_history.db``, ``phase3.pipeline.*``, or any broker / network module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Identifiers must be non-empty, no internal whitespace, <= 128 chars.
# This is deliberately permissive: it accepts ASCII, digits, underscores,
# colons (for entity_id like "company:TW:2330"), hyphens, and dots — but
# rejects leading/trailing whitespace and any embedded whitespace.
_IDENTIFIER_PATTERN = re.compile(r"^[^\s]{1,128}$")

# ISO 4217 currency code — exactly 3 uppercase letters. We do not maintain
# the full ISO list here (out of scope for M2); the regex is the only guard.
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")

# Maximum number of positions per portfolio. M2 sets a generous cap to allow
# fixture-driven tests; M3+ risk engine will enforce tighter concentration
# limits. The cap protects against pathological inputs.
_MAX_POSITIONS = 4096


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityId:
    """Stable identifier for a portfolio-constituent entity.

    Examples: ``"company:TW:2330"``, ``"industry:TW:semiconductor"``,
    ``"macro:global:fed_rate"``. The value is opaque to the portfolio domain
    layer — it is a stable foreign key back to the Phase 3/4
    ``ScoreBreakdown.entity_id`` contract.
    """

    value: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.match(self.value):
            raise ValueError(
                f"EntityId.value must be non-empty, no whitespace, <= 128 chars; "
                f"got {self.value!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntityId":
        return cls(value=data["value"])


@dataclass(frozen=True)
class PortfolioId:
    """Stable identifier for a portfolio instance."""

    value: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.match(self.value):
            raise ValueError(
                f"PortfolioId.value must be non-empty, no whitespace, <= 128 chars; "
                f"got {self.value!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortfolioId":
        return cls(value=data["value"])


@dataclass(frozen=True)
class PositionId:
    """Stable identifier for a single position within a portfolio.

    Position IDs MUST be unique within a single ``Portfolio`` (enforced by
    ``Portfolio.__post_init__``). They MAY be reused across portfolios.
    """

    value: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.match(self.value):
            raise ValueError(
                f"PositionId.value must be non-empty, no whitespace, <= 128 chars; "
                f"got {self.value!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PositionId":
        return cls(value=data["value"])


@dataclass(frozen=True)
class Weight:
    """Portfolio weight as a fraction in ``[0.0, 1.0]``.

    ``Weight`` normalizes the convention: 0.05 == 5%. The fraction is stored
    directly; no percentage conversion is performed at the type level.
    """

    value: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.value <= 1.0):
            raise ValueError(
                f"Weight.value must be in [0.0, 1.0]; got {self.value!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Weight":
        return cls(value=float(data["value"]))


@dataclass(frozen=True)
class Quantity:
    """Position size in shares (or contracts). Non-negative integer."""

    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError(
                f"Quantity.value must be >= 0; got {self.value!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Quantity":
        return cls(value=int(data["value"]))


@dataclass(frozen=True)
class Price:
    """Quoted price in a specific currency. Non-negative.

    The currency code is an ISO 4217 alpha-3 code (3 uppercase letters). M2
    does not maintain the full ISO list; the regex is the only guard.
    """

    value: float
    currency: str

    def __post_init__(self) -> None:
        if self.value < 0.0:
            raise ValueError(
                f"Price.value must be >= 0.0; got {self.value!r}"
            )
        if not _CURRENCY_PATTERN.match(self.currency):
            raise ValueError(
                f"Price.currency must be an ISO 4217 alpha-3 code "
                f"(3 uppercase letters); got {self.currency!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "currency": self.currency}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Price":
        return cls(value=float(data["value"]), currency=data["currency"])


@dataclass(frozen=True)
class CostBasis:
    """Cost basis per share in a specific currency. Non-negative.

    M2 allows ``CostBasis`` to be optional on a ``Position`` (for untracked
    positions). When present, the currency MUST match the position's
    ``Price.currency`` (defensive — enforced on ``Position.__post_init__``).
    """

    value: float
    currency: str

    def __post_init__(self) -> None:
        if self.value < 0.0:
            raise ValueError(
                f"CostBasis.value must be >= 0.0; got {self.value!r}"
            )
        if not _CURRENCY_PATTERN.match(self.currency):
            raise ValueError(
                f"CostBasis.currency must be an ISO 4217 alpha-3 code "
                f"(3 uppercase letters); got {self.currency!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "currency": self.currency}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CostBasis":
        return cls(value=float(data["value"]), currency=data["currency"])


# ---------------------------------------------------------------------------
# Domain entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """A single position within a portfolio.

    A ``Position`` is identified by ``position_id`` (unique within its
    ``Portfolio``) and references an external entity via ``entity_id``. The
    position carries a target ``weight`` (fraction in ``[0.0, 1.0]``), a
    ``quantity`` (shares, non-negative integer), and optionally a ``price``
    and ``cost_basis``. When both ``price`` and ``cost_basis`` are present,
    they MUST share the same currency code.

    ``metadata`` is an arbitrary JSON-serializable dict for downstream
    consumers (M3+). It is NOT validated by M2 beyond the JSON-serializable
    invariant on ``to_dict()`` (the dataclass itself stores whatever is
    passed; ``to_dict()`` will fail at serialization time if non-JSON values
    are present — this is intentional, to keep the construction path cheap).
    """

    position_id: PositionId
    entity_id: EntityId
    weight: Weight
    quantity: Quantity
    price: Optional[Price] = None
    cost_basis: Optional[CostBasis] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Defensive currency consistency: if both price and cost_basis are
        # present, they MUST share the same currency. This is a sanity guard,
        # not a portfolio-level invariant — M3+ risk engine may relax it for
        # cross-currency portfolios.
        if self.price is not None and self.cost_basis is not None:
            if self.price.currency != self.cost_basis.currency:
                raise ValueError(
                    f"Position {self.position_id.value!r}: price.currency "
                    f"({self.price.currency!r}) != cost_basis.currency "
                    f"({self.cost_basis.currency!r})"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id.to_dict(),
            "entity_id": self.entity_id.to_dict(),
            "weight": self.weight.to_dict(),
            "quantity": self.quantity.to_dict(),
            "price": self.price.to_dict() if self.price is not None else None,
            "cost_basis": (
                self.cost_basis.to_dict()
                if self.cost_basis is not None
                else None
            ),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        price_data = data.get("price")
        cost_data = data.get("cost_basis")
        return cls(
            position_id=PositionId.from_dict(data["position_id"]),
            entity_id=EntityId.from_dict(data["entity_id"]),
            weight=Weight.from_dict(data["weight"]),
            quantity=Quantity.from_dict(data["quantity"]),
            price=Price.from_dict(price_data) if price_data is not None else None,
            cost_basis=(
                CostBasis.from_dict(cost_data)
                if cost_data is not None
                else None
            ),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class Portfolio:
    """A portfolio: an immutable collection of ``Position`` entities.

    M2 defines the ``Portfolio`` as an aggregate of positions. The portfolio
    itself carries a ``portfolio_id`` and a human-readable ``name``. The
    ``positions`` field is an immutable tuple — mutations return a new
    ``Portfolio`` (M2 does not provide mutators; M3+ may add them).

    Invariants enforced at construction:
    - ``positions`` is a tuple (defensive: a list is converted to a tuple).
    - No two positions share the same ``position_id``.
    - No two positions share the same ``entity_id`` (one position per entity).
    - The number of positions is bounded by ``_MAX_POSITIONS``.
    - ``name`` is non-empty, stripped, <= 256 chars.

    Derived properties (read-only):
    - ``allocation_total``: sum of ``position.weight.value`` across positions.
      M2 exposes this as a derived property; M3+ risk engine enforces the
      sum-to-1.0 (or target) invariant.
    - ``position_count``: ``len(self.positions)``.
    """

    portfolio_id: PortfolioId
    name: str
    positions: tuple[Position, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Normalize positions to a tuple (defensive — accept list input).
        if not isinstance(self.positions, tuple):
            positions_tuple = tuple(self.positions)
            # Use object.__setattr__ because the dataclass is frozen.
            object.__setattr__(self, "positions", positions_tuple)

        # Name invariant.
        if not isinstance(self.name, str):
            raise ValueError(
                f"Portfolio.name must be a str; got {type(self.name).__name__}"
            )
        stripped = self.name.strip()
        if not stripped:
            raise ValueError("Portfolio.name must be non-empty after stripping")
        if len(stripped) > 256:
            raise ValueError(
                f"Portfolio.name must be <= 256 chars after stripping; "
                f"got {len(stripped)}"
            )
        if stripped != self.name:
            object.__setattr__(self, "name", stripped)

        # Position-count cap.
        if len(self.positions) > _MAX_POSITIONS:
            raise ValueError(
                f"Portfolio.positions count {len(self.positions)} exceeds "
                f"_MAX_POSITIONS={_MAX_POSITIONS}"
            )

        # Uniqueness invariants.
        seen_position_ids: set[str] = set()
        seen_entity_ids: set[str] = set()
        for pos in self.positions:
            if not isinstance(pos, Position):
                raise ValueError(
                    f"Portfolio.positions must contain Position instances; "
                    f"got {type(pos).__name__}"
                )
            pid = pos.position_id.value
            if pid in seen_position_ids:
                raise ValueError(
                    f"Duplicate PositionId {pid!r} in Portfolio.positions"
                )
            seen_position_ids.add(pid)
            eid = pos.entity_id.value
            if eid in seen_entity_ids:
                raise ValueError(
                    f"Duplicate EntityId {eid!r} in Portfolio.positions "
                    f"(one position per entity per portfolio)"
                )
            seen_entity_ids.add(eid)

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def allocation_total(self) -> float:
        """Sum of ``position.weight.value`` across positions.

        This is a derived property, not a stored field. M2 exposes it for
        diagnostics; M3+ risk engine enforces the sum-to-1.0 (or target)
        invariant on top of this.
        """
        return sum(p.weight.value for p in self.positions)

    @property
    def position_count(self) -> int:
        """Number of positions in this portfolio."""
        return len(self.positions)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id.to_dict(),
            "name": self.name,
            "positions": tuple(p.to_dict() for p in self.positions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Portfolio":
        return cls(
            portfolio_id=PortfolioId.from_dict(data["portfolio_id"]),
            name=data["name"],
            positions=tuple(
                Position.from_dict(p) for p in data.get("positions", ())
            ),
        )


# ---------------------------------------------------------------------------
# Deterministic representation helper
# ---------------------------------------------------------------------------


def portfolio_repr(portfolio: Portfolio) -> str:
    """Return a deterministic, human-readable representation of a Portfolio.

    The representation is stable: the same ``Portfolio`` always produces the
    same string. Positions are listed in their tuple order (insertion order
    is preserved by the tuple). This is a module-level helper (not a method
    on ``Portfolio``) to keep the ``Portfolio`` dataclass minimal — M3+ may
    promote it to a method if the surface grows.
    """
    lines = [
        f"Portfolio(id={portfolio.portfolio_id.value!r}, "
        f"name={portfolio.name!r}, "
        f"positions={portfolio.position_count}, "
        f"allocation_total={portfolio.allocation_total:.6f})",
    ]
    for p in portfolio.positions:
        lines.append(
            f"  - {p.position_id.value} entity={p.entity_id.value} "
            f"weight={p.weight.value:.4f} qty={p.quantity.value}"
        )
    return "\n".join(lines)