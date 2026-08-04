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

# --------------------------------------------------------------------------- #
# Result DTO: CorrelationReport (M3-S3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CorrelationReport:
    """Result DTO for ``compute_correlation()``.

    Fields
    ------
    portfolio_id : str
        The portfolio's ``PortfolioId.value`` (flattened for serialization).
    correlation_matrix : dict[str, dict[str, float]]
        Pairwise Pearson correlation matrix, keyed by entity_id → entity_id →
        correlation coefficient. Symmetric (``corr(i,j) == corr(j,i)``) with
        diagonal entries equal to 1.0 (when the portfolio has positions and
        returns are provided). Range ``[-1.0, 1.0]`` per off-diagonal pair.
        Empty dict for an empty portfolio.
    weighted_average_correlation : float
        Portfolio-weighted average of the off-diagonal pairwise correlations,
        weighted by the product of the two positions' weights
        (``w_i * w_j`` for pair ``(i, j)``). Range ``[-1.0, 1.0]``. ``0.0`` for
        an empty or single-entity portfolio (no off-diagonal pairs).
    average_pairwise_correlation : float
        Unweighted arithmetic mean of the off-diagonal pairwise correlations.
        Range ``[-1.0, 1.0]``. ``0.0`` for an empty or single-entity portfolio.
    pair_count : int
        Number of off-diagonal pairs (``N*(N-1)/2`` for ``N`` entities).
        ``0`` for empty or single-entity portfolios.
    entity_count : int
        Number of entities in the portfolio (equals ``position_count`` in
        the M2 long-only one-position-per-entity domain).
    """

    portfolio_id: str
    correlation_matrix: dict[str, dict[str, float]] = field(default_factory=dict)
    weighted_average_correlation: float = 0.0
    average_pairwise_correlation: float = 0.0
    pair_count: int = 0
    entity_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict with fixed key order.

        The nested ``correlation_matrix`` (``dict[str, dict[str, float]]``)
        is preserved as a nested dict structure. Round-trip invariant:
        ``from_dict(to_dict(x)).to_dict() == to_dict(x)``.
        """
        return {
            "portfolio_id": self.portfolio_id,
            "correlation_matrix": {
                k: dict(v) for k, v in self.correlation_matrix.items()
            },
            "weighted_average_correlation": self.weighted_average_correlation,
            "average_pairwise_correlation": self.average_pairwise_correlation,
            "pair_count": self.pair_count,
            "entity_count": self.entity_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CorrelationReport":
        """Reconstruct from a ``to_dict()`` output. Round-trip inverse.

        The nested ``correlation_matrix`` is reconstructed as a
        ``dict[str, dict[str, float]]``.
        """
        return cls(
            portfolio_id=data["portfolio_id"],
            correlation_matrix={
                k: dict(v) for k, v in data.get("correlation_matrix", {}).items()
            },
            weighted_average_correlation=float(
                data.get("weighted_average_correlation", 0.0)
            ),
            average_pairwise_correlation=float(
                data.get("average_pairwise_correlation", 0.0)
            ),
            pair_count=int(data.get("pair_count", 0)),
            entity_count=int(data.get("entity_count", 0)),
        )


# --------------------------------------------------------------------------- #
# Result DTO: RiskBudgetReport (M3-S3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RiskBudgetReport:
    """Result DTO for ``compute_risk_budget()``.

    Implements the variance-contribution risk budget method: for position
    ``i`` with weight ``w_i`` and covariance matrix ``Cov``:

    - Marginal risk contribution: ``MRC_i = sum_j(w_j * Cov(i,j))``
    - Risk contribution: ``RC_i = w_i * MRC_i``
    - Total portfolio variance: ``sigma2_p = w^T . Cov . w``
    - Relative risk contribution: ``RRC_i = RC_i / sigma2_p`` (when
      ``sigma2_p > 0``)

    Fields
    ------
    portfolio_id : str
        The portfolio's ``PortfolioId.value`` (flattened for serialization).
    total_variance : float
        Portfolio variance ``sigma2_p = w^T . Cov . w``. ``>= 0.0``. Guarded
        to be non-negative (``max(0.0, computed)``) to absorb tiny FP
        rounding on near-singular covariance. ``0.0`` for an empty portfolio
        or an all-zero covariance matrix.
    total_volatility : float
        Portfolio standard deviation ``sqrt(total_variance)``. ``>= 0.0``.
        ``0.0`` when ``total_variance == 0.0``.
    risk_contributions : dict[str, float]
        Entity ID → absolute risk contribution ``RC_i``. Empty for an empty
        portfolio.
    relative_risk_contributions : dict[str, float]
        Entity ID → relative risk contribution ``RRC_i``. When
        ``total_variance > 0``, ``sum(RRC_i) == 1.0``. When
        ``total_variance == 0`` (division-by-zero guard), all ``RRC_i = 0.0``.
    total_risk_budget_utilization : float
        ``sum(RRC_i)``. Equals ``1.0`` when ``total_variance > 0`` and
        ``0.0`` when ``total_variance == 0``.
    per_position_budget : float
        Caller-supplied per-position budget threshold (default ``1.0``).
        Documented: this is a measurement, not an enforcement — M3 is
        advisory-only (M4 enforces).
    budget_breach : bool
        ``True`` if any ``RRC_i > per_position_budget``. With the default
        threshold ``1.0``, this is ``False`` for any valid long-only
        positive-semi-definite covariance (``RRC_i in [0, 1]``).
    entity_count : int
        Number of entities in the portfolio (equals ``position_count`` in
        the M2 long-only one-position-per-entity domain).
    """

    portfolio_id: str
    total_variance: float = 0.0
    total_volatility: float = 0.0
    risk_contributions: dict[str, float] = field(default_factory=dict)
    relative_risk_contributions: dict[str, float] = field(default_factory=dict)
    total_risk_budget_utilization: float = 0.0
    per_position_budget: float = 1.0
    budget_breach: bool = False
    entity_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict with fixed key order.

        Round-trip invariant:
        ``from_dict(to_dict(x)).to_dict() == to_dict(x)``.
        """
        return {
            "portfolio_id": self.portfolio_id,
            "total_variance": self.total_variance,
            "total_volatility": self.total_volatility,
            "risk_contributions": dict(self.risk_contributions),
            "relative_risk_contributions": dict(self.relative_risk_contributions),
            "total_risk_budget_utilization": self.total_risk_budget_utilization,
            "per_position_budget": self.per_position_budget,
            "budget_breach": self.budget_breach,
            "entity_count": self.entity_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskBudgetReport":
        """Reconstruct from a ``to_dict()`` output. Round-trip inverse."""
        return cls(
            portfolio_id=data["portfolio_id"],
            total_variance=float(data["total_variance"]),
            total_volatility=float(data["total_volatility"]),
            risk_contributions=dict(data.get("risk_contributions", {})),
            relative_risk_contributions=dict(
                data.get("relative_risk_contributions", {})
            ),
            total_risk_budget_utilization=float(
                data["total_risk_budget_utilization"]
            ),
            per_position_budget=float(data["per_position_budget"]),
            budget_breach=bool(data["budget_breach"]),
            entity_count=int(data["entity_count"]),
        )


# --------------------------------------------------------------------------- #
# Input validation helpers (M3-S3)
# --------------------------------------------------------------------------- #


def _pearson_correlation(x: list[float], y: list[float]) -> float:
    """Compute the Pearson correlation coefficient between two series.

    Formula: ``cov(x, y) / (sigma_x * sigma_y)``.

    Guards
    ------
    - If either series has zero variance (constant series), the correlation
      is defined as ``0.0`` (not NaN). This is the documented guard for
      RR-S3-1: zero-variance series must not produce NaN.
    - Series must be equal-length (caller responsibility — validated by
      ``_validate_returns_matrix`` before this helper is called).
    """
    n = len(x)
    if n == 0:
        return 0.0
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = 0.0
    var_x = 0.0
    var_y = 0.0
    for i in range(n):
        dx = x[i] - mean_x
        dy = y[i] - mean_y
        cov += dx * dy
        var_x += dx * dx
        var_y += dy * dy
    if var_x == 0.0 or var_y == 0.0:
        # RR-S3-1: zero-variance series → correlation = 0.0, not NaN.
        return 0.0
    denom = math.sqrt(var_x * var_y)
    if denom == 0.0:
        return 0.0
    return cov / denom


def _validate_returns_matrix(
    returns_matrix: Any,
    entity_ids: frozenset[str],
) -> None:
    """Validate the ``returns_matrix`` input for ``compute_correlation()``.

    Rules (per kickoff plan §4.6):
    - Must be a ``dict[str, list[float]]``.
    - Keys must cover ALL portfolio entity IDs (no missing, no extra).
    - All lists must have equal length ``>= 2``.
    - All values must be finite (no NaN / inf).

    Raises ``ValueError`` on violation.
    """
    if not isinstance(returns_matrix, dict):
        raise ValueError(
            f"returns_matrix must be a dict[str, list[float]]; "
            f"got {type(returns_matrix).__name__}"
        )
    # Coverage check: every portfolio entity must be present.
    missing = entity_ids - set(returns_matrix.keys())
    if missing:
        raise ValueError(
            f"returns_matrix is missing entity IDs: {sorted(missing)}"
        )
    extra = set(returns_matrix.keys()) - entity_ids
    if extra:
        raise ValueError(
            f"returns_matrix contains unknown entity IDs: {sorted(extra)}"
        )
    if not returns_matrix:
        return  # empty portfolio → empty matrix; nothing else to check
    # Determine the reference length from the first entry.
    reference_len = None
    for key, series in returns_matrix.items():
        if not isinstance(series, list):
            raise ValueError(
                f"returns_matrix[{key!r}] must be a list[float]; "
                f"got {type(series).__name__}"
            )
        if reference_len is None:
            reference_len = len(series)
            if reference_len < 2:
                raise ValueError(
                    f"returns_matrix[{key!r}] must have length >= 2; "
                    f"got {reference_len}"
                )
        else:
            if len(series) != reference_len:
                raise ValueError(
                    f"returns_matrix[{key!r}] has length {len(series)}; "
                    f"expected {reference_len} (all lists must be equal length)"
                )
        for i, v in enumerate(series):
            if not _is_finite(v):
                raise ValueError(
                    f"returns_matrix[{key!r}][{i}] must be finite; got {v!r}"
                )


def _validate_covariance_matrix(
    covariance_matrix: Any,
    entity_ids: frozenset[str],
) -> None:
    """Validate the ``covariance_matrix`` input for ``compute_risk_budget()``.

    Rules (per kickoff plan §4.6):
    - Must be a ``dict[str, dict[str, float]]``.
    - Outer keys must cover ALL portfolio entity IDs (no missing, no extra).
    - Inner keys must also cover ALL portfolio entity IDs.
    - Must be symmetric: ``Cov(i, j) == Cov(j, i)`` within
      ``rel_tol=1e-9, abs_tol=0.0``.
    - Diagonal entries must be ``>= 0.0`` (variance non-negative).
    - All values must be finite (no NaN / inf).

    Raises ``ValueError`` on violation.
    """
    if not isinstance(covariance_matrix, dict):
        raise ValueError(
            f"covariance_matrix must be a dict[str, dict[str, float]]; "
            f"got {type(covariance_matrix).__name__}"
        )
    missing = entity_ids - set(covariance_matrix.keys())
    if missing:
        raise ValueError(
            f"covariance_matrix is missing entity IDs: {sorted(missing)}"
        )
    extra = set(covariance_matrix.keys()) - entity_ids
    if extra:
        raise ValueError(
            f"covariance_matrix contains unknown entity IDs: {sorted(extra)}"
        )
    if not covariance_matrix:
        return  # empty portfolio → empty matrix
    for i, row in covariance_matrix.items():
        if not isinstance(row, dict):
            raise ValueError(
                f"covariance_matrix[{i!r}] must be a dict[str, float]; "
                f"got {type(row).__name__}"
            )
        row_missing = entity_ids - set(row.keys())
        if row_missing:
            raise ValueError(
                f"covariance_matrix[{i!r}] is missing entity IDs: "
                f"{sorted(row_missing)}"
            )
        row_extra = set(row.keys()) - entity_ids
        if row_extra:
            raise ValueError(
                f"covariance_matrix[{i!r}] contains unknown entity IDs: "
                f"{sorted(row_extra)}"
            )
        for j, v in row.items():
            if not _is_finite(v):
                raise ValueError(
                    f"covariance_matrix[{i!r}][{j!r}] must be finite; "
                    f"got {v!r}"
                )
            if i == j and v < 0.0:
                raise ValueError(
                    f"covariance_matrix[{i!r}][{i!r}] (diagonal) must be "
                    f">= 0.0; got {v!r}"
                )
    # Symmetry check: Cov(i, j) == Cov(j, i) within rel_tol=1e-9.
    for i in covariance_matrix:
        for j in covariance_matrix:
            if i < j:
                a = covariance_matrix[i][j]
                b = covariance_matrix[j][i]
                if not math.isclose(a, b, rel_tol=1e-9, abs_tol=0.0):
                    raise ValueError(
                        f"covariance_matrix must be symmetric: "
                        f"Cov({i!r},{j!r})={a!r} != Cov({j!r},{i!r})={b!r}"
                    )


# --------------------------------------------------------------------------- #
# Risk metric: compute_correlation (M3-S3)
# --------------------------------------------------------------------------- #


def compute_correlation(
    portfolio: Portfolio,
    returns_matrix: dict[str, list[float]],
) -> CorrelationReport:
    """Compute pairwise correlation metrics for a portfolio.

    Parameters
    ----------
    portfolio : Portfolio
        The portfolio to analyze (from ``phase3.portfolio.domain``).
    returns_matrix : dict[str, list[float]]
        Entity ID → list of period returns. Must cover ALL portfolio entity
        IDs. All lists must have equal length ``>= 2``. All values must be
        finite (no NaN / inf).

    Returns
    -------
    CorrelationReport
        Pairwise correlation matrix (symmetric, diagonal=1.0), portfolio-
        weighted average correlation (weighted by product of weights
        ``w_i * w_j``), unweighted average pairwise correlation, and pair
        count.

    Raises
    ------
    ValueError
        If ``returns_matrix`` fails validation (missing entity IDs, unequal
        list lengths, list length < 2, NaN/inf values).

    Notes
    -----
    - Pure function: no I/O, no datetime, no randomness, no global state.
    - For an empty portfolio, returns a ``CorrelationReport`` with an empty
      ``correlation_matrix``, ``weighted_average_correlation=0.0``,
      ``average_pairwise_correlation=0.0``, ``pair_count=0``,
      ``entity_count=0``.
    - For a single-entity portfolio, returns a correlation matrix with a
      single diagonal entry ``{e: {e: 1.0}}``, averages ``0.0`` (no
      off-diagonal pairs), and ``pair_count=0``.
    - Zero-variance series guard (RR-S3-1): if either series in a pair has
      zero variance, the correlation for that pair is defined as ``0.0``
      (not NaN).
    - Weighted average correlation uses the product of the two positions'
      weights ``w_i * w_j`` as the weight for pair ``(i, j)`` (standard
      portfolio-correlation weighting; RR-S3-9 mitigation).
    """
    entity_ids = frozenset(p.entity_id.value for p in portfolio.positions)
    _validate_returns_matrix(returns_matrix, entity_ids)

    if not portfolio.positions:
        return CorrelationReport(
            portfolio_id=portfolio.portfolio_id.value,
            correlation_matrix={},
            weighted_average_correlation=0.0,
            average_pairwise_correlation=0.0,
            pair_count=0,
            entity_count=0,
        )

    # Build ordered entity list + weight lookup.
    entities = [p.entity_id.value for p in portfolio.positions]
    weights = {p.entity_id.value: p.weight.value for p in portfolio.positions}
    n = len(entities)

    # Pairwise correlation matrix.
    correlation_matrix: dict[str, dict[str, float]] = {}
    for i, ei in enumerate(entities):
        correlation_matrix.setdefault(ei, {})
        for j, ej in enumerate(entities):
            if i == j:
                correlation_matrix[ei][ei] = 1.0
            elif i < j:
                corr = _pearson_correlation(
                    returns_matrix[ei], returns_matrix[ej]
                )
                correlation_matrix[ei][ej] = corr
                correlation_matrix.setdefault(ej, {})[ei] = corr

    # Off-diagonal pairwise correlations.
    off_diag_values: list[float] = []
    weighted_sum = 0.0
    weight_total = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            ei = entities[i]
            ej = entities[j]
            corr = correlation_matrix[ei][ej]
            off_diag_values.append(corr)
            w_ij = weights[ei] * weights[ej]
            weighted_sum += w_ij * corr
            weight_total += w_ij

    pair_count = n * (n - 1) // 2
    average_pairwise = (
        sum(off_diag_values) / len(off_diag_values)
        if off_diag_values
        else 0.0
    )
    weighted_average = (
        weighted_sum / weight_total if weight_total > 0.0 else 0.0
    )

    return CorrelationReport(
        portfolio_id=portfolio.portfolio_id.value,
        correlation_matrix=correlation_matrix,
        weighted_average_correlation=weighted_average,
        average_pairwise_correlation=average_pairwise,
        pair_count=pair_count,
        entity_count=n,
    )


# --------------------------------------------------------------------------- #
# Risk metric: compute_risk_budget (M3-S3)
# --------------------------------------------------------------------------- #


def compute_risk_budget(
    portfolio: Portfolio,
    covariance_matrix: dict[str, dict[str, float]],
    per_position_budget: float = 1.0,
) -> RiskBudgetReport:
    """Compute risk-budget (variance-contribution) metrics for a portfolio.

    Parameters
    ----------
    portfolio : Portfolio
        The portfolio to analyze (from ``phase3.portfolio.domain``).
    covariance_matrix : dict[str, dict[str, float]]
        Entity ID → (Entity ID → covariance). Must cover ALL portfolio
        entity IDs. Must be symmetric (``Cov(i,j) == Cov(j,i)`` within
        ``rel_tol=1e-9``). Diagonal entries must be ``>= 0.0``. All values
        finite.
    per_position_budget : float, optional
        Per-position risk budget threshold. Default ``1.0`` (no breach for
        any valid long-only PSD covariance). Documented: this is a
        measurement, not an enforcement — M3 is advisory-only (M4 enforces).

    Returns
    -------
    RiskBudgetReport
        Per-position risk contributions (RC), relative risk contributions
        (RRC), total portfolio variance, total volatility, total risk
        budget utilization, and breach flag.

    Raises
    ------
    ValueError
        If ``covariance_matrix`` fails validation (missing entity IDs,
        asymmetric, negative diagonal, NaN/inf values).

    Notes
    -----
    - Pure function: no I/O, no datetime, no randomness, no global state.
    - For an empty portfolio, returns a ``RiskBudgetReport`` with
      ``total_variance=0.0``, ``total_volatility=0.0``, empty
      ``risk_contributions`` / ``relative_risk_contributions``,
      ``total_risk_budget_utilization=0.0``, and ``budget_breach=False``.
    - For a single-entity portfolio, ``RC = w * (w * variance) = w^2 *
      variance`` and ``RRC = 1.0`` (when ``variance > 0``) or ``0.0`` (when
      ``variance == 0``).
    - Division-by-zero guard (RR-S3-3): when ``total_variance == 0.0``, all
      ``RRC_i = 0.0`` and ``total_risk_budget_utilization = 0.0``.
    - Negative-variance guard (RR-S3-2): the computed variance is clamped
      to ``max(0.0, computed)`` before taking the square root to absorb
      tiny FP rounding on near-singular covariance matrices.
    - RC is computed on raw weights (no normalization). If weights do not
      sum to 1.0, the RC values reflect that (documented behavior).
    """
    entity_ids = frozenset(p.entity_id.value for p in portfolio.positions)
    _validate_covariance_matrix(covariance_matrix, entity_ids)

    if not isinstance(per_position_budget, (int, float)) or isinstance(
        per_position_budget, bool
    ):
        raise ValueError(
            f"per_position_budget must be a float; "
            f"got {type(per_position_budget).__name__}"
        )
    if not _is_finite(float(per_position_budget)):
        raise ValueError(
            f"per_position_budget must be finite; got {per_position_budget!r}"
        )

    if not portfolio.positions:
        return RiskBudgetReport(
            portfolio_id=portfolio.portfolio_id.value,
            total_variance=0.0,
            total_volatility=0.0,
            risk_contributions={},
            relative_risk_contributions={},
            total_risk_budget_utilization=0.0,
            per_position_budget=float(per_position_budget),
            budget_breach=False,
            entity_count=0,
        )

    entities = [p.entity_id.value for p in portfolio.positions]
    weights = {p.entity_id.value: p.weight.value for p in portfolio.positions}
    n = len(entities)

    # Marginal risk contributions: MRC_i = sum_j(w_j * Cov(i, j)).
    # Risk contributions: RC_i = w_i * MRC_i.
    risk_contributions: dict[str, float] = {}
    total_variance = 0.0
    for i, ei in enumerate(entities):
        mrc_i = 0.0
        for j, ej in enumerate(entities):
            mrc_i += weights[ej] * covariance_matrix[ei][ej]
        rc_i = weights[ei] * mrc_i
        risk_contributions[ei] = rc_i
        # Accumulate total variance: sigma2_p = sum_i RC_i
        # (equivalent to w^T . Cov . w).
        total_variance += rc_i

    # RR-S3-2: clamp tiny-negative variance from FP rounding to 0.0.
    if total_variance < 0.0:
        total_variance = 0.0

    total_volatility = math.sqrt(total_variance) if total_variance > 0.0 else 0.0

    # Relative risk contributions: RRC_i = RC_i / sigma2_p (RR-S3-3 guard).
    relative_risk_contributions: dict[str, float] = {}
    if total_variance > 0.0:
        for ei in entities:
            relative_risk_contributions[ei] = risk_contributions[ei] / total_variance
    else:
        for ei in entities:
            relative_risk_contributions[ei] = 0.0

    utilization = sum(relative_risk_contributions.values())
    # Clamp utilization to [0.0, 1.0] when variance > 0 (FP guard so that
    # the invariant ``sum(RRC) == 1.0`` holds within tolerance).
    if total_variance > 0.0:
        if utilization < 0.0:
            utilization = 0.0
        elif utilization > 1.0:
            utilization = 1.0 if utilization > 1.0 + 1e-9 else utilization

    breach = any(
        rrc > per_position_budget for rrc in relative_risk_contributions.values()
    )

    return RiskBudgetReport(
        portfolio_id=portfolio.portfolio_id.value,
        total_variance=total_variance,
        total_volatility=total_volatility,
        risk_contributions=risk_contributions,
        relative_risk_contributions=relative_risk_contributions,
        total_risk_budget_utilization=utilization,
        per_position_budget=float(per_position_budget),
        budget_breach=breach,
        entity_count=n,
    )
