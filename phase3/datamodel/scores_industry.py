"""IndustryScore — one per industry sector per `as_of`."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from phase3.datamodel._version import (
    INDUSTRY_SCORER_TYPE,
    INDUSTRY_SCORER_VERSION,
    SCHEMA_VERSION,
)
from phase3.datamodel.scores import ScoreBreakdown

# 6 industry dimensions, ordered to match docs/phase3/05_industry_scoring.md.
INDUSTRY_DIMENSIONS: tuple[str, ...] = (
    "rotation",
    "relative_strength",
    "cyclicality",
    "macro_sensitivity",
    "industry_news",
    "capital_flow",
)


@dataclass(frozen=True)
class IndustryScore:
    """One industry sector's score."""

    breakdown: ScoreBreakdown
    industry_name: str = ""
    constituent_count: int = 0
    scorer_type: Literal["industry"] = "industry"
    scorer_version: str = INDUSTRY_SCORER_VERSION
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
        return self.breakdown.entity_id

    @property
    def entity_type(self) -> str:
        return self.breakdown.entity_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer_type": self.scorer_type,
            "scorer_version": self.scorer_version,
            "entity_type": self.breakdown.entity_type,
            "entity_id": self.breakdown.entity_id,
            "industry_name": self.industry_name,
            "constituent_count": self.constituent_count,
            **self.breakdown.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IndustryScore":
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
        bd_data["scorer_type"] = INDUSTRY_SCORER_TYPE
        bd_data["entity_type"] = data.get("entity_type", "industry")
        bd_data["entity_id"] = data.get("entity_id", "")
        breakdown = ScoreBreakdown.from_dict(bd_data)
        return cls(
            breakdown=breakdown,
            industry_name=data.get("industry_name", ""),
            constituent_count=int(data.get("constituent_count", 0)),
            scorer_version=data.get("scorer_version", INDUSTRY_SCORER_VERSION),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )
