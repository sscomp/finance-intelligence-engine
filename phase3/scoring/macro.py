"""MacroScorer — 6-dimension weighted score of the global macro environment.

Phase 3A scope:
  - Pure function. No I/O. No live data.
  - Each dimension implements a small set of named sub-indicators
    (gdp_growth_score, pmi_score, etc.) and a per-dimension weight
    map. The user feeds the scorer raw indicator values; the scorer
    normalizes them to sub-scores and combines.

  - The dimension weights are configured externally (via
    ScorerWeights). The sub-indicator weights inside each dimension
    are hard-coded in this file (Phase 3A). Phase 3B will move them
    into config/phase3/indicators/macro_indicators.yaml.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Sequence, cast

from phase3.datamodel import (
    DimensionResult,
    Evidence,
    MACRO_DEFAULT_WEIGHTS,
    MACRO_DIMENSIONS,
    MACRO_ENTITY_ID,
    MACRO_ENTITY_TYPE,
    MacroScore,
    ScorerWeights,
    SubIndicatorResult,
    WeightedFactor,
)
from phase3.datamodel.evidence import make_evidence_id
from phase3.scoring.base import BaseScorer


# ---------- per-indicator scoring helpers ----------

def _clip_pm100(x: float) -> float:
    return max(-100.0, min(100.0, float(x)))


def _indicator_threshold(
    value: float,
    thresholds: list[tuple[float, float]],
) -> float:
    """Map a value to a sub-score by walking sorted thresholds.

    `thresholds` is a list of (value_threshold, sub_score) pairs in
    ASCENDING value_threshold order. The first pair whose value_threshold
    is greater than `value` determines the sub-score. If `value` exceeds
    every threshold, the last sub_score is used. If `value` is below
    the first threshold, the first sub_score is used.

    Example: [(0, 0), (1, 20), (3, 60)] → value 0.5 → 20, value 4 → 60.
    """
    if not thresholds:
        return 0.0
    last_score = thresholds[-1][1]
    for t, s in thresholds:
        if value <= t:
            return s
    return last_score


def _gdp_growth_score(yoy: float) -> float:
    """GDP YoY: >3% = +60, >1% = +20, 0% = 0, <-1% = -60."""
    return _indicator_threshold(
        yoy,
        [(-1.0, -60.0), (0.0, 0.0), (1.0, 20.0), (3.0, 60.0)],
    )


def _pmi_score(pmi: float) -> float:
    """PMI: >55 = +60, >50 = +20, <45 = -60, between 45-50 = 0."""
    return _indicator_threshold(
        pmi, [(45.0, -60.0), (50.0, 0.0), (55.0, 20.0), (60.0, 60.0)]
    )


def _employment_score(unemployment_delta_pp: float) -> float:
    """Δ-unemployment: rising = bearish, falling = bullish.

    unemployment_delta_pp > 0 = unemployment went up (bad).
    unemployment_delta_pp < 0 = went down (good).
    """
    if unemployment_delta_pp > 0.5:
        return -40.0
    if unemployment_delta_pp > 0:
        return -20.0
    if unemployment_delta_pp < -0.5:
        return 40.0
    if unemployment_delta_pp < 0:
        return 20.0
    return 0.0


def _retail_score(yoy: float) -> float:
    """Retail sales YoY: >5% = +40, <-2% = -40."""
    return _indicator_threshold(
        yoy, [(-2.0, -40.0), (0.0, 0.0), (5.0, 40.0)]
    )


def _fed_funds_score(action: str) -> float:
    """Fed action vs expectation: 'hike' = -40, 'cut' = +40, 'hold' = 0."""
    if action == "hike":
        return -40.0
    if action == "cut":
        return 40.0
    return 0.0


def _dot_plot_score(hawkish_count: int, total: int) -> float:
    """Hawkish dots / total. Higher = more bearish (-30..+20)."""
    if total <= 0:
        return 0.0
    pct = hawkish_count / total
    return _clip_pm100((0.5 - pct) * 60.0)


def _cpi_score(cpi_yoy: float) -> float:
    """CPI YoY: too high = bearish for equities."""
    if cpi_yoy > 5.0:
        return -60.0
    if cpi_yoy > 3.0:
        return -30.0
    if cpi_yoy > 2.0:
        return 0.0
    if cpi_yoy > 0.0:
        return 20.0
    return 0.0


def _core_inflation_score(core_yoy: float) -> float:
    if core_yoy > 4.0:
        return -40.0
    if core_yoy > 2.5:
        return -20.0
    if core_yoy > 1.5:
        return 20.0
    return 40.0


def _yield_curve_score(spread_pp: float) -> float:
    """2y10y spread: inverted = bearish, normal = +20, steep = +40."""
    if spread_pp < 0:
        return -60.0
    if spread_pp < 0.5:
        return -20.0
    if spread_pp < 1.5:
        return 20.0
    return 40.0


def _real_rate_score(real_rate: float) -> float:
    """Real rate: rising = bearish."""
    if real_rate > 2.0:
        return -30.0
    if real_rate > 0.5:
        return -10.0
    if real_rate < -0.5:
        return 20.0
    return 0.0


def _rate_vol_score(vol: float) -> float:
    """Rate vol: MOVE index. High = bearish."""
    if vol > 130:
        return -20.0
    if vol > 100:
        return -10.0
    if vol < 70:
        return 20.0
    return 0.0


def _vix_score(vix: float) -> float:
    if vix > 30:
        return -50.0
    if vix > 20:
        return -20.0
    if vix < 12:
        return 30.0
    return 0.0


def _dxy_score(dxy: float) -> float:
    """Strong dollar = bad for emerging markets / TWSE."""
    if dxy > 110:
        return -30.0
    if dxy > 105:
        return -15.0
    if dxy < 95:
        return 20.0
    return 0.0


def _m2_growth_score(yoy: float) -> float:
    if yoy > 0.10:
        return 30.0
    if yoy > 0.03:
        return 10.0
    if yoy < -0.05:
        return -30.0
    return 0.0


def _credit_spread_score(spread_bp: float) -> float:
    """IG credit spread in bp. Wider = bearish."""
    if spread_bp > 250:
        return -30.0
    if spread_bp > 150:
        return -15.0
    if spread_bp < 100:
        return 20.0
    return 0.0


def _geopolitics_score(events: list[str]) -> float:
    """Lightweight keyword tally. Phase 3A: presence of known-hot
    words moves the score. Phase 3B will swap in an LLM-tagged score."""
    if not events:
        return 0.0
    bearish_words = {"war", "sanction", "trade_war", "election_risk", "policy_change"}
    bullish_words = {"ceasefire", "treaty", "dovish_election"}
    s = 0.0
    for ev in events:
        if ev in bearish_words:
            s -= 25.0
        elif ev in bullish_words:
            s += 25.0
    return _clip_pm100(s / max(1, len(events) // 2 + 1))


# ---------- dimension-level sub-indicator definitions ----------

# Each dimension is {sub_indicator_name: (weight, fn)}. Sub-indicator
# weight is normalized within the dimension so weights sum to 1.0.

def _normalize_sub_weights(d: dict[str, float]) -> dict[str, float]:
    s = sum(d.values())
    if s == 0:
        return {k: 0.0 for k in d}
    return {k: v / s for k, v in d.items()}


def _dimension_with_subs(
    name: str,
    dim_score: float,
    subs: Sequence[tuple[str, Any, str, float, float, Evidence | None]],
    sub_weights_norm: dict[str, float],
    dim_confidence: float = 1.0,
) -> DimensionResult:
    """Build a DimensionResult with explicit per-sub evidence and weights.

    `subs` is a list of (name, raw_value, unit, sub_score, sub_weight, evidence).
    """
    sub_results: list[SubIndicatorResult] = []
    factors: list[WeightedFactor] = []
    for n, raw, unit, ss, sw, ev in subs:
        w = sub_weights_norm.get(n, 0.0)
        sub_results.append(
            SubIndicatorResult(
                name=n,
                raw_value=raw,
                raw_unit=unit,
                sub_score=_clip_pm100(ss),
                transformation="threshold",
                source="macro_scorer",
                source_ref=n,
                evidence=[ev] if ev else [],
            )
        )
        factors.append(
            WeightedFactor(
                name=n,
                raw_value=raw,
                raw_unit=unit,
                sub_score=_clip_pm100(ss),
                sub_weight=w,
                signed_score=_clip_pm100(ss) * w,
                transformation="threshold",
                source="macro_scorer",
                source_ref=n,
                evidence=[ev] if ev else [],
            )
        )
    return DimensionResult(
        name=name,
        score=_clip_pm100(dim_score),
        sub_indicators=sub_results,
        weight=0.0,  # filled by BaseScorer
        confidence=dim_confidence,
        factors=factors,
        evidence=[f.evidence[0] for f in factors if f.evidence],
    )


# Per-dimension sub-indicator weight map (Phase 3A: hard-coded).
_MACRO_SUB_WEIGHTS: dict[str, dict[str, float]] = {
    "economic": {"gdp": 0.4, "pmi": 0.3, "employment": 0.2, "retail": 0.1},
    "monetary": {"fed_funds": 0.5, "dot_plot": 0.3, "central_bank_unanimity": 0.2},
    "inflation": {"cpi": 0.6, "core": 0.4},
    "rates": {"yield_curve": 0.5, "real_rate": 0.3, "rate_vol": 0.2},
    "liquidity": {"vix": 0.4, "dxy": 0.3, "m2": 0.2, "credit_spread": 0.1},
    "geopolitics": {"events": 1.0},
}
for k in _MACRO_SUB_WEIGHTS:
    _MACRO_SUB_WEIGHTS[k] = _normalize_sub_weights(_MACRO_SUB_WEIGHTS[k])


def _ev(source: str, ref: str, raw: str, desc: str, ts: datetime, weight: float | None = None) -> Evidence:
    return Evidence(
        evidence_id=make_evidence_id(source, ref, raw),
        source_type=source,
        source_ref=ref,
        raw_value=raw,
        description=desc,
        timestamp=ts,
        weight=weight,
    )


# ---------- MacroScorer ----------

class MacroScorer(BaseScorer):
    """6-dimension macro environment score, output [-100, +100]."""

    scorer_type = "macro"
    entity_type = MACRO_ENTITY_TYPE
    default_dimensions = MACRO_DIMENSIONS

    def __init__(
        self,
        weights: ScorerWeights | None = None,
        ttl_hours: int = 24,
        config_hash: str = "no-config",
        as_of: datetime | None = None,
    ) -> None:
        super().__init__(
            weights or ScorerWeights(
                scorer_type=self.scorer_type,
                weights=MACRO_DEFAULT_WEIGHTS,
                dimension_order=MACRO_DIMENSIONS,
            ),
            ttl_hours=ttl_hours,
            config_hash=config_hash,
            as_of=as_of,
        )

    def compute_dimension(
        self,
        name: str,
        inputs: dict[str, Any],
        config: Any,
    ) -> DimensionResult:
        now = self._as_of
        subs_w = _MACRO_SUB_WEIGHTS[name]
        if name == "economic":
            subs = [
                (
                    "gdp",
                    float(inputs.get("gdp_yoy", 0.0)),
                    "pct",
                    _gdp_growth_score(float(inputs.get("gdp_yoy", 0.0))),
                    subs_w["gdp"],
                    _ev(
                        "yfinance_macro",
                        "^DJI-macro",
                        str(inputs.get("gdp_yoy", 0.0)),
                        f"GDP YoY {inputs.get('gdp_yoy', 0.0)}",
                        now,
                    ),
                ),
                (
                    "pmi",
                    float(inputs.get("pmi", 50.0)),
                    "index",
                    _pmi_score(float(inputs.get("pmi", 50.0))),
                    subs_w["pmi"],
                    _ev(
                        "rss_wealth",
                        "macro.pmi",
                        str(inputs.get("pmi", 50.0)),
                        f"PMI {inputs.get('pmi', 50.0)}",
                        now,
                    ),
                ),
                (
                    "employment",
                    float(inputs.get("unemployment_delta_pp", 0.0)),
                    "pp",
                    _employment_score(float(inputs.get("unemployment_delta_pp", 0.0))),
                    subs_w["employment"],
                    _ev(
                        "rss_wealth",
                        "macro.employment",
                        str(inputs.get("unemployment_delta_pp", 0.0)),
                        f"Δunemployment {inputs.get('unemployment_delta_pp', 0.0)}pp",
                        now,
                    ),
                ),
                (
                    "retail",
                    float(inputs.get("retail_yoy", 0.0)),
                    "pct",
                    _retail_score(float(inputs.get("retail_yoy", 0.0))),
                    subs_w["retail"],
                    _ev(
                        "rss_wealth",
                        "macro.retail",
                        str(inputs.get("retail_yoy", 0.0)),
                        f"Retail YoY {inputs.get('retail_yoy', 0.0)}",
                        now,
                    ),
                ),
            ]
        elif name == "monetary":
            action = str(inputs.get("fed_action", "hold"))
            hawk = int(inputs.get("hawkish_dots", 9))
            total = int(inputs.get("total_dots", 18))
            unanimity = float(inputs.get("cb_unanimity", 0.0))
            subs = [
                (
                    "fed_funds",
                    action,
                    "action",
                    _fed_funds_score(action),
                    subs_w["fed_funds"],
                    _ev(
                        "yfinance_macro",
                        "^IRX-fed",
                        action,
                        f"Fed action: {action}",
                        now,
                    ),
                ),
                (
                    "dot_plot",
                    hawk / max(1, total),
                    "ratio",
                    _dot_plot_score(hawk, total),
                    subs_w["dot_plot"],
                    _ev(
                        "rss_wealth",
                        "macro.dot_plot",
                        f"{hawk}/{total}",
                        f"Dots {hawk}/{total} hawkish",
                        now,
                    ),
                ),
                (
                    "central_bank_unanimity",
                    unanimity,
                    "ratio",
                    unanimity * 20.0,
                    subs_w["central_bank_unanimity"],
                    _ev(
                        "rss_wealth",
                        "macro.cb",
                        str(unanimity),
                        f"CB unanimity {unanimity}",
                        now,
                    ),
                ),
            ]
        elif name == "inflation":
            cpi = float(inputs.get("cpi_yoy", 0.0))
            core = float(inputs.get("core_cpi_yoy", 0.0))
            subs = [
                (
                    "cpi",
                    cpi, "pct", _cpi_score(cpi), subs_w["cpi"],
                    _ev("yfinance_macro", "TIP-cpi", str(cpi), f"CPI YoY {cpi}", now),
                ),
                (
                    "core",
                    core, "pct", _core_inflation_score(core), subs_w["core"],
                    _ev("yfinance_macro", "TIP-core", str(core), f"Core CPI YoY {core}", now),
                ),
            ]
        elif name == "rates":
            spread = float(inputs.get("yield_spread_pp", 1.0))
            real = float(inputs.get("real_rate", 0.0))
            vol = float(inputs.get("rate_vol", 90.0))
            subs = [
                (
                    "yield_curve",
                    spread, "pp", _yield_curve_score(spread), subs_w["yield_curve"],
                    _ev("yfinance_macro", "yield-curve", str(spread), f"2y10y spread {spread}pp", now),
                ),
                (
                    "real_rate",
                    real, "pct", _real_rate_score(real), subs_w["real_rate"],
                    _ev("yfinance_macro", "TIP-real", str(real), f"Real rate {real}", now),
                ),
                (
                    "rate_vol",
                    vol, "index", _rate_vol_score(vol), subs_w["rate_vol"],
                    _ev("yfinance_macro", "MOVE-vol", str(vol), f"MOVE {vol}", now),
                ),
            ]
        elif name == "liquidity":
            vix = float(inputs.get("vix", 15.0))
            dxy = float(inputs.get("dxy", 100.0))
            m2 = float(inputs.get("m2_yoy", 0.0))
            credit = float(inputs.get("credit_spread_bp", 120.0))
            subs = [
                (
                    "vix", vix, "index", _vix_score(vix), subs_w["vix"],
                    _ev("yfinance_macro", "^VIX", str(vix), f"VIX {vix}", now),
                ),
                (
                    "dxy", dxy, "index", _dxy_score(dxy), subs_w["dxy"],
                    _ev("yfinance_macro", "DX-Y.NYB", str(dxy), f"DXY {dxy}", now),
                ),
                (
                    "m2", m2, "pct", _m2_growth_score(m2), subs_w["m2"],
                    _ev("rss_wealth", "macro.m2", str(m2), f"M2 YoY {m2}", now),
                ),
                (
                    "credit_spread", credit, "bp", _credit_spread_score(credit), subs_w["credit_spread"],
                    _ev("rss_wealth", "macro.credit", str(credit), f"Credit spread {credit}bp", now),
                ),
            ]
        elif name == "geopolitics":
            events = list(inputs.get("events", []) or [])
            score = _geopolitics_score(events)
            subs = [
                (
                    "events",
                    ",".join(events) if events else "",
                    "list",
                    score,
                    1.0,
                    _ev(
                        "rss_wealth",
                        "macro.geopolitics",
                        str(events),
                        f"Geopolitical events: {events}",
                        now,
                    ) if events else None,
                ),
            ]
        else:
            raise ValueError(f"Unknown macro dimension {name!r}")

        dim_score = sum(ss * sw for _, _, _, ss, sw, _ in subs)
        # Confidence: 1.0 if any input was provided, 0.0 if completely empty.
        # Sub-indicators that defaulted (e.g. 0) get a low confidence.
        n_present = sum(1 for _, raw, _, _, _, _ in subs if raw not in (0, 0.0, "", "hold", None))
        n_total = len(subs)
        conf = 0.5 + 0.5 * (n_present / n_total) if n_total else 1.0
        return _dimension_with_subs(
            name, dim_score, subs, subs_w, dim_confidence=conf
        )

    def score_macro(
        self,
        inputs: dict[str, dict[str, Any]],
        entity_id: str = MACRO_ENTITY_ID,
    ) -> MacroScore:
        breakdown = self.score(entity_id, inputs, config=None)
        return MacroScore(breakdown=breakdown, _entity_id=entity_id, _entity_type=MACRO_ENTITY_TYPE)


__all__ = ["MacroScorer"]
