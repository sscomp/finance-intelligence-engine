"""MacroScore — singleton score for the global macro environment."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from phase3.datamodel._version import COMPANY_SCORER_TYPE as _UNUSED  # noqa: F401
from phase3.datamodel._version import (
    MACRO_SCORER_TYPE,
    MACRO_SCORER_VERSION,
    SCHEMA_VERSION,
)
from phase3.datamodel.scores import ScoreBreakdown

# Macro entity is always a single global identifier; this is fixed by
# design (no per-country macro scoring in Phase 3A).
MACRO_ENTITY_ID: str = "global"
MACRO_ENTITY_TYPE: str = "macro"

# 6 macro dimensions, ordered to match docs/phase3/03_macro_scoring.md §1.
MACRO_DIMENSIONS: tuple[str, ...] = (
    "economic",
    "monetary",
    "inflation",
    "rates",
    "liquidity",
    "geopolitics",
)


@dataclass(frozen=True)
class MacroScore:
    """Macro environment score. Singleton per `as_of` (entity_id = 'global').

    Wraps ScoreBreakdown for serializer compatibility but adds the macro
    discriminators explicitly.
    """

    breakdown: ScoreBreakdown
    scorer_type: Literal["macro"] = "macro"
    scorer_version: str = MACRO_SCORER_VERSION
    schema_version: str = SCHEMA_VERSION
    # Convenience accessors — duplicated to avoid forcing every caller to
    # dig into .breakdown. They are NOT stored independently; they are
    # properties over breakdown.
    _entity_id: str = field(default=MACRO_ENTITY_ID)
    _entity_type: str = field(default=MACRO_ENTITY_TYPE)

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
    def timestamp(self) -> datetime:
        return self.breakdown.timestamp

    @property
    def entity_id(self) -> str:
        return self._entity_id

    @property
    def entity_type(self) -> str:
        return self._entity_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer_type": self.scorer_type,
            "scorer_version": self.scorer_version,
            "entity_type": self._entity_type,
            "entity_id": self._entity_id,
            **self.breakdown.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MacroScore":
        # Pull nested ScoreBreakdown fields out of the flat dict.
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
        bd_data["scorer_type"] = MACRO_SCORER_TYPE
        bd_data["entity_type"] = data.get("entity_type", MACRO_ENTITY_TYPE)
        bd_data["entity_id"] = data.get("entity_id", MACRO_ENTITY_ID)
        breakdown = ScoreBreakdown.from_dict(bd_data)
        return cls(
            breakdown=breakdown,
            scorer_version=data.get("scorer_version", MACRO_SCORER_VERSION),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            _entity_id=data.get("entity_id", MACRO_ENTITY_ID),
            _entity_type=data.get("entity_type", MACRO_ENTITY_TYPE),
        )
