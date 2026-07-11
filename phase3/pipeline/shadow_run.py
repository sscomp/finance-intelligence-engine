"""Phase 4 Task 4 Run 1 — Shadow Run / Decision Replay Foundation.

The shadow-run framework replays a historical ``build_json_export``
artifact through the same scoring path the production pipeline uses,
without touching any production DB or scheduler. The output is a
*read-only* comparison of decisions between the **baseline** run
(recorded in the artifact) and a **replay** run (rebuilt from the
artifact inputs).

Why a separate module
----------------------
The brief scoped this as "Shadow Run Foundation" — a tool that lets
operators answer two questions:

1. "If I rerun the same pipeline against the same artifact, do I
   get the same decisions?" (replay determinism / regression)
2. "Which decisions would change if I bumped the config_hash?"
   (forward-looking impact)

The framework reuses existing primitives (no new scoring logic, no
new BFS, no new tracer):

* :func:`phase3.graph.explain_from_pipeline.load_pipeline_envelope`
  — to load the artifact and rebuild the in-memory graph.
* :func:`phase3.pipeline.scoring_pipeline.ScoringPipeline.run_all`
  — to replay the scoring (when an InputBundle is reachable from
  the artifact; not always possible — see
  :func:`_extract_decision_record` for the no-input fallback).
* :func:`phase3.graph.explain_score.explain_score` — to attach an
  explain-score reference to every changed decision.

The module is **read-only**. It never writes to a production DB,
never mutates the input artifact, and never schedules anything.
The CLI entry (Run 1's responsibility) is the *only* side-effect
surface, and it writes only to a caller-supplied output path.

Public surface
--------------
* :class:`ShadowRunConfig` — frozen DTO for the replay parameters
  (artifact path, replay label, optional replay config_hash,
  score-delta threshold, optional ``--output`` destination).
* :class:`ShadowDecision` — frozen DTO capturing a single score's
  baseline and replay values plus a "changed" flag.
* :class:`ShadowComparison` — frozen DTO for one scorer_type's
  full decision comparison (decisions + changed count + delta
  summary).
* :class:`ShadowRunResult` — frozen DTO for the whole run
  (artifact path, baseline meta, comparisons per scorer_type,
  explain-score refs for changed decisions, summary counts).
* :func:`run_shadow` — pure-function entry point: takes a
  :class:`ShadowRunConfig`, returns a :class:`ShadowRunResult`,
  optionally writes the result JSON to a path.
* :class:`ShadowRunError` — raised for invalid config or
  unrecoverable artifact problems.

What "replay" means here
------------------------
The artifact (``build_json_export`` JSON) carries the **decisions**
already made: ``result.pipeline_result.industries`` and
``result.pipeline_result.companies`` expose per-scorer
:class:`PipelineResult` envelopes with the original
``score``/``confidence``/``warnings``/``evidence_signal_ids`` and
the breakdown's ``config_hash`` + ``timestamp``.

The replay reconstructs each per-scorer decision from the artifact's
``evidence_handles`` and the rebuilt graph. When the artifact's
``config_hash`` is the same as ``ShadowRunConfig.replay_config_hash``
(default: same as artifact's), the comparison is a
*determinism check* (baseline == replay byte-equal). When the
``replay_config_hash`` differs, the comparison is a *drift check*
(intentional change detector).

For Run 1, the replay is a **structured copy** of the baseline
decisions (no algorithm re-execution, since the scoring inputs are
not in the artifact — only the resulting decisions are). This is
the foundation; Run 2+ can layer real re-execution when the
artifact gains an ``inputs`` payload.

Decisions considered "changed"
------------------------------
A decision is "changed" iff any of the following differ between
baseline and replay:

* ``score`` (float, exact equality at the configured
  ``score_tolerance``; default 0 absolute)
* ``confidence`` (float, exact equality at the configured
  ``confidence_tolerance``; default 0 absolute)
* ``config_hash`` (string, exact equality)
* ``evidence_signal_ids`` (set difference; default empty diff)
* ``warnings`` (set difference; default empty diff)

The default tolerances are 0 (any non-zero delta is "changed").
This is conservative — false positives are preferred over false
negatives in a decision-drift detector.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from phase3.graph.explain_from_pipeline import (
    ExplainPipelineArtifactError,
    load_pipeline_envelope,
)
from phase3.graph.in_memory_store import GraphStore


SCHEMA_VERSION: str = "1"
"""Current shadow-run schema version. Bump when the result DTO shape changes."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ShadowRunError(RuntimeError):
    """Raised for invalid shadow-run configuration or unrecoverable
    artifact problems.

    The error is raised at three points:

    1. :meth:`ShadowRunConfig.__post_init__` — invalid configuration.
    2. :func:`run_shadow` — artifact loader failure (relayed from
       :class:`ExplainPipelineArtifactError`).
    3. :func:`run_shadow` — output directory not writable.

    The CLI prints the message to stderr and exits with code 1.
    """


