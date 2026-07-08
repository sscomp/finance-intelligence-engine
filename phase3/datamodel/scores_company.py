"""CompanyScore — one per company per `as_of`. Supports cross-layer adjustment."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from phase3.datamodel._version import (
    COMPANY_SCORER_TYPE,
    COMPANY_SCORER_VERSION,
    SCHEMA_VERSION,
)
from phase3.datamodel.scores import ScoreBreakdown

# 7 company dimensions, ordered to match docs/phase3/04_company_scoring.md.
COMPANY_DIMENSIONS: tuple[str, ...] = (
    "financial_quality",
    "growth",
    "profitability",
    "valuation",
    "momentum",
    "risk",
    "news_sentiment",
)


@dataclass(frozen=True)
class CompanyScore:
    """One company's score, with optional macro/industry adjustment.

    raw_score = 7-dim weighted score before cross-layer adjustment.
    macro_adjustment + industry_adjustment = the cross-layer delta applied
    to get the final `breakdown.score`. The full chain is recorded in
    `breakdown.cross_layer_adjustments` for audit.
    """

    breakdown: ScoreBreakdown
    code: str = ""
    name: str = ""
    sector: str = ""
    raw_score: float = 0.0
    macro_adjustment: float = 0.0
    industry_adjustment: float = 0.0
    macro_context_hash: str | None = None
    industry_context_hash: str | None = None
    scorer_type: Literal["company"] = "company"
    scorer_version: str = COMPANY_SCORER_VERSION
    schema_version: str = SCHEMA_VERSION

    @property
    def score(self) -> float:
        return self.breakdown.score

    @property
    def confidence(self) -> float:
        return self.breakdown.confidence

    @property
    def dimensions(self) -> list:
        return list(self.breakdown.dimensions)

    @property
    def entity_id(self) -> str:
        return self.code or self.breakdown.entity_id

    @property
    def entity_type(self) -> str:
        return self.breakdown.entity_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer_type": self.scorer_type,
            "scorer_version": self.scorer_version,
            "entity_type": self.breakdown.entity_type,
            "entity_id": self.breakdown.entity_id,
            "code": self.code,
            "name": self.name,
            "sector": self.sector,
            "raw_score": self.raw_score,
            "macro_adjustment": self.macro_adjustment,
            "industry_adjustment": self.industry_adjustment,
            "macro_context_hash": self.macro_context_hash,
            "industry_context_hash": self.industry_context_hash,
            **self.breakdown.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompanyScore":
        bd_keys = {
            "score",
            "confidence",
            "dimensions",
            "overall_evidence",
            "timestamp",
            "config_hash",
            "valid_until",
            "cross_layer_adjustments",
        }
        bd_data = {k: data[k] for k in bd_keys if k in data}
        bd_data["scorer_type"] = COMPANY_SCORER_TYPE
        bd_data["entity_type"] = data.get("entity_type", "company")
        bd_data["entity_id"] = data.get("entity_id", data.get("code", ""))
        breakdown = ScoreBreakdown.from_dict(bd_data)
        return cls(
            breakdown=breakdown,
            code=data.get("code", ""),
            name=data.get("name", ""),
            sector=data.get("sector", ""),
            raw_score=float(data.get("raw_score", 0.0)),
            macro_adjustment=float(data.get("macro_adjustment", 0.0)),
            industry_adjustment=float(data.get("industry_adjustment", 0.0)),
            macro_context_hash=data.get("macro_context_hash"),
            industry_context_hash=data.get("industry_context_hash"),
            scorer_version=data.get("scorer_version", COMPANY_SCORER_VERSION),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )
