"""Phase 5 M4 — Allocation DTO.

This module defines the ``Allocation`` aggregate that M2 explicitly excluded
(see ``phase3/portfolio/__init__.py`` module docstring + SSOT §36.1). An
``Allocation`` wraps a tuple of target ``Position`` entities plus a
``target_total`` (the intended sum of weights, default 1.0) and provides
deterministic ``to_dict()`` / ``from_dict()`` round-trip serialization.

M4-S1 scope: DTO + invariants + serialization ONLY. The allocation
*optimization logic* (score-weighted policy, risk-aware policy, constraint
enforcement) is deferred to M4-S2 / M4-S3 and lives in ``decision.py``.

Design principles (mirrors M2 ``domain.py``)
--------------------------------------------

1. **Immutable.** ``Allocation`` is ``@dataclass(frozen=True)``; the
   ``positions`` field is a tuple. Mutations return a new ``Allocation``.

2. **Composition, not inheritance.** ``Allocation`` holds ``Position``
   entities from ``phase3.portfolio.domain`` — it does NOT subclass them.

3. **Deterministic serialization.** ``to_dict()`` returns a
   JSON-serializable ``dict[str, Any]`` with keys emitted in a fixed,
   code-defined order. ``from_dict(to_dict(x)).to_dict() == to_dict(x)``
   (byte-identical round-trip, AG-M4-8).

4. **No floats as weights except via ``Weight``.** Target weights are
   expressed as ``Position.weight`` (a ``Weight`` value object in
   ``[0.0, 1.0]``), reusing the M2 invariant.

5. **Sum-to-target invariant.** ``Allocation.total_weight`` (derived
   property) must equal ``target_total`` within floating-point tolerance
   (``math.isclose(rel_tol=1e-9, abs_tol=1e-12)``). The empty allocation
   (``positions=()``) is permitted with ``total_weight == 0.0``; in that
   case ``target_total`` must also be ``0.0`` (or the allocation is
   vacuously consistent).

Invariants (enforced at construction)
--------------------------------------

- ``allocation_id`` matches the ``_IDENTIFIER_PATTERN`` (non-empty, no
  whitespace, <= 128 chars) — same rule as M2 ``PortfolioId``.
- ``positions`` is a tuple of ``Position`` instances (defensive: a list is
  converted to a tuple). No two positions share the same ``position_id``.
- ``target_total`` is a finite ``float`` in ``[0.0, 1.0]``.
- ``total_weight`` (sum of ``position.weight.value``) is within tolerance of
  ``target_total``. An empty allocation (``positions=()``) requires
  ``target_total == 0.0``.

Boundary contract
-------------------

This module imports ONLY from the Python standard library and from
``phase3.portfolio.domain`` (the M2 layer). It MUST NOT import ``sqlite3``,
``macro_history``, ``phase3.pipeline``, ``phase3.datamodel``,
``phase3.graph``, or any broker / network module (AST-enforced by TD8,
``tests/phase3/test_portfolio_decision_safety_guards.py``).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from phase3.portfolio.domain import Position, PositionId

# --------------------------------------------------------------------------- #
# Module constants
# --------------------------------------------------------------------------- #

# Identifier pattern — identical rule to M2 domain.py (non-empty, no
# whitespace, <= 128 chars). Deliberately duplicated rather than imported
# to keep the boundary contract self-contained (M4 allocation must not
# reach into M2 private helpers).
_IDENTIFIER_PATTERN = re.compile(r"^[^\s]{1,128}$")

# Floating-point tolerance for weight comparisons (pinned by §5.3 of the
# M4 kickoff plan). Tests document this tolerance in their docstrings.
_FP_REL_TOL = 1e-9
_FP_ABS_TOL = 1e-12


# --------------------------------------------------------------------------- #
# Allocation aggregate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Allocation:
    """Target allocation: an immutable tuple of ``Position`` entities plus
    a ``target_total``.

    An ``Allocation`` represents the *target* state of a portfolio after a
    decision has been made. It is distinct from a ``Portfolio`` (current
    state) — the delta between the two is the rebalancing signal (computed
    by M5 execution, not M4).

    Construction enforces:

    - ``allocation_id`` is a non-empty, whitespace-free string <= 128 chars.
    - ``positions`` is a tuple of ``Position`` instances (a list is
      defensively converted to a tuple). No duplicate ``position_id``.
    - ``target_total`` is a finite float in ``[0.0, 1.0]``.
    - The sum of ``position.weight.value`` across ``positions`` is within
      ``math.isclose`` tolerance (``rel_tol=1e-9, abs_tol=1e-12``) of
      ``target_total``. The empty allocation (``positions=()``) requires
      ``target_total == 0.0``.

    ``metadata`` is an arbitrary JSON-serializable dict (mirrors M2
    ``Position.metadata``). ``to_dict()`` will fail at serialization time
    if non-JSON values are present — intentional, to keep construction
    cheap.
    """

    allocation_id: str
    positions: tuple[Position, ...] = field(default_factory=tuple)
    target_total: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # allocation_id invariant.
        if not isinstance(self.allocation_id, str):
            raise ValueError(
                f"Allocation.allocation_id must be a str; "
                f"got {type(self.allocation_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.allocation_id):
            raise ValueError(
                f"Allocation.allocation_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.allocation_id!r}"
            )

        # Normalize positions to a tuple (defensive — accept list input).
        if not isinstance(self.positions, tuple):
            positions_tuple = tuple(self.positions)
            object.__setattr__(self, "positions", positions_tuple)

        # target_total invariant.
        if not isinstance(self.target_total, (int, float)):
            raise ValueError(
                f"Allocation.target_total must be a float; "
                f"got {type(self.target_total).__name__}"
            )
        target_total_f = float(self.target_total)
        if target_total_f != self.target_total:
            object.__setattr__(self, "target_total", target_total_f)
        if not math.isfinite(target_total_f):
            raise ValueError(
                f"Allocation.target_total must be finite; "
                f"got {self.target_total!r}"
            )
        if not (0.0 <= target_total_f <= 1.0):
            raise ValueError(
                f"Allocation.target_total must be in [0.0, 1.0]; "
                f"got {target_total_f!r}"
            )

        # Position type + uniqueness invariants.
        seen_position_ids: set[str] = set()
        for pos in self.positions:
            if not isinstance(pos, Position):
                raise ValueError(
                    f"Allocation.positions must contain Position "
                    f"instances; got {type(pos).__name__}"
                )
            pid = pos.position_id.value
            if pid in seen_position_ids:
                raise ValueError(
                    f"Duplicate PositionId {pid!r} in Allocation.positions"
                )
            seen_position_ids.add(pid)

        # Sum-to-target invariant (with FP tolerance).
        total_weight = self.total_weight
        if not math.isclose(
            total_weight,
            target_total_f,
            rel_tol=_FP_REL_TOL,
            abs_tol=_FP_ABS_TOL,
        ):
            raise ValueError(
                f"Allocation total_weight {total_weight!r} does not match "
                f"target_total {target_total_f!r} within tolerance "
                f"(rel_tol={_FP_REL_TOL}, abs_tol={_FP_ABS_TOL})"
            )

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #

    @property
    def total_weight(self) -> float:
        """Sum of ``position.weight.value`` across positions.

        Derived property, not stored. For the empty allocation this is
        ``0.0``; ``__post_init__`` ensures ``target_total == 0.0`` in that
        case so the sum-to-target invariant holds.
        """
        return sum(p.weight.value for p in self.positions)

    @property
    def position_count(self) -> int:
        """Number of positions in this allocation."""
        return len(self.positions)

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict.

        Keys are emitted in a fixed, code-defined order so that
        ``from_dict(to_dict(x)).to_dict() == to_dict(x)`` (byte-identical
        round-trip, AG-M4-8).
        """
        return {
            "allocation_id": self.allocation_id,
            "positions": tuple(p.to_dict() for p in self.positions),
            "target_total": self.target_total,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Allocation":
        """Reconstruct an ``Allocation`` from its ``to_dict()`` output.

        The inverse of ``to_dict()``. Accepts ``positions`` as a list or
        tuple (defensive normalization happens in ``__post_init__``).
        """
        raw_positions = data.get("positions", ())
        if isinstance(raw_positions, (list, tuple)):
            positions_tuple = tuple(
                Position.from_dict(p) for p in raw_positions
            )
        else:
            positions_tuple = ()
        return cls(
            allocation_id=data["allocation_id"],
            positions=positions_tuple,
            target_total=float(data.get("target_total", 1.0)),
            metadata=dict(data.get("metadata", {})),
        )


__all__ = ["Allocation"]