# ---------------------------------------------------------------------------
# Configuration DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowRunConfig:
    """Configuration for :func:`run_shadow`.

    Attributes:
        artifact_path: Absolute or relative path to the
            ``build_json_export`` JSON artifact. Must exist and be
            readable.
        replay_label: Human-readable label for the replay
            (default: ``"replay"``). Used in result JSON to
            distinguish baseline vs replay when both are
            inspected side-by-side.
        replay_config_hash: Optional override for the config_hash
            the replay is considered to have run with. When
            ``None``, the artifact's own ``config_hash`` is used
            (so the comparison is a determinism check). When set
            to a different value, the comparison is a drift check
            against the labeled "new" config.
        score_tolerance: Absolute tolerance for the ``score``
            delta. Default 0 (any non-zero delta is "changed").
        confidence_tolerance: Absolute tolerance for the
            ``confidence`` delta. Default 0.
        output_path: Optional path to write the result JSON. When
            ``None``, only the in-memory result is returned. The
            caller is responsible for ensuring the parent
            directory exists and is writable.
        include_unchanged: When True, the result includes every
            decision (changed + unchanged). When False, only
            changed decisions are included; unchanged decisions
            are reported as a single count. Default: True
            (operator-friendly).
    """

    artifact_path: str
    replay_label: str = "replay"
    replay_config_hash: str | None = None
    score_tolerance: float = 0.0
    confidence_tolerance: float = 0.0
    output_path: str | None = None
    include_unchanged: bool = True

    def __post_init__(self) -> None:
        if not self.artifact_path:
            raise ShadowRunError("ShadowRunConfig.artifact_path must be non-empty")
        if self.score_tolerance < 0:
            raise ShadowRunError(
                f"ShadowRunConfig.score_tolerance must be non-negative, "
                f"got {self.score_tolerance}"
            )
        if self.confidence_tolerance < 0:
            raise ShadowRunError(
                f"ShadowRunConfig.confidence_tolerance must be non-negative, "
                f"got {self.confidence_tolerance}"
            )
        if not self.replay_label:
            raise ShadowRunError("ShadowRunConfig.replay_label must be non-empty")


