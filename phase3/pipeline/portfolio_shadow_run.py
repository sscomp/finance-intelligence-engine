"""Phase 5 M8 — Portfolio Shadow-Run Extension.

This module is the **additive** portfolio shadow-run extension required
by M8 Operational Acceptance (kickoff plan §9.4). It is a completely
separate module from ``phase3.pipeline.shadow_run`` — the existing
shadow-run MUST NOT be modified.

Purpose
-------
Replays ``portfolio-run`` against a pipeline-export artifact + portfolio
JSON, captures the output as a baseline, re-executes for a determinism
check (byte-identical modulo ``generated_at``), and emits a reconciliation
row. The framework is **read-only**:

* No production DB writes.
* No broker API calls, no order placement.
* No scheduling, no cron integration.
* No mutation of the input artifact or portfolio file.

Design
------
The module delegates all portfolio-decision logic to the existing
``PortfolioDecisionEngine`` (M4-S3, commit ``0ea8d8e``). It does NOT
re-implement portfolio scoring, allocation, or risk computation.

Artifact format
---------------
Two artifact formats are supported:

1. **PipelineRunReport JSON** (``tests/phase3/fixtures/pipeline_export_sample.json``)
   — top-level keys ``macro``, ``industries``, ``companies``, ``warnings``.
   This is the format ``portfolio-run`` CLI expects.

2. **Intelligence Report JSON** (``metadata/reports/artifacts/*.json``)
   — top-level keys ``artifact``, ``errors``, ``generated_at``, ``result``,
   ``schema_version``, ``summary``, ``warnings``. The ``result`` block
   contains ``evidence_handles``, ``graph_writes``, ``snapshot_ids``, etc.
   These artifacts do NOT carry ``PipelineResult`` companies/industries
   in the format ``portfolio-run`` expects. When this format is detected,
   the framework extracts the ``evidence_handles`` and ``snapshot_ids``
   to produce a minimal ``PipelineRunReport`` stub for the engine.

Public surface
--------------
* :class:`PortfolioShadowRunConfig` — frozen DTO for the run parameters.
* :class:`PortfolioShadowRunResult` — frozen DTO for the full result.
* :class:`PortfolioReconciliationRow` — frozen DTO for one day's
  reconciliation.
* :func:`run_portfolio_shadow` — pure-function entry point.
* :class:`PortfolioShadowRunError` — raised for invalid config or
  unrecoverable artifact problems.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION: str = "1"
"""Current portfolio shadow-run schema version."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PortfolioShadowRunError(RuntimeError):
    """Raised for invalid portfolio shadow-run configuration or
    unrecoverable artifact problems."""


# ---------------------------------------------------------------------------
# Configuration DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioShadowRunConfig:
    """Configuration for :func:`run_portfolio_shadow`.

    Attributes:
        artifact_path: Path to a pipeline-export JSON artifact
            (PipelineRunReport format) or an intelligence-report JSON
            (morning-brief artifact format). Must exist and be readable.
        portfolio_path: Path to a portfolio JSON file with
            ``portfolio_id`` and ``positions``. Must exist and be readable.
        replay_label: Human-readable label for the replay (default:
            ``"replay"``).
        output_path: Optional path to write the result JSON. When
            ``None``, only the in-memory result is returned.
        generated_at: Optional ISO-8601 timestamp for determinism.
            When provided, both the baseline and replay runs use the
            same timestamp so they are byte-identical (modulo truly
            random components). When ``None``, the engine generates
            its own (and the two runs will differ on ``generated_at``).
        policy_type: Allocation policy type (default: ``"score_weighted"``).
        per_position_cap: Maximum weight per position (default: 0.25).
        target_total: Target sum of weights (default: 1.0).
    """

    artifact_path: str
    portfolio_path: str
    replay_label: str = "replay"
    output_path: str | None = None
    generated_at: str = ""
    policy_type: str = "score_weighted"
    per_position_cap: float = 0.25
    target_total: float = 1.0

    def __post_init__(self) -> None:
        if not self.artifact_path:
            raise PortfolioShadowRunError(
                "PortfolioShadowRunConfig.artifact_path must be non-empty"
            )
        if not self.portfolio_path:
            raise PortfolioShadowRunError(
                "PortfolioShadowRunConfig.portfolio_path must be non-empty"
            )
        if not self.replay_label:
            raise PortfolioShadowRunError(
                "PortfolioShadowRunConfig.replay_label must be non-empty"
            )


