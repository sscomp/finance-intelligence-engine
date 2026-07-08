"""IndustryScorer — 6-dimension weighted score of an industry sector.

Phase 3A scope:
  - Pure function. No I/O. No live data.
  - Six dimensions, each with named sub-indicators.
  - `macro_sensitivity` reads a `MacroContext` (passed via `config`).
  - Output: IndustryScore with score in [-100, +100], evidence per factor.

The six dimensions (default weights sum to 1.0):
  rotation (0.15)            — sector rotation, ETF relative perf
  relative_strength (0.20)   — RS rating, momentum, breadth
  cyclicality (0.15)         — PMI, book-to-bill, export YoY
  macro_sensitivity (0.15)   — beta-weighted exposure to macro dims
  industry_news (0.20)       — news volume, sentiment, event severity
  capital_flow (0.15)        — foreign + prop net (sector-aggregated)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from phase3.datamodel import (
    DimensionResult,
    Evidence,
    IndustryScore,
    INDUSTRY_DEFAULT_WEIGHTS,
    INDUSTRY_DIMENSIONS,
    ScoreBreakdown,
    ScorerWeights,
    SubIndicatorResult,
    WeightedFactor,
)
from phase3.datamodel.evidence import make_evidence_id
from phase3.scoring.base import BaseScorer, _clip_pm100


# ---------- per-indicator scoring helpers ----------

def _rotation_relative_perf_score(rel_perf: float) -> float:
    """rel_perf = sector_ret - benchmark_ret (decimal). +0.05 → +60."""
    return _clip_pm100(rel_perf * 1200.0)


def _rotation_money_flow_proxy_score(breadth: float) -> float:
    """breadth in [-1, 1]."""
    return _clip_pm100(breadth * 120.0)


def _rs_rating_score(rs: float) -> float:
    """rs in [0, 99] percentile. 80 → +60, 20 → -60."""
    return _clip_pm100((rs - 50.0) * 2.0)


def _industry_etf_momentum_score(mom_1m: float) -> float:
    return _clip_pm100(mom_1m * 1500.0)


def _breadth_pct_score(breadth_pct: float) -> float:
    """breadth_pct in [0, 1]."""
    return _clip_pm100((breadth_pct - 0.5) * 200.0)


def _pmi_direction_score(pmi_delta: float) -> float:
    return _clip_pm100(pmi_delta * 30.0)


def _book_to_bill_score(bb: float) -> float:
    if bb >= 1.2:
        return 40.0
    if bb >= 1.0:
        return 20.0
    if bb >= 0.9:
        return 0.0
    return -40.0


def _export_yoy_score(yoy: float) -> float:
    if yoy > 0.10:
        return 40.0
    if yoy > 0.0:
        return 20.0
    if yoy > -0.05:
        return 0.0
    return -40.0


def _news_volume_change_score(change: float) -> float:
    """change = (this_week - last_week) / last_week."""
    return _clip_pm100(change * 60.0)


def _sentiment_polarity_score(polarity: float) -> float:
    """polarity in [-1, 1]."""
    return _clip_pm100(polarity * 80.0)


def _event_severity_score(severity: float) -> float:
    """severity in [-100, +100] (already event-weighted)."""
    return _clip_pm100(severity * 0.6)


def _flow_zscore_to_score(z: float) -> float:
    return _clip_pm100(z * 30.0)


# ---------- evidence helper (matches macro.py _ev) ----------

def _ev(source_type: str, ref: str, raw: str, desc: str, ts: datetime) -> Evidence:
    return Evidence(
        evidence_id=make_evidence_id(source_type, ref, raw),
        source_type=source_type,
        source_ref=ref,
        raw_value=raw,
        description=desc,
        timestamp=ts,
    )


# ---------- Scorer ----------

class IndustryScorer(BaseScorer):
    """6-dimension industry scorer.

    Inputs (per dimension):
      rotation:            {sector_relative_perf_5d, sector_relative_perf_20d, money_flow_proxy}
      relative_strength:   {rs_rating, industry_etf_momentum_1m, stock_breadth_pct}
      cyclicality:         {pmi_direction, book_to_bill, export_yoy}
      macro_sensitivity:   {}  (special: reads MacroContext from config)
      industry_news:       {news_volume_change, sentiment_polarity, event_severity_aggregate}
      capital_flow:        {foreign_net_industry_total, prop_net_industry_total}
    """

    scorer_type: str = "industry"
    entity_type: str = "industry"
    default_dimensions = INDUSTRY_DIMENSIONS

    def __init__(
        self,
        weights: ScorerWeights | None = None,
        ttl_hours: int = 24,
        config_hash: str = "no-config",
        as_of: datetime | None = None,
    ) -> None:
        if weights is None:
            weights = ScorerWeights(
                scorer_type=self.scorer_type,
                weights=INDUSTRY_DEFAULT_WEIGHTS,
                dimension_order=INDUSTRY_DIMENSIONS,
            )
        super().__init__(
            weights=weights,
            ttl_hours=ttl_hours,
            config_hash=config_hash,
            as_of=as_of,
        )

    # ---- dimension computation ----

    def _compute_rotation(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"sector_relative_perf_5d": 0.4, "sector_relative_perf_20d": 0.3, "money_flow_proxy": 0.3}
        factors: list[WeightedFactor] = []
        for name, fn, w in [
            ("sector_relative_perf_5d", _rotation_relative_perf_score, weights["sector_relative_perf_5d"]),
            ("sector_relative_perf_20d", _rotation_relative_perf_score, weights["sector_relative_perf_20d"]),
            ("money_flow_proxy", _rotation_money_flow_proxy_score, weights["money_flow_proxy"]),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            ev = _ev("industry_input", f"rotation.{name}", str(v),
                     f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit="ratio", sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="range",
                source="industry_input", source_ref=f"rotation.{name}", evidence=[ev],
            ))
        return self._aggregate("rotation", factors, 0.0, 1.0)

    def _compute_relative_strength(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"rs_rating": 0.5, "industry_etf_momentum_1m": 0.3, "stock_breadth_pct": 0.2}
        factors: list[WeightedFactor] = []
        for name, fn, w, unit in [
            ("rs_rating", _rs_rating_score, weights["rs_rating"], "percentile"),
            ("industry_etf_momentum_1m", _industry_etf_momentum_score, weights["industry_etf_momentum_1m"], "ratio"),
            ("stock_breadth_pct", _breadth_pct_score, weights["stock_breadth_pct"], "pct"),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            ev = _ev("industry_input", f"relative_strength.{name}", str(v),
                     f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit=unit, sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="range",
                source="industry_input", source_ref=f"relative_strength.{name}", evidence=[ev],
            ))
        return self._aggregate("relative_strength", factors, 0.0, 1.0)

    def _compute_cyclicality(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"pmi_direction": 0.4, "book_to_bill": 0.3, "export_yoy": 0.3}
        factors: list[WeightedFactor] = []
        for name, fn, w, unit in [
            ("pmi_direction", _pmi_direction_score, weights["pmi_direction"], "delta"),
            ("book_to_bill", _book_to_bill_score, weights["book_to_bill"], "ratio"),
            ("export_yoy", _export_yoy_score, weights["export_yoy"], "ratio"),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            ev = _ev("industry_input", f"cyclicality.{name}", str(v),
                     f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit=unit, sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="threshold",
                source="industry_input", source_ref=f"cyclicality.{name}", evidence=[ev],
            ))
        return self._aggregate("cyclicality", factors, 0.0, 1.0)

    def _compute_macro_sensitivity(
        self, d: Mapping[str, Any], config: Any
    ) -> DimensionResult:
        """Reads MacroContext (ScoreBreakdown with macro dim scores) from
        `config.macro_context` (or `config` itself if it's the breakdown).
        """
        macro_ctx = None
        beta: dict[str, float] = {}
        if isinstance(config, Mapping):
            macro_ctx = config.get("macro_context")
            beta = dict(config.get("industry_macro_beta", {}))  # type: ignore[arg-type]
        elif config is not None and hasattr(config, "dimensions"):
            macro_ctx = config

        factors: list[WeightedFactor] = []
        if macro_ctx is not None and beta:
            macro_dims = {dm.name: dm.score for dm in macro_ctx.dimensions}  # type: ignore[union-attr]
            for macro_dim, b in beta.items():
                if macro_dim not in macro_dims:
                    continue
                mscore = float(macro_dims[macro_dim])
                contribution = b * mscore
                sub = _clip_pm100(contribution)
                ev = _ev(
                    "macro_context", f"macro.{macro_dim}", str(round(mscore, 4)),
                    f"beta={b:.2f} × {macro_dim}={mscore:+.1f} → {sub:+.1f}",
                    self._as_of,
                )
                factors.append(WeightedFactor(
                    name=macro_dim, raw_value=mscore, raw_unit="score",
                    sub_score=sub, sub_weight=abs(b), signed_score=sub,
                    transformation="invert_threshold",
                    source="macro_context", source_ref=f"macro.{macro_dim}", evidence=[ev],
                ))
        return self._aggregate("macro_sensitivity", factors, 0.0, 0.5 if factors else 0.0)

    def _compute_industry_news(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"news_volume_change": 0.5, "sentiment_polarity": 0.3, "event_severity_aggregate": 0.2}
        factors: list[WeightedFactor] = []
        for name, fn, w, unit in [
            ("news_volume_change", _news_volume_change_score, weights["news_volume_change"], "ratio"),
            ("sentiment_polarity", _sentiment_polarity_score, weights["sentiment_polarity"], "polarity"),
            ("event_severity_aggregate", _event_severity_score, weights["event_severity_aggregate"], "score"),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            ev = _ev("rss", f"news.{name}", str(v), f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit=unit, sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="range",
                source="rss", source_ref=f"news.{name}", evidence=[ev],
            ))
        return self._aggregate("industry_news", factors, 0.0, 1.0)

    def _compute_capital_flow(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"foreign_net_industry_total": 0.5, "prop_net_industry_total": 0.5}
        factors: list[WeightedFactor] = []
        for name, fn, w, unit in [
            ("foreign_net_industry_total", _flow_zscore_to_score, weights["foreign_net_industry_total"], "zscore"),
            ("prop_net_industry_total", _flow_zscore_to_score, weights["prop_net_industry_total"], "zscore"),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            ev = _ev("t86", f"t86.{name}", str(v), f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit=unit, sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="z_score",
                source="t86", source_ref=f"t86.{name}", evidence=[ev],
            ))
        return self._aggregate("capital_flow", factors, 0.0, 1.0)

    # ---- dispatcher ----

    def compute_dimension(
        self, name: str, inputs: dict[str, Any], config: Any
    ) -> DimensionResult:
        if name == "rotation":
            return self._compute_rotation(inputs)
        if name == "relative_strength":
            return self._compute_relative_strength(inputs)
        if name == "cyclicality":
            return self._compute_cyclicality(inputs)
        if name == "macro_sensitivity":
            return self._compute_macro_sensitivity(inputs, config)
        if name == "industry_news":
            return self._compute_industry_news(inputs)
        if name == "capital_flow":
            return self._compute_capital_flow(inputs)
        raise ValueError(f"unknown industry dimension: {name!r}")

    # ---- aggregation helper ----

    def _aggregate(
        self,
        name: str,
        factors: list[WeightedFactor],
        default_score: float,
        default_conf: float,
    ) -> DimensionResult:
        if not factors:
            return DimensionResult(
                name=name, score=default_score, sub_indicators=[],
                weight=self._weights.weights.get(name, 0.0), confidence=default_conf,
                factors=[], evidence=[],
            )
        sub_total = sum(f.signed_score for f in factors)
        weight_sum = sum(f.sub_weight for f in factors)
        if weight_sum > 0:
            score = _clip_pm100(sub_total / weight_sum)
            conf = min(1.0, weight_sum)
        else:
            score = default_score
            conf = default_conf
        sub_indicators = [
            SubIndicatorResult(
                name=f.name, raw_value=f.raw_value, raw_unit=f.raw_unit,
                sub_score=f.sub_score, transformation=f.transformation,
                threshold_legend=None, source=f.source, source_ref=f.source_ref,
                evidence=f.evidence,
            )
            for f in factors
        ]
        return DimensionResult(
            name=name, score=score, sub_indicators=sub_indicators,
            weight=self._weights.weights.get(name, 0.0), confidence=conf,
            factors=factors,
            evidence=[ev for f in factors for ev in f.evidence],
        )

    # ---- high-level ----

    def score_industry(
        self,
        industry_id: str,
        industry_name: str = "",
        inputs: dict[str, dict[str, Any]] | None = None,
        config: Any = None,
        constituent_count: int = 0,
    ) -> IndustryScore:
        """Compute the full IndustryScore.

        `inputs` shape: {dimension_name: {indicator_name: value}}.
        `config` may carry a `macro_context` (a ScoreBreakdown) and
        `industry_macro_beta` mapping of macro dim name → beta.
        """
        inputs = inputs or {}
        breakdown: ScoreBreakdown = self.score(
            entity_id=industry_id, inputs=inputs, config=config,
        )
        return IndustryScore(
            breakdown=breakdown,
            industry_name=industry_name or industry_id,
            constituent_count=constituent_count,
        )


__all__ = ["IndustryScorer"]