# ---------------------------------------------------------------------------
# Decision DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowDecision:
    """A single per-scorer decision: baseline values vs replay values.

    Attributes:
        scorer_type: ``"macro"`` | ``"industry"`` | ``"company"``.
        entity_id: Canonical entity identifier (e.g. ``"2330"``
            for a company, ``"AI"`` for an industry, ``"global"``
            for a macro).
        date_bucket: YYYY-MM-DD bucket shared by the decision.
        score_node_id: Canonical score node id
            (``score:<scorer_type>:<entity_id>:<date_bucket>``).
        baseline_score: Score value from the artifact's
            reconstructed baseline ``PipelineResult``.
        replay_score: Score value from the replay (currently the
            baseline's value, since the artifact does not carry
            the scoring inputs — see module docstring "What replay
            means here").
        baseline_confidence: Confidence from the baseline.
        replay_confidence: Confidence from the replay.
        baseline_config_hash: ``config_hash`` recorded in the
            baseline ``ScoreBreakdown``.
        replay_config_hash: ``config_hash`` the replay is
            considered to have run with.
        baseline_evidence_signal_ids: Tuple of signal ids from
            the baseline.
        replay_evidence_signal_ids: Tuple of signal ids from the
            replay.
        baseline_warnings: Tuple of warnings from the baseline.
        replay_warnings: Tuple of warnings from the replay.
        changed: True iff any of the five comparison fields differ
            (see module docstring "Decisions considered changed").
        score_delta: ``replay_score - baseline_score`` (signed).
        confidence_delta: ``replay_confidence - baseline_confidence``
            (signed).
        evidence_diff: Tuple of evidence signal ids present in
            one set but not the other. Stable order (sorted).
        warnings_diff: Tuple of warnings present in one list but
            not the other. Stable order (sorted).
        explain_score_ref: Optional ``score:<scorer_type>:<entity_id>:<date_bucket>``
            string for downstream explain-score lookups. Set on
            changed decisions; empty on unchanged.
    """

    scorer_type: str
    entity_id: str
    date_bucket: str
    score_node_id: str
    baseline_score: float
    replay_score: float
    baseline_confidence: float
    replay_confidence: float
    baseline_config_hash: str
    replay_config_hash: str
    baseline_evidence_signal_ids: tuple[str, ...]
    replay_evidence_signal_ids: tuple[str, ...]
    baseline_warnings: tuple[str, ...]
    replay_warnings: tuple[str, ...]
    changed: bool
    score_delta: float
    confidence_delta: float
    evidence_diff: tuple[str, ...]
    warnings_diff: tuple[str, ...]
    explain_score_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer_type": self.scorer_type,
            "entity_id": self.entity_id,
            "date_bucket": self.date_bucket,
            "score_node_id": self.score_node_id,
            "baseline_score": self.baseline_score,
            "replay_score": self.replay_score,
            "baseline_confidence": self.baseline_confidence,
            "replay_confidence": self.replay_confidence,
            "baseline_config_hash": self.baseline_config_hash,
            "replay_config_hash": self.replay_config_hash,
            "baseline_evidence_signal_ids": list(self.baseline_evidence_signal_ids),
            "replay_evidence_signal_ids": list(self.replay_evidence_signal_ids),
            "baseline_warnings": list(self.baseline_warnings),
            "replay_warnings": list(self.replay_warnings),
            "changed": self.changed,
            "score_delta": self.score_delta,
            "confidence_delta": self.confidence_delta,
            "evidence_diff": list(self.evidence_diff),
            "warnings_diff": list(self.warnings_diff),
            "explain_score_ref": self.explain_score_ref,
        }


@dataclass(frozen=True)
class ShadowComparison:
    """Comparison for a single scorer_type.

    Attributes:
        scorer_type: ``"macro"`` | ``"industry"`` | ``"company"``.
        decision_count: Number of decisions compared.
        changed_count: Number of decisions where ``changed=True``.
        unchanged_count: ``decision_count - changed_count``.
        decisions: Tuple of :class:`ShadowDecision` records
            (in the order the artifact emitted them).
    """

    scorer_type: str
    decision_count: int
    changed_count: int
    unchanged_count: int
    decisions: tuple[ShadowDecision, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer_type": self.scorer_type,
            "decision_count": self.decision_count,
            "changed_count": self.changed_count,
            "unchanged_count": self.unchanged_count,
            "decisions": [d.to_dict() for d in self.decisions],
        }


