"""Rule-based explainability: turn a ScoreBreakdown into a Chinese
short summary plus a structured JSON-friendly list.

Phase 3A uses a template-based explainer (no LLM). Phase 3C will add
an optional LLM-enhanced path (phase3.explain.llm_optional).

The explainer is intentionally deterministic and pure. Same input →
same output, no randomness, no I/O.
"""
from __future__ import annotations

from typing import Iterable

from phase3.datamodel import ScoreBreakdown


def _direction_zh(score: float) -> str:
    if score >= 60:
        return "強烈偏多"
    if score >= 20:
        return "偏多"
    if score <= -60:
        return "強烈偏空"
    if score <= -20:
        return "偏空"
    return "中性"


def _bucket_zh(score: float) -> str:
    if score >= 80:
        return "極端樂觀"
    if score >= 40:
        return "偏樂觀"
    if score > 20:
        return "略偏多"
    if score > -20:
        return "區間震盪"
    if score > -40:
        return "略偏空"
    if score > -80:
        return "偏悲觀"
    return "極端悲觀"


def explain_score(breakdown: ScoreBreakdown, top_n_dimensions: int = 3) -> dict:
    """Produce a structured explanation of a score.

    Returns a dict with:
      - summary_zh: one-line Chinese summary
      - bucket: bucket label
      - direction: one-line direction label
      - top_dimensions: top-N dimensions with the highest |score × weight|
      - evidence_count: how many evidence items are attached
    """
    dims = list(breakdown.dimensions)
    # Sort by absolute contribution to the parent score
    contribs = sorted(
        dims, key=lambda d: abs(d.score * d.weight), reverse=True
    )
    top = contribs[: max(0, top_n_dimensions)]
    top_lines = [
        {
            "name": d.name,
            "score": d.score,
            "weight": d.weight,
            "contribution": d.score * d.weight,
            "sub_indicator_count": len(d.sub_indicators),
        }
        for d in top
    ]
    score = breakdown.score
    summary = (
        f"{breakdown.scorer_type} 評分 {score:+.1f}（{_direction_zh(score)}，"
        f"{_bucket_zh(score)}），信心 {breakdown.confidence:.0%}。"
    )
    if top:
        lead = "、".join(f"{d['name']} {d['score']:+.0f}" for d in top_lines)
        summary += f" 主力：{lead}。"
    return {
        "scorer_type": breakdown.scorer_type,
        "entity_id": breakdown.entity_id,
        "score": breakdown.score,
        "confidence": breakdown.confidence,
        "direction": _direction_zh(score),
        "bucket": _bucket_zh(score),
        "summary_zh": summary,
        "top_dimensions": top_lines,
        "evidence_count": len(breakdown.overall_evidence),
        "config_hash": breakdown.config_hash,
        "timestamp": breakdown.timestamp.isoformat(),
    }


__all__ = ["explain_score"]
