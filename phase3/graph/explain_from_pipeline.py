"""Phase 4 Task 3B Run 2 — ``--from-pipeline`` adapter for explain-score.

Why a separate module
---------------------
Run 1 reserved ``--from-pipeline`` on the ``explain-score`` subcommand
but left the wiring as a no-op. Run 2 turns the flag into a real
integration path: it accepts a JSON artifact that
:func:`phase3.pipeline.reporting.build_json_export` produced (the
``IntelligenceRunResult`` export envelope written to disk by
:func:`phase3.pipeline.reporting.export_report`), reconstructs a
:class:`phase3.pipeline.scoring_pipeline.PipelineResult` + a fresh
:class:`phase3.graph.in_memory_store.GraphStore` from the artifact, and
threads the result into :func:`phase3.graph.explain_score.explain_score`
so the three canonical graph queries see real signal / source / score
nodes.

The module is read-only. It never writes back to the artifact or to any
graph store the caller passes in (we build a fresh in-memory store per
call so two CLI invocations cannot collide).

Why JSON artifact and not SQLite run-id
----------------------------------------
The brief asked for "one deterministic local input mechanism" and named
"JSON artifact path or temp SQLite run identifier" as the two options.
The JSON artifact path was chosen because
:func:`phase3.pipeline.reporting.export_report` already produces a
stable, on-disk representation of an ``IntelligenceRunResult`` (with
``envelope`` + ``artifact`` + ``result`` + ``summary`` + ``warnings``)
and the format is round-trippable through :func:`json.dumps`. SQLite
run-identifier support would require a new persistence table the
project does not yet have, which is out of scope for Run 2.

Public surface
--------------
* :func:`load_pipeline_envelope` — parses and validates a JSON
  artifact. Raises :class:`ExplainPipelineArtifactError` for any
  shape mismatch (missing fields, wrong types, empty ``result``,
  non-existent / unreadable file).
* :class:`PipelineEnvelope` — the typed payload returned by the
  loader. Carries ``run_id``, ``config_hash``, ``date_bucket``,
  ``persisted``, ``score_node_id`` (canonical), the
  reconstructed :class:`PipelineResult`, the rebuilt
  :class:`GraphStore`, and any recovery / warning metadata.
* :func:`pipeline_result_from_envelope` — light-weight reconstructor
  that re-emits a :class:`PipelineResult` for the chosen score (used
  as the ``pipeline_result`` argument of
  :func:`phase3.graph.explain_score.explain_score`).

No production DB writes. No network. No side effects beyond reading
the file at ``artifact_path`` and constructing in-memory objects.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    make_graph_edge_id,
)
from phase3.datamodel.scores import ScoreBreakdown
from phase3.graph.in_memory_store import GraphStore
from phase3.pipeline.scoring_pipeline import PipelineResult


SCHEMA_VERSION = "1"


class ExplainPipelineArtifactError(ValueError):
    """Raised when the JSON artifact cannot be loaded or is malformed.

    The error message is operator-facing: it names the artifact path
    and the specific field that failed validation. The CLI prints it
    to stderr and exits with code 1.
    """


@dataclass(frozen=True)
class PipelineEnvelope:
    """Typed view of a loaded JSON artifact.

    Attributes:
        run_id: Run handle echoed from the artifact's
            ``artifact.run_id``.
        config_hash: Scorer-config hash from ``artifact.config_hash``.
        date_bucket: YYYY-MM-DD shared by every score in the run.
        persisted: Whether the source run wrote to a persistence
            layer (forwarded from ``artifact.persist``).
        score_node_id: Canonical score node id the caller asked
            for. Either the explicit ``--node`` choice (validated to
            exist in the artifact) or the first evidence-handle's
            ``score_node_id``.
        graph: A fresh in-memory graph store populated from
            ``graph_writes``. Read-only callers can compose the
            three canonical queries against it.
        pipeline_result: A :class:`PipelineResult` reconstructed for
            the chosen score. Its ``metadata`` carries the
            artifact-level fields (``run_id``, ``config_hash``,
            ``date_bucket``) so :func:`explain_score` can project
            them into ``score_metadata``.
        warnings: Run-level warnings from the artifact
            (``result.warnings``).
        errors: Run-level errors from the artifact
            (``result.errors``).
        recovery: The full :class:`IntelligenceRunResult` payload
            from the artifact, available for callers that want
            run-level fields (``metadata``, ``evidence_handles``,
            etc.) without re-parsing the artifact.
        source_artifact_path: Absolute path of the loaded file
            (audit trail).
        schema_version: The envelope's ``schema_version`` (string).
    """

    run_id: str
    config_hash: str
    date_bucket: str
    persisted: bool
    score_node_id: str
    graph: GraphStore
    pipeline_result: PipelineResult
    warnings: tuple[str, ...]
    errors: tuple[dict[str, Any], ...]
    recovery: dict[str, Any]
    source_artifact_path: str
    schema_version: str
    extra_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render the envelope as a JSON-serializable dict.

        The shape is stable; the ``graph`` is reported as counts
        (the full node / edge dump would dwarf the response). The
        nested :class:`PipelineResult` is reported via its
        ``metadata`` dict and the canonical fields.
        """
        return {
            "schema_version": self.schema_version,
            "source_artifact_path": self.source_artifact_path,
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "date_bucket": self.date_bucket,
            "persisted": self.persisted,
            "score_node_id": self.score_node_id,
            "pipeline_result": {
                "scorer_type": self.pipeline_result.metadata.get("scorer_type"),
                "entity_id": self.pipeline_result.metadata.get("entity_id"),
                "date_bucket": self.pipeline_result.metadata.get("date_bucket"),
                "config_hash": self.pipeline_result.metadata.get("config_hash"),
                "run_id": self.pipeline_result.metadata.get("run_id"),
                "dry_run": self.pipeline_result.metadata.get("dry_run"),
                "evidence_signal_ids": list(
                    self.pipeline_result.evidence_signal_ids
                ),
                "warnings": list(self.pipeline_result.warnings),
                "snapshot_id": self.pipeline_result.snapshot_id,
            },
            "graph": {
                "node_count": self.graph.node_count(),
                "edge_count": self.graph.edge_count(),
            },
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "extra_metadata": dict(self.extra_metadata),
        }


