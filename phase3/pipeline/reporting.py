"""Phase 3B Task 5 Run 3 — Report / Export Layer.

Sits on top of the IntelligencePipeline (Run 1) and RecoveryManager
(Run 2) result envelopes. The reporting layer is *pure presentation*:

* No business logic. All counts/sections are derived from the existing
  ``IntelligenceRunResult.to_dict()`` and ``RecoveryResult.to_dict()``
  views; the layer never reads from a scorer, signal store, or graph
  store directly.
* No DB writes. Tests run against ``tempfile.TemporaryDirectory`` only.
* Deterministic. JSON keys are sorted, list ordering is preserved from
  the source DTO, and repeated export of the same input produces the
  same bytes (timestamp-free envelopes only — wall-clock timestamps come
  from the caller-supplied DTOs and are not regenerated here).
* UTF-8 throughout. Markdown headings + Chinese characters are encoded
  as ``utf-8``.

Public API
----------

* :class:`ReportArtifact` — frozen DTO for one export item
  (kind, path, sha256, size).
* :class:`SummaryStatistics` — frozen DTO with the run-level counts
  (signals, scores, snapshots, graph nodes/edges, warnings, errors).
* :class:`ReportConfig` — frozen DTO controlling output paths and
  schema version.
* :func:`build_summary_statistics` — pure-function counter
  (``IntelligenceRunResult`` → ``SummaryStatistics``).
* :func:`build_json_export` — pure-function DTO builder
  (``IntelligenceRunResult`` → ``dict[str, Any]``).
* :func:`build_recovery_json_export` — same shape for
  ``RecoveryResult``.
* :func:`render_markdown_report` — pure-function Markdown renderer
  (``IntelligenceRunResult`` → ``str``).
* :func:`export_report` — entry point that writes JSON + Markdown to
  ``ReportConfig.output_dir`` and returns a tuple of
  :class:`ReportArtifact`.

Failure modes
-------------

* Empty input (``result=None``) is supported and emits a clearly-flagged
  "no intelligence produced" section.
* ``output_dir`` is validated: must exist, must be a directory, must be
  writable. Any other condition raises :class:`ReportExportError`.
* Invalid ``ReportConfig`` (negative schema_version, empty output_dir)
  raises :class:`ReportExportError` at construction time.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION: int = 1
"""Current export schema version. Bump when JSON DTO shape changes."""

_REPORT_KIND_JSON: str = "intelligence_report.json"
_REPORT_KIND_MARKDOWN: str = "intelligence_report.md"
_RECOVERY_KIND_JSON: str = "recovery_report.json"
_RECOVERY_KIND_MARKDOWN: str = "recovery_report.md"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReportExportError(RuntimeError):
    """Raised when the export layer cannot produce a valid artifact.

    The error is raised at three points:
    1. ``ReportConfig.__post_init__`` — invalid configuration.
    2. ``export_report`` — invalid destination directory.
    3. ``export_report`` — filesystem write failure (rare; the writer
       catches ``OSError`` and re-raises as ``ReportExportError``).
    """


# ---------------------------------------------------------------------------
# Configuration DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportConfig:
    """Configuration for :func:`export_report`.

    Attributes:
        output_dir: Destination directory for the JSON + Markdown
            artifacts. Must exist and be writable.
        run_label: Optional human-readable label prefixed to the file
            names (``<run_label>.intelligence_report.json`` when set,
            otherwise the bare kind name). Useful when exporting
            multiple runs in the same directory.
        schema_version: Schema version stamped into the JSON envelope.
            Must be positive.
        include_evidence_payload: When True, the JSON export embeds
            ``evidence_handles[*].evidence_summary`` (Run 1 default);
            when False, the section is omitted to keep the export
            small. Default: True.
        recovery: When True, the export is for a
            :class:`RecoveryResult` and the artifact kinds/markdown
            sections reflect that. When False (default), the export is
            for an :class:`IntelligenceRunResult`.
    """

    output_dir: str
    run_label: str = ""
    schema_version: int = SCHEMA_VERSION
    include_evidence_payload: bool = True
    recovery: bool = False

    def __post_init__(self) -> None:
        if not self.output_dir:
            raise ReportExportError("ReportConfig.output_dir must be non-empty")
        if self.schema_version <= 0:
            raise ReportExportError(
                f"ReportConfig.schema_version must be positive, got {self.schema_version}"
            )

    def file_prefix(self) -> str:
        """Filename prefix combining ``run_label`` (if any) with a dot."""
        if not self.run_label:
            return ""
        return f"{self.run_label}."


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummaryStatistics:
    """Run-level counts derived from an :class:`IntelligenceRunResult`.

    Attributes:
        signal_count: Distinct evidence signal ids observed across all
            graph writes (deduplicated by id).
        score_count: Total scored entities (macro + industries + companies).
        snapshot_count: Persisted snapshot rows (zero when ``persist=False``).
        graph_node_count: Total graph nodes recorded for the run.
        graph_edge_count: Total graph edges recorded for the run.
        evidence_handle_count: Number of evidence handles returned.
        warning_count: Length of the run-level ``warnings`` tuple.
        error_count: Length of the run-level ``errors`` tuple.
        company_score_count: Subset of ``score_count`` that is company scores.
        industry_score_count: Subset of ``score_count`` that is industry scores.
        macro_score_count: Subset of ``score_count`` that is macro scores.
    """

    signal_count: int
    score_count: int
    snapshot_count: int
    graph_node_count: int
    graph_edge_count: int
    evidence_handle_count: int
    warning_count: int
    error_count: int
    company_score_count: int
    industry_score_count: int
    macro_score_count: int

    def to_dict(self) -> dict[str, int]:
        return {
            "signal_count": self.signal_count,
            "score_count": self.score_count,
            "snapshot_count": self.snapshot_count,
            "graph_node_count": self.graph_node_count,
            "graph_edge_count": self.graph_edge_count,
            "evidence_handle_count": self.evidence_handle_count,
            "warning_count": self.warning_count,
            "error_count": self.error_count,
            "company_score_count": self.company_score_count,
            "industry_score_count": self.industry_score_count,
            "macro_score_count": self.macro_score_count,
        }


@dataclass(frozen=True)
class ReportArtifact:
    """Metadata for one exported file.

    Attributes:
        kind: The artifact kind (e.g. ``"intelligence_report.json"``).
        path: Absolute path to the written file.
        sha256: SHA-256 of the file contents (computed after the write).
        size_bytes: Size of the file in bytes.
    """

    kind: str
    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------


def build_summary_statistics(
    result: Any | None,
) -> SummaryStatistics:
    """Derive :class:`SummaryStatistics` from an ``IntelligenceRunResult``.

    Args:
        result: The run result, or ``None`` for an empty/failed run.

    Returns:
        Frozen :class:`SummaryStatistics` with all-zero counts when
        ``result is None``.
    """
    if result is None:
        return SummaryStatistics(
            signal_count=0,
            score_count=0,
            snapshot_count=0,
            graph_node_count=0,
            graph_edge_count=0,
            evidence_handle_count=0,
            warning_count=0,
            error_count=0,
            company_score_count=0,
            industry_score_count=0,
            macro_score_count=0,
        )

    snapshot_count = sum(1 for v in (result.snapshot_ids or {}).values() if v is not None)
    graph_writes = list(result.graph_writes or ())
    graph_node_count = sum(getattr(gw, "created_node_count", 0) for gw in graph_writes)
    graph_edge_count = sum(getattr(gw, "created_edge_count", 0) for gw in graph_writes)
    evidence_handle_count = len(result.evidence_handles or ())

    # Distinct evidence signal ids across all graph writes.
    signal_ids: set[str] = set()
    for gw in graph_writes:
        for sid in getattr(gw, "evidence_signal_ids", ()) or ():
            if sid:
                signal_ids.add(sid)
        for sid in getattr(gw, "signal_node_ids", ()) or ():
            if sid:
                signal_ids.add(sid)

    pipeline_result = getattr(result, "pipeline_result", None)
    macro_count = 1 if (pipeline_result is not None and getattr(pipeline_result, "macro", None)) else 0
    industry_count = len(getattr(pipeline_result, "industries", ()) or ()) if pipeline_result else 0
    company_count = len(getattr(pipeline_result, "companies", ()) or ()) if pipeline_result else 0

    return SummaryStatistics(
        signal_count=len(signal_ids),
        score_count=macro_count + industry_count + company_count,
        snapshot_count=snapshot_count,
        graph_node_count=graph_node_count,
        graph_edge_count=graph_edge_count,
        evidence_handle_count=evidence_handle_count,
        warning_count=len(result.warnings or ()),
        error_count=len(result.errors or ()),
        company_score_count=company_count,
        industry_score_count=industry_count,
        macro_score_count=macro_count,
    )


# ---------------------------------------------------------------------------
# JSON DTO
# ---------------------------------------------------------------------------


def _envelope_header(
    config: ReportConfig,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": int(config.schema_version),
        "generated_at": generated_at,
        "recovery": bool(config.recovery),
    }


def _result_to_payload(result: Any | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return result.to_dict()


def build_json_export(
    result: Any | None,
    *,
    config: ReportConfig | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic JSON export DTO for an ``IntelligenceRunResult``.

    The DTO is the union of:
    1. The envelope header (``schema_version``, ``generated_at``,
       ``recovery``).
    2. Artifact metadata (``run_id``, ``config_hash``, ``date_bucket``,
       ``started_at``, ``finished_at``, ``duration_seconds``).
    3. Summary statistics.
    4. The full ``IntelligenceRunResult.to_dict()`` payload.
    5. Warnings / errors at the envelope level for cheap filtering.
    6. Optional ``evidence_handles`` (toggleable via
       ``config.include_evidence_payload``).

    Args:
        result: The run result (or ``None`` for an empty run).
        config: Optional :class:`ReportConfig`. When None, a default
            config with ``schema_version=SCHEMA_VERSION`` is used.
        generated_at: Optional UTC ISO-8601 timestamp string. When None,
            the current UTC time is used.

    Returns:
        A ``dict`` ready for ``json.dumps(..., sort_keys=True)``.
    """
    cfg = config or ReportConfig(output_dir=tempfile.gettempdir())
    ts = generated_at or _utc_iso()

    header = _envelope_header(cfg, ts)
    summary = build_summary_statistics(result).to_dict()
    payload = _result_to_payload(result)

    if payload is None:
        envelope_meta: dict[str, Any] = {
            "run_id": "",
            "config_hash": "",
            "date_bucket": "",
            "started_at": "",
            "finished_at": "",
            "duration_seconds": 0.0,
        }
        return {
            **header,
            "artifact": {
                **envelope_meta,
                "empty": True,
            },
            "summary": summary,
            "warnings": [],
            "errors": [],
            "result": None,
        }

    artifact_meta = {
        "run_id": payload.get("run_id", ""),
        "config_hash": payload.get("config_hash", ""),
        "date_bucket": payload.get("date_bucket", ""),
        "started_at": payload.get("started_at", ""),
        "finished_at": payload.get("finished_at", ""),
        "duration_seconds": payload.get("duration_seconds", 0.0),
        "persist": payload.get("persist", False),
        "dry_run": payload.get("dry_run", True),
    }

    if not cfg.include_evidence_payload:
        # Drop evidence_handles[*].evidence_summary and any precomputed
        # traces from the payload so the export stays small.
        payload = dict(payload)
        payload["evidence_handles"] = [
            {k: v for k, v in h.items() if k != "evidence_summary"}
            for h in payload.get("evidence_handles", [])
        ]
        if payload.get("metadata"):
            md = dict(payload["metadata"])
            md.pop("evidence_trace", None)
            payload["metadata"] = md

    return {
        **header,
        "artifact": artifact_meta,
        "summary": summary,
        "warnings": list(payload.get("warnings", []) or []),
        "errors": list(payload.get("errors", []) or []),
        "result": payload,
    }


def build_recovery_json_export(
    recovery: Any | None,
    *,
    config: ReportConfig | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a JSON export DTO for a :class:`RecoveryResult`.

    The shape mirrors :func:`build_json_export` but the nested
    ``result`` is a ``RecoveryResult.to_dict()`` and the summary
    statistics describe the recovery envelope (attempts, succeeded,
    final category).
    """
    cfg = config or ReportConfig(output_dir=tempfile.gettempdir(), recovery=True)
    # Re-validate the config: callers may have constructed a default
    # for an intelligence run; ensure the recovery flag is set.
    if not cfg.recovery:
        cfg = dataclasses.replace(cfg, recovery=True)
    ts = generated_at or _utc_iso()
    header = _envelope_header(cfg, ts)

    if recovery is None:
        return {
            **header,
            "artifact": {
                "run_id": "",
                "config_hash": "",
                "date_bucket": "",
                "started_at": "",
                "finished_at": "",
                "duration_seconds": 0.0,
                "empty": True,
            },
            "summary": {
                "attempt_count": 0,
                "succeeded_attempt_count": 0,
                "failed_attempt_count": 0,
                "warning_count": 0,
            },
            "warnings": [],
            "recovery": None,
        }

    payload = recovery.to_dict()
    attempts = payload.get("attempts", []) or []
    succeeded = sum(1 for a in attempts if a.get("succeeded"))
    failed = sum(1 for a in attempts if not a.get("succeeded"))
    summary = {
        "attempt_count": len(attempts),
        "succeeded_attempt_count": succeeded,
        "failed_attempt_count": failed,
        "warning_count": len(payload.get("warnings", []) or []),
    }
    artifact_meta = {
        "run_id": payload.get("run_id", ""),
        "config_hash": (payload.get("state", {}) or {}).get("config_hash", ""),
        "date_bucket": (payload.get("state", {}) or {}).get("date_bucket", ""),
        "started_at": payload.get("started_at", ""),
        "finished_at": payload.get("finished_at", ""),
        "duration_seconds": payload.get("duration_seconds", 0.0),
        "completed_through": payload.get("completed_through", ""),
        "resume_succeeded": payload.get("resume_succeeded", False),
        "final_category": payload.get("final_category"),
    }
    return {
        **header,
        "artifact": artifact_meta,
        "summary": summary,
        "warnings": list(payload.get("warnings", []) or []),
        "recovery": payload,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown_report(
    result: Any | None,
    *,
    config: ReportConfig | None = None,
    generated_at: str | None = None,
) -> str:
    """Render a Markdown report for an ``IntelligenceRunResult``.

    Sections:
    1. Header (run_id, config_hash, date_bucket, timestamps)
    2. Summary table
    3. Per-scorer breakdown (macro/industry/company) — derived from
       ``pipeline_result`` when present
    4. Snapshot rows
    5. Graph write rows
    6. Evidence handles (when ``include_evidence_payload`` is True)
    7. Warnings
    8. Errors
    9. Recovery metadata footer (when ``result.metadata`` carries one)

    The function is pure: it returns a string, no IO. ``export_report``
    is responsible for writing it to disk.
    """
    cfg = config or ReportConfig(output_dir=tempfile.gettempdir())
    ts = generated_at or _utc_iso()
    summary = build_summary_statistics(result)

    lines: list[str] = []
    if result is None:
        lines.append("# Intelligence Report")
        lines.append("")
        lines.append("> **Empty run** — no intelligence was produced.")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        for k, v in summary.to_dict().items():
            lines.append(f"| {k} | {v} |")
        lines.append("")
        lines.append(f"_Generated: {ts} • schema v{cfg.schema_version}_")
        return "\n".join(lines) + "\n"

    payload = result.to_dict()
    meta = payload.get("metadata", {}) or {}
    recovery_meta = meta.get("recovery") if isinstance(meta, dict) else None

    lines.append("# Intelligence Report")
    lines.append("")
    lines.append("## Run Metadata")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| run_id | `{payload.get('run_id', '')}` |")
    lines.append(f"| config_hash | `{payload.get('config_hash', '')}` |")
    lines.append(f"| date_bucket | `{payload.get('date_bucket', '')}` |")
    lines.append(f"| persist | `{payload.get('persist', False)}` |")
    lines.append(f"| dry_run | `{payload.get('dry_run', True)}` |")
    lines.append(f"| started_at | `{payload.get('started_at', '')}` |")
    lines.append(f"| finished_at | `{payload.get('finished_at', '')}` |")
    lines.append(f"| duration_seconds | `{payload.get('duration_seconds', 0.0):.6f}` |")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    for k, v in summary.to_dict().items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    lines.append("## Score Breakdown")
    lines.append("")
    pipeline_result = getattr(result, "pipeline_result", None)
    if pipeline_result is None:
        lines.append("_No pipeline result attached._")
    else:
        if getattr(pipeline_result, "macro", None):
            macro = pipeline_result.macro
            lines.append("### Macro")
            lines.append("")
            lines.append(f"- entity_id: `{getattr(macro, 'input_bundle', None) and macro.input_bundle.entity_id or 'global'}`")
            lines.append(f"- snapshot_id: `{getattr(macro, 'snapshot_id', None)}`")
            lines.append(f"- evidence_signal_ids: `{len(getattr(macro, 'evidence_signal_ids', ()) or ())}`")
            if getattr(macro, "warnings", None):
                lines.append(f"- warnings: {len(macro.warnings)}")
        industries = getattr(pipeline_result, "industries", ()) or ()
        if industries:
            lines.append("")
            lines.append("### Industries")
            lines.append("")
            lines.append("| entity_id | snapshot_id | evidence_signal_ids | warnings |")
            lines.append("| --- | --- | --- | --- |")
            for ind in industries:
                lines.append(
                    f"| `{ind.input_bundle.entity_id}` "
                    f"| `{ind.snapshot_id}` "
                    f"| `{len(ind.evidence_signal_ids or ())}` "
                    f"| `{len(ind.warnings or ())}` |"
                )
        companies = getattr(pipeline_result, "companies", ()) or ()
        if companies:
            lines.append("")
            lines.append("### Companies")
            lines.append("")
            lines.append("| entity_id | snapshot_id | evidence_signal_ids | warnings |")
            lines.append("| --- | --- | --- | --- |")
            for comp in companies:
                lines.append(
                    f"| `{comp.input_bundle.entity_id}` "
                    f"| `{comp.snapshot_id}` "
                    f"| `{len(comp.evidence_signal_ids or ())}` "
                    f"| `{len(comp.warnings or ())}` |"
                )
    lines.append("")

    lines.append("## Snapshots")
    lines.append("")
    snapshot_ids = list(payload.get("snapshot_ids", []) or [])
    if not snapshot_ids:
        lines.append("_No snapshot rows persisted (dry-run or zero entities)._")
    else:
        lines.append("| scorer_type | entity_id | snapshot_id |")
        lines.append("| --- | --- | --- |")
        for s in snapshot_ids:
            lines.append(
                f"| `{s.get('scorer_type', '')}` "
                f"| `{s.get('entity_id', '')}` "
                f"| `{s.get('snapshot_id')}` |"
            )
    lines.append("")

    lines.append("## Graph Writes")
    lines.append("")
    graph_writes = list(payload.get("graph_writes", []) or [])
    if not graph_writes:
        lines.append("_No graph writes recorded._")
    else:
        lines.append("| score_node_id | created_nodes | created_edges | evidence_signals |")
        lines.append("| --- | --- | --- | --- |")
        for gw in graph_writes:
            lines.append(
                f"| `{gw.get('score_node_id', '')}` "
                f"| `{gw.get('created_node_count', 0)}` "
                f"| `{gw.get('created_edge_count', 0)}` "
                f"| `{len(gw.get('evidence_signal_ids', []) or [])}` |"
            )
    lines.append(f"_node_count={payload.get('node_count', 0)} • edge_count={payload.get('edge_count', 0)} • evidence_node_count={payload.get('evidence_node_count', 0)}_")
    lines.append("")

    if cfg.include_evidence_payload:
        lines.append("## Evidence Handles")
        lines.append("")
        handles = list(payload.get("evidence_handles", []) or [])
        if not handles:
            lines.append("_No evidence handles._")
        else:
            lines.append("| scorer_type | entity_id | score_node_id | has_evidence_summary |")
            lines.append("| --- | --- | --- | --- |")
            for h in handles:
                lines.append(
                    f"| `{h.get('scorer_type', '')}` "
                    f"| `{h.get('entity_id', '')}` "
                    f"| `{h.get('score_node_id', '')}` "
                    f"| `{bool(h.get('evidence_summary'))}` |"
                )
        lines.append("")

    lines.append("## Warnings")
    lines.append("")
    warnings = list(payload.get("warnings", []) or [])
    if not warnings:
        lines.append("_No warnings._")
    else:
        for w in warnings:
            lines.append(f"- {w}")
    lines.append("")

    lines.append("## Errors")
    lines.append("")
    errors = list(payload.get("errors", []) or [])
    if not errors:
        lines.append("_No errors._")
    else:
        for e in errors:
            lines.append(
                f"- `{e.get('component', '')}` **{e.get('error_class', '')}** — {e.get('message', '')}"
            )
    lines.append("")

    if recovery_meta:
        lines.append("## Recovery")
        lines.append("")
        if isinstance(recovery_meta, Mapping):
            for k, v in recovery_meta.items():
                lines.append(f"- {k}: `{v}`")
        else:
            lines.append(f"- {recovery_meta}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"_Generated: {ts} • schema v{cfg.schema_version}_")
    return "\n".join(lines) + "\n"


def render_recovery_markdown_report(
    recovery: Any | None,
    *,
    config: ReportConfig | None = None,
    generated_at: str | None = None,
) -> str:
    """Render a Markdown report for a :class:`RecoveryResult`."""
    cfg = config or ReportConfig(output_dir=tempfile.gettempdir(), recovery=True)
    if not cfg.recovery:
        cfg = dataclasses.replace(cfg, recovery=True)
    ts = generated_at or _utc_iso()

    lines: list[str] = []
    if recovery is None:
        lines.append("# Recovery Report")
        lines.append("")
        lines.append("> **Empty recovery envelope.**")
        lines.append("")
        lines.append(f"_Generated: {ts} • schema v{cfg.schema_version}_")
        return "\n".join(lines) + "\n"

    payload = recovery.to_dict()
    state = payload.get("state", {}) or {}
    lines.append("# Recovery Report")
    lines.append("")
    lines.append("## Run Metadata")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| run_id | `{payload.get('run_id', '')}` |")
    lines.append(f"| config_hash | `{state.get('config_hash', '')}` |")
    lines.append(f"| date_bucket | `{state.get('date_bucket', '')}` |")
    lines.append(f"| last_completed_stage | `{state.get('last_completed_stage', '')}` |")
    lines.append(f"| completed_through | `{payload.get('completed_through', '')}` |")
    lines.append(f"| resume_succeeded | `{payload.get('resume_succeeded', False)}` |")
    lines.append(f"| final_category | `{payload.get('final_category')}` |")
    lines.append(f"| started_at | `{payload.get('started_at', '')}` |")
    lines.append(f"| finished_at | `{payload.get('finished_at', '')}` |")
    lines.append(f"| duration_seconds | `{payload.get('duration_seconds', 0.0):.6f}` |")
    lines.append("")

    attempts = list(payload.get("attempts", []) or [])
    lines.append("## Attempts")
    lines.append("")
    if not attempts:
        lines.append("_No attempts recorded._")
    else:
        lines.append("| stage | attempt | succeeded | category | error_class | duration_seconds |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for a in attempts:
            lines.append(
                f"| `{a.get('stage', '')}` "
                f"| `{a.get('attempt_number', 0)}` "
                f"| `{a.get('succeeded', False)}` "
                f"| `{a.get('category')}` "
                f"| `{a.get('error_class', '')}` "
                f"| `{a.get('duration_seconds', 0.0):.6f}` |"
            )
    lines.append("")

    warnings = list(payload.get("warnings", []) or [])
    lines.append("## Warnings")
    lines.append("")
    if not warnings:
        lines.append("_No warnings._")
    else:
        for w in warnings:
            lines.append(f"- {w}")
    lines.append("")
    lines.append(f"_Generated: {ts} • schema v{cfg.schema_version}_")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Export entry point
# ---------------------------------------------------------------------------


def export_report(
    result: Any | None,
    *,
    config: ReportConfig,
    generated_at: str | None = None,
) -> tuple[ReportArtifact, ReportArtifact]:
    """Write the JSON + Markdown export to ``config.output_dir``.

    The output directory must exist and be writable. Existing files
    with the same name are overwritten.

    Args:
        result: The :class:`IntelligenceRunResult` (or ``None``).
        config: :class:`ReportConfig` describing the destination.
        generated_at: Optional timestamp string for deterministic
            snapshots. When None, ``datetime.now(timezone.utc)`` is
            used.

    Returns:
        Tuple ``(json_artifact, markdown_artifact)``.

    Raises:
        ReportExportError: When the destination is invalid or the
            write fails for any reason.
    """
    output_dir = Path(config.output_dir)
    if not output_dir.exists():
        raise ReportExportError(f"output_dir does not exist: {output_dir}")
    if not output_dir.is_dir():
        raise ReportExportError(f"output_dir is not a directory: {output_dir}")
    if not os.access(str(output_dir), os.W_OK):
        raise ReportExportError(f"output_dir is not writable: {output_dir}")

    json_payload: dict[str, Any]
    md_payload: str

    if config.recovery:
        json_payload = build_recovery_json_export(result, config=config, generated_at=generated_at)
        md_payload = render_recovery_markdown_report(result, config=config, generated_at=generated_at)
    else:
        json_payload = build_json_export(result, config=config, generated_at=generated_at)
        md_payload = render_markdown_report(result, config=config, generated_at=generated_at)

    prefix = config.file_prefix()
    if config.recovery:
        json_name = f"{prefix}{_RECOVERY_KIND_JSON}"
        md_name = f"{prefix}{_RECOVERY_KIND_MARKDOWN}"
    else:
        json_name = f"{prefix}{_REPORT_KIND_JSON}"
        md_name = f"{prefix}{_REPORT_KIND_MARKDOWN}"

    json_path = output_dir / json_name
    md_path = output_dir / md_name

    json_artifact = _write_file(
        kind=json_name.rsplit(".", 1)[0],
        path=json_path,
        content=json.dumps(json_payload, sort_keys=True, ensure_ascii=False, indent=2),
    )
    md_artifact = _write_file(
        kind=md_name.rsplit(".", 1)[0],
        path=md_path,
        content=md_payload,
    )
    return json_artifact, md_artifact


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_file(*, kind: str, path: Path, content: str) -> ReportArtifact:
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ReportExportError(f"failed to write {path}: {exc}") from exc
    raw = path.read_bytes()
    return ReportArtifact(
        kind=kind,
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )


__all__ = [
    "ReportConfig",
    "ReportArtifact",
    "SummaryStatistics",
    "ReportExportError",
    "SCHEMA_VERSION",
    "build_summary_statistics",
    "build_json_export",
    "build_recovery_json_export",
    "render_markdown_report",
    "render_recovery_markdown_report",
    "export_report",
]