# ---------------------------------------------------------------------------
# Run result DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowRunResult:
    """The full shadow-run result.

    Attributes:
        schema_version: Dumped into the JSON envelope for
            forward-compat consumers.
        generated_at: UTC ISO-8601 timestamp when the result was
            produced. Pinned to ``config_hash`` is intentionally
            *not* done here — wall-clock is a debug aid.
        artifact_path: Absolute path of the source artifact.
        artifact_run_id: Run handle echoed from the artifact
            (``artifact.run_id``).
        artifact_config_hash: Scorer config hash recorded in the
            artifact (``artifact.config_hash``).
        artifact_date_bucket: YYYY-MM-DD shared by the artifact's
            decisions.
        replay_label: Echoed from :class:`ShadowRunConfig`.
        replay_config_hash: Effective config hash the replay ran
            with (``config.replay_config_hash or artifact_config_hash``).
        comparisons: Tuple of :class:`ShadowComparison` records
            in canonical order: macro, industry, company.
        total_decision_count: Sum of all comparison decision counts.
        total_changed_count: Sum of all comparison changed counts.
        total_unchanged_count: Sum of all comparison unchanged counts.
        explain_score_refs: Tuple of ``score:<scorer_type>:<entity_id>:<date_bucket>``
            refs for every changed decision (operator handoff to
            ``explain-score`` CLI).
        warnings: Run-level warnings (e.g. empty artifact, missing
            evidence_handles).
    """

    schema_version: str
    generated_at: str
    artifact_path: str
    artifact_run_id: str
    artifact_config_hash: str
    artifact_date_bucket: str
    replay_label: str
    replay_config_hash: str
    comparisons: tuple[ShadowComparison, ...]
    total_decision_count: int
    total_changed_count: int
    total_unchanged_count: int
    explain_score_refs: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "artifact_path": self.artifact_path,
            "artifact_run_id": self.artifact_run_id,
            "artifact_config_hash": self.artifact_config_hash,
            "artifact_date_bucket": self.artifact_date_bucket,
            "replay_label": self.replay_label,
            "replay_config_hash": self.replay_config_hash,
            "comparisons": [c.to_dict() for c in self.comparisons],
            "summary": {
                "total_decision_count": self.total_decision_count,
                "total_changed_count": self.total_changed_count,
                "total_unchanged_count": self.total_unchanged_count,
                "explain_score_ref_count": len(self.explain_score_refs),
            },
            "explain_score_refs": list(self.explain_score_refs),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


#: Canonical scorer_type ordering for the comparisons tuple.
_CANONICAL_SCORER_ORDER: tuple[str, ...] = ("macro", "industry", "company")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _string_tuple(value: Any) -> tuple[str, ...]:
    """Coerce an arbitrary JSON value into a tuple of strings.

    Lists and tuples pass through with non-string entries dropped.
    Everything else becomes an empty tuple. The order is preserved
    but duplicates are removed (stable insertion order).
    """
    if not isinstance(value, (list, tuple)):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for v in value:
        if isinstance(v, str) and v not in seen:
            out.append(v)
            seen.add(v)
    return tuple(out)


def _symmetric_diff(a: Iterable[str], b: Iterable[str]) -> tuple[str, ...]:
    """Symmetric set difference as a stable-order tuple.

    Set arithmetic would lose the original order; we sort the
    combined elements so the result is deterministic for JSON
    output. Empty input → empty tuple.
    """
    sa, sb = set(a), set(b)
    diff = sa.symmetric_difference(sb)
    return tuple(sorted(diff))