# ---------------------------------------------------------------------------
# Artifact loader
# ---------------------------------------------------------------------------


def load_pipeline_envelope(
    artifact_path: "str | os.PathLike[str]",
    *,
    score_node_id: str | None = None,
) -> PipelineEnvelope:
    """Load and validate a ``build_json_export`` artifact.

    The function reads the file, decodes the JSON, validates the
    top-level shape (envelope header + ``result`` payload), and
    rebuilds a :class:`PipelineEnvelope`.

    Args:
        artifact_path: Absolute or relative path to the JSON
            artifact on disk.
        score_node_id: Optional explicit score node id. When
            provided, must be present in the artifact's
            ``evidence_handles`` or ``graph_writes``; otherwise
            :class:`ExplainPipelineArtifactError` is raised. When
            None, the first ``graph_writes`` entry's
            ``score_node_id`` is used (or, if no graph writes were
            emitted, the first ``evidence_handles`` entry's id).

    Returns:
        A :class:`PipelineEnvelope` ready to be fed to
        :func:`phase3.graph.explain_score.explain_score`.

    Raises:
        ExplainPipelineArtifactError: When the file is missing,
        unreadable, not valid JSON, or has an unexpected shape.
    """
    abs_path = os.path.abspath(artifact_path)
    if not os.path.exists(abs_path):
        raise ExplainPipelineArtifactError(
            f"artifact not found: {abs_path}"
        )
    if not os.path.isfile(abs_path):
        raise ExplainPipelineArtifactError(
            f"artifact path is not a regular file: {abs_path}"
        )
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as exc:
        raise ExplainPipelineArtifactError(
            f"failed to read artifact {abs_path!r}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ExplainPipelineArtifactError(
            f"artifact is not valid JSON ({abs_path}): {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ExplainPipelineArtifactError(
            f"artifact root must be a JSON object, got "
            f"{type(raw).__name__}: {abs_path}"
        )

    schema_version = str(raw.get("schema_version", SCHEMA_VERSION))
    artifact_meta = raw.get("artifact")
    result_payload = raw.get("result")
    if not isinstance(artifact_meta, dict):
        raise ExplainPipelineArtifactError(
            f"artifact envelope missing 'artifact' dict: {abs_path}"
        )
    if not isinstance(result_payload, dict):
        raise ExplainPipelineArtifactError(
            f"artifact envelope missing 'result' dict "
            f"(empty run?): {abs_path}"
        )

    run_id = str(artifact_meta.get("run_id", ""))
    config_hash = str(artifact_meta.get("config_hash", ""))
    date_bucket = str(artifact_meta.get("date_bucket", ""))
    persisted = bool(artifact_meta.get("persist", False))

    graph_writes = result_payload.get("graph_writes") or []
    evidence_handles = result_payload.get("evidence_handles") or []
    warnings = tuple(result_payload.get("warnings") or [])
    errors_raw = result_payload.get("errors") or []
    errors: tuple[dict[str, Any], ...] = tuple(
        dict(e) for e in errors_raw if isinstance(e, Mapping)
    )

    # Resolve the score node id the caller wants explained.
    # Dedupe while preserving order so the error message and the
    # default-pick are stable; a single score id can appear once
    # in graph_writes and again in evidence_handles (the writer
    # stamps the canonical id into both buckets by design).
    candidate_ids: list[str] = []
    seen: set[str] = set()
    for gw in graph_writes:
        sid = gw.get("score_node_id")
        if isinstance(sid, str) and sid and sid not in seen:
            candidate_ids.append(sid)
            seen.add(sid)
    for eh in evidence_handles:
        sid = eh.get("score_node_id")
        if isinstance(sid, str) and sid and sid not in seen:
            candidate_ids.append(sid)
            seen.add(sid)
    if not candidate_ids:
        raise ExplainPipelineArtifactError(
            f"artifact contains no score_node_id in graph_writes "
            f"or evidence_handles: {abs_path}"
        )

    if score_node_id is not None:
        if score_node_id not in candidate_ids:
            raise ExplainPipelineArtifactError(
                f"requested score_node_id {score_node_id!r} not "
                f"present in artifact (candidates: "
                f"{candidate_ids[:5]}{'...' if len(candidate_ids) > 5 else ''})"
            )
        chosen = score_node_id
    else:
        chosen = candidate_ids[0]

    # Pin node ``created_at`` to the artifact's ``date_bucket`` so
    # repeated invocations produce byte-identical output (the
    # ``score_metadata.created_at`` field would otherwise vary).
    node_created_at = (
        datetime.strptime(date_bucket, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if date_bucket
        else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    graph = _build_graph_from_writes(
        graph_writes, node_created_at=node_created_at,
    )
    pipeline_result = _reconstruct_pipeline_result(
        result_payload=result_payload,
        score_node_id=chosen,
        run_id=run_id,
        config_hash=config_hash,
        date_bucket=date_bucket,
    )

    extra_metadata: dict[str, Any] = {}
    md = result_payload.get("metadata")
    if isinstance(md, Mapping):
        for k, v in md.items():
            if k in (
                "evidence_trace",
                "graph_writes",
                "evidence_handles",
                "snapshot_ids",
            ):
                continue
            extra_metadata[k] = v

    return PipelineEnvelope(
        run_id=run_id,
        config_hash=config_hash,
        date_bucket=date_bucket,
        persisted=persisted,
        score_node_id=chosen,
        graph=graph,
        pipeline_result=pipeline_result,
        warnings=warnings,
        errors=errors,
        recovery=dict(result_payload),
        source_artifact_path=abs_path,
        schema_version=schema_version,
        extra_metadata=extra_metadata,
    )


# ---------------------------------------------------------------------------
# Graph reconstruction
# ---------------------------------------------------------------------------


def _build_graph_from_writes(
    graph_writes: list[Any],
    *,
    node_created_at: datetime,
) -> GraphStore:
    """Rebuild a fresh in-memory graph from ``graph_writes`` rows.

    Each write row contains ``score_node_id``, ``entity_node_id``,
    ``signal_node_ids`` (a list of canonical ``signal:<id>`` strings),
    ``source_node_ids`` (canonical ``source:<id>`` strings), and
    ``cross_layer_edge_ids``. We synthesize the nodes + the
    connecting edges using the canonical id helpers already in
    :mod:`phase3.pipeline.graph_writer`.

    The ``node_created_at`` argument pins every synthesized node's
    ``created_at`` so repeated CLI invocations against the same
    artifact produce byte-identical output. Without it the
    ``GraphNode`` default factory would stamp ``datetime.now(utc)``
    on every call and the Run 2 determinism contract would be
    broken (see ``explain_score``'s ``score_metadata.created_at``
    projection).
    """
    store = GraphStore()

    for gw in graph_writes:
        if not isinstance(gw, Mapping):
            continue
        score_id = gw.get("score_node_id")
        entity_id = gw.get("entity_node_id")
        signal_ids = list(gw.get("signal_node_ids") or [])
        source_ids = list(gw.get("source_node_ids") or [])
        if not isinstance(score_id, str) or not score_id:
            continue

        # Score node (always present per Run 1 contract).
        if not store.has_node(score_id):
            store.add_node(GraphNode(
                node_id=score_id,
                node_type=NodeType.SCORE,
                label=score_id.split(":")[-1] if ":" in score_id else score_id,
                created_at=node_created_at,
            ))

        # Entity node (optional).
        if isinstance(entity_id, str) and entity_id and not store.has_node(entity_id):
            store.add_node(GraphNode(
                node_id=entity_id,
                node_type=NodeType.COMPANY,
                label=entity_id,
                created_at=node_created_at,
            ))

        # Signal nodes.
        for sig in signal_ids:
            if not isinstance(sig, str) or not sig:
                continue
            if not store.has_node(sig):
                store.add_node(GraphNode(
                    node_id=sig,
                    node_type=NodeType.SIGNAL,
                    label=sig.split(":", 1)[-1] if ":" in sig else sig,
                    created_at=node_created_at,
                ))
            edge_id = make_graph_edge_id(
                EdgeType.CONTRIBUTES_TO, sig, score_id
            )
            if store.get_edge(edge_id) is None:
                store.add_edge(GraphEdge(
                    edge_id=edge_id,
                    edge_type=EdgeType.CONTRIBUTES_TO,
                    from_node_id=sig,
                    to_node_id=score_id,
                    weight=1.0,
                    created_at=node_created_at,
                ))

        # Source nodes (optional; usually empty for orchestrator runs).
        for src in source_ids:
            if not isinstance(src, str) or not src:
                continue
            if not store.has_node(src):
                store.add_node(GraphNode(
                    node_id=src,
                    node_type=NodeType.SOURCE,
                    label=src.split(":", 1)[-1] if ":" in src else src,
                    created_at=node_created_at,
                ))

    return store


# ---------------------------------------------------------------------------
# PipelineResult reconstruction
# ---------------------------------------------------------------------------


def _reconstruct_pipeline_result(
    *,
    result_payload: Mapping[str, Any],
    score_node_id: str,
    run_id: str,
    config_hash: str,
    date_bucket: str,
) -> PipelineResult:
    """Build a :class:`PipelineResult` for ``score_node_id``.

    The reconstructed :class:`PipelineResult` is a *thin* shim that
    satisfies :func:`phase3.graph.explain_score.explain_score`'s
    ``pipeline_result`` parameter contract:

    * ``score.breakdown.scorer_type``
    * ``score.breakdown.entity_id``
    * ``score.breakdown.timestamp`` (date_bucket)
    * ``metadata.scorer_type / entity_id / date_bucket / config_hash
      / run_id``

    No breakdown dimensions / factors are reconstructed — the
    artifact doesn't carry them. :func:`explain_score` only reads
    the metadata fields, so this is sufficient.
    """
    scorer_type, entity_id = _parse_score_node_id(score_node_id)

    # Prefer the canonical ``entity_node_id`` from the matching
    # graph_writes entry — that field carries the
    # ``entity:<type>:<id>`` form (``entity:company:2330``) which
    # is what ``explain_score`` projects into ``score_metadata``
    # and what the canonical ``make_entity_node_id`` helper
    # produces. The score_node_id format
    # ``score:<type>:<entity>:<date>`` is *not* guaranteed to
    # carry the entity prefix (e.g. ``score:company:2330:2026-XX``
    # is what the writer currently emits for the 2330 case).
    canonical_entity_id: str | None = None
    for gw in result_payload.get("graph_writes") or []:
        if not isinstance(gw, Mapping):
            continue
        if gw.get("score_node_id") != score_node_id:
            continue
        ent = gw.get("entity_node_id")
        if isinstance(ent, str) and ent:
            canonical_entity_id = ent
            break
    if canonical_entity_id is not None:
        entity_id = canonical_entity_id

    snapshot_ids = result_payload.get("snapshot_ids") or []
    snapshot_id: int | None = None
    for entry in snapshot_ids:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("scorer_type") == scorer_type and entry.get("entity_id") == entity_id:
            snap = entry.get("snapshot_id")
            if isinstance(snap, int):
                snapshot_id = snap
            break

    evidence_signal_ids: tuple[str, ...] = ()
    for gw in result_payload.get("graph_writes") or []:
        if not isinstance(gw, Mapping):
            continue
        if gw.get("score_node_id") != score_node_id:
            continue
        for sig in gw.get("signal_node_ids") or []:
            if not isinstance(sig, str) or not sig:
                continue
            plain = sig.split(":", 1)[-1] if ":" in sig else sig
            evidence_signal_ids = evidence_signal_ids + (plain,)

    breakdown = ScoreBreakdown(
        scorer_type=scorer_type,
        entity_type=scorer_type,
        entity_id=entity_id,
        score=0.0,
        confidence=0.0,
        dimensions=[],
        overall_evidence=[],
        cross_layer_adjustments=[],
        schema_version="explain-from-pipeline",
        timestamp=datetime.strptime(date_bucket, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        ) if date_bucket else datetime.now(timezone.utc),
        # Deterministic ``valid_until``: derive from ``timestamp``
        # plus the same 24h TTL the live scorers use (see
        # ``phase3.scoring.base.Scorer._ttl_hours``). Using
        # ``datetime.now(timezone.utc)`` here would inject a
        # different value on every CLI invocation and break
        # the Run 2 determinism contract.
        valid_until=(
            datetime.strptime(date_bucket, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ) + timedelta(hours=24)
            if date_bucket
            else datetime.now(timezone.utc)
        ),
        config_hash=config_hash or "explain-from-pipeline",
    )

    # Minimal ``Score``-shaped shim. ``explain_score`` only ever reads
    # ``result.score.breakdown`` (via the canonical ``score_node_id_for_result``).
    # We do not import the typed ``CompanyScore`` / ``IndustryScore`` / ``MacroScore``
    # classes here — the explain-score composition is duck-typed on the breakdown.
    score_obj = _ScoreShim(breakdown=breakdown)

    return PipelineResult(
        score=score_obj,  # type: ignore[arg-type]
        input_bundle=None,  # type: ignore[arg-type]
        evidence_signal_ids=evidence_signal_ids,
        snapshot_id=snapshot_id,
        warnings=(),
        metadata={
            "scorer_type": scorer_type,
            "entity_id": entity_id,
            "date_bucket": date_bucket,
            "config_hash": config_hash,
            "run_id": run_id,
            "dry_run": not bool(_find_persist_flag(result_payload)),
        },
    )


class _ScoreShim:
    """Tiny shim satisfying ``result.score.breakdown`` duck-typing.

    :func:`phase3.graph.explain_score.explain_score` reads the
    breakdown's ``scorer_type`` / ``entity_id`` / ``timestamp`` and
    the ``metadata`` dict. It never inspects any other attribute
    on the score, so the shim is sufficient.
    """

    __slots__ = ("breakdown",)

    def __init__(self, breakdown: ScoreBreakdown) -> None:
        self.breakdown = breakdown


def _parse_score_node_id(score_node_id: str) -> tuple[str, str]:
    """Parse ``score:<type>:<entity>:<date>`` into ``(scorer_type, entity_id)``.

    The canonical id is emitted by
    :func:`phase3.pipeline.graph_writer.make_score_node_id`. We only
    need ``scorer_type`` + ``entity_id`` for the PipelineResult
    metadata, so a plain split is sufficient. The ``date_bucket`` is
    already carried in the envelope and re-attached separately.
    """
    parts = score_node_id.split(":")
    if len(parts) < 4 or parts[0] != "score":
        return ("unknown", "unknown")
    scorer_type = parts[1].strip() or "unknown"
    # ``entity_id`` may itself contain ``:`` (rare but allowed). The
    # canonical id is ``score:<type>:<entity>:<date>`` so the date is
    # always the last segment and the entity is everything in between.
    entity_id = ":".join(parts[2:-1]).strip() or "unknown"
    return (scorer_type, entity_id)


def _find_persist_flag(result_payload: Mapping[str, Any]) -> bool:
    """Recover the ``persist`` boolean from the result payload.

    ``IntelligenceRunResult.to_dict()`` emits ``persist`` at the
    top level, so the value is mirrored in the artifact.
    """
    val = result_payload.get("persist")
    return bool(val) if isinstance(val, bool) else False


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


__all__ = [
    "ExplainPipelineArtifactError",
    "PipelineEnvelope",
    "SCHEMA_VERSION",
    "load_pipeline_envelope",
]
