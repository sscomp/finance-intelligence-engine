"""Explain-Score / Decision Trace — Phase 4 Task 3B Run 1.

Read-only composition of the canonical graph queries (Lineage,
Blast Radius, Cross-layer Impact) around a single score node,
plus the score's own metadata. Answers the operator question
"why does this score exist, what contributed, what does it
touch, and is the trace complete?".

What this module is
-------------------
This is a **read-only** explanation service. It does not write to
any DB, does not mutate the graph store, and does not re-implement
any of the BFS machinery. It composes the three canonical
graph queries from Phase 3B Task 4 Run 4A/4B:

* :func:`phase3.graph.lineage.compute_lineage` — upstream
  provenance (signals, sources, scoring layers above this one).
* :func:`phase3.graph.blast_radius.compute_blast_radius` —
  downstream consumers (scores / reports / signals that
  transitively depend on this score).
* :func:`phase3.graph.cross_layer_impact.compute_cross_layer_impact`
  — cross-layer score-to-score impact (INFLUENCES / DERIVED_FROM
  in both directions, focused on the score graph).

The result is :class:`ExplainedScore`, a frozen dataclass with
a :meth:`ExplainedScore.to_dict` JSON-serializable view and a
:meth:`ExplainedScore.to_markdown` human-readable view. The
shape is designed for two consumers:

1. The ``explain-score`` CLI subcommand (operator demo / audit).
2. Any future caller (e.g. the intelligence pipeline) that wants
   a one-call summary of a score's decision evidence.

What this module is NOT
-----------------------
* Not a replacement for :func:`phase3.scoring.explain.explain_score`
  (the existing Chinese-text breakdown explainer). The two are
  complementary: that one renders the scorer's per-dimension
  breakdown; this one renders the graph-based decision evidence.
* Not a re-implementation of any BFS. The three query modules
  own their BFS and direction policy; this module composes their
  DTOs.
* Not a writer. There is no ``--persist`` mode, no SQLite INSERT,
  no intelligence.db writes.

Determinism
-----------
The three query modules are themselves deterministic (stable
``(edge_id, neighbor_id)`` order at every hop). Composition is
deterministic when the underlying store and the inputs are
deterministic. The :meth:`ExplainedScore.to_dict` view sorts
keys and the :meth:`ExplainedScore.to_markdown` view emits a
fixed field order, so two explanations over the same graph
produce byte-for-byte identical output.

Read-only contract
------------------
:class:`ExplainedScore` exposes the result of three read-only
queries and a metadata projection. It never opens a connection,
never INSERTs, and never opens a network socket. The
``--db-path`` guard against ``macro_history.db`` is enforced
upstream in the CLI layer (see :func:`phase3.cli._resolve_graph_store`)
— this module is DB-agnostic and only consumes the
:class:`GraphStore` Protocol surface.

No production DB writes. No cron / jobs.json edits. No commits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from phase3.datamodel.graph import EdgeType, GraphNode, NodeType
from phase3.graph.blast_radius import (
    BlastRadiusResult,
    compute_blast_radius,
)
from phase3.graph.cross_layer_impact import (
    CrossLayerImpactResult,
    compute_cross_layer_impact,
)
from phase3.graph.in_memory_store import GraphStore
from phase3.graph.lineage import LineageQuery, compute_lineage


class _GraphStoreLike(Protocol):
    """Structural protocol for the graph-store surface this
    module depends on. Both
    :class:`phase3.graph.in_memory_store.GraphStore` and
    :class:`phase3.graph.sqlite_store.SQLiteGraphStore` satisfy
    this protocol (duck-typed). Mirrors the protocol in the
    three query modules so we can stay decoupled from a
    concrete class hierarchy.
    """

    def has_node(self, node_id: str) -> bool: ...
    def get_node(self, node_id: str) -> GraphNode | None: ...
    def edges_from(self, node_id: str) -> list[Any]: ...
    def edges_to(self, node_id: str) -> list[Any]: ...


# Public alias matching the three query modules' style.
GraphStoreLike = _GraphStoreLike


#: Default max-depth used by the three underlying queries.
DEFAULT_MAX_DEPTH: int = 5

#: Default max-nodes cap used by the three underlying queries.
DEFAULT_MAX_NODES: int | None = None


# ---------------------------------------------------------------------------
# Score metadata projection
# ---------------------------------------------------------------------------


def _project_score_metadata(node: GraphNode) -> dict[str, Any]:
    """Project a :class:`GraphNode` of type :data:`NodeType.SCORE`
    into a JSON-friendly metadata dict.

    The graph node is the load-bearing source of truth: the
    ``label`` field carries the score value (per the canonical
    sample graph convention), the ``node_id`` carries the
    stable identifier, and any extra fields (metadata, tags)
    are surfaced as-is. The :class:`PipelineResult`-derived
    metadata is layered on top of this by the caller.

    Defensive: any non-``SCORE`` node still gets a
    ``"node_id"`` / ``"node_type"`` projection so a caller
    that points at the wrong node can still see *what* the
    node was.
    """
    out: dict[str, Any] = {
        "node_id": node.node_id,
        "node_type": node.node_type.value,
        "label": node.label,
        "created_at": node.created_at.isoformat()
        if node.created_at is not None
        else None,
        "metadata": dict(node.metadata),
        "tags": list(node.tags),
        "schema_version": node.schema_version,
    }
    if node.node_type == NodeType.SCORE:
        # Operator-facing fields: the score's id and a string
        # projection of its value (the label is the canonical
        # string for the value in the sample graph; in
        # production it would come from a separate field).
        out["score_id"] = node.node_id
        out["score_value_label"] = node.label
    return out


# ---------------------------------------------------------------------------
# Result DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplainedScore:
    """The result of an explain-score composition.

    Attributes:
        score_node_id: The anchor node id the explanation was
            computed for. Always present (even when missing from
            the graph) so the consumer can match the response
            to the request.
        score_metadata: Projection of the graph node's
            ``label`` / ``created_at`` / ``metadata`` / ``tags``
            plus any optional ``PipelineResult`` enrichment
            (``scorer_type``, ``entity_id``, ``date_bucket``,
            ``config_hash``, ``run_id``, ``valid_until``,
            ``confidence``). Empty dict when the node is not
            in the graph.
        present: True when the score node was found in the
            graph store. False triggers a single warning and
            empty / default query payloads below.
        lineage: Result of the upstream lineage query. Empty
            :class:`LineageQuery` when the node is missing.
        blast_radius: Result of the downstream blast-radius
            query. Empty :class:`BlastRadiusResult` when the
            node is missing.
        cross_layer_impact: Result of the cross-layer impact
            query. Empty :class:`CrossLayerImpactResult` when
            the node is missing.
        warnings: Aggregate of all warnings from the three
            underlying queries plus the top-level "not in
            graph" warning when applicable. Order is stable:
            top-level first, then lineage, blast, cross-layer.
        truncated: True when any of the three walks truncated
            (max_depth or max_nodes hit). Signals that the
            explanation may be incomplete.
        depth_reached: The maximum ``depth_reached`` across
            the three walks. 0 when the node is missing.

    Note on the default factories: each of the three nested
    DTOs requires a non-empty ``start`` field. We expose them
    here as default-constructed with an empty-string ``start``
    placeholder; callers should treat the field as authoritative
    (``score_node_id``) and never read the nested ``start``
    without going through the top-level field. The default
    factory is intentionally a lambda (Pyright's strict mode
    rejects bare class references in :func:`field`).
    """

    score_node_id: str
    score_metadata: dict[str, Any] = field(default_factory=dict)
    present: bool = False
    lineage: LineageQuery = field(
        default_factory=lambda: LineageQuery(start="")
    )
    blast_radius: BlastRadiusResult = field(
        default_factory=lambda: BlastRadiusResult(start="")
    )
    cross_layer_impact: CrossLayerImpactResult = field(
        default_factory=lambda: CrossLayerImpactResult(start="")
    )
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    depth_reached: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Render the explanation as a JSON-serializable dict.

        Top-level keys are stable and sorted alphabetically
        when serialized. The three nested query payloads are
        rendered via their own :meth:`to_dict` views. The
        result round-trips through :func:`json.dumps` without
        further conversion.
        """
        return {
            "score_node_id": self.score_node_id,
            "score_metadata": dict(self.score_metadata),
            "present": self.present,
            "lineage": self.lineage.to_dict(),
            "blast_radius": self.blast_radius.to_dict(),
            "cross_layer_impact": self.cross_layer_impact.to_dict(),
            "warnings": list(self.warnings),
            "truncated": self.truncated,
            "depth_reached": self.depth_reached,
        }

    def to_markdown(self) -> str:
        """Render the explanation as a human-readable Markdown
        document.

        The shape is fixed: header, score metadata, lineage
        summary, blast-radius summary, cross-layer impact
        summary, warnings, and a one-line truncation flag.
        Designed to be readable in a terminal and copy-pastable
        into a chat without further formatting.
        """
        lines: list[str] = []
        sm = self.score_metadata
        # Header
        lines.append(f"# Explain-Score: `{self.score_node_id}`")
        lines.append("")
        if not self.present:
            lines.append(
                f"⚠️ **Score node not found in the graph store.**"
            )
            lines.append("")
        # Score metadata block
        lines.append("## Score metadata")
        lines.append("")
        if not sm:
            lines.append("_(no metadata available — node not in graph)_")
        else:
            # Stable key order; canonical fields first.
            canonical_keys = (
                "score_id", "score_value_label", "scorer_type",
                "entity_id", "entity_type", "date_bucket",
                "config_hash", "run_id", "valid_until",
                "confidence", "node_type", "label",
                "created_at", "schema_version",
            )
            seen: set[str] = set()
            for k in canonical_keys:
                if k in sm:
                    lines.append(f"- **{k}**: `{sm[k]}`")
                    seen.add(k)
            for k in sorted(sm):
                if k in seen:
                    continue
                v = sm[k]
                if isinstance(v, (list, dict)):
                    lines.append(f"- **{k}**: `{v!r}`")
                else:
                    lines.append(f"- **{k}**: `{v}`")
        lines.append("")
        # Lineage
        lines.append("## Lineage (upstream provenance)")
        lines.append("")
        if not self.present:
            lines.append("_(skipped — node not in graph)_")
        else:
            lq = self.lineage
            lines.append(
                f"- visited_node_ids: `{len(lq.visited_node_ids)}`"
            )
            lines.append(
                f"- source_node_ids: `{len(lq.source_node_ids)}` "
                f"({', '.join(lq.source_node_ids) or '—'})"
            )
            lines.append(
                f"- signal_node_ids: `{len(lq.signal_node_ids)}` "
                f"({', '.join(lq.signal_node_ids) or '—'})"
            )
            lines.append(
                f"- score_node_ids: `{len(lq.score_node_ids)}` "
                f"({', '.join(lq.score_node_ids) or '—'})"
            )
            lines.append(
                f"- entity_node_ids: `{len(lq.entity_node_ids)}` "
                f"({', '.join(lq.entity_node_ids) or '—'})"
            )
            lines.append(
                f"- leaf_node_ids: `{len(lq.leaf_node_ids)}`"
            )
            lines.append(f"- depth_reached: `{lq.depth_reached}`")
            lines.append(f"- truncated: `{lq.truncated}`")
        lines.append("")
        # Blast Radius
        lines.append("## Blast Radius (downstream consumers)")
        lines.append("")
        if not self.present:
            lines.append("_(skipped — node not in graph)_")
        else:
            br = self.blast_radius
            lines.append(
                f"- visited_node_ids: `{len(br.visited_node_ids)}`"
            )
            lines.append(
                f"- score_node_ids: `{len(br.score_node_ids)}` "
                f"({', '.join(br.score_node_ids) or '—'})"
            )
            lines.append(
                f"- signal_node_ids: `{len(br.signal_node_ids)}` "
                f"({', '.join(br.signal_node_ids) or '—'})"
            )
            lines.append(
                f"- report_node_ids: `{len(br.report_node_ids)}` "
                f"({', '.join(br.report_node_ids) or '—'})"
            )
            if br.impact_counts:
                ic = ", ".join(
                    f"{k}×{v}" for k, v in sorted(br.impact_counts.items())
                )
                lines.append(f"- impact_counts: `{ic}`")
            lines.append(f"- depth_reached: `{br.depth_reached}`")
            lines.append(f"- truncated: `{br.truncated}`")
        lines.append("")
        # Cross-layer Impact
        lines.append("## Cross-layer Impact (score-to-score)")
        lines.append("")
        if not self.present:
            lines.append("_(skipped — node not in graph)_")
        else:
            cli = self.cross_layer_impact
            lines.append(
                f"- upstream_chain: `[{', '.join(cli.upstream_chain) or '—'}]`"
            )
            lines.append(
                f"- downstream_chain: `[{', '.join(cli.downstream_chain) or '—'}]`"
            )
            if cli.upstream_layers:
                ul = ", ".join(
                    f"{k}={v}" for k, v in sorted(cli.upstream_layers.items())
                )
                lines.append(f"- upstream_layers: `{ul}`")
            if cli.downstream_layers:
                dl = ", ".join(
                    f"{k}={v}" for k, v in sorted(cli.downstream_layers.items())
                )
                lines.append(f"- downstream_layers: `{dl}`")
            lines.append(
                f"- depth_reached_upstream: "
                f"`{cli.depth_reached_upstream}`"
            )
            lines.append(
                f"- depth_reached_downstream: "
                f"`{cli.depth_reached_downstream}`"
            )
            lines.append(
                f"- truncated_upstream: `{cli.truncated_upstream}`"
            )
            lines.append(
                f"- truncated_downstream: `{cli.truncated_downstream}`"
            )
        lines.append("")
        # Warnings
        if self.warnings:
            lines.append("## Warnings")
            lines.append("")
            for w in self.warnings:
                lines.append(f"- {w}")
            lines.append("")
        # Footer
        lines.append("---")
        lines.append(
            f"truncated: `{self.truncated}` · "
            f"depth_reached: `{self.depth_reached}` · "
            f"warnings: `{len(self.warnings)}`"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _enrich_metadata_from_pipeline(
    base: dict[str, Any],
    pipeline_result: Any | None,
) -> dict[str, Any]:
    """Optionally enrich the score metadata with fields pulled
    from a :class:`PipelineResult`.

    Only fields that are stable and present on the result are
    added. The base dict is never mutated; we return a new
    dict. Fields are added under the same names callers
    already use elsewhere (``scorer_type``, ``entity_id``,
    ``date_bucket``, ``config_hash``, ``run_id``,
    ``valid_until``, ``confidence``).
    """
    out = dict(base)
    if pipeline_result is None:
        return out
    breakdown = getattr(pipeline_result, "score", None)
    if breakdown is not None:
        breakdown = getattr(breakdown, "breakdown", breakdown)
    # Pull fields defensively — the PipelineResult is an
    # optional enrichment, not a hard requirement.
    def _get(obj: Any, *names: str) -> Any:
        for n in names:
            v = getattr(obj, n, None)
            if v is not None:
                return v
        return None

    scorer_type = _get(breakdown, "scorer_type")
    entity_id = _get(breakdown, "entity_id")
    entity_type = _get(breakdown, "entity_type")
    confidence = _get(breakdown, "confidence")
    valid_until = _get(breakdown, "valid_until")
    config_hash = _get(breakdown, "config_hash")

    md = getattr(pipeline_result, "metadata", None)
    date_bucket = None
    run_id = None
    if isinstance(md, Mapping):
        date_bucket = md.get("date_bucket")
        run_id = md.get("run_id") or None

    if scorer_type is not None and "scorer_type" not in out:
        out["scorer_type"] = str(scorer_type)
    if entity_id is not None and "entity_id" not in out:
        out["entity_id"] = str(entity_id)
    if entity_type is not None and "entity_type" not in out:
        out["entity_type"] = str(entity_type)
    if confidence is not None and "confidence" not in out:
        out["confidence"] = float(confidence)
    if valid_until is not None and "valid_until" not in out:
        out["valid_until"] = (
            valid_until.isoformat()
            if hasattr(valid_until, "isoformat")
            else str(valid_until)
        )
    if config_hash is not None and "config_hash" not in out:
        out["config_hash"] = str(config_hash)
    if date_bucket is not None and "date_bucket" not in out:
        out["date_bucket"] = str(date_bucket)
    if run_id is not None and "run_id" not in out:
        out["run_id"] = str(run_id)
    return out


def explain_score(
    store: GraphStoreLike,
    score_node_id: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int | None = DEFAULT_MAX_NODES,
    pipeline_result: Any | None = None,
) -> ExplainedScore:
    """Compute the explain-score composition for ``score_node_id``.

    Parameters
    ----------
    store:
        The graph store to query. Any object satisfying the
        :class:`GraphStoreLike` protocol works (in-memory,
        SQLite, or a test double).
    score_node_id:
        The anchor node id, e.g.
        ``"score:company:2330:2026-07-08"``. Must be a
        :class:`NodeType.SCORE` node for a meaningful result;
        the function does not refuse non-score ids, but the
        three query payloads will be empty / warnings-laden.
    max_depth:
        Maximum hop count for each of the three underlying
        queries. Default 5.
    max_nodes:
        Optional cap on the number of nodes each underlying
        walk may visit. Default ``None`` (no cap).
    pipeline_result:
        Optional :class:`PipelineResult` to enrich the
        ``score_metadata`` projection. When provided, fields
        such as ``scorer_type`` / ``entity_id`` / ``date_bucket``
        / ``config_hash`` / ``run_id`` / ``valid_until`` /
        ``confidence`` are added if not already present in
        the graph node's metadata. Default ``None``.

    Returns
    -------
    :class:`ExplainedScore`. When ``score_node_id`` is not in
    the graph, ``present=False`` and the three query payloads
    are empty DTOs (default-constructed). The
    ``warnings`` list contains a single top-level entry
    ``"score node {id!r} not in graph"`` followed by any
    warnings the three queries produced (which will be
    none in this branch because the queries themselves
    detect the missing node and short-circuit).
    """
    warnings: list[str] = []

    if not store.has_node(score_node_id):
        warnings.append(f"score node {score_node_id!r} not in graph")
        # Defensive: also warn that downstream queries are
        # skipped (they would have produced the same warning
        # themselves, but emitting it twice would be noise).
        empty_lineage = LineageQuery(start=score_node_id, warnings=[])
        empty_blast = BlastRadiusResult(start=score_node_id, warnings=[])
        empty_cross = CrossLayerImpactResult(start=score_node_id, warnings=[])
        return ExplainedScore(
            score_node_id=score_node_id,
            score_metadata={},
            present=False,
            lineage=empty_lineage,
            blast_radius=empty_blast,
            cross_layer_impact=empty_cross,
            warnings=warnings,
            truncated=False,
            depth_reached=0,
        )

    node = store.get_node(score_node_id)
    if node is None:
        # Defensive fallback: has_node() said yes but
        # get_node() returned None. Treat as missing.
        warnings.append(
            f"score node {score_node_id!r} has_node=True "
            f"but get_node() returned None"
        )
        return ExplainedScore(
            score_node_id=score_node_id,
            score_metadata={},
            present=False,
            lineage=LineageQuery(start=score_node_id, warnings=[]),
            blast_radius=BlastRadiusResult(start=score_node_id, warnings=[]),
            cross_layer_impact=CrossLayerImpactResult(
                start=score_node_id, warnings=[]
            ),
            warnings=warnings,
            truncated=False,
            depth_reached=0,
        )

    # Score metadata projection (with optional PipelineResult enrichment).
    base_meta = _project_score_metadata(node)
    score_meta = _enrich_metadata_from_pipeline(base_meta, pipeline_result)

    # Run the three canonical queries. They are themselves
    # deterministic; the composition is deterministic when
    # the store is deterministic.
    lineage = compute_lineage(
        store,
        score_node_id,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    blast = compute_blast_radius(
        store,
        score_node_id,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    cross = compute_cross_layer_impact(
        store,
        score_node_id,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )

    # Aggregate warnings in a stable, predictable order:
    # top-level first (none in the present-true branch),
    # then lineage, then blast-radius, then cross-layer.
    if lineage.warnings:
        warnings.extend(lineage.warnings)
    if blast.warnings:
        warnings.extend(blast.warnings)
    if cross.warnings:
        warnings.extend(cross.warnings)

    truncated = bool(
        lineage.truncated
        or blast.truncated
        or cross.truncated_upstream
        or cross.truncated_downstream
    )
    depth_reached = max(
        lineage.depth_reached,
        blast.depth_reached,
        cross.depth_reached_upstream,
        cross.depth_reached_downstream,
    )

    # Surface a top-level truncation warning when the aggregate
    # truncated flag flipped but no underlying query emitted a
    # human-readable warning. This is the case for
    # max_nodes-driven truncation in the canonical query
    # modules: their BFS short-circuits on the cap and the
    # truncated flag flips to True, but the warnings list is
    # left empty. Without this synthetic warning the operator
    # would only learn the trace is incomplete from a single
    # boolean — the to_markdown() / to_dict() consumers would
    # see "truncated: True, warnings: []" with no explanation.
    if truncated and not warnings:
        warnings.append(
            "trace truncated: one or more of the upstream lineage, "
            "downstream blast-radius, or cross-layer impact walks "
            "hit the max_depth or max_nodes cap; the explanation "
            "above is incomplete"
        )

    return ExplainedScore(
        score_node_id=score_node_id,
        score_metadata=score_meta,
        present=True,
        lineage=lineage,
        blast_radius=blast,
        cross_layer_impact=cross,
        warnings=warnings,
        truncated=truncated,
        depth_reached=depth_reached,
    )


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES",
    "ExplainedScore",
    "GraphStoreLike",
    "explain_score",
]