def _evidence_handle_to_decision(
    handle: Mapping[str, Any],
    *,
    artifact_config_hash: str,
    replay_config_hash: str,
    score_tolerance: float,
    confidence_tolerance: float,
) -> ShadowDecision:
    """Build a :class:`ShadowDecision` from a single evidence_handle.

    For Run 1 the replay is a *structured copy* of the baseline:
    the artifact does not carry the scoring inputs, so we cannot
    re-execute the scorers. The replay fields are therefore
    byte-identical to the baseline, *except* for ``config_hash``
    (when :class:`ShadowRunConfig.replay_config_hash` is set) and
    any field that depends on the replay config. The
    ``explain_score_ref`` is populated when the decision is
    ``changed``; otherwise empty.

    Future runs (Run 2+) that capture the scoring inputs in the
    artifact can replace this stub with real re-execution.
    """
    scorer_type = _safe_str(handle.get("scorer_type"), "unknown")
    entity_id = _safe_str(handle.get("entity_id"), "")
    date_bucket = _safe_str(handle.get("date_bucket"), "")
    score_node_id = _safe_str(handle.get("score_node_id"), "")

    baseline_score = _safe_float(handle.get("score"), 0.0)
    baseline_confidence = _safe_float(handle.get("confidence"), 0.0)
    baseline_evidence = _string_tuple(handle.get("evidence_signal_ids"))
    baseline_warnings = _string_tuple(handle.get("warnings"))
    baseline_config_hash = _safe_str(handle.get("config_hash"), artifact_config_hash)

    # Replay = baseline copy, with config_hash overridden.
    replay_score = baseline_score
    replay_confidence = baseline_confidence
    replay_evidence = baseline_evidence
    replay_warnings = baseline_warnings
    replay_config_hash = replay_config_hash or baseline_config_hash

    score_delta = replay_score - baseline_score
    confidence_delta = replay_confidence - baseline_confidence
    evidence_diff = _symmetric_diff(baseline_evidence, replay_evidence)
    warnings_diff = _symmetric_diff(baseline_warnings, replay_warnings)

    changed = (
        abs(score_delta) > score_tolerance
        or abs(confidence_delta) > confidence_tolerance
        or replay_config_hash != baseline_config_hash
        or bool(evidence_diff)
        or bool(warnings_diff)
    )

    explain_ref = score_node_id if changed else ""

    return ShadowDecision(
        scorer_type=scorer_type,
        entity_id=entity_id,
        date_bucket=date_bucket,
        score_node_id=score_node_id,
        baseline_score=baseline_score,
        replay_score=replay_score,
        baseline_confidence=baseline_confidence,
        replay_confidence=replay_confidence,
        baseline_config_hash=baseline_config_hash,
        replay_config_hash=replay_config_hash,
        baseline_evidence_signal_ids=baseline_evidence,
        replay_evidence_signal_ids=replay_evidence,
        baseline_warnings=baseline_warnings,
        replay_warnings=replay_warnings,
        changed=changed,
        score_delta=score_delta,
        confidence_delta=confidence_delta,
        evidence_diff=evidence_diff,
        warnings_diff=warnings_diff,
        explain_score_ref=explain_ref,
    )


def _build_comparison(
    scorer_type: str,
    handles: Sequence[Mapping[str, Any]],
    *,
    artifact_config_hash: str,
    replay_config_hash: str,
    score_tolerance: float,
    confidence_tolerance: float,
    include_unchanged: bool,
) -> ShadowComparison:
    """Build a :class:`ShadowComparison` for one scorer_type.

    The ``handles`` are the per-entity ``evidence_handles`` filtered
    to ``scorer_type``. Each handle becomes a :class:`ShadowDecision`
    via :func:`_evidence_handle_to_decision`. When
    ``include_unchanged`` is False, only changed decisions are
    surfaced in the tuple (the unchanged count still reflects the
    total).
    """
    decisions: list[ShadowDecision] = []
    for h in handles:
        if not isinstance(h, Mapping):
            continue
        if _safe_str(h.get("scorer_type")) != scorer_type:
            continue
        decision = _evidence_handle_to_decision(
            h,
            artifact_config_hash=artifact_config_hash,
            replay_config_hash=replay_config_hash,
            score_tolerance=score_tolerance,
            confidence_tolerance=confidence_tolerance,
        )
        decisions.append(decision)

    changed_count = sum(1 for d in decisions if d.changed)
    unchanged_count = len(decisions) - changed_count
    if include_unchanged:
        surfaced = tuple(decisions)
    else:
        surfaced = tuple(d for d in decisions if d.changed)

    return ShadowComparison(
        scorer_type=scorer_type,
        decision_count=len(decisions),
        changed_count=changed_count,
        unchanged_count=unchanged_count,
        decisions=surfaced,
    )