# ---------------------------------------------------------------------------
# Reconciliation DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioReconciliationRow:
    """One day's reconciliation result.

    Attributes:
        label: ``"baseline"`` or ``"replay"``.
        decision_id: The ``decision_id`` from the PortfolioDecision.
        portfolio_id: The ``portfolio_id`` from the PortfolioDecision.
        generated_at: The ``generated_at`` timestamp.
        position_count: Number of positions in the allocation.
        target_total: Target sum of weights.
        policy_type: Allocation policy type used.
        artifact_path: Path to the source artifact.
        success: Whether the run completed without error.
        error: Error message if ``success`` is False.
    """

    label: str
    decision_id: str
    portfolio_id: str
    generated_at: str
    position_count: int
    target_total: float
    policy_type: str
    artifact_path: str
    success: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "decision_id": self.decision_id,
            "portfolio_id": self.portfolio_id,
            "generated_at": self.generated_at,
            "position_count": self.position_count,
            "target_total": self.target_total,
            "policy_type": self.policy_type,
            "artifact_path": self.artifact_path,
            "success": self.success,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Run result DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioShadowRunResult:
    """The full portfolio shadow-run result.

    Attributes:
        schema_version: Schema version for forward-compat consumers.
        generated_at: UTC ISO-8601 timestamp when this result was
            produced.
        artifact_path: Absolute path of the source artifact.
        portfolio_path: Absolute path of the portfolio file.
        artifact_format: ``"pipeline_run_report"`` or
            ``"intelligence_report"``.
        baseline: The baseline reconciliation row.
        replay: The replay reconciliation row.
        deterministic: True iff baseline and replay are byte-identical
            modulo ``generated_at``.
        baseline_decision: The full baseline decision dict (for
            operator inspection).
        replay_decision: The full replay decision dict.
        warnings: Run-level warnings.
    """

    schema_version: str
    generated_at: str
    artifact_path: str
    portfolio_path: str
    artifact_format: str
    baseline: PortfolioReconciliationRow
    replay: PortfolioReconciliationRow
    deterministic: bool
    baseline_decision: dict[str, Any] = field(default_factory=dict)
    replay_decision: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "artifact_path": self.artifact_path,
            "portfolio_path": self.portfolio_path,
            "artifact_format": self.artifact_format,
            "baseline": self.baseline.to_dict(),
            "replay": self.replay.to_dict(),
            "deterministic": self.deterministic,
            "baseline_decision": self.baseline_decision,
            "replay_decision": self.replay_decision,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detect_artifact_format(data: dict[str, Any]) -> str:
    """Detect whether the artifact is a PipelineRunReport or an
    intelligence-report JSON.

    Returns ``"pipeline_run_report"`` or ``"intelligence_report"``.
    """
    if "result" in data and "schema_version" in data and "artifact" in data:
        return "intelligence_report"
    if "macro" in data or "industries" in data or "companies" in data:
        return "pipeline_run_report"
    # Fallback: if it has evidence_handles at top level, treat as
    # intelligence_report (some formats may nest differently).
    if "evidence_handles" in data:
        return "intelligence_report"
    # Default: try pipeline_run_report (the portfolio-run native format).
    return "pipeline_run_report"


