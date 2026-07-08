"""CompanyScorer — 7-dimension weighted score of a single company.

Phase 3A scope:
  - Pure function. No I/O. No live data.
  - Seven dimensions, each with named sub-indicators.
  - Optional cross-layer adjustment from MacroContext (sector-based
    sensitivity) and IndustryContext (industry relative strength).
  - Output: CompanyScore with raw_score, final score, evidence per factor,
    and a CrossLayerAdjustment list capturing the full audit trail.

The seven dimensions (default weights sum to 1.0):
  financial_quality (0.20)   — ROE, ROA, debt/equity, current ratio, FCF
  growth (0.15)              — revenue YoY, EPS YoY, FCF growth, guidance
  profitability (0.15)       — gross/op/net margin (or NIM for financial sector)
  valuation (0.15)           — PE, PB, PEG, dividend yield
  momentum (0.10)            — 1M/3M/6M price, distance from 52w high
  risk (0.10)                — beta, debt ratio, earnings volatility
  news_sentiment (0.15)      — RSS sentiment + event severity

Cross-layer adjustments:
  macro_adjustment = sum(beta[macro_dim] × macro_dim_score)
                     clipped to [-macro_to_company_max_abs, +macro_to_company_max_abs]
  industry_adjustment = ±industry_to_company_weight × |industry_score|
                       with sign(industry_score)
                       clipped to [-industry_to_company_max_abs, +industry_to_company_max_abs]
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from phase3.datamodel import (
    COMPANY_DEFAULT_WEIGHTS,
    COMPANY_DIMENSIONS,
    CompanyScore,
    CrossLayerAdjustment,
    DimensionResult,
    Evidence,
    ScoreBreakdown,
    ScorerWeights,
    SubIndicatorResult,
    WeightedFactor,
)
from phase3.datamodel.evidence import make_evidence_id
from phase3.scoring.base import BaseScorer, _clip_pm100


# ---------- per-indicator scoring helpers ----------

def _roe_score(roe: float) -> float:
    if roe >= 0.25:
        return 60.0
    if roe >= 0.15:
        return 20.0
    if roe >= 0.08:
        return 0.0
    return -40.0


def _roa_score(roa: float) -> float:
    if roa >= 0.10:
        return 50.0
    if roa >= 0.05:
        return 20.0
    if roa >= 0.02:
        return 0.0
    return -30.0


def _debt_equity_score(de: float) -> float:
    if de < 0.3:
        return 40.0
    if de < 1.0:
        return 0.0
    if de < 2.0:
        return -20.0
    return -40.0


def _current_ratio_score(cr: float) -> float:
    if cr >= 2.0:
        return 30.0
    if cr >= 1.5:
        return 20.0
    if cr >= 1.0:
        return 0.0
    return -30.0


def _fcf_positive_score(fcf: float) -> float:
    if fcf > 0:
        return 30.0
    if fcf == 0:
        return 0.0
    return -30.0


def _yoy_score(value: float, high: float, low: float, high_score: float, low_score: float) -> float:
    """Generic YoY/score curve."""
    if value >= high:
        return high_score
    if value > 0:
        return high_score * 0.33
    if value > low:
        return 0.0
    return low_score


def _revenue_yoy_score(yoy: float) -> float:
    return _yoy_score(yoy, 0.20, -0.10, 60.0, -50.0)


def _eps_yoy_score(yoy: float) -> float:
    return _yoy_score(yoy, 0.30, -0.20, 60.0, -60.0)


def _fcf_growth_score(yoy: float) -> float:
    return _yoy_score(yoy, 0.20, -0.10, 40.0, -40.0)


def _guidance_score(upgrade: float) -> float:
    """upgrade in [-1, +1] (analyst upgrade ratio)."""
    return _clip_pm100(upgrade * 30.0)


def _margin_score(margin: float) -> float:
    if margin >= 0.50:
        return 60.0
    if margin >= 0.30:
        return 20.0
    if margin >= 0.10:
        return 0.0
    return -40.0


def _nim_growth_score(growth: float) -> float:
    """Net Interest Margin growth (financial sector)."""
    if growth >= 0.05:
        return 60.0
    if growth > 0:
        return 20.0
    if growth >= -0.05:
        return 0.0
    return -40.0


def _interest_spread_score(spread: float) -> float:
    """Interest spread (financial sector), e.g. 0.02 = 2%."""
    if spread >= 0.025:
        return 60.0
    if spread >= 0.015:
        return 20.0
    if spread >= 0.005:
        return 0.0
    return -40.0


def _pe_score(pe: float) -> float:
    if pe < 10:
        return 40.0
    if pe < 20:
        return 20.0
    if pe < 40:
        return 0.0
    return -30.0


def _pb_score(pb: float) -> float:
    if pb < 1.0:
        return 40.0
    if pb < 3.0:
        return 20.0
    if pb < 5.0:
        return 0.0
    return -20.0


def _peg_score(peg: float) -> float:
    if peg < 1.0:
        return 30.0
    if peg < 2.0:
        return 0.0
    if peg < 3.0:
        return -10.0
    return -20.0


def _dividend_yield_score(y: float) -> float:
    if y >= 0.04:
        return 30.0
    if y >= 0.02:
        return 10.0
    return 0.0


def _price_momentum_score(mom: float) -> float:
    return _clip_pm100(mom * 1500.0)  # 0.04 → +60


def _dist_from_52w_high_score(dist: float) -> float:
    """dist = (price - 52w_high) / 52w_high, e.g. -0.05 = 5% below."""
    if dist >= -0.05:
        return 30.0
    if dist >= -0.15:
        return 10.0
    if dist >= -0.30:
        return -10.0
    return -30.0


def _relative_to_market_score(rel: float) -> float:
    return _clip_pm100(rel * 1500.0)


def _beta_score(beta: float) -> float:
    """Risk dimension — high beta = bad, so negative score contribution."""
    if beta < 0.8:
        return 20.0
    if beta < 1.2:
        return 0.0
    if beta < 1.5:
        return -15.0
    return -30.0


def _debt_ratio_score(ratio: float) -> float:
    if ratio < 0.3:
        return 20.0
    if ratio < 0.5:
        return 0.0
    if ratio < 0.7:
        return -15.0
    return -30.0


def _earnings_vol_score(vol: float) -> float:
    """vol = std of quarterly EPS YoY; high = bad."""
    if vol < 0.10:
        return 15.0
    if vol < 0.25:
        return 0.0
    if vol < 0.50:
        return -15.0
    return -30.0


def _risk_event_count_score(count: float) -> float:
    if count == 0:
        return 10.0
    if count <= 1:
        return 0.0
    if count <= 3:
        return -10.0
    return -20.0


def _sentiment_score(s: float) -> float:
    """s in [-1, +1] (polarity)."""
    return _clip_pm100(s * 60.0)  # 0.5 → +30


def _event_severity_score(sev: float) -> float:
    """sev in [-100, +100]."""
    return _clip_pm100(sev * 0.6)


# ---------- evidence helper ----------

def _ev(source_type: str, ref: str, raw: str, desc: str, ts: datetime) -> Evidence:
    return Evidence(
        evidence_id=make_evidence_id(source_type, ref, raw),
        source_type=source_type, source_ref=ref, raw_value=raw,
        description=desc, timestamp=ts,
    )


# ---------- Scorer ----------

class CompanyScorer(BaseScorer):
    """7-dimension company scorer with optional cross-layer adjustment."""

    scorer_type: str = "company"
    entity_type: str = "company"
    default_dimensions = COMPANY_DIMENSIONS

    def __init__(
        self,
        weights: ScorerWeights | None = None,
        ttl_hours: int = 24 * 7,  # 7 days
        config_hash: str = "no-config",
        as_of: datetime | None = None,
        sector_sensitivity: dict[str, dict[str, float]] | None = None,
        macro_to_company_max_abs: float = 15.0,
        industry_to_company_weight: float = 0.10,
        industry_to_company_max_abs: float = 10.0,
    ) -> None:
        if weights is None:
            weights = ScorerWeights(
                scorer_type=self.scorer_type,
                weights=COMPANY_DEFAULT_WEIGHTS,
                dimension_order=COMPANY_DIMENSIONS,
            )
        super().__init__(
            weights=weights, ttl_hours=ttl_hours,
            config_hash=config_hash, as_of=as_of,
        )
        self._sector_sensitivity = dict(sector_sensitivity or {})
        self._macro_max_abs = float(macro_to_company_max_abs)
        self._industry_w = float(industry_to_company_weight)
        self._industry_max_abs = float(industry_to_company_max_abs)

    # ---- dimension computation ----

    def _compute_financial_quality(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"roe": 0.30, "roa": 0.20, "debt_equity": 0.20,
                   "current_ratio": 0.15, "fcf": 0.15}
        factors: list[WeightedFactor] = []
        for name, fn, w in [
            ("roe", _roe_score, weights["roe"]),
            ("roa", _roa_score, weights["roa"]),
            ("debt_equity", _debt_equity_score, weights["debt_equity"]),
            ("current_ratio", _current_ratio_score, weights["current_ratio"]),
            ("fcf", _fcf_positive_score, weights["fcf"]),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            ev = _ev("yfinance", f"company.financial_quality.{name}", str(v),
                     f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit="ratio", sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="threshold",
                source="yfinance", source_ref=f"company.financial_quality.{name}",
                evidence=[ev],
            ))
        return self._aggregate("financial_quality", factors, 0.0, 1.0)

    def _compute_growth(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"revenue_yoy": 0.35, "eps_yoy": 0.35, "fcf_growth": 0.20, "guidance": 0.10}
        factors: list[WeightedFactor] = []
        for name, fn, w in [
            ("revenue_yoy", _revenue_yoy_score, weights["revenue_yoy"]),
            ("eps_yoy", _eps_yoy_score, weights["eps_yoy"]),
            ("fcf_growth", _fcf_growth_score, weights["fcf_growth"]),
            ("guidance", _guidance_score, weights["guidance"]),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            ev = _ev("yfinance", f"company.growth.{name}", str(v),
                     f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit="ratio", sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="threshold",
                source="yfinance", source_ref=f"company.growth.{name}",
                evidence=[ev],
            ))
        return self._aggregate("growth", factors, 0.0, 1.0)

    def _compute_profitability(self, d: Mapping[str, Any], is_financial: bool) -> DimensionResult:
        factors: list[WeightedFactor] = []
        if is_financial:
            weights = {"nim_growth": 0.5, "interest_spread": 0.5}
            for name, fn, w in [
                ("nim_growth", _nim_growth_score, weights["nim_growth"]),
                ("interest_spread", _interest_spread_score, weights["interest_spread"]),
            ]:
                v = d.get(name)
                if v is None:
                    continue
                try:
                    sub = float(fn(float(v)))
                except (TypeError, ValueError):
                    continue
                ev = _ev("yfinance", f"company.profitability.{name}", str(v),
                         f"{name}={v}", self._as_of)
                factors.append(WeightedFactor(
                    name=name, raw_value=float(v), raw_unit="ratio", sub_score=sub,
                    sub_weight=w, signed_score=sub * w, transformation="threshold",
                    source="yfinance", source_ref=f"company.profitability.{name}",
                    evidence=[ev],
                ))
        else:
            weights = {"gross_margin": 0.4, "operating_margin": 0.3, "net_margin": 0.3}
            for name, fn, w in [
                ("gross_margin", _margin_score, weights["gross_margin"]),
                ("operating_margin", _margin_score, weights["operating_margin"]),
                ("net_margin", _margin_score, weights["net_margin"]),
            ]:
                v = d.get(name)
                if v is None:
                    continue
                try:
                    sub = float(fn(float(v)))
                except (TypeError, ValueError):
                    continue
                ev = _ev("yfinance", f"company.profitability.{name}", str(v),
                         f"{name}={v}", self._as_of)
                factors.append(WeightedFactor(
                    name=name, raw_value=float(v), raw_unit="ratio", sub_score=sub,
                    sub_weight=w, signed_score=sub * w, transformation="threshold",
                    source="yfinance", source_ref=f"company.profitability.{name}",
                    evidence=[ev],
                ))
        return self._aggregate("profitability", factors, 0.0, 1.0)

    def _compute_valuation(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"pe": 0.4, "pb": 0.3, "peg": 0.2, "dividend_yield": 0.1}
        factors: list[WeightedFactor] = []
        for name, fn, w, invert in [
            ("pe", _pe_score, weights["pe"], True),         # pe is negative-score
            ("pb", _pb_score, weights["pb"], False),
            ("peg", _peg_score, weights["peg"], False),
            ("dividend_yield", _dividend_yield_score, weights["dividend_yield"], False),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            if invert:
                sub = -sub
            ev = _ev("yfinance", f"company.valuation.{name}", str(v),
                     f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit="ratio", sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="threshold",
                source="yfinance", source_ref=f"company.valuation.{name}",
                evidence=[ev],
            ))
        return self._aggregate("valuation", factors, 0.0, 1.0)

    def _compute_momentum(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"price_momentum_1m": 0.4, "price_momentum_3m": 0.3,
                   "dist_from_52w_high": 0.2, "relative_to_market": 0.1}
        factors: list[WeightedFactor] = []
        for name, fn, w in [
            ("price_momentum_1m", _price_momentum_score, weights["price_momentum_1m"]),
            ("price_momentum_3m", _price_momentum_score, weights["price_momentum_3m"]),
            ("dist_from_52w_high", _dist_from_52w_high_score, weights["dist_from_52w_high"]),
            ("relative_to_market", _relative_to_market_score, weights["relative_to_market"]),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            ev = _ev("yfinance", f"company.momentum.{name}", str(v),
                     f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit="ratio", sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="range",
                source="yfinance", source_ref=f"company.momentum.{name}",
                evidence=[ev],
            ))
        return self._aggregate("momentum", factors, 0.0, 1.0)

    def _compute_risk(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"beta": 0.4, "debt_ratio": 0.3, "earnings_volatility": 0.2,
                   "risk_event_count": 0.1}
        factors: list[WeightedFactor] = []
        for name, fn, w in [
            ("beta", _beta_score, weights["beta"]),
            ("debt_ratio", _debt_ratio_score, weights["debt_ratio"]),
            ("earnings_volatility", _earnings_vol_score, weights["earnings_volatility"]),
            ("risk_event_count", _risk_event_count_score, weights["risk_event_count"]),
        ]:
            v = d.get(name)
            if v is None:
                continue
            try:
                sub = float(fn(float(v)))
            except (TypeError, ValueError):
                continue
            ev = _ev("yfinance", f"company.risk.{name}", str(v),
                     f"{name}={v}", self._as_of)
            factors.append(WeightedFactor(
                name=name, raw_value=float(v), raw_unit="ratio", sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="range",
                source="yfinance", source_ref=f"company.risk.{name}",
                evidence=[ev],
            ))
        return self._aggregate("risk", factors, 0.0, 1.0)

    def _compute_news_sentiment(self, d: Mapping[str, Any]) -> DimensionResult:
        weights = {"sentiment": 0.6, "event_severity": 0.4}
        factors: list[WeightedFactor] = []
        for name, fn, w in [
            ("sentiment", _sentiment_score, weights["sentiment"]),
            ("event_severity", _event_severity_score, weights["event_severity"]),
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
                name=name, raw_value=float(v), raw_unit="score", sub_score=sub,
                sub_weight=w, signed_score=sub * w, transformation="range",
                source="rss", source_ref=f"news.{name}", evidence=[ev],
            ))
        return self._aggregate("news_sentiment", factors, 0.0, 1.0)

    # ---- dispatcher ----

    def compute_dimension(
        self, name: str, inputs: dict[str, Any], config: Any
    ) -> DimensionResult:
        is_financial = False
        if isinstance(config, Mapping):
            is_financial = bool(config.get("is_financial_sector", False))
        if name == "financial_quality":
            return self._compute_financial_quality(inputs)
        if name == "growth":
            return self._compute_growth(inputs)
        if name == "profitability":
            return self._compute_profitability(inputs, is_financial=is_financial)
        if name == "valuation":
            return self._compute_valuation(inputs)
        if name == "momentum":
            return self._compute_momentum(inputs)
        if name == "risk":
            return self._compute_risk(inputs)
        if name == "news_sentiment":
            return self._compute_news_sentiment(inputs)
        raise ValueError(f"unknown company dimension: {name!r}")

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

    # ---- cross-layer adjustment ----

    def _apply_cross_layer(
        self,
        raw_score: float,
        sector: str,
        macro_context: Any,
        industry_score: Any,
    ) -> tuple[float, float, list[CrossLayerAdjustment], str | None, str | None]:
        """Apply macro + industry adjustments, returning
        (macro_adj, industry_adj, adjustment_list, macro_hash, industry_hash).
        """
        adj_list: list[CrossLayerAdjustment] = []
        macro_adj = 0.0
        industry_adj = 0.0
        macro_hash: str | None = None
        industry_hash: str | None = None

        # 1) Macro adjustment: sector-specific beta on macro dim scores.
        if macro_context is not None and sector and self._sector_sensitivity:
            beta_map = self._sector_sensitivity.get(sector, {})
            if beta_map and hasattr(macro_context, "dimensions"):
                macro_dims = {d.name: d.score for d in macro_context.dimensions}
                contrib = sum(beta_map.get(m, 0.0) * macro_dims.get(m, 0.0)
                              for m in beta_map)
                macro_adj = max(-self._macro_max_abs,
                                min(self._macro_max_abs, contrib))
                # Use macro_context's own config_hash (don't substitute our own)
                inner = getattr(macro_context, "breakdown", macro_context)
                macro_hash = getattr(inner, "config_hash", None) or "macro"
                adj_list.append(CrossLayerAdjustment(
                    from_scorer="macro",
                    from_score_id=macro_hash,
                    to_scorer="company",
                    to_score_id="",  # filled by caller (company code)
                    adjustment=macro_adj,
                    reason=f"sector={sector!r} β={[round(b, 3) for b in beta_map.values()]}, max_abs={self._macro_max_abs}",
                    evidence=[],
                ))

        # 2) Industry adjustment: sign(industry_score) × weight × |industry_score|
        if industry_score is not None:
            iscore = float(getattr(industry_score, "score", 0.0))
            industry_adj = self._industry_w * iscore
            industry_adj = max(-self._industry_max_abs,
                               min(self._industry_max_abs, industry_adj))
            # IndustryScore's own config_hash (fall back to attribute or 'industry')
            inner_ind = getattr(industry_score, "breakdown", industry_score)
            industry_hash = getattr(inner_ind, "config_hash", None) or "industry"
            adj_list.append(CrossLayerAdjustment(
                from_scorer="industry",
                from_score_id=industry_hash,
                to_scorer="company",
                to_score_id="",
                adjustment=industry_adj,
                reason=f"industry_score={iscore:+.2f} × weight={self._industry_w}, max_abs={self._industry_max_abs}",
                evidence=[],
            ))

        return macro_adj, industry_adj, adj_list, macro_hash, industry_hash

    # ---- high-level ----

    def score_company(
        self,
        code: str,
        name: str = "",
        sector: str = "",
        inputs: dict[str, dict[str, Any]] | None = None,
        config: Any = None,
        macro_context: Any = None,
        industry_score: Any = None,
    ) -> CompanyScore:
        """Compute CompanyScore with optional cross-layer adjustment.

        `config` may carry `is_financial_sector` flag for profitability
        dimension switching. `macro_context` should be a ScoreBreakdown
        (or MacroScore with .breakdown) and `industry_score` should be
        an IndustryScore (or any object with .score and .config_hash).
        """
        inputs = inputs or {}
        # Base 7-dim score
        breakdown: ScoreBreakdown = self.score(
            entity_id=code, inputs=inputs, config=config,
        )
        raw_score = breakdown.score

        # Cross-layer adjustment
        macro_adj, industry_adj, adj_list, macro_hash, industry_hash = (
            self._apply_cross_layer(
                raw_score=raw_score,
                sector=sector,
                macro_context=macro_context.breakdown if hasattr(macro_context, "breakdown") else macro_context,
                industry_score=industry_score,
            )
        )
        final_score = _clip_pm100(raw_score + macro_adj + industry_adj)

        # Stamp to_score_id into the adjustment records
        adj_list = [
            CrossLayerAdjustment(
                from_scorer=a.from_scorer,
                from_score_id=a.from_score_id,
                to_scorer=a.to_scorer,
                to_score_id=code,
                adjustment=a.adjustment,
                reason=a.reason,
                evidence=a.evidence,
            )
            for a in adj_list
        ]

        # Rebuild the breakdown with adjusted score + cross-layer entries
        adjusted = ScoreBreakdown(
            scorer_type=breakdown.scorer_type,
            entity_type=breakdown.entity_type,
            entity_id=breakdown.entity_id,
            score=final_score,
            confidence=breakdown.confidence,
            dimensions=breakdown.dimensions,
            overall_evidence=list(breakdown.overall_evidence),
            timestamp=breakdown.timestamp,
            config_hash=breakdown.config_hash,
            valid_until=breakdown.valid_until,
            cross_layer_adjustments=adj_list,
            schema_version=breakdown.schema_version,
        )

        return CompanyScore(
            breakdown=adjusted,
            code=code, name=name, sector=sector,
            raw_score=raw_score,
            macro_adjustment=macro_adj,
            industry_adjustment=industry_adj,
            macro_context_hash=macro_hash,
            industry_context_hash=industry_hash,
        )


__all__ = ["CompanyScorer"]