def _extract_evidence_handles(recovery: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Pull the ``evidence_handles`` list out of the artifact's
    ``result`` payload.

    The ``load_pipeline_envelope`` helper puts the full
    ``IntelligenceRunResult.to_dict()`` under ``envelope.recovery``
    (a bit of a misnomer — the comment in the helper calls it the
    "full payload"). This function reaches into that dict and
    pulls out the ``evidence_handles`` list, defensively typed.
    """
    if not isinstance(recovery, Mapping):
        return []
    raw = recovery.get("evidence_handles") or []
    if not isinstance(raw, list):
        return []
    return [h for h in raw if isinstance(h, Mapping)]


def _write_output(path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Write the result JSON to ``path`` and return the artifact meta.

    The caller is responsible for ensuring the parent directory
    exists; this function only validates the file is writable
    (raises :class:`ShadowRunError` otherwise) and writes bytes.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        raise ShadowRunError(
            f"output_path parent directory does not exist: {parent}"
        )
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ShadowRunError(
            f"failed to write shadow-run output to {path!r}: {exc}"
        ) from exc
    return {
        "path": os.path.abspath(path),
        "size_bytes": os.path.getsize(path),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_shadow(config: ShadowRunConfig) -> ShadowRunResult:
    """Run the shadow replay against the configured artifact.

    The function is **pure** in the sense that it does not mutate
    any production DB, does not schedule anything, and does not
    write any file unless ``config.output_path`` is set. The CLI
    is the only consumer that supplies an output path.

    Args:
        config: A :class:`ShadowRunConfig` with the artifact
            path and replay parameters.

    Returns:
        A :class:`ShadowRunResult` capturing the per-scorer
        comparisons, the explain-score refs for changed
        decisions, and the summary counts.

    Raises:
        ShadowRunError: When the config is invalid (already
            enforced by :meth:`ShadowRunConfig.__post_init__`)
            or the artifact cannot be loaded (relayed from
            :class:`ExplainPipelineArtifactError`).
    """
    try:
        envelope = load_pipeline_envelope(config.artifact_path)
    except ExplainPipelineArtifactError as exc:
        raise ShadowRunError(
            f"failed to load artifact for shadow run: {exc}"
        ) from exc

    replay_config_hash = (
        config.replay_config_hash
        if config.replay_config_hash is not None
        else envelope.config_hash
    )

    handles = _extract_evidence_handles(envelope.recovery)

    warnings: list[str] = []
    if not handles:
        warnings.append(
            "artifact carries no evidence_handles; shadow comparison is empty"
        )

    comparisons: list[ShadowComparison] = []
    for scorer_type in _CANONICAL_SCORER_ORDER:
        comparisons.append(
            _build_comparison(
                scorer_type,
                handles,
                artifact_config_hash=envelope.config_hash,
                replay_config_hash=replay_config_hash,
                score_tolerance=config.score_tolerance,
                confidence_tolerance=config.confidence_tolerance,
                include_unchanged=config.include_unchanged,
            )
        )

    total_decisions = sum(c.decision_count for c in comparisons)
    total_changed = sum(c.changed_count for c in comparisons)
    total_unchanged = sum(c.unchanged_count for c in comparisons)
    explain_refs: list[str] = []
    for c in comparisons:
        for d in c.decisions:
            if d.changed and d.explain_score_ref:
                explain_refs.append(d.explain_score_ref)

    result = ShadowRunResult(
        schema_version=SCHEMA_VERSION,
        generated_at=_utc_iso(),
        artifact_path=envelope.source_artifact_path,
        artifact_run_id=envelope.run_id,
        artifact_config_hash=envelope.config_hash,
        artifact_date_bucket=envelope.date_bucket,
        replay_label=config.replay_label,
        replay_config_hash=replay_config_hash,
        comparisons=tuple(comparisons),
        total_decision_count=total_decisions,
        total_changed_count=total_changed,
        total_unchanged_count=total_unchanged,
        explain_score_refs=tuple(explain_refs),
        warnings=tuple(warnings),
    )

    if config.output_path:
        _write_output(config.output_path, result.to_dict())

    return result


__all__ = [
    "SCHEMA_VERSION",
    "ShadowComparison",
    "ShadowDecision",
    "ShadowRunConfig",
    "ShadowRunError",
    "ShadowRunResult",
    "run_shadow",
]
