"""Phase 5 — Portfolio Decision Engine.

Public API surface for the portfolio domain layer. Phase 5 adds a portfolio
decision layer on top of the existing Phase 3/4 Investment Intelligence
Engine. This sub-package is purely additive: it consumes the existing
``PipelineResult`` / ``ScoreBreakdown`` contracts produced by
``phase3.pipeline.intelligence_pipeline.IntelligencePipeline`` and emits
portfolio-level artefacts.

M2 (this milestone) ships ONLY the portfolio domain model:
``Portfolio`` + ``Position`` + the value objects (``Weight``, ``EntityId``,
``Quantity``, ``CostBasis``, ``Price``) + the core invariants +
deterministic ``to_dict()`` serialization.

Out of scope for M2 (deferred to later milestones):
- ``Allocation`` aggregate (M3 / M4)
- ``PortfolioDecision`` (M4)
- Risk engine (M3)
- Execution planning (M5)
- Reporting (M6)

Boundary rules (enforced by ``tests/phase3/test_portfolio_safety_guards.py``):
1. ``phase3.portfolio.*`` MUST NOT import ``macro_history.db`` directly.
2. ``phase3.portfolio.*`` MUST NOT write to ``macro_history.db`` or create
   ``intelligence.db*`` artefacts.
3. ``phase3.portfolio.*`` MUST NOT modify any file under ``phase3/pipeline/``,
   ``phase3/datamodel/``, ``phase3/graph/``, or ``phase3/cli.py``.
"""
from __future__ import annotations

from phase3.portfolio.domain import (
    CostBasis,
    EntityId,
    Portfolio,
    PortfolioId,
    Position,
    PositionId,
    Price,
    Quantity,
    Weight,
)
from phase3.portfolio.risk import (
    ConcentrationReport,
    CorrelationReport,
    DrawdownReport,
    ExposureReport,
    RiskBudgetReport,
    compute_concentration,
    compute_correlation,
    compute_drawdown,
    compute_exposure,
    compute_risk_budget,
)
from phase3.portfolio.allocation import Allocation
from phase3.portfolio.decision import (
    AllocationConstraintError,
    AllocationPolicyConfig,
    PortfolioDecision,
    PortfolioDecisionEngine,
)

__all__ = [
    # Value objects
    "EntityId",
    "PortfolioId",
    "PositionId",
    "Weight",
    "Quantity",
    "Price",
    "CostBasis",
    # Domain entities
    "Position",
    "Portfolio",
    # Risk engine (M3-S1)
    "ExposureReport",
    "compute_exposure",
    # Risk engine (M3-S2)
    "ConcentrationReport",
    "compute_concentration",
    "DrawdownReport",
    "compute_drawdown",
    # Risk engine (M3-S3)
    "CorrelationReport",
    "compute_correlation",
    "RiskBudgetReport",
    "compute_risk_budget",
    # Allocation & decision engine (M4-S1)
    "Allocation",
    "PortfolioDecision",
    "PortfolioDecisionEngine",
    # Decision engine config (M4-S2)
    "AllocationPolicyConfig",
    # Constraint error (M4-S3)
    "AllocationConstraintError",
]