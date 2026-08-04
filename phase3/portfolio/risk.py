"""Phase 5 M3 — Portfolio Risk Engine.

This module implements the portfolio risk metric layer for Phase 5 of the
Finance Intelligence Engine. It consumes the M2 portfolio domain model
(``phase3.portfolio.domain``) and produces deterministic, serializable risk
result DTOs.

M3-S1 scope (this file, first atomic work item)
----------------------------------------------

- ``ExposureReport`` — frozen dataclass result DTO for exposure metrics.
- ``compute_exposure(portfolio, market_prices=None) -> ExposureReport`` —
  aggregate exposure by entity / currency with gross / net / long / short
  decomposition. Pure function, stdlib-only, deterministic.

Deferred to M3-S2 / M3-S3 (NOT implemented in this slice)
---------------------------------------------------------

- ``CorrelationReport`` + ``compute_correlation()`` — M3-S3
- ``RiskBudgetReport`` + ``compute_risk_budget()`` — M3-S3
- ``DrawdownReport`` + ``compute_drawdown()`` — M3-S2
- ``ConcentrationReport`` + ``compute_concentration()`` — M3-S2

These are deliberately omitted from M3-S1 to keep the first atomic work item
small and reviewable. They will be added in their respective sub-milestones.

Boundary contract
-----------------

This module imports ONLY from ``phase3.portfolio.domain`` and the Python
standard library. It MUST NOT import ``sqlite3``, ``macro_history``,
``phase3.pipeline.*``, ``phase3.datamodel.*``, ``phase3.graph.*``, or any
broker / network / I/O module. The TD7 safety guards
(``tests/phase3/test_portfolio_safety_guards.py``) enforce this at test
time via AST scan and file-level sha256 baseline.

Deterministic computation contract
-----------------------------------

- ``compute_exposure()`` is a pure function: no ``datetime.now()``, no
  ``random.*``, no I/O, no global mutable state.
- Same inputs produce byte-identical outputs (modulo floating-point edge
  cases pinned via ``math.isclose`` in tests).
- ``ExposureReport`` is ``@dataclass(frozen=True)`` — immutable, hashable.
- ``to_dict()`` produces ``dict[str, Any]`` with fixed key order.
- ``from_dict(to_dict(x)).to_dict() == to_dict(x)`` — byte-identical
  round-trip.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from phase3.portfolio.domain import Portfolio, Position


# --------------------------------------------------------------------------- #
# Result DTO: ExposureReport
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExposureReport:
    """Result DTO for ``compute_exposure()``.

    Fields are weight-based fractions in ``[0.0, 1.0]`` (long-only domain
    model in M2 — short exposure is always 0.0). Notional ($) exposure based
    on ``market_prices`` is a future extension; M3-S1 ships weight-based
    decomposition only.

    Fields
    ------
    portfolio_id : str
        The portfolio's ``PortfolioId.value`` (flattened for serialization).
    gross_exposure : float
        Sum of all position weights. Equals ``allocation_total`` on the
        portfolio. For a fully-invested long-only portfolio this is 1.0.
    net_exposure : float
        Long exposure minus short exposure. In the M2 long-only domain this
        equals ``gross_exposure``.
    long_exposure : float
        Sum of long-position weights. In M2 all positions are long, so this
        equals ``gross_exposure``.
    short_exposure : float
        Sum of short-position weights. M2 has no short positions, so this is
        always 0.0 in M3-S1.
    by_entity : dict[str, float]
        Entity ID → aggregate weight. Sums to ``gross_exposure``.
    by_currency : dict[str, float]
        ISO 4217 currency code → aggregate weight. Positions without a
        ``Price`` are bucketed under the key ``"UNKNOWN"``.
    position_count : int
        Number of positions in the portfolio.
    """

    portfolio_id: str
    gross_exposure: float
    net_exposure: float
    long_exposure: float
    short_exposure: float
    by_entity: dict[str, float] = field(default_factory=dict)
    by_currency: dict[str, float] = field(default_factory=dict)
    position_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict with fixed key order.

        Round-trip invariant: ``from_dict(to_dict(x)).to_dict() == to_dict(x)``.
        """
        return {
            "portfolio_id": self.portfolio_id,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "long_exposure": self.long_exposure,
            "short_exposure": self.short_exposure,
            "by_entity": dict(self.by_entity),
            "by_currency": dict(self.by_currency),
            "position_count": self.position_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExposureReport":
        """Reconstruct from a ``to_dict()`` output. Round-trip inverse."""
        return cls(
            portfolio_id=data["portfolio_id"],
            gross_exposure=float(data["gross_exposure"]),
            net_exposure=float(data["net_exposure"]),
            long_exposure=float(data["long_exposure"]),
            short_exposure=float(data["short_exposure"]),
            by_entity=dict(data.get("by_entity", {})),
            by_currency=dict(data.get("by_currency", {})),
            position_count=int(data["position_count"]),
        )


# --------------------------------------------------------------------------- #
# Input validation helpers
# --------------------------------------------------------------------------- #


def _is_finite(value: float) -> bool:
    """Return True if value is a finite real number (not NaN, not inf)."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return not (math.isnan(value) or math.isinf(value))


def _validate_market_prices(
    market_prices: Optional[dict[str, float]],
    entity_ids: frozenset[str],
) -> None:
    """Validate optional ``market_prices`` input.

    Rules (per kickoff plan §4.6):
    - Keys must be a subset of the portfolio's entity IDs.
    - Values must be >= 0.0 and finite (no NaN / inf).

    Raises ``ValueError`` on violation.
    """
    if market_prices is None:
        return
    if not isinstance(market_prices, dict):
        raise ValueError(
            f"market_prices must be a dict[str, float]; "
            f"got {type(market_prices).__name__}"
        )
    for key, value in market_prices.items():
        if not isinstance(key, str):
            raise ValueError(
                f"market_prices key must be str; got {type(key).__name__}"
            )
        if key not in entity_ids:
            raise ValueError(
                f"market_prices key {key!r} is not a portfolio entity_id"
            )
        if not _is_finite(value):
            raise ValueError(
                f"market_prices[{key!r}] must be finite; got {value!r}"
            )
        if value < 0.0:
            raise ValueError(
                f"market_prices[{key!r}] must be >= 0.0; got {value!r}"
            )


# --------------------------------------------------------------------------- #
# Risk metric: compute_exposure
# --------------------------------------------------------------------------- #


def compute_exposure(
    portfolio: Portfolio,
    market_prices: Optional[dict[str, float]] = None,
) -> ExposureReport:
    """Compute weight-based exposure decomposition for a portfolio.

    Parameters
    ----------
    portfolio : Portfolio
        The portfolio to analyze (from ``phase3.portfolio.domain``).
    market_prices : dict[str, float], optional
        Entity ID → current market price. If provided, keys must be a
        subset of the portfolio's entity IDs and values must be finite and
        non-negative. M3-S1 validates this input but does not yet use it
        for notional ($) exposure computation; notional exposure is a
        future extension.

    Returns
    -------
    ExposureReport
        Weight-based exposure decomposition by entity and currency.

    Raises
    ------
    ValueError
        If ``market_prices`` fails validation (keys not in entity IDs,
        values NaN/inf/negative).

    Notes
    -----
    - Pure function: no I/O, no datetime, no randomness, no global state.
    - For an empty portfolio, returns an ``ExposureReport`` with all-zero
      exposures, empty ``by_entity`` / ``by_currency``, and
      ``position_count=0``.
    - M2 domain model is long-only: ``short_exposure`` is always 0.0 and
      ``net_exposure`` equals ``gross_exposure``.
    """
    # Collect entity IDs up front for market_prices validation.
    entity_ids = frozenset(p.entity_id.value for p in portfolio.positions)
    _validate_market_prices(market_prices, entity_ids)

    gross = 0.0
    long_exp = 0.0
    short_exp = 0.0
    by_entity: dict[str, float] = {}
    by_currency: dict[str, float] = {}

    for pos in portfolio.positions:
        w = pos.weight.value
        gross += w
        # M2 is long-only: every position is long.
        long_exp += w
        # short_exp stays 0.0

        eid = pos.entity_id.value
        by_entity[eid] = by_entity.get(eid, 0.0) + w

        if pos.price is not None:
            ccy = pos.price.currency
        else:
            ccy = "UNKNOWN"
        by_currency[ccy] = by_currency.get(ccy, 0.0) + w

    return ExposureReport(
        portfolio_id=portfolio.portfolio_id.value,
        gross_exposure=gross,
        net_exposure=gross - short_exp,
        long_exposure=long_exp,
        short_exposure=short_exp,
        by_entity=by_entity,
        by_currency=by_currency,
        position_count=portfolio.position_count,
    )