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


# --------------------------------------------------------------------------- #
# Result DTO: ConcentrationReport (M3-S2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConcentrationReport:
    """Result DTO for ``compute_concentration()``.

    Fields
    ------
    portfolio_id : str
        The portfolio's ``PortfolioId.value`` (flattened for serialization).
    hhi : float
        Herfindahl-Hirschman Index on raw position weights:
        ``HHI = sum(w_i ** 2)``. Range ``[1/N, 1.0]`` for equal-weight to
        single-position. Higher = more concentrated. Computed on raw
        weights; NOT normalized — if weights do not sum to 1.0, the HHI
        reflects that (documented behavior, not an error).
    top_n_concentration : float
        Sum of the top-N position weights (descending). Range ``[0.0, 1.0]``
        when weights sum to 1.0; otherwise the raw sum of the top-N weights.
    top_n : int
        The effective N used (after clamping to ``position_count``).
    effective_position_count : float
        Inverse HHI: ``1 / HHI``. Range ``[1.0, N]`` for single-position to
        equal-weight. ``0.0`` when ``HHI == 0.0`` (empty portfolio guard).
    position_count : int
        Number of positions in the portfolio.
    top_entity_concentration : Optional[float]
        Sum of the top-N entity-level aggregate weights. ``None`` when
        entity-level aggregation equals position-level (one position per
        entity, which is the M2 invariant). When multiple positions share
        an entity (relaxed in future milestones), this differs from
        ``top_n_concentration``. In M3-S2 (one position per entity), this
        is always ``None``.
    by_entity : dict[str, float]
        Entity ID → aggregate weight. Sums to ``allocation_total``.
    """

    portfolio_id: str
    hhi: float
    top_n_concentration: float
    top_n: int
    effective_position_count: float
    position_count: int
    top_entity_concentration: Optional[float] = None
    by_entity: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict with fixed key order.

        Round-trip invariant: ``from_dict(to_dict(x)).to_dict() == to_dict(x)``.
        """
        return {
            "portfolio_id": self.portfolio_id,
            "hhi": self.hhi,
            "top_n_concentration": self.top_n_concentration,
            "top_n": self.top_n,
            "effective_position_count": self.effective_position_count,
            "position_count": self.position_count,
            "top_entity_concentration": self.top_entity_concentration,
            "by_entity": dict(self.by_entity),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConcentrationReport":
        """Reconstruct from a ``to_dict()`` output. Round-trip inverse."""
        return cls(
            portfolio_id=data["portfolio_id"],
            hhi=float(data["hhi"]),
            top_n_concentration=float(data["top_n_concentration"]),
            top_n=int(data["top_n"]),
            effective_position_count=float(data["effective_position_count"]),
            position_count=int(data["position_count"]),
            top_entity_concentration=(
                None if data.get("top_entity_concentration") is None
                else float(data["top_entity_concentration"])
            ),
            by_entity=dict(data.get("by_entity", {})),
        )


# --------------------------------------------------------------------------- #
# Result DTO: DrawdownReport (M3-S2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DrawdownReport:
    """Result DTO for ``compute_drawdown()``.

    Fields
    ------
    max_drawdown : float
        Maximum peak-to-trough drawdown fraction. Range ``[0.0, 1.0]``.
        ``0.0`` for a monotonically-increasing or flat series.
    average_drawdown : float
        Arithmetic mean of the per-period drawdown fractions. Range
        ``[0.0, 1.0]``. ``0.0`` for a monotonic or flat series.
    drawdown_duration : int
        Longest consecutive run of periods where ``drawdown_i > 0.0``.
        ``0`` for a monotonic or flat series. Strict ``> 0.0`` comparison
        (a drawdown of exactly 0.0 at a point breaks the run).
    peak_index : int
        Index of the running peak immediately preceding the max-drawdown
        trough. ``0`` when there is no drawdown. Diagnostic.
    trough_index : int
        Index of the max-drawdown trough. ``0`` when there is no drawdown.
        Diagnostic.
    drawdown_series : tuple[float, ...]
        Per-period drawdown fractions in ``[0.0, 1.0]``, length
        ``series_length``. Frozen tuple (immutable); ``to_dict()``
        serializes as ``list[float]``, ``from_dict()`` converts back to
        ``tuple``.
    series_length : int
        Number of points in the input value series.
    """

    max_drawdown: float
    average_drawdown: float
    drawdown_duration: int
    peak_index: int
    trough_index: int
    drawdown_series: tuple[float, ...]
    series_length: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict with fixed key order.

        ``drawdown_series`` (tuple) is serialized as ``list[float]``.
        Round-trip invariant: ``from_dict(to_dict(x)).to_dict() == to_dict(x)``.
        """
        return {
            "max_drawdown": self.max_drawdown,
            "average_drawdown": self.average_drawdown,
            "drawdown_duration": self.drawdown_duration,
            "peak_index": self.peak_index,
            "trough_index": self.trough_index,
            "drawdown_series": list(self.drawdown_series),
            "series_length": self.series_length,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DrawdownReport":
        """Reconstruct from a ``to_dict()`` output. Round-trip inverse.

        ``drawdown_series`` (list in the dict) is converted back to a
        frozen ``tuple[float, ...]``.
        """
        return cls(
            max_drawdown=float(data["max_drawdown"]),
            average_drawdown=float(data["average_drawdown"]),
            drawdown_duration=int(data["drawdown_duration"]),
            peak_index=int(data["peak_index"]),
            trough_index=int(data["trough_index"]),
            drawdown_series=tuple(float(x) for x in data["drawdown_series"]),
            series_length=int(data["series_length"]),
        )


# --------------------------------------------------------------------------- #
# Input validation helper (M3-S2)
# --------------------------------------------------------------------------- #


def _validate_value_series(series: list[float], min_length: int) -> None:
    """Validate a portfolio value series for ``compute_drawdown()``.

    Rules
    -----
    - ``series`` must be a ``list`` (not a tuple, not a generator).
    - ``len(series) >= min_length``.
    - Every value must be a finite real number (no NaN, no inf) and
      ``>= 0.0``.

    Raises ``ValueError`` on violation.
    """
    if not isinstance(series, list):
        raise ValueError(
            f"series must be a list[float]; got {type(series).__name__}"
        )
    if len(series) < min_length:
        raise ValueError(
            f"series must have length >= {min_length}; got {len(series)}"
        )
    for i, v in enumerate(series):
        if not _is_finite(v):
            raise ValueError(
                f"series[{i}] must be finite; got {v!r}"
            )
        if v < 0.0:
            raise ValueError(
                f"series[{i}] must be >= 0.0; got {v!r}"
            )


# --------------------------------------------------------------------------- #
# Risk metric: compute_concentration (M3-S2)
# --------------------------------------------------------------------------- #


def compute_concentration(
    portfolio: Portfolio,
    top_n: int = 5,
) -> ConcentrationReport:
    """Compute concentration metrics for a portfolio.

    Parameters
    ----------
    portfolio : Portfolio
        The portfolio to analyze (from ``phase3.portfolio.domain``).
    top_n : int, optional
        Number of top positions to include in the top-N concentration
        ratio. Default 5. Must be ``>= 1``. If ``top_n > position_count``,
        it is clamped to ``position_count`` (documented, not an error).

    Returns
    -------
    ConcentrationReport
        HHI + top-N + effective N + entity aggregation.

    Raises
    ------
    ValueError
        If ``top_n < 1``.

    Notes
    -----
    - Pure function: no I/O, no datetime, no randomness, no global state.
    - HHI is computed on raw weights, NOT normalized. If weights do not
      sum to 1.0, the HHI reflects that (documented behavior).
    - For an empty portfolio, returns a ``ConcentrationReport`` with
      ``hhi=0.0``, ``top_n_concentration=0.0``,
      ``effective_position_count=0.0``, ``position_count=0``, empty
      ``by_entity``, and ``top_entity_concentration=None``.
    - The M2 domain enforces one position per entity, so
      ``top_entity_concentration`` is always ``None`` in M3-S2 (entity
      aggregation equals position aggregation). It is reserved for
      future milestones that relax the one-position-per-entity rule.
    """
    if not isinstance(top_n, int) or isinstance(top_n, bool):
        raise ValueError(
            f"top_n must be an int; got {type(top_n).__name__}"
        )
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1; got {top_n}")

    weights = [p.weight.value for p in portfolio.positions]
    position_count = len(weights)

    # HHI on raw weights.
    hhi = sum(w * w for w in weights)

    # Effective N: 1/HHI with empty-portfolio guard.
    if hhi == 0.0:
        effective_n = 0.0
    else:
        effective_n = 1.0 / hhi

    # Top-N concentration: sum of top-N weights (clamped to position_count).
    effective_top_n = min(top_n, position_count)
    sorted_desc = sorted(weights, reverse=True)
    top_n_concentration = sum(sorted_desc[:effective_top_n])

    # Entity-level aggregation (one position per entity in M2 → equals
    # position-level; top_entity_concentration stays None).
    by_entity: dict[str, float] = {}
    for p in portfolio.positions:
        eid = p.entity_id.value
        by_entity[eid] = by_entity.get(eid, 0.0) + p.weight.value
    # In M2 (one position per entity), len(by_entity) == position_count.
    # top_entity_concentration is set only when entity aggregation differs.
    top_entity_concentration: Optional[float] = None
    if len(by_entity) != position_count:
        entity_weights_sorted = sorted(by_entity.values(), reverse=True)
        top_entity_concentration = sum(
            entity_weights_sorted[:effective_top_n]
        )

    return ConcentrationReport(
        portfolio_id=portfolio.portfolio_id.value,
        hhi=hhi,
        top_n_concentration=top_n_concentration,
        top_n=effective_top_n,
        effective_position_count=effective_n,
        position_count=position_count,
        top_entity_concentration=top_entity_concentration,
        by_entity=by_entity,
    )


# --------------------------------------------------------------------------- #
# Risk metric: compute_drawdown (M3-S2)
# --------------------------------------------------------------------------- #


def compute_drawdown(
    portfolio_value_series: list[float],
) -> DrawdownReport:
    """Compute drawdown metrics from a portfolio value series.

    Parameters
    ----------
    portfolio_value_series : list[float]
        Chronological mark-to-market portfolio values. Must be a list of
        length ``>= 2``, with all values finite and ``>= 0.0``.

    Returns
    -------
    DrawdownReport
        Max/average drawdown, drawdown duration, peak/trough indices,
        and the full drawdown series.

    Raises
    ------
    ValueError
        If the series is not a list, has length < 2, or contains a
        non-finite or negative value.

    Notes
    -----
    - Pure function: no I/O, no datetime, no randomness, no global state.
    - Drawdown fraction at point ``i``: ``(peak_i - value_i) / peak_i``
      where ``peak_i = max(value[0..i])``.
    - Division-by-zero guard: if ``peak_i == 0.0`` (all-zero prefix),
      the drawdown fraction is defined as ``0.0`` (not NaN/inf).
    - Drawdown duration: longest consecutive run where
      ``drawdown_i > 0.0`` (strict comparison). For a monotonic or flat
      series, duration = 0.
    - ``peak_index`` is the index of the running peak immediately
      preceding the max-drawdown trough; ``trough_index`` is the index of
      the max-drawdown trough. Both are 0 when there is no drawdown.
    """
    _validate_value_series(portfolio_value_series, min_length=2)

    n = len(portfolio_value_series)
    drawdown_series: list[float] = [0.0] * n
    peak = portfolio_value_series[0]
    peak_index = 0
    max_dd = 0.0
    max_dd_trough_index = 0
    max_dd_peak_index = 0

    for i in range(n):
        v = portfolio_value_series[i]
        if v > peak:
            peak = v
            peak_index = i
        if peak == 0.0:
            dd = 0.0
        else:
            dd = (peak - v) / peak
        drawdown_series[i] = dd
        if dd > max_dd:
            max_dd = dd
            max_dd_trough_index = i
            max_dd_peak_index = peak_index

    average_dd = sum(drawdown_series) / n if n > 0 else 0.0

    # Drawdown duration: longest consecutive run where dd > 0.0.
    longest = 0
    current = 0
    for dd in drawdown_series:
        if dd > 0.0:
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0

    return DrawdownReport(
        max_drawdown=max_dd,
        average_drawdown=average_dd,
        drawdown_duration=longest,
        peak_index=max_dd_peak_index,
        trough_index=max_dd_trough_index,
        drawdown_series=tuple(drawdown_series),
        series_length=n,
    )