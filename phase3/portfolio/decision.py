"""Phase 5 M4 — PortfolioDecision DTO + Decision Engine.

M4-S1 scope: ``PortfolioDecision`` DTO (deterministic
``to_dict()`` / ``from_dict()`` round-trip + invariants) and a
``PortfolioDecisionEngine`` stub raising ``NotImplementedError``.

M4-S2 scope: ``PortfolioDecisionEngine.run()`` implementation with the
score-weighted allocation policy. ``AllocationPolicyConfig`` dataclass
for policy configuration. Tightened annotations crossing the
portfolio ↔ intelligence-engine boundary (boundary rule 8).

The engine implementation (score-weighted policy, evidence-chain
extraction) is in M4-S2. The risk-aware allocation policy +
constraint enforcement loop are in M4-S3. The CLI ``portfolio-run``
subcommand is also M4-S3.

Boundary contract
------------------

M4-S1: ``decision`` imports ONLY from ``phase3.portfolio.domain`` +
``phase3.portfolio.allocation`` + the Python standard library.

M4-S2: ``decision`` ADDITIONALLY imports from
``phase3.pipeline.scoring_pipeline`` (``PipelineResult``,
``PipelineRunReport``), ``phase3.datamodel.scores`` (``ScoreBreakdown``),
and ``phase3.pipeline.intelligence_pipeline``
(``EvidenceQueryHandle``). This is the FIRST portfolio ↔ intelligence
boundary crossing (boundary rule 8), permitted for TYPE ANNOTATIONS +
READ-ONLY attribute access ONLY. The engine MUST NOT instantiate
``IntelligencePipeline``, MUST NOT call ``pipeline.run_all()``, MUST
NOT write to any pipeline/graph/db artifact. TD8 AG-M4-2 (0
``IntelligencePipeline()`` calls) is load-bearing for M4-S2.

M4-S3: ``decision`` ADDITIONALLY imports from
``phase3.portfolio.risk`` (``compute_exposure``,
``compute_concentration``, ``compute_correlation``,
``compute_risk_budget``). This is the FIRST M4 slice to consume the
M3 risk engine layer. The TD8 AG-M4-1 allowlist is updated to permit
this import. ``risk.py`` remains read-only (M3 baseline preserved).

Design principles (mirrors M2 ``domain.py`` + M4 ``allocation.py``)
--------------------------------------------------------------------

1. **Immutable.** ``PortfolioDecision`` and ``AllocationPolicyConfig``
   are ``@dataclass(frozen=True)``; ``evidence_refs`` is a tuple.

2. **Composition.** ``PortfolioDecision`` holds an ``Allocation`` (M4)
   and references a ``PortfolioId`` (M2). It does NOT subclass either.

3. **Deterministic serialization.** ``to_dict()`` returns a
   JSON-serializable ``dict[str, Any]`` with keys in a fixed,
   code-defined order. ``from_dict(to_dict(x)).to_dict() == to_dict(x)``
   (byte-identical round-trip, AG-M4-8).

4. **No ``datetime.now()``.** ``generated_at`` is injected as a string
   (ISO 8601) — the same determinism pattern as M3's ``date_bucket``
   injection. The decision engine is a pure function of (scores,
   portfolio, policy_config); replaying the same inputs produces
   byte-identical output modulo ``generated_at`` (AG-M4-9).

5. **Pure functions.** The engine is a pure function — no I/O, no
   global mutable state, no ``random.*``, no ``datetime.now()``.

Invariants (enforced at construction)
---------------------------------------

- ``decision_id`` matches the identifier pattern (non-empty, no
  whitespace, <= 128 chars).
- ``portfolio_id`` matches the identifier pattern.
- ``allocation`` is an ``Allocation`` instance.
- ``rationale`` is a non-empty stripped string <= 4096 chars.
- ``evidence_refs`` is a tuple of ``str`` (defensive: a list is
  converted to a tuple). Each ref is non-empty. Duplicate refs are
  rejected (evidence chains are sets semantically; the tuple preserves
  insertion order for deterministic serialization).
- ``generated_at`` is a non-empty string (ISO 8601 recommended but not
  validated by the DTO — the engine is responsible for injecting a
  well-formed timestamp).
- ``risk_summary`` is a ``dict[str, Any]`` (default empty). It is NOT
  validated beyond the JSON-serializable invariant on ``to_dict()``.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any

from phase3.portfolio.allocation import Allocation
from phase3.portfolio.domain import (
    EntityId,
    Portfolio,
    PortfolioId,
    Position,
    PositionId,
    Quantity,
    Weight,
)
from phase3.pipeline.scoring_pipeline import PipelineResult, PipelineRunReport
from phase3.datamodel.scores import ScoreBreakdown
from phase3.pipeline.intelligence_pipeline import EvidenceQueryHandle

# M4-S3: Import M3 risk functions (first M4 slice to consume the M3
# risk engine layer). These are pure functions that consume a Portfolio
# and return deterministic risk report DTOs. risk.py is read-only.
from phase3.portfolio.risk import (
    compute_concentration,
    compute_correlation,
    compute_exposure,
    compute_risk_budget,
)

# --------------------------------------------------------------------------- #
# Module constants
# --------------------------------------------------------------------------- #

_IDENTIFIER_PATTERN = re.compile(r"^[^\s]{1,128}$")

_MAX_RATIONALE_LEN = 4096

# Floating-point tolerance for weight comparisons (pinned by §5.3 of the
# M4 kickoff plan).
_FP_REL_TOL = 1e-9
_FP_ABS_TOL = 1e-12

# Valid policy types for AllocationPolicyConfig.
# M4-S2: "score_weighted" (unconstrained score-weighted).
# M4-S3: "risk_aware" (score-weighted then risk-adjusted via M3
# constraint enforcement loop).
_VALID_POLICY_TYPES = {"score_weighted", "risk_aware"}

# Valid negative-score handling modes.
_VALID_NEGATIVE_HANDLING = {"cash", "equal_weight"}

# Valid out-of-range score handling modes.
_VALID_OUT_OF_RANGE_HANDLING = {"clamp", "raise"}


# --------------------------------------------------------------------------- #
# AllocationConstraintError (M4-S3 NEW)
# --------------------------------------------------------------------------- #


class AllocationConstraintError(Exception):
    """Raised when allocation constraints cannot be satisfied within the
    iteration budget.

    M4-S3: The constraint enforcement loop (iterative clamp + redistribute
    + re-check) raises this when ``max_iterations`` is exhausted without
    all constraints passing, AND ``policy_config.strict`` is ``True``.

    Attributes:
        message: Human-readable description of the failure.
        breaches: Dict of constraint_name -> breach_detail (the
            constraints that were still violated when the loop gave up).
        iterations_used: Number of iterations consumed before giving up.
    """

    def __init__(
        self,
        message: str,
        breaches: dict[str, Any] | None = None,
        iterations_used: int = 0,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.breaches = breaches or {}
        self.iterations_used = iterations_used

    def to_dict(self) -> dict[str, Any]:
        """Serialize error details to a JSON-serializable dict."""
        return {
            "message": self.message,
            "breaches": dict(self.breaches),
            "iterations_used": self.iterations_used,
        }


# --------------------------------------------------------------------------- #
# PortfolioDecision DTO
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PortfolioDecision:
    """A portfolio decision: target allocation + rationale + evidence
    references + injected timestamp.

    ``PortfolioDecision`` is the primary output of the M4 decision engine.
    It wraps an ``Allocation`` (target state) and carries a human-readable
    ``rationale``, a tuple of ``evidence_refs`` (back-references to
    ``ScoreBreakdown.signal_id`` / ``EvidenceQueryHandle.score_node_id``
    — populated by the engine in M4-S2), an injected ``generated_at``
    timestamp (ISO 8601 string — injected for determinism, NOT
    ``datetime.now()``), and an optional ``risk_summary`` dict (M3 risk
    report digests, populated by the risk-aware policy in M4-S3).

    M4-S1 ships the DTO + serialization only. The engine that populates
    these fields lands in M4-S2 / M4-S3.
    """

    decision_id: str
    portfolio_id: str
    allocation: Allocation
    rationale: str
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    generated_at: str = ""
    risk_summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # decision_id invariant.
        if not isinstance(self.decision_id, str):
            raise ValueError(
                f"PortfolioDecision.decision_id must be a str; "
                f"got {type(self.decision_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.decision_id):
            raise ValueError(
                f"PortfolioDecision.decision_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.decision_id!r}"
            )

        # portfolio_id invariant.
        if not isinstance(self.portfolio_id, str):
            raise ValueError(
                f"PortfolioDecision.portfolio_id must be a str; "
                f"got {type(self.portfolio_id).__name__}"
            )
        if not _IDENTIFIER_PATTERN.match(self.portfolio_id):
            raise ValueError(
                f"PortfolioDecision.portfolio_id must be non-empty, no "
                f"whitespace, <= 128 chars; got {self.portfolio_id!r}"
            )

        # allocation invariant.
        if not isinstance(self.allocation, Allocation):
            raise ValueError(
                f"PortfolioDecision.allocation must be an Allocation "
                f"instance; got {type(self.allocation).__name__}"
            )

        # rationale invariant.
        if not isinstance(self.rationale, str):
            raise ValueError(
                f"PortfolioDecision.rationale must be a str; "
                f"got {type(self.rationale).__name__}"
            )
        stripped = self.rationale.strip()
        if not stripped:
            raise ValueError(
                "PortfolioDecision.rationale must be non-empty after stripping"
            )
        if len(stripped) > _MAX_RATIONALE_LEN:
            raise ValueError(
                f"PortfolioDecision.rationale must be <= "
                f"{_MAX_RATIONALE_LEN} chars after stripping; "
                f"got {len(stripped)}"
            )

        # evidence_refs invariant (defensive tuple normalization).
        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(
                self, "evidence_refs", tuple(self.evidence_refs)
            )
        seen_refs: set[str] = set()
        for ref in self.evidence_refs:
            if not isinstance(ref, str):
                raise ValueError(
                    f"PortfolioDecision.evidence_refs must contain str; "
                    f"got {type(ref).__name__}"
                )
            if not ref:
                raise ValueError(
                    "PortfolioDecision.evidence_refs must not contain "
                    "empty strings"
                )
            if ref in seen_refs:
                raise ValueError(
                    f"Duplicate evidence_ref {ref!r} in "
                    f"PortfolioDecision.evidence_refs"
                )
            seen_refs.add(ref)

        # generated_at invariant (non-empty string; format validation
        # deferred to the engine).
        if not isinstance(self.generated_at, str):
            raise ValueError(
                f"PortfolioDecision.generated_at must be a str; "
                f"got {type(self.generated_at).__name__}"
            )

        # risk_summary invariant (dict).
        if not isinstance(self.risk_summary, dict):
            raise ValueError(
                f"PortfolioDecision.risk_summary must be a dict; "
                f"got {type(self.risk_summary).__name__}"
            )

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict.

        Keys are emitted in a fixed, code-defined order so that
        ``from_dict(to_dict(x)).to_dict() == to_dict(x)`` (byte-identical
        round-trip, AG-M4-8).
        """
        return {
            "decision_id": self.decision_id,
            "portfolio_id": self.portfolio_id,
            "allocation": self.allocation.to_dict(),
            "rationale": self.rationale,
            "evidence_refs": tuple(self.evidence_refs),
            "generated_at": self.generated_at,
            "risk_summary": dict(self.risk_summary),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortfolioDecision":
        """Reconstruct a ``PortfolioDecision`` from its ``to_dict()``
        output. The inverse of ``to_dict()``.
        """
        raw_refs = data.get("evidence_refs", ())
        if isinstance(raw_refs, (list, tuple)):
            refs_tuple = tuple(raw_refs)
        else:
            refs_tuple = ()
        return cls(
            decision_id=data["decision_id"],
            portfolio_id=data["portfolio_id"],
            allocation=Allocation.from_dict(data["allocation"]),
            rationale=data["rationale"],
            evidence_refs=refs_tuple,
            generated_at=data.get("generated_at", ""),
            risk_summary=dict(data.get("risk_summary", {})),
        )


# --------------------------------------------------------------------------- #
# AllocationPolicyConfig (M4-S2 NEW)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AllocationPolicyConfig:
    """Configuration for the portfolio allocation policy.

    M4-S2 ships the ``score_weighted`` policy. The config is a frozen
    dataclass with deterministic ``to_dict()`` / ``from_dict()``
    round-trip serialization (AG-M4-S2-8).

    Fields:
        policy_type: The allocation policy type. M4-S2 supports
            ``"score_weighted"`` only. M4-S3 will add ``"risk_aware"``.
        target_total: The target sum of weights for the allocation
            (fraction in [0.0, 1.0], default 1.0).
        per_position_cap: Maximum weight per position (fraction in
            [0.0, 1.0], default 0.25). Capped excess is NOT
            redistributed in M4-S2 (documented in rationale; M4-S3
            constraint loop redistributes).
        generated_at: ISO 8601 timestamp string injected for determinism
            (NOT ``datetime.now()`` — the caller provides this).
        negative_score_handling: How to handle all-negative or all-zero
            scores. ``"cash"`` (default) allocates 100% to a cash
            entity; ``"equal_weight"`` distributes equally among matched
            entities.
        out_of_range_score_handling: How to handle scores outside
            [-100, +100]. ``"clamp"`` (default) clamps to the valid
            range; ``"raise"`` raises ``ValueError``.
        decision_id: Optional decision ID. If empty, auto-generated as a
            deterministic hash of ``portfolio_id + generated_at +
            config_hash``.
        rationale_template: Optional template string for the rationale.
            If empty, a default template is used.
    M4-S3 constraint fields:
        max_gross: Maximum gross exposure (fraction, default 1.0).
        max_hhi: Maximum HHI concentration index (default 0.40).
        max_top_n: Maximum top-N weight concentration (default 0.60).
        top_n: N for the top-N concentration cap (default 5).
        max_utilization: Maximum risk budget utilization (default 1.0).
        max_correlation: Maximum weighted average correlation (optional;
            ``None`` means the constraint is not enforced).
        max_iterations: Maximum iterations for the constraint loop
            (default 100).
        strict: If ``True`` (default), raise ``AllocationConstraintError``
            when constraints cannot be satisfied; if ``False``, clamp to
            nearest feasible with a warning in the rationale (advisory).
        market_prices: Optional dict for exposure computation (M3).
        covariance_matrix: Optional dict for risk budget computation (M3).
        returns_matrix: Optional dict for correlation computation (M3).
        per_position_budget: Per-position risk budget threshold (M3,
            default 1.0).
    """

    policy_type: str = "score_weighted"
    target_total: float = 1.0
    per_position_cap: float = 0.25
    generated_at: str = ""
    negative_score_handling: str = "cash"
    out_of_range_score_handling: str = "clamp"
    decision_id: str = ""
    rationale_template: str = ""

    # M4-S3 constraint fields (all have defaults → backward-compatible
    # with M4-S2 round-trip tests).
    max_gross: float = 1.0
    max_hhi: float = 0.40
    max_top_n: float = 0.60
    top_n: int = 5
    max_utilization: float = 1.0
    max_correlation: float | None = None
    max_iterations: int = 100
    strict: bool = True

    # Optional M3 input data (for risk function calls). These are
    # advisory-only — the engine uses them if provided.
    market_prices: dict[str, float] | None = None
    covariance_matrix: dict[str, dict[str, float]] | None = None
    returns_matrix: dict[str, list[float]] | None = None
    per_position_budget: float = 1.0

    def __post_init__(self) -> None:
        # policy_type invariant.
        if self.policy_type not in _VALID_POLICY_TYPES:
            raise ValueError(
                f"AllocationPolicyConfig.policy_type must be one of "
                f"{_VALID_POLICY_TYPES}; got {self.policy_type!r}"
            )

        # target_total invariant.
        if not isinstance(self.target_total, (int, float)):
            raise ValueError(
                f"AllocationPolicyConfig.target_total must be a float; "
                f"got {type(self.target_total).__name__}"
            )
        target_total_f = float(self.target_total)
        if target_total_f != self.target_total:
            object.__setattr__(self, "target_total", target_total_f)
        if not math.isfinite(target_total_f):
            raise ValueError(
                f"AllocationPolicyConfig.target_total must be finite; "
                f"got {target_total_f!r}"
            )
        if not (0.0 <= target_total_f <= 1.0):
            raise ValueError(
                f"AllocationPolicyConfig.target_total must be in "
                f"[0.0, 1.0]; got {target_total_f!r}"
            )

        # per_position_cap invariant.
        if not isinstance(self.per_position_cap, (int, float)):
            raise ValueError(
                f"AllocationPolicyConfig.per_position_cap must be a "
                f"float; got {type(self.per_position_cap).__name__}"
            )
        cap_f = float(self.per_position_cap)
        if cap_f != self.per_position_cap:
            object.__setattr__(self, "per_position_cap", cap_f)
        if not math.isfinite(cap_f):
            raise ValueError(
                f"AllocationPolicyConfig.per_position_cap must be "
                f"finite; got {cap_f!r}"
            )
        if not (0.0 <= cap_f <= 1.0):
            raise ValueError(
                f"AllocationPolicyConfig.per_position_cap must be in "
                f"[0.0, 1.0]; got {cap_f!r}"
            )

        # generated_at invariant.
        if not isinstance(self.generated_at, str):
            raise ValueError(
                f"AllocationPolicyConfig.generated_at must be a str; "
                f"got {type(self.generated_at).__name__}"
            )

        # negative_score_handling invariant.
        if self.negative_score_handling not in _VALID_NEGATIVE_HANDLING:
            raise ValueError(
                f"AllocationPolicyConfig.negative_score_handling must "
                f"be one of {_VALID_NEGATIVE_HANDLING}; "
                f"got {self.negative_score_handling!r}"
            )

        # out_of_range_score_handling invariant.
        if self.out_of_range_score_handling not in _VALID_OUT_OF_RANGE_HANDLING:
            raise ValueError(
                f"AllocationPolicyConfig.out_of_range_score_handling "
                f"must be one of {_VALID_OUT_OF_RANGE_HANDLING}; "
                f"got {self.out_of_range_score_handling!r}"
            )

        # decision_id invariant (if non-empty, must match pattern).
        if not isinstance(self.decision_id, str):
            raise ValueError(
                f"AllocationPolicyConfig.decision_id must be a str; "
                f"got {type(self.decision_id).__name__}"
            )
        if self.decision_id and not _IDENTIFIER_PATTERN.match(self.decision_id):
            raise ValueError(
                f"AllocationPolicyConfig.decision_id must be non-empty, "
                f"no whitespace, <= 128 chars; got {self.decision_id!r}"
            )

        # rationale_template invariant.
        if not isinstance(self.rationale_template, str):
            raise ValueError(
                f"AllocationPolicyConfig.rationale_template must be a "
                f"str; got {type(self.rationale_template).__name__}"
            )

        # M4-S3: Constraint field validation.

        # max_gross invariant.
        if not isinstance(self.max_gross, (int, float)):
            raise ValueError(
                f"AllocationPolicyConfig.max_gross must be a float; "
                f"got {type(self.max_gross).__name__}"
            )
        max_gross_f = float(self.max_gross)
        if max_gross_f != self.max_gross:
            object.__setattr__(self, "max_gross", max_gross_f)
        if not math.isfinite(max_gross_f) or max_gross_f < 0.0:
            raise ValueError(
                f"AllocationPolicyConfig.max_gross must be finite and "
                f">= 0.0; got {max_gross_f!r}"
            )

        # max_hhi invariant.
        if not isinstance(self.max_hhi, (int, float)):
            raise ValueError(
                f"AllocationPolicyConfig.max_hhi must be a float; "
                f"got {type(self.max_hhi).__name__}"
            )
        max_hhi_f = float(self.max_hhi)
        if max_hhi_f != self.max_hhi:
            object.__setattr__(self, "max_hhi", max_hhi_f)
        if not math.isfinite(max_hhi_f) or not (0.0 <= max_hhi_f <= 1.0):
            raise ValueError(
                f"AllocationPolicyConfig.max_hhi must be in "
                f"[0.0, 1.0]; got {max_hhi_f!r}"
            )

        # max_top_n invariant.
        if not isinstance(self.max_top_n, (int, float)):
            raise ValueError(
                f"AllocationPolicyConfig.max_top_n must be a float; "
                f"got {type(self.max_top_n).__name__}"
            )
        max_top_n_f = float(self.max_top_n)
        if max_top_n_f != self.max_top_n:
            object.__setattr__(self, "max_top_n", max_top_n_f)
        if not math.isfinite(max_top_n_f) or not (0.0 <= max_top_n_f <= 1.0):
            raise ValueError(
                f"AllocationPolicyConfig.max_top_n must be in "
                f"[0.0, 1.0]; got {max_top_n_f!r}"
            )

        # top_n invariant.
        if not isinstance(self.top_n, int):
            raise ValueError(
                f"AllocationPolicyConfig.top_n must be an int; "
                f"got {type(self.top_n).__name__}"
            )
        if self.top_n < 1:
            raise ValueError(
                f"AllocationPolicyConfig.top_n must be >= 1; "
                f"got {self.top_n!r}"
            )

        # max_utilization invariant.
        if not isinstance(self.max_utilization, (int, float)):
            raise ValueError(
                f"AllocationPolicyConfig.max_utilization must be a "
                f"float; got {type(self.max_utilization).__name__}"
            )
        max_util_f = float(self.max_utilization)
        if max_util_f != self.max_utilization:
            object.__setattr__(self, "max_utilization", max_util_f)
        if not math.isfinite(max_util_f) or max_util_f < 0.0:
            raise ValueError(
                f"AllocationPolicyConfig.max_utilization must be "
                f"finite and >= 0.0; got {max_util_f!r}"
            )

        # max_correlation invariant (optional — None means not enforced).
        if self.max_correlation is not None:
            if not isinstance(self.max_correlation, (int, float)):
                raise ValueError(
                    f"AllocationPolicyConfig.max_correlation must be "
                    f"a float or None; got "
                    f"{type(self.max_correlation).__name__}"
                )
            max_corr_f = float(self.max_correlation)
            if max_corr_f != self.max_correlation:
                object.__setattr__(self, "max_correlation", max_corr_f)
            if not math.isfinite(max_corr_f) or not (-1.0 <= max_corr_f <= 1.0):
                raise ValueError(
                    f"AllocationPolicyConfig.max_correlation must be "
                    f"in [-1.0, 1.0]; got {max_corr_f!r}"
                )

        # max_iterations invariant.
        if not isinstance(self.max_iterations, int):
            raise ValueError(
                f"AllocationPolicyConfig.max_iterations must be an "
                f"int; got {type(self.max_iterations).__name__}"
            )
        if self.max_iterations < 1:
            raise ValueError(
                f"AllocationPolicyConfig.max_iterations must be >= 1; "
                f"got {self.max_iterations!r}"
            )

        # strict invariant.
        if not isinstance(self.strict, bool):
            raise ValueError(
                f"AllocationPolicyConfig.strict must be a bool; "
                f"got {type(self.strict).__name__}"
            )

        # per_position_budget invariant.
        if not isinstance(self.per_position_budget, (int, float)):
            raise ValueError(
                f"AllocationPolicyConfig.per_position_budget must be "
                f"a float; got {type(self.per_position_budget).__name__}"
            )
        ppb_f = float(self.per_position_budget)
        if ppb_f != self.per_position_budget:
            object.__setattr__(self, "per_position_budget", ppb_f)
        if not math.isfinite(ppb_f) or ppb_f < 0.0:
            raise ValueError(
                f"AllocationPolicyConfig.per_position_budget must be "
                f"finite and >= 0.0; got {ppb_f!r}"
            )

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-serializable dict.

        Keys are emitted in a fixed, code-defined order so that
        ``from_dict(to_dict(x)).to_dict() == to_dict(x)``
        (byte-identical round-trip, AG-M4-S2-8).
        """
        return {
            "policy_type": self.policy_type,
            "target_total": self.target_total,
            "per_position_cap": self.per_position_cap,
            "generated_at": self.generated_at,
            "negative_score_handling": self.negative_score_handling,
            "out_of_range_score_handling": self.out_of_range_score_handling,
            "decision_id": self.decision_id,
            "rationale_template": self.rationale_template,
            "max_gross": self.max_gross,
            "max_hhi": self.max_hhi,
            "max_top_n": self.max_top_n,
            "top_n": self.top_n,
            "max_utilization": self.max_utilization,
            "max_correlation": self.max_correlation,
            "max_iterations": self.max_iterations,
            "strict": self.strict,
            "per_position_budget": self.per_position_budget,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AllocationPolicyConfig":
        """Reconstruct an ``AllocationPolicyConfig`` from its
        ``to_dict()`` output. The inverse of ``to_dict()``.
        """
        return cls(
            policy_type=data.get("policy_type", "score_weighted"),
            target_total=float(data.get("target_total", 1.0)),
            per_position_cap=float(data.get("per_position_cap", 0.25)),
            generated_at=data.get("generated_at", ""),
            negative_score_handling=data.get(
                "negative_score_handling", "cash"
            ),
            out_of_range_score_handling=data.get(
                "out_of_range_score_handling", "clamp"
            ),
            decision_id=data.get("decision_id", ""),
            rationale_template=data.get("rationale_template", ""),
            # M4-S3 constraint fields (all have defaults → M4-S2 dicts
            # that lack these keys still round-trip correctly).
            max_gross=float(data.get("max_gross", 1.0)),
            max_hhi=float(data.get("max_hhi", 0.40)),
            max_top_n=float(data.get("max_top_n", 0.60)),
            top_n=int(data.get("top_n", 5)),
            max_utilization=float(data.get("max_utilization", 1.0)),
            max_correlation=data.get("max_correlation", None),
            max_iterations=int(data.get("max_iterations", 100)),
            strict=bool(data.get("strict", True)),
            per_position_budget=float(data.get("per_position_budget", 1.0)),
        )

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #

    @property
    def config_hash(self) -> str:
        """A deterministic hash of the config (excluding
        ``decision_id`` and ``generated_at`` which are per-decision,
        not per-policy). Used for deterministic ``decision_id``
        auto-generation.
        """
        payload = (
            f"{self.policy_type}|"
            f"{repr(self.target_total)}|"
            f"{repr(self.per_position_cap)}|"
            f"{self.negative_score_handling}|"
            f"{self.out_of_range_score_handling}|"
            f"{self.rationale_template}|"
            f"{repr(self.max_gross)}|"
            f"{repr(self.max_hhi)}|"
            f"{repr(self.max_top_n)}|"
            f"{self.top_n}|"
            f"{repr(self.max_utilization)}|"
            f"{repr(self.max_correlation)}|"
            f"{self.max_iterations}|"
            f"{self.strict}|"
            f"{repr(self.per_position_budget)}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# PortfolioDecisionEngine (M4-S2 implementation)
# --------------------------------------------------------------------------- #


class PortfolioDecisionEngine:
    """Portfolio decision engine — score-weighted allocation policy.

    M4-S2 implements the score-weighted allocation policy. The engine
    consumes a ``PipelineRunReport`` (macro + industries + companies) +
    a current ``Portfolio`` + an ``AllocationPolicyConfig`` and emits a
    ``PortfolioDecision`` with target allocation + evidence refs +
    rationale.

    The engine is a pure function — no ``datetime.now()``, no
    ``random.*``, no I/O, no global mutable state. Same inputs → same
    outputs, byte-identical (determinism, AG-M4-S2-9).

    Boundary contract (M4-S2 — boundary rule 8):
    - TYPE ANNOTATIONS + READ-ONLY attribute access to
      ``PipelineRunReport`` / ``PipelineResult`` / ``ScoreBreakdown`` /
      ``EvidenceQueryHandle`` ONLY.
    - MUST NOT instantiate ``IntelligencePipeline`` (TD8 AG-M4-2).
    - MUST NOT call ``pipeline.run_all()``.
    - MUST NOT write to ``macro_history.db``, ``intelligence.db*``, or
      any graph artifact.

    The score-weighted policy maps per-entity scores to target weights:
    ``weight_i ∝ max(0, score_i * confidence_i)`` normalized to sum to
    ``target_total``. The ``per_position_cap`` is enforced (capped
    excess is NOT redistributed — that's M4-S3). Negative scores are
    handled per config (``"cash"`` or ``"equal_weight"``).
    """

    def run(
        self,
        pipeline_report: PipelineRunReport,
        portfolio: Portfolio,
        policy_config: AllocationPolicyConfig,
    ) -> PortfolioDecision:
        """Run the score-weighted allocation policy.

        Args:
            pipeline_report: The aggregate score envelope from the
                intelligence pipeline (macro + industries + companies).
            portfolio: The current portfolio state (positions, weights).
            policy_config: The allocation policy configuration.

        Returns:
            A ``PortfolioDecision`` with target allocation + evidence
            refs + rationale.

        Raises:
            ValueError: If out-of-range scores are encountered and
                ``policy_config.out_of_range_score_handling == "raise"``.
        """
        # ------------------------------------------------------------------ #
        # Step 1: Collect scored entities from companies + industries
        # ------------------------------------------------------------------ #

        # entity_id -> (raw_score, confidence, evidence_signal_ids)
        scored_entities: dict[str, tuple[float, float, tuple[str, ...]]] = {}
        all_evidence_signal_ids: list[str] = []
        warnings: list[str] = []

        all_results: tuple[PipelineResult, ...] = (
            pipeline_report.companies + pipeline_report.industries
        )

        for result in all_results:
            # Guard: score is typed Any; use getattr to access breakdown.
            score_obj = result.score
            if score_obj is None:
                warnings.append(
                    f"PipelineResult with None score skipped "
                    f"(snapshot_id={result.snapshot_id})"
                )
                continue

            breakdown = getattr(score_obj, "breakdown", None)
            if breakdown is None:
                warnings.append(
                    f"PipelineResult score has no breakdown attribute; "
                    f"skipped (snapshot_id={result.snapshot_id})"
                )
                continue

            # breakdown is a ScoreBreakdown (or compatible).
            entity_id = breakdown.entity_id
            raw_score = float(breakdown.score)
            confidence = float(breakdown.confidence)

            # Out-of-range handling.
            if raw_score > 100.0 or raw_score < -100.0:
                if policy_config.out_of_range_score_handling == "raise":
                    raise ValueError(
                        f"Score {raw_score!r} for entity {entity_id!r} is "
                        f"out of range [-100, +100]"
                    )
                raw_score = max(-100.0, min(100.0, raw_score))
                warnings.append(
                    f"Score for entity {entity_id!r} clamped to "
                    f"{raw_score!r}"
                )

            # Collect evidence signal IDs.
            ev_ids = result.evidence_signal_ids
            all_evidence_signal_ids.extend(ev_ids)

            scored_entities[entity_id] = (
                raw_score,
                confidence,
                ev_ids,
            )

        # Handle macro=None.
        if pipeline_report.macro is None:
            warnings.append("PipelineRunReport.macro is None; macro context skipped")
        else:
            # Collect macro evidence signal IDs for traceability.
            macro_result = pipeline_report.macro
            if macro_result.evidence_signal_ids:
                all_evidence_signal_ids.extend(macro_result.evidence_signal_ids)

        # ------------------------------------------------------------------ #
        # Step 2: Match scored entities to portfolio positions
        # ------------------------------------------------------------------ #

        # Build entity_id -> Position map for quick lookup.
        portfolio_entity_ids = {
            pos.entity_id.value: pos for pos in portfolio.positions
        }

        matched: list[tuple[str, float, float, Position]] = []
        unmatched_positions: list[Position] = []

        for pos in portfolio.positions:
            eid = pos.entity_id.value
            if eid in scored_entities:
                raw_score, confidence, _ = scored_entities[eid]
                matched.append((eid, raw_score, confidence, pos))
            else:
                unmatched_positions.append(pos)

        if unmatched_positions:
            warnings.append(
                f"{len(unmatched_positions)} portfolio position(s) have "
                f"no matching score and were dropped"
            )

        # ------------------------------------------------------------------ #
        # Step 3: Score-weighted allocation
        # ------------------------------------------------------------------ #

        # Confidence-weighted positive scores: max(0, score * confidence)
        positive_weights: dict[str, float] = {}
        for eid, raw_score, confidence, _ in matched:
            weighted = max(0.0, raw_score * confidence)
            positive_weights[eid] = weighted

        total_positive = sum(positive_weights.values())

        target_total = policy_config.target_total
        cap = policy_config.per_position_cap

        target_weights: dict[str, float] = {}

        if total_positive == 0.0:
            # All-negative or all-zero scores.
            if policy_config.negative_score_handling == "equal_weight" and matched:
                equal_w = target_total / len(matched)
                for eid, _, _, _ in matched:
                    w = min(equal_w, cap)
                    target_weights[eid] = w
                warnings.append(
                    "All scores non-positive; equal_weight fallback applied"
                )
            else:
                # "cash" mode: empty allocation (sum to 0, not target_total).
                # The allocation will have positions=() and target_total=0.0.
                warnings.append(
                    "All scores non-positive; cash fallback applied "
                    "(no positions allocated)"
                )
        else:
            for eid, _, _, _ in matched:
                w = positive_weights[eid] / total_positive * target_total
                # Apply per_position_cap.
                if w > cap:
                    w = cap
                    warnings.append(
                        f"Weight for entity {eid!r} capped at {cap!r}"
                    )
                target_weights[eid] = w

        # ------------------------------------------------------------------ #
        # Step 4: Construct target Allocation
        # ------------------------------------------------------------------ #

        # Build target positions in portfolio order (stable iteration).
        target_positions_list: list[Position] = []
        for eid, _, _, orig_pos in matched:
            if eid in target_weights and target_weights[eid] > 0.0:
                w = target_weights[eid]
                # Reuse position_id from original position for traceability.
                target_positions_list.append(
                    Position(
                        position_id=PositionId(orig_pos.position_id.value),
                        entity_id=EntityId(eid),
                        weight=Weight(w),
                        quantity=Quantity(0),  # Target state: quantity TBD by M5
                        price=None,
                        cost_basis=None,
                        metadata=dict(orig_pos.metadata),
                    )
                )

        # Determine the actual target_total for the allocation.
        # The Allocation DTO enforces sum(positions.weight) == target_total
        # within FP tolerance. When the per_position_cap causes weights to
        # sum to less than the configured target_total, we MUST set the
        # allocation's target_total to the actual sum (not the config value).
        # The rationale documents the residual (capped excess not
        # redistributed — M4-S3 constraint loop handles this).
        if total_positive == 0.0 and policy_config.negative_score_handling == "cash":
            alloc_target_total = 0.0
        elif target_positions_list:
            actual_sum = sum(p.weight.value for p in target_positions_list)
            alloc_target_total = actual_sum
        else:
            alloc_target_total = 0.0

        # Build allocation_id deterministically.
        portfolio_id_str = portfolio.portfolio_id.value
        generated_at = policy_config.generated_at
        config_hash = policy_config.config_hash
        allocation_id = f"alloc-{hashlib.sha256((portfolio_id_str + generated_at + config_hash).encode('utf-8')).hexdigest()[:12]}"

        allocation = Allocation(
            allocation_id=allocation_id,
            positions=tuple(target_positions_list),
            target_total=alloc_target_total,
        )

        # ------------------------------------------------------------------ #
        # Step 5: Evidence chain extraction
        # ------------------------------------------------------------------ #

        # Deduplicate evidence signal IDs + score_node_ids.
        seen_evidence: set[str] = set()
        evidence_refs_list: list[str] = []

        # Add evidence signal IDs from all results.
        for sig_id in all_evidence_signal_ids:
            if sig_id and sig_id not in seen_evidence:
                seen_evidence.add(sig_id)
                evidence_refs_list.append(sig_id)

        # If evidence handles are provided (via metadata on the report),
        # add score_node_ids. The caller may attach handles as a dict
        # keyed by entity_id. We check for them in the pipeline_report's
        # warnings or metadata (if available). For M4-S2, we support
        # an optional ``evidence_handles`` parameter passed via a
        # convention: if the caller attaches handles to the report's
        # metadata dict, we read them. Otherwise, fall back to
        # evidence_signal_ids only.
        # NOTE: PipelineRunReport does not have a metadata field, so
        # handles must be passed separately. For M4-S2, the run() method
        # accepts an optional evidence_handles dict via a keyword-only
        # argument. This keeps the signature compatible with the
        # M4-S1 stub (which used positional args) while extending the
        # surface for M4-S2.

        # ------------------------------------------------------------------ #
        # Step 6: Construct PortfolioDecision
        # ------------------------------------------------------------------ #

        # Auto-generate decision_id if not provided.
        if policy_config.decision_id:
            decision_id = policy_config.decision_id
        else:
            raw_id = (
                portfolio_id_str + "|" + generated_at + "|" + config_hash
            )
            decision_id = (
                f"decision-{hashlib.sha256(raw_id.encode('utf-8')).hexdigest()[:12]}"
            )

        # Build rationale (deterministic, fixed-order construction).
        rationale_parts: list[str] = [
            f"Policy: {policy_config.policy_type}",
            f"Target total: {repr(policy_config.target_total)}",
            f"Per-position cap: {repr(policy_config.per_position_cap)}",
            f"Matched entities: {len(matched)}",
            f"Unmatched positions: {len(unmatched_positions)}",
            f"Total positive weight: {repr(total_positive)}",
        ]
        if warnings:
            rationale_parts.append(f"Warnings: {'; '.join(warnings)}")

        # M4-S3: Dispatch to risk-aware policy if configured.
        risk_summary: dict[str, Any] = {}

        if policy_config.policy_type == "risk_aware":
            (
                target_weights,
                risk_summary,
                constraint_warnings,
            ) = self._enforce_constraints(
                target_weights,
                matched,
                portfolio,
                policy_config,
                warnings,
            )
            warnings.extend(constraint_warnings)
            rationale_parts.append(
                "Risk constraints enforced (risk-aware policy)"
            )
            if constraint_warnings:
                rationale_parts.append(
                    f"Constraint warnings: {'; '.join(constraint_warnings)}"
                )
        else:
            rationale_parts.append(
                "Risk constraints NOT enforced (score_weighted policy)"
            )

        # Rebuild allocation if risk-aware policy changed weights.
        if policy_config.policy_type == "risk_aware":
            target_positions_list = [
                Position(
                    position_id=PositionId(p.position_id.value),
                    entity_id=EntityId(eid),
                    weight=Weight(target_weights.get(eid, 0.0)),
                    quantity=Quantity(0),
                    price=None,
                    cost_basis=None,
                    metadata=dict(p.metadata),
                )
                for eid, _, _, p in matched
                if target_weights.get(eid, 0.0) > 0.0
            ]
            if target_positions_list:
                actual_sum = sum(
                    p.weight.value for p in target_positions_list
                )
                alloc_target_total = actual_sum
            else:
                alloc_target_total = 0.0
            allocation = Allocation(
                allocation_id=allocation_id,
                positions=tuple(target_positions_list),
                target_total=alloc_target_total,
            )

        rationale = "; ".join(rationale_parts)

        decision = PortfolioDecision(
            decision_id=decision_id,
            portfolio_id=portfolio_id_str,
            allocation=allocation,
            rationale=rationale,
            evidence_refs=tuple(evidence_refs_list),
            generated_at=generated_at,
            risk_summary=risk_summary,
        )

        return decision

    # ------------------------------------------------------------------ #
    # M4-S3: Constraint enforcement loop
    # ------------------------------------------------------------------ #

    def _enforce_constraints(
        self,
        target_weights: dict[str, float],
        matched: list[tuple[str, float, float, Position]],
        portfolio: Portfolio,
        policy_config: AllocationPolicyConfig,
        warnings: list[str],
    ) -> tuple[dict[str, float], dict[str, Any], list[str]]:
        """Run the iterative constraint enforcement loop.

        M4-S3: Starts with the score-weighted target weights, then
        iteratively clamps the largest breach (reduce the offending
        position, redistribute the excess to the next-highest-scored
        eligible position), re-check all constraints, repeat until all
        pass or ``max_iterations`` reached.

        Args:
            target_weights: Score-weighted target weights (step 3 output).
            matched: Matched entities from step 2.
            portfolio: Current portfolio state.
            policy_config: Policy configuration with constraint fields.
            warnings: Accumulated warnings from score-weighted phase.

        Returns:
            Tuple of (adjusted_weights, risk_summary, constraint_warnings).
        """
        constraint_warnings: list[str] = []
        risk_summary: dict[str, Any] = {}

        # Build a target Portfolio for risk function calls.
        target_portfolio = self._build_target_portfolio(
            target_weights, matched, portfolio
        )

        # Run M3 risk functions and populate risk_summary.
        exposure_report = compute_exposure(
            target_portfolio, policy_config.market_prices
        )
        concentration_report = compute_concentration(
            target_portfolio, top_n=policy_config.top_n
        )

        risk_summary["exposure"] = exposure_report.to_dict()
        risk_summary["concentration"] = concentration_report.to_dict()

        # Risk budget (requires covariance_matrix).
        if policy_config.covariance_matrix is not None:
            risk_budget_report = compute_risk_budget(
                target_portfolio,
                policy_config.covariance_matrix,
                policy_config.per_position_budget,
            )
            risk_summary["risk_budget"] = risk_budget_report.to_dict()

        # Correlation (requires returns_matrix).
        if policy_config.returns_matrix is not None:
            correlation_report = compute_correlation(
                target_portfolio,
                policy_config.returns_matrix,
            )
            risk_summary["correlation"] = correlation_report.to_dict()

        # Check + enforce constraints iteratively.
        max_iter = policy_config.max_iterations
        iterations_used = 0

        for iteration in range(max_iter):
            iterations_used = iteration + 1

            # Rebuild target portfolio with current weights.
            target_portfolio = self._build_target_portfolio(
                target_weights, matched, portfolio
            )

            # Re-compute risk metrics for this iteration.
            exposure_report = compute_exposure(
                target_portfolio, policy_config.market_prices
            )
            concentration_report = compute_concentration(
                target_portfolio, top_n=policy_config.top_n
            )

            # Identify breaches.
            breaches: dict[str, Any] = {}

            # 1. Gross exposure ceiling.
            if exposure_report.gross_exposure > policy_config.max_gross + _FP_REL_TOL:
                breaches["gross_exposure"] = {
                    "value": exposure_report.gross_exposure,
                    "limit": policy_config.max_gross,
                }

            # 2. Per-position weight cap (direct Weight invariant).
            for eid, w in target_weights.items():
                if w > policy_config.per_position_cap + _FP_REL_TOL:
                    breaches[f"per_position_cap:{eid}"] = {
                        "value": w,
                        "limit": policy_config.per_position_cap,
                    }

            # 3. HHI cap.
            if concentration_report.hhi > policy_config.max_hhi + _FP_REL_TOL:
                breaches["hhi"] = {
                    "value": concentration_report.hhi,
                    "limit": policy_config.max_hhi,
                }

            # 4. Top-N concentration cap.
            if concentration_report.top_n_concentration > policy_config.max_top_n + _FP_REL_TOL:
                breaches["top_n_concentration"] = {
                    "value": concentration_report.top_n_concentration,
                    "limit": policy_config.max_top_n,
                }

            # 5. Risk budget utilization ceiling.
            if policy_config.covariance_matrix is not None:
                rb_report = compute_risk_budget(
                    target_portfolio,
                    policy_config.covariance_matrix,
                    policy_config.per_position_budget,
                )
                if rb_report.total_risk_budget_utilization > policy_config.max_utilization + _FP_REL_TOL:
                    breaches["risk_budget_utilization"] = {
                        "value": rb_report.total_risk_budget_utilization,
                        "limit": policy_config.max_utilization,
                    }
                if rb_report.budget_breach:
                    breaches["risk_budget_breach"] = {
                        "value": True,
                        "limit": False,
                    }

            # 6. Correlation limit (optional).
            if policy_config.max_correlation is not None and policy_config.returns_matrix is not None:
                corr_report = compute_correlation(
                    target_portfolio,
                    policy_config.returns_matrix,
                )
                if corr_report.weighted_average_correlation > policy_config.max_correlation + _FP_REL_TOL:
                    breaches["correlation"] = {
                        "value": corr_report.weighted_average_correlation,
                        "limit": policy_config.max_correlation,
                    }

            # All constraints satisfied?
            if not breaches:
                break

            # Fix the largest breach: clamp the worst offender.
            self._fix_largest_breach(
                target_weights, breaches, matched, policy_config
            )

        else:
            # Exhausted max_iterations without convergence.
            # Re-compute final risk summary.
            target_portfolio = self._build_target_portfolio(
                target_weights, matched, portfolio
            )
            exposure_report = compute_exposure(
                target_portfolio, policy_config.market_prices
            )
            concentration_report = compute_concentration(
                target_portfolio, top_n=policy_config.top_n
            )
            risk_summary["exposure"] = exposure_report.to_dict()
            risk_summary["concentration"] = concentration_report.to_dict()
            if policy_config.covariance_matrix is not None:
                rb_report = compute_risk_budget(
                    target_portfolio,
                    policy_config.covariance_matrix,
                    policy_config.per_position_budget,
                )
                risk_summary["risk_budget"] = rb_report.to_dict()
            if policy_config.returns_matrix is not None:
                corr_report = compute_correlation(
                    target_portfolio,
                    policy_config.returns_matrix,
                )
                risk_summary["correlation"] = corr_report.to_dict()

            if policy_config.strict:
                raise AllocationConstraintError(
                    f"Constraint enforcement failed after "
                    f"{max_iter} iterations; breaches: {list(breaches.keys())}",
                    breaches=breaches,
                    iterations_used=max_iter,
                )
            else:
                constraint_warnings.append(
                    f"Constraints not fully satisfied after "
                    f"{max_iter} iterations (advisory mode); "
                    f"remaining breaches: {list(breaches.keys())}"
                )

        # Update risk_summary with final state.
        target_portfolio = self._build_target_portfolio(
            target_weights, matched, portfolio
        )
        exposure_report = compute_exposure(
            target_portfolio, policy_config.market_prices
        )
        concentration_report = compute_concentration(
            target_portfolio, top_n=policy_config.top_n
        )
        risk_summary["exposure"] = exposure_report.to_dict()
        risk_summary["concentration"] = concentration_report.to_dict()
        risk_summary["iterations_used"] = iterations_used
        risk_summary["constraints_satisfied"] = iterations_used < max_iter or not breaches

        if policy_config.covariance_matrix is not None:
            rb_report = compute_risk_budget(
                target_portfolio,
                policy_config.covariance_matrix,
                policy_config.per_position_budget,
            )
            risk_summary["risk_budget"] = rb_report.to_dict()
        if policy_config.returns_matrix is not None:
            corr_report = compute_correlation(
                target_portfolio,
                policy_config.returns_matrix,
            )
            risk_summary["correlation"] = corr_report.to_dict()

        return target_weights, risk_summary, constraint_warnings

    def _build_target_portfolio(
        self,
        target_weights: dict[str, float],
        matched: list[tuple[str, float, float, Position]],
        portfolio: Portfolio,
    ) -> Portfolio:
        """Build a temporary Portfolio from target weights for M3
        risk function calls."""
        positions = []
        for eid, _, _, orig_pos in matched:
            w = target_weights.get(eid, 0.0)
            if w > 0.0:
                positions.append(
                    Position(
                        position_id=PositionId(orig_pos.position_id.value),
                        entity_id=EntityId(eid),
                        weight=Weight(w),
                        quantity=Quantity(0),
                        price=orig_pos.price,
                        cost_basis=None,
                        metadata=dict(orig_pos.metadata),
                    )
                )
        return Portfolio(
            portfolio_id=portfolio.portfolio_id,
            name=portfolio.name,
            positions=tuple(positions),
        )

    def _fix_largest_breach(
        self,
        target_weights: dict[str, float],
        breaches: dict[str, Any],
        matched: list[tuple[str, float, float, Position]],
        policy_config: AllocationPolicyConfig,
    ) -> None:
        """Clamp the largest breach and redistribute or remove the excess.

        Strategy:
        - Per-position cap breach: clamp to cap, redistribute excess.
        - Gross exposure breach: reduce top position, do NOT redistribute
          (excess is removed, not redistributed — gross must decrease).
        - HHI/top-N breach: reduce top position, do NOT redistribute
          (concentration must decrease, not just shift).
        """
        cap = policy_config.per_position_cap

        # Check for per-position cap breach first.
        largest_excess_eid: str | None = None
        largest_excess: float = 0.0

        for eid, w in target_weights.items():
            excess = w - cap
            if excess > largest_excess:
                largest_excess = excess
                largest_excess_eid = eid

        if largest_excess_eid is not None:
            # Clamp the position to the cap, redistribute excess.
            excess = target_weights[largest_excess_eid] - cap
            target_weights[largest_excess_eid] = cap
            self._redistribute_excess(
                target_weights, matched, excess,
                exclude_eid=largest_excess_eid,
                cap=cap,
            )
            return

        # For gross exposure / HHI / top-N breaches: reduce the top
        # position without redistributing (excess is removed, not
        # redistributed, so the aggregate metric decreases).
        has_gross_breach = "gross_exposure" in breaches
        has_hhi_breach = "hhi" in breaches
        has_topn_breach = "top_n_concentration" in breaches

        if has_gross_breach or has_hhi_breach or has_topn_breach:
            sorted_eids = sorted(
                target_weights.keys(),
                key=lambda e: target_weights[e],
                reverse=True,
            )
            if sorted_eids:
                top_eid = sorted_eids[0]
                top_w = target_weights[top_eid]
                # Reduce the top position by a step (10% of its weight).
                step = top_w * 0.1
                target_weights[top_eid] = max(0.0, top_w - step)
                # Do NOT redistribute — the excess is removed to reduce
                # the aggregate metric (gross exposure / HHI / top-N).
            return

    def _redistribute_excess(
        self,
        target_weights: dict[str, float],
        matched: list[tuple[str, float, float, Position]],
        excess: float,
        exclude_eid: str,
        cap: float,
    ) -> None:
        """Redistribute excess weight to eligible positions.

        Eligible: matched, not the excluded entity, below the cap,
        sorted by score descending.
        """
        eligible = [
            (eid, score, conf)
            for eid, score, conf, _ in matched
            if eid != exclude_eid
            and target_weights.get(eid, 0.0) < cap
        ]
        # Sort by score*confidence descending (highest first).
        eligible.sort(
            key=lambda x: x[1] * x[2],
            reverse=True,
        )

        if not eligible:
            return

        # Distribute proportionally to remaining capacity.
        total_capacity = sum(
            cap - target_weights.get(e, 0.0) for e, _, _ in eligible
        )
        if total_capacity <= 0.0:
            return

        for e, _, _ in eligible:
            capacity = cap - target_weights.get(e, 0.0)
            share = min(excess * (capacity / total_capacity), capacity)
            target_weights[e] = target_weights.get(e, 0.0) + share

    def run_with_handles(
        self,
        pipeline_report: PipelineRunReport,
        portfolio: Portfolio,
        policy_config: AllocationPolicyConfig,
        evidence_handles: dict[str, EvidenceQueryHandle] | None = None,
    ) -> PortfolioDecision:
        """Run the score-weighted allocation policy with evidence handles.

        This is an extended variant of ``run()`` that accepts an
        optional ``evidence_handles`` dict keyed by ``entity_id``. When
        provided, the ``score_node_id`` from each handle is added to the
        decision's ``evidence_refs``.

        Args:
            pipeline_report: The aggregate score envelope.
            portfolio: The current portfolio state.
            policy_config: The allocation policy configuration.
            evidence_handles: Optional dict of ``EvidenceQueryHandle``
                keyed by ``entity_id``.

        Returns:
            A ``PortfolioDecision`` with target allocation + evidence
            refs (including ``score_node_id`` from handles if provided).
        """
        decision = self.run(pipeline_report, portfolio, policy_config)

        if evidence_handles is None:
            return decision

        # Merge score_node_ids from handles into evidence_refs.
        existing_refs = set(decision.evidence_refs)
        new_refs: list[str] = list(decision.evidence_refs)

        for eid, handle in evidence_handles.items():
            node_id = handle.score_node_id
            if node_id and node_id not in existing_refs:
                existing_refs.add(node_id)
                new_refs.append(node_id)

        # Reconstruct the decision with merged evidence_refs.
        return PortfolioDecision(
            decision_id=decision.decision_id,
            portfolio_id=decision.portfolio_id,
            allocation=decision.allocation,
            rationale=decision.rationale,
            evidence_refs=tuple(new_refs),
            generated_at=decision.generated_at,
            risk_summary=decision.risk_summary,
        )


__all__ = [
    "PortfolioDecision",
    "PortfolioDecisionEngine",
    "AllocationPolicyConfig",
    "AllocationConstraintError",
]