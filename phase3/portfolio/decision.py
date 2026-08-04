"""Phase 5 M4 — PortfolioDecision DTO + Engine Stub.

M4-S1 scope: ``PortfolioDecision`` DTO (deterministic
``to_dict()`` / ``from_dict()`` round-trip + invariants) and a
``PortfolioDecisionEngine`` stub raising ``NotImplementedError``.

The engine implementation (score-weighted policy, risk-aware policy,
evidence-chain extraction, constraint enforcement) is deferred to M4-S2 /
M4-S3.

Boundary contract (M4-S1)
--------------------------

Per the M4 kickoff plan §12.3 rationale 5, M4-S1 DTOs import ONLY from
``phase3.portfolio.domain`` + ``phase3.portfolio.allocation`` + the Python
standard library. The portfolio ↔ intelligence-engine boundary crossing
(imports of ``PipelineResult`` / ``ScoreBreakdown`` /
``EvidenceQueryHandle`` from ``phase3.pipeline`` / ``phase3.datamodel``)
lands in M4-S2 with the engine. This isolates the boundary-crossing risk
to M4-S2, where TD8 is already in place to enforce it (AST scan:
0 ``IntelligencePipeline()`` calls, 0 forbidden imports).

The engine stub ``PortfolioDecisionEngine.run()`` is defined here so the
public API surface is stable before M4-S2 fills in the implementation.
The stub raises ``NotImplementedError`` with a message pointing at M4-S2.

Design principles (mirrors M2 ``domain.py`` + M4 ``allocation.py``)
--------------------------------------------------------------------

1. **Immutable.** ``PortfolioDecision`` is ``@dataclass(frozen=True)``;
   ``evidence_refs`` is a tuple.

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

import re
from dataclasses import dataclass, field
from typing import Any

from phase3.portfolio.allocation import Allocation
from phase3.portfolio.domain import PortfolioId

# --------------------------------------------------------------------------- #
# Module constants
# --------------------------------------------------------------------------- #

_IDENTIFIER_PATTERN = re.compile(r"^[^\s]{1,128}$")

_MAX_RATIONALE_LEN = 4096


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
# PortfolioDecisionEngine stub
# --------------------------------------------------------------------------- #


class PortfolioDecisionEngine:
    """Decision engine stub.

    M4-S1 ships the DTO layer only. The engine implementation (consuming
    a ``PipelineRunReport`` + ``Portfolio`` + ``AllocationPolicyConfig``
    and emitting a ``PortfolioDecision`` with score-weighted target
    weights + evidence-chain back-references) lands in M4-S2.

    Calling ``run()`` before M4-S2 raises ``NotImplementedError``. This
    keeps the public API surface stable so M4-S2 can fill in the body
    without changing the signature.

    The signature below intentionally uses ``Any`` for the inputs because
    the concrete types (``PipelineRunReport``, ``Portfolio``,
    ``AllocationPolicyConfig``) are not imported in M4-S1 (boundary rule
    8 — the portfolio ↔ intelligence crossing lands in M4-S2). M4-S2
    will tighten the annotations.
    """

    def run(
        self,
        pipeline_report: Any,
        portfolio: Any,
        policy_config: Any,
    ) -> PortfolioDecision:
        """Run the decision engine (M4-S2). Raises ``NotImplementedError``."""
        raise NotImplementedError(
            "PortfolioDecisionEngine.run() is implemented in M4-S2 "
            "(score-weighted allocation policy + evidence-chain "
            "extraction). M4-S1 ships the DTO layer only."
        )


__all__ = ["PortfolioDecision", "PortfolioDecisionEngine"]