def _load_json(path: str) -> dict[str, Any]:
    """Load a JSON file and return the parsed dict."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        raise PortfolioShadowRunError(f"File not found: {path}")
    except json.JSONDecodeError as exc:
        raise PortfolioShadowRunError(f"Invalid JSON in {path}: {exc}")


def _build_report_from_intelligence(data: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal PipelineRunReport dict from an intelligence-report
    JSON artifact.

    The morning-brief artifacts (produced by the cron pipeline) do NOT
    carry ``PipelineResult`` companies/industries in the format
    ``portfolio-run`` expects. They carry ``evidence_handles`` and
    ``snapshot_ids`` in the ``result`` block.

    This function extracts whatever scoring information is available
    from the intelligence report and constructs a PipelineRunReport-
    shaped dict that ``PortfolioDecisionEngine`` can consume. When no
    company/industry data is available, the report will have empty
    tuples — the engine will produce an empty allocation (which is
    a valid, deterministic result for testing purposes).
    """
    result_block = data.get("result", {})
    evidence_handles = result_block.get("evidence_handles", [])
    snapshot_ids = result_block.get("snapshot_ids", [])

    # Extract any scored entities from evidence_handles.
    companies: list[dict[str, Any]] = []
    industries: list[dict[str, Any]] = []
    macro: dict[str, Any] | None = None

    for handle in evidence_handles:
        scorer_type = handle.get("scorer_type", "")
        entity_id = handle.get("entity_id", "")
        score = handle.get("score")
        confidence = handle.get("confidence")
        config_hash = handle.get("config_hash", "")
        timestamp = handle.get("timestamp", "")
        valid_until = handle.get("valid_until", "")

        if scorer_type == "macro":
            macro = _make_pipeline_result_dict(
                scorer_type, entity_id, score, confidence,
                config_hash, timestamp, valid_until,
            )
        elif scorer_type == "industry":
            industries.append(_make_pipeline_result_dict(
                scorer_type, entity_id, score, confidence,
                config_hash, timestamp, valid_until,
            ))
        elif scorer_type == "company":
            companies.append(_make_pipeline_result_dict(
                scorer_type, entity_id, score, confidence,
                config_hash, timestamp, valid_until,
            ))

    return {
        "macro": macro,
        "industries": tuple(industries),
        "companies": tuple(companies),
        "warnings": tuple(data.get("warnings", [])),
    }


def _make_pipeline_result_dict(
    scorer_type: str,
    entity_id: str,
    score: Any,
    confidence: Any,
    config_hash: str,
    timestamp: str,
    valid_until: str,
) -> dict[str, Any]:
    """Build a minimal PipelineResult dict for the engine."""
    breakdown = {
        "scorer_type": scorer_type,
        "entity_type": scorer_type,
        "entity_id": entity_id,
        "score": float(score) if score is not None else 0.0,
        "confidence": float(confidence) if confidence is not None else 0.0,
        "dimensions": [],
        "overall_evidence": [],
        "timestamp": timestamp or "2026-01-01T00:00:00+00:00",
        "valid_until": valid_until or "2026-01-01T00:00:00+00:00",
        "config_hash": config_hash or "unknown",
        "cross_layer_adjustments": [],
        "schema_version": "2.0",
    }
    return {
        "score": {"breakdown": breakdown},
        "input_bundle": None,
        "evidence_signal_ids": [],
        "snapshot_id": None,
        "warnings": [],
        "metadata": {"scorer_type": scorer_type, "entity_id": entity_id},
    }


def _run_portfolio_engine(
    report_data: dict[str, Any],
    portfolio_data: dict[str, Any],
    config: PortfolioShadowRunConfig,
) -> dict[str, Any]:
    """Run the PortfolioDecisionEngine and return the decision dict.

    This function mirrors the logic in ``cmd_portfolio_run`` but is
    callable from Python (not requiring argparse). It uses the exact
    same ``PortfolioDecisionEngine`` and ``AllocationPolicyConfig``
    classes.
    """
    from phase3.portfolio.domain import (
        EntityId, Portfolio, PortfolioId, Position, PositionId,
        Quantity, Weight,
    )
    from phase3.portfolio.decision import (
        AllocationPolicyConfig, PortfolioDecisionEngine,
    )
    from phase3.pipeline.scoring_pipeline import PipelineResult, PipelineRunReport
    from phase3.datamodel.scores import ScoreBreakdown
    from datetime import datetime as _dt

    def _deserialize_result(data: dict[str, Any]) -> PipelineResult:
        score_data = data.get("score", {})
        breakdown_data = score_data.get("breakdown") if score_data else None
        if breakdown_data:
            breakdown = ScoreBreakdown(
                scorer_type=breakdown_data["scorer_type"],
                entity_type=breakdown_data["entity_type"],
                entity_id=breakdown_data["entity_id"],
                score=float(breakdown_data["score"]),
                confidence=float(breakdown_data["confidence"]),
                dimensions=[],
                overall_evidence=[],
                timestamp=_dt.fromisoformat(breakdown_data["timestamp"]),
                config_hash=breakdown_data["config_hash"],
                valid_until=_dt.fromisoformat(breakdown_data["valid_until"]),
                cross_layer_adjustments=[],
                schema_version=breakdown_data.get("schema_version", "2.0"),
            )

            class _MockScore:
                def __init__(self, b):
                    self.breakdown = b

            score = _MockScore(breakdown)
        else:
            score = None
        return PipelineResult(
            score=score,
            input_bundle=None,
            evidence_signal_ids=tuple(data.get("evidence_signal_ids", [])),
            snapshot_id=data.get("snapshot_id"),
            warnings=tuple(data.get("warnings", [])),
            metadata=dict(data.get("metadata", {})),
        )

    macro = None
    if report_data.get("macro") is not None:
        macro = _deserialize_result(report_data["macro"])
    industries = tuple(
        _deserialize_result(r) for r in report_data.get("industries", [])
    )
    companies = tuple(
        _deserialize_result(r) for r in report_data.get("companies", [])
    )
    report = PipelineRunReport(
        macro=macro, industries=industries, companies=companies,
        warnings=tuple(report_data.get("warnings", [])),
    )

    # Load portfolio.
    positions = []
    for i, pos_data in enumerate(portfolio_data.get("positions", [])):
        positions.append(Position(
            position_id=PositionId(pos_data.get("position_id", f"pos-{i:04d}")),
            entity_id=EntityId(pos_data["entity_id"]),
            weight=Weight(float(pos_data.get("weight", 0.0))),
            quantity=Quantity(int(pos_data.get("quantity", 0))),
        ))
    portfolio = Portfolio(
        portfolio_id=PortfolioId(portfolio_data.get("portfolio_id", "portfolio-001")),
        name=portfolio_data.get("name", "Portfolio"),
        positions=tuple(positions),
    )

    # Build config.
    config_obj = AllocationPolicyConfig(
        policy_type=config.policy_type,
        target_total=config.target_total,
        per_position_cap=config.per_position_cap,
        generated_at=config.generated_at,
        strict=True,
    )

    engine = PortfolioDecisionEngine()
    decision = engine.run(report, portfolio, config_obj)
    return decision.to_dict()


def _extract_recon_row(
    label: str,
    decision: dict[str, Any],
    artifact_path: str,
    success: bool,
    error: str = "",
) -> PortfolioReconciliationRow:
    """Extract a reconciliation row from a decision dict."""
    if not success:
        return PortfolioReconciliationRow(
            label=label,
            decision_id="",
            portfolio_id="",
            generated_at="",
            position_count=0,
            target_total=0.0,
            policy_type="",
            artifact_path=artifact_path,
            success=False,
            error=error,
        )

    allocation = decision.get("allocation", {})
    positions = allocation.get("positions", [])
    return PortfolioReconciliationRow(
        label=label,
        decision_id=decision.get("decision_id", ""),
        portfolio_id=decision.get("portfolio_id", ""),
        generated_at=decision.get("generated_at", ""),
        position_count=len(positions),
        target_total=allocation.get("target_total", 0.0),
        policy_type=decision.get("policy_type", ""),
        artifact_path=artifact_path,
        success=True,
    )


