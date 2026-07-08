"""BaseScorer — shared scaffolding for the three concrete scorers.

Each scorer implements `compute_dimension(name, inputs)` returning a
DimensionResult. The base class handles weight validation, score
aggregation, normalization to [-100, +100], evidence roll-up, and
the ScoreBreakdown envelope construction.

The scorer is a pure function. The same inputs + same config → same
output. No I/O, no logging, no global state.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from phase3.datamodel import (
    CrossLayerAdjustment,
    DimensionResult,
    Evidence,
    ScoreBreakdown,
    ScorerWeights,
    SubIndicatorResult,
    WeightedFactor,
)
from phase3.datamodel.evidence import make_evidence_id


def _clip_pm100(x: float) -> float:
    return max(-100.0, min(100.0, float(x)))


class BaseScorer(ABC):
    """Common scorer scaffolding.

    Subclasses must set:
      - scorer_type: str  (e.g. "macro")
      - entity_type: str  (e.g. "macro")
      - default_dimensions: tuple[str, ...]  (ordered dimension keys)

    Subclasses must implement:
      - compute_dimension(name, inputs, config) -> DimensionResult

    Inputs shape (per scorer, documented in 03/04/05):
      - macro:    {"economic": {"gdp_yoy": 0.025, "pmi": 51.2, ...}, ...}
      - industry: {"rotation": {...}, "relative_strength": {...}, ...}
      - company:  {"financial_quality": {...}, "growth": {...}, ...}
    """

    scorer_type: str = ""
    entity_type: str = ""
    default_dimensions: tuple[str, ...] = ()

    def __init__(
        self,
        weights: ScorerWeights,
        ttl_hours: int = 24,
        config_hash: str = "no-config",
        as_of: datetime | None = None,
    ) -> None:
        # Validate that the weights cover exactly the dimensions we own.
        own = set(self.default_dimensions)
        have = set(weights.weights.keys())
        if own and have != own:
            missing = own - have
            extra = have - own
            msgs: list[str] = []
            if missing:
                msgs.append(f"missing {sorted(missing)}")
            if extra:
                msgs.append(f"unexpected {sorted(extra)}")
            raise ValueError(
                f"{self.scorer_type} scorer weights: dimension mismatch; "
                + "; ".join(msgs)
            )
        self._weights = weights
        self._ttl_hours = int(ttl_hours)
        self._config_hash = config_hash
        self._as_of = as_of or datetime.now(timezone.utc)

    @property
    def weights(self) -> ScorerWeights:
        return self._weights

    @property
    def config_hash(self) -> str:
        return self._config_hash

    @property
    def as_of(self) -> datetime:
        return self._as_of

    @property
    def valid_until(self) -> datetime:
        return self._as_of + timedelta(hours=self._ttl_hours)

    @abstractmethod
    def compute_dimension(
        self,
        name: str,
        inputs: dict[str, Any],
        config: Any,
    ) -> DimensionResult:
        """Compute one dimension's DimensionResult.

        The implementation owns:
          - How raw indicators are turned into sub-scores (-100..+100)
          - How the per-indicator weights inside the dimension are chosen
          - What evidence to attach

        The base class handles:
          - The dimension's *outer* weight (in self._weights)
          - The dimension's *confidence* aggregation (caller-supplied
            or 1.0 if the dimension didn't compute one)
        """

    def _validate_inputs(
        self, inputs: dict[str, dict[str, Any]]
    ) -> None:
        """Default: every dimension listed in weights must have an
        input dict (even if empty). Concrete scorers may override."""
        for dim in self._weights.weights:
            if dim not in inputs:
                raise ValueError(
                    f"{self.scorer_type} scorer: missing input for dimension {dim!r}"
                )

    def score(
        self,
        entity_id: str,
        inputs: dict[str, dict[str, Any]],
        config: Any = None,
        extra_evidence: Iterable[Evidence] = (),
    ) -> ScoreBreakdown:
        """Compute the full ScoreBreakdown for the entity.

        Args:
            entity_id: e.g. "global" (macro), "AI" (industry), "2330" (company)
            inputs:    {dimension_name: {indicator_name: value, ...}}
            config:    config object passed to compute_dimension (defaults to None)
            extra_evidence: evidence attached at the top level (e.g. a macro
                            release timestamp the whole macro score depends on)
        """
        self._validate_inputs(inputs)
        dimensions: list[DimensionResult] = []
        overall_evidence: list[Evidence] = list(extra_evidence)
        # weighted sum, where each dimension's weight is in self._weights
        weighted_sum = 0.0
        # The "outer" weights from self._weights sum to 1.0 (validated at load
        # time). The dimension's own score is in [-100, 100]. We multiply.
        for dim_name, dim_weight in self._weights.weights.items():
            dim_in = inputs.get(dim_name, {})
            dim_result = self.compute_dimension(dim_name, dim_in, config)
            # Re-stamp the dimension's outer weight to be the canonical
            # value (the dimension fn might not have set it).
            dim_result = DimensionResult(
                name=dim_result.name,
                score=_clip_pm100(dim_result.score),
                sub_indicators=dim_result.sub_indicators,
                weight=float(dim_weight),
                confidence=dim_result.confidence,
                factors=dim_result.factors,
                evidence=dim_result.evidence,
            )
            dimensions.append(dim_result)
            weighted_sum += dim_result.score * dim_weight
            overall_evidence.extend(dim_result.evidence)

        score = _clip_pm100(weighted_sum)
        # Confidence: weighted average of dimension confidences by outer weight.
        if any(d.confidence > 0 for d in dimensions):
            conf = sum(d.confidence * d.weight for d in dimensions)
            conf = max(0.0, min(1.0, conf))
        else:
            conf = 1.0

        return ScoreBreakdown(
            scorer_type=self.scorer_type,
            entity_type=self.entity_type,
            entity_id=entity_id,
            score=score,
            confidence=conf,
            dimensions=dimensions,
            overall_evidence=overall_evidence,
            timestamp=self._as_of,
            config_hash=self._config_hash,
            valid_until=self.valid_until,
        )


__all__ = ["BaseScorer"]
