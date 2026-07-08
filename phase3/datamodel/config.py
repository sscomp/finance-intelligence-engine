"""Config dataclasses — default weights, decay rules, source weights.

These are the *defaults* that ship with Phase 3A. The YAML loader
(phase3.config.loader) can override any of them at runtime, but the
defaults are the source of truth if no config is provided.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Default weight maps. Keys MUST match the dimension tuples in
# datamodel.scores_*.py (MACRO_DIMENSIONS / INDUSTRY_DIMENSIONS /
# COMPANY_DIMENSIONS). Each map sums to exactly 1.0 — enforced by
# phase3.config.loader when loaded from YAML.

MACRO_DEFAULT_WEIGHTS: dict[str, float] = {
    "economic": 0.20,
    "monetary": 0.20,
    "inflation": 0.15,
    "rates": 0.20,
    "liquidity": 0.15,
    "geopolitics": 0.10,
}

INDUSTRY_DEFAULT_WEIGHTS: dict[str, float] = {
    "rotation": 0.15,
    "relative_strength": 0.20,
    "cyclicality": 0.15,
    "macro_sensitivity": 0.15,
    "industry_news": 0.20,
    "capital_flow": 0.15,
}

COMPANY_DEFAULT_WEIGHTS: dict[str, float] = {
    "financial_quality": 0.20,
    "growth": 0.15,
    "profitability": 0.15,
    "valuation": 0.15,
    "momentum": 0.10,
    "risk": 0.10,
    "news_sentiment": 0.15,
}


@dataclass(frozen=True)
class ScorerWeights:
    """Validated weight map for a scorer. Sum of values must == 1.0 within
    a small float tolerance. Enforced by the loader, not the dataclass,
    so an invalid map is catchable at load time."""

    scorer_type: str  # "macro" | "industry" | "company"
    weights: dict[str, float]
    dimension_order: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.weights:
            raise ValueError(f"{self.scorer_type} weights map is empty")
        for k in self.weights:
            if not isinstance(k, str):
                raise TypeError(
                    f"{self.scorer_type} weight keys must be str, got {type(k).__name__}"
                )
            if not isinstance(self.weights[k], (int, float)):
                raise TypeError(
                    f"{self.scorer_type} weight[{k!r}] must be numeric, got {type(self.weights[k]).__name__}"
                )
        if not (-1e-6 <= sum(self.weights.values()) - 1.0 <= 1e-6):
            raise ValueError(
                f"{self.scorer_type} weights must sum to 1.0, got {sum(self.weights.values())}"
            )

    def normalized(self) -> "ScorerWeights":
        """Return a copy with weights re-scaled to sum to exactly 1.0.

        Used when validation should be permissive (e.g. for a user-edited
        YAML that's slightly off). The original ScorerWeights is left
        untouched.
        """
        s = sum(self.weights.values())
        if s == 0:
            raise ValueError("Cannot normalize weights that sum to 0")
        new = {k: v / s for k, v in self.weights.items()}
        return ScorerWeights(
            scorer_type=self.scorer_type,
            weights=new,
            dimension_order=self.dimension_order,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer_type": self.scorer_type,
            "weights": dict(self.weights),
            "dimension_order": list(self.dimension_order),
        }


@dataclass(frozen=True)
class DecayRule:
    """One signal-type decay rule.

    `function`:
      - "linear":     weight *= max(0, 1 - age_days / half_life_days)
      - "exponential": weight *= 0.5 ** (age_days / half_life_days)
      - "step":       weight = 1.0 if age_days <= step_threshold_days else 0.0
      - "none":       weight stays 1.0
    """

    signal_type: str
    function: str  # "linear" | "exponential" | "step" | "none"
    half_life_days: float = 0.0
    step_threshold_days: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_type": self.signal_type,
            "function": self.function,
            "half_life_days": self.half_life_days,
            "step_threshold_days": self.step_threshold_days,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecayRule":
        return cls(
            signal_type=data["signal_type"],
            function=data["function"],
            half_life_days=float(data.get("half_life_days", 0.0)),
            step_threshold_days=float(data.get("step_threshold_days", 0.0)),
        )


@dataclass(frozen=True)
class SourceWeightEntry:
    """One source's weight profile."""

    source_id: str
    source_type: str
    source_weight: float  # 0..1, overall reputation
    type_weights: dict[str, float] = field(default_factory=dict)
    # Per-signal-type weight (0..1). Falls back to source_weight if a type
    # is missing.

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "source_weight": self.source_weight,
            "type_weights": dict(self.type_weights),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceWeightEntry":
        return cls(
            source_id=data["source_id"],
            source_type=data["source_type"],
            source_weight=float(data["source_weight"]),
            type_weights=dict(data.get("type_weights", {})),
        )

    def type_weight(self, signal_type: str) -> float:
        """Return weight for a given signal_type, falling back to source_weight."""
        v = self.type_weights.get(signal_type)
        if v is None:
            return self.source_weight
        return float(v)


@dataclass(frozen=True)
class MacroScorerConfig:
    """Aggregated config for the MacroScorer."""

    weights: ScorerWeights
    decay_rules: dict[str, DecayRule] = field(default_factory=dict)
    source_weights: dict[str, SourceWeightEntry] = field(default_factory=dict)
    ttl_hours: int = 24

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights.to_dict(),
            "decay_rules": {k: v.to_dict() for k, v in self.decay_rules.items()},
            "source_weights": {
                k: v.to_dict() for k, v in self.source_weights.items()
            },
            "ttl_hours": self.ttl_hours,
        }


@dataclass(frozen=True)
class IndustryScorerConfig:
    """Aggregated config for the IndustryScorer."""

    weights: ScorerWeights
    decay_rules: dict[str, DecayRule] = field(default_factory=dict)
    source_weights: dict[str, SourceWeightEntry] = field(default_factory=dict)
    ttl_hours: int = 24
    cross_layer_macro_weight: float = 0.10  # how much macro score shifts industry

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights.to_dict(),
            "decay_rules": {k: v.to_dict() for k, v in self.decay_rules.items()},
            "source_weights": {
                k: v.to_dict() for k, v in self.source_weights.items()
            },
            "ttl_hours": self.ttl_hours,
            "cross_layer_macro_weight": self.cross_layer_macro_weight,
        }


@dataclass(frozen=True)
class CompanyScorerConfig:
    """Aggregated config for the CompanyScorer."""

    weights: ScorerWeights
    decay_rules: dict[str, DecayRule] = field(default_factory=dict)
    source_weights: dict[str, SourceWeightEntry] = field(default_factory=dict)
    ttl_hours: int = 168  # 7 days
    cross_layer_macro_max_abs: float = 15.0  # clamp macro adjustment
    cross_layer_industry_max_abs: float = 10.0  # clamp industry adjustment

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights.to_dict(),
            "decay_rules": {k: v.to_dict() for k, v in self.decay_rules.items()},
            "source_weights": {
                k: v.to_dict() for k, v in self.source_weights.items()
            },
            "ttl_hours": self.ttl_hours,
            "cross_layer_macro_max_abs": self.cross_layer_macro_max_abs,
            "cross_layer_industry_max_abs": self.cross_layer_industry_max_abs,
        }