def _compare_deterministic(
    baseline: dict[str, Any],
    replay: dict[str, Any],
) -> bool:
    """Compare two decision dicts for determinism (byte-identical
    modulo ``generated_at``)."""
    baseline_copy = dict(baseline)
    replay_copy = dict(replay)
    # Remove generated_at from both (it's expected to differ if
    # the caller didn't pin it).
    baseline_copy.pop("generated_at", None)
    replay_copy.pop("generated_at", None)
    return baseline_copy == replay_copy


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_portfolio_shadow(
    config: PortfolioShadowRunConfig,
) -> PortfolioShadowRunResult:
    """Run a portfolio shadow-run: execute ``portfolio-run`` against
    an artifact + portfolio, capture the baseline, re-execute for
    determinism, and return the comparison result.

    This function is **read-only**: it never writes to a production DB,
    never mutates the input files, and never makes broker calls.

    Raises :class:`PortfolioShadowRunError` for invalid configuration
    or unrecoverable artifact problems.
    """
    # Validate file existence.
    if not os.path.isfile(config.artifact_path):
        raise PortfolioShadowRunError(
            f"Artifact file not found: {config.artifact_path}"
        )
    if not os.path.isfile(config.portfolio_path):
        raise PortfolioShadowRunError(
            f"Portfolio file not found: {config.portfolio_path}"
        )

    # Refuse macro_history.db (safety guard).
    artifact_basename = os.path.basename(config.artifact_path)
    if artifact_basename.lower() == "macro_history.db":
        raise PortfolioShadowRunError(
            f"Refusing to open {config.artifact_path!r} "
            f"(basename is reserved)"
        )

    # Load artifact and portfolio.
    artifact_data = _load_json(config.artifact_path)
    portfolio_data = _load_json(config.portfolio_path)

    # Detect artifact format.
    artifact_format = _detect_artifact_format(artifact_data)

    # Build report data in PipelineRunReport format.
    if artifact_format == "intelligence_report":
        report_data = _build_report_from_intelligence(artifact_data)
    else:
        report_data = artifact_data

    warnings: list[str] = []

    # If the intelligence report has no scored companies, warn.
    if artifact_format == "intelligence_report":
        if not report_data.get("companies") and not report_data.get("industries"):
            warnings.append(
                "intelligence_report artifact has no scored companies/industries; "
                "portfolio decision will have empty allocation"
            )

    # --- Baseline run ---
    try:
        baseline_decision = _run_portfolio_engine(
            report_data, portfolio_data, config,
        )
        baseline_row = _extract_recon_row(
            "baseline", baseline_decision, config.artifact_path, True,
        )
    except Exception as exc:
        baseline_decision = {}
        baseline_row = _extract_recon_row(
            "baseline", {}, config.artifact_path, False, str(exc),
        )

    # --- Replay run (same inputs, should be byte-identical) ---
    try:
        replay_decision = _run_portfolio_engine(
            report_data, portfolio_data, config,
        )
        replay_row = _extract_recon_row(
            config.replay_label, replay_decision, config.artifact_path, True,
        )
    except Exception as exc:
        replay_decision = {}
        replay_row = _extract_recon_row(
            config.replay_label, {}, config.artifact_path, False, str(exc),
        )

    # --- Determinism check ---
    if baseline_row.success and replay_row.success:
        deterministic = _compare_deterministic(
            baseline_decision, replay_decision,
        )
    else:
        deterministic = False

    result = PortfolioShadowRunResult(
        schema_version=SCHEMA_VERSION,
        generated_at=_utc_iso(),
        artifact_path=os.path.abspath(config.artifact_path),
        portfolio_path=os.path.abspath(config.portfolio_path),
        artifact_format=artifact_format,
        baseline=baseline_row,
        replay=replay_row,
        deterministic=deterministic,
        baseline_decision=baseline_decision,
        replay_decision=replay_decision,
        warnings=tuple(warnings),
    )

    # Write output if requested.
    if config.output_path and config.output_path != "-":
        with open(config.output_path, "w") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)

    return result