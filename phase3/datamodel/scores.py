"""Shared scoring primitives: WeightedFactor, SubIndicatorResult,
DimensionResult, ScoreBreakdown, CrossLayerAdjustment.

The three concrete Score types (MacroScore, IndustryScore, CompanyScore)
live in scores_macro.py / scores_industry.py / scores_company.py. They
all wrap ScoreBreakdown with type-specific fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from phase3.datamodel._version import SCHEMA_VERSION
from phase3.datamodel.evidence import Evidence
from phase3.datamodel.signals import _utcnow  # noqa: F401 (re-export for callers)

ScoreDirection = Literal["bullish", "bearish", "neutral", "mixed"]
Transformation = Literal[
    "threshold", "z_score", "invert", "range", "invert_threshold", "lookup"
]


@dataclass(frozen=True)
class WeightedFactor:
    """One (sub-)indicator contribution inside a Dimension / SubIndicator.

    `signed_score` is the *contribution* to the parent's score (already
    multiplied by sub_weight). Stored alongside the raw value so any
    parent score can be exactly reconstructed by summing.
    """

    name: str
    raw_value: float | str | None
    raw_unit: str
    sub_score: float  # -100..+100 after transformation
    sub_weight: float  # 0..1, weight inside the parent Dimension
    signed_score: float  # sub_score * sub_weight, the actual contribution
    transformation: Transformation
    source: str  # which adapter or dimension produced this
    source_ref: str  # provenance pointer (URL / signal_id / config key)
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "raw_value": self.raw_value,
            "raw_unit": self.raw_unit,
            "sub_score": self.sub_score,
            "sub_weight": self.sub_weight,
            "signed_score": self.signed_score,
            "transformation": self.transformation,
            "source": self.source,
            "source_ref": self.source_ref,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeightedFactor":
        return cls(
            name=data["name"],
            raw_value=data["raw_value"],
            raw_unit=data["raw_unit"],
            sub_score=float(data["sub_score"]),
            sub_weight=float(data["sub_weight"]),
            signed_score=float(data["signed_score"]),
            transformation=data["transformation"],
            source=data["source"],
            source_ref=data["source_ref"],
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
        )


@dataclass(frozen=True)
class SubIndicatorResult:
    """Alias-friendly holder for a list of WeightedFactor that share a
    transformation strategy. Currently informational — kept distinct from
    WeightedFactor so Phase 3B can add structured threshold_legend etc.
    without breaking callers.
    """

    name: str
    raw_value: float | str | None
    raw_unit: str
    sub_score: float  # -100..+100
    transformation: Transformation
    threshold_legend: dict[str, Any] | None = None
    source: str = ""
    source_ref: str = ""
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "raw_value": self.raw_value,
            "raw_unit": self.raw_unit,
            "sub_score": self.sub_score,
            "transformation": self.transformation,
            "threshold_legend": self.threshold_legend,
            "source": self.source,
            "source_ref": self.source_ref,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubIndicatorResult":
        return cls(
            name=data["name"],
            raw_value=data["raw_value"],
            raw_unit=data["raw_unit"],
            sub_score=float(data["sub_score"]),
            transformation=data["transformation"],
            threshold_legend=data.get("threshold_legend"),
            source=data.get("source", ""),
            source_ref=data.get("source_ref", ""),
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
        )


@dataclass(frozen=True)
class DimensionResult:
    """One of the 6 macro / 6 industry / 7 company dimensions."""

    name: str  # e.g. "financial_quality", "monetary"
    score: float  # -100..+100, dimension-level sub-score
    sub_indicators: list[SubIndicatorResult]
    weight: float  # 0..1, weight of this dimension in its parent scorer
    confidence: float  # 0..1
    factors: list[WeightedFactor] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "sub_indicators": [s.to_dict() for s in self.sub_indicators],
            "weight": self.weight,
            "confidence": self.confidence,
            "factors": [f.to_dict() for f in self.factors],
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DimensionResult":
        return cls(
            name=data["name"],
            score=float(data["score"]),
            sub_indicators=[
                SubIndicatorResult.from_dict(s)
                for s in data.get("sub_indicators", [])
            ],
            weight=float(data["weight"]),
            confidence=float(data["confidence"]),
            factors=[WeightedFactor.from_dict(f) for f in data.get("factors", [])],
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
        )


@dataclass(frozen=True)
class CrossLayerAdjustment:
    """One cross-layer adjustment entry (macro/industry → company)."""

    from_scorer: str  # "macro" | "industry"
    from_score_id: str
    to_scorer: str  # typically "company"
    to_score_id: str
    adjustment: float  # signed delta to apply
    reason: str  # human-readable: "liquidity +35 × sensitivity 0.05"
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_scorer": self.from_scorer,
            "from_score_id": self.from_score_id,
            "to_scorer": self.to_scorer,
            "to_score_id": self.to_score_id,
            "adjustment": self.adjustment,
            "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CrossLayerAdjustment":
        return cls(
            from_scorer=data["from_scorer"],
            from_score_id=data["from_score_id"],
            to_scorer=data["to_scorer"],
            to_score_id=data["to_score_id"],
            adjustment=float(data["adjustment"]),
            reason=data["reason"],
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
        )


@dataclass(frozen=True)
class ScoreBreakdown:
    """The shared body of every Score: dimensions + evidence + cross-layer.

    Concrete scores (MacroScore/IndustryScore/CompanyScore) wrap this with
    type-specific discriminators and (for Company) the pre/post cross-layer
    split. Keeping ScoreBreakdown shared avoids divergent serialization.
    """

    scorer_type: str  # "macro" | "industry" | "company"
    entity_type: str
    entity_id: str
    score: float  # -100..+100
    confidence: float  # 0..1
    dimensions: list[DimensionResult]
    overall_evidence: list[Evidence]
    timestamp: datetime
    config_hash: str
    valid_until: datetime
    cross_layer_adjustments: list[CrossLayerAdjustment] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer_type": self.scorer_type,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "score": self.score,
            "confidence": self.confidence,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "overall_evidence": [e.to_dict() for e in self.overall_evidence],
            "timestamp": self.timestamp.isoformat(),
            "config_hash": self.config_hash,
            "valid_until": self.valid_until.isoformat(),
            "cross_layer_adjustments": [
                a.to_dict() for a in self.cross_layer_adjustments
            ],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoreBreakdown":
        return cls(
            scorer_type=data["scorer_type"],
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            score=float(data["score"]),
            confidence=float(data["confidence"]),
            dimensions=[DimensionResult.from_dict(d) for d in data["dimensions"]],
            overall_evidence=[
                Evidence.from_dict(e) for e in data["overall_evidence"]
            ],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            config_hash=data["config_hash"],
            valid_until=datetime.fromisoformat(data["valid_until"]),
            cross_layer_adjustments=[
                CrossLayerAdjustment.from_dict(a)
                for a in data.get("cross_layer_adjustments", [])
            ],
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )
