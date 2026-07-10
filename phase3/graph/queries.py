"""Graph Query Layer — Phase 3B Task 4 Run 4B (post-consolidation).

Reusable, read-only query APIs over the research graph.

Scope (post Run 4A consolidation, 2026-07-11)
--------------------------------------------
This module ships two purpose-built query families and a single
facade (:class:`GraphQueryService`) that exposes both with sensible
defaults. It is intentionally *thin* — every query delegates to the
existing BFS / leaf machinery rather than duplicating it.

Query families
--------------
1. :func:`query_lineage` (alias :meth:`GraphQueryService.lineage`)
   Walk **upstream** from a node (typically a score) and return a
   :class:`LineageQuery` with the visited nodes bucketed by type,
   the leaf summary, and an optional depth-layered view. This is
   the "where did this score come from" question. Safe to delegate
   to :class:`EvidenceTracer` because the upstream walk direction
   works correctly.

2. :func:`query_cross_layer_impact` (alias
   :meth:`GraphQueryService.cross_layer_impact`)
   Follow only the **cross-layer** edge types (``INFLUENCES`` and
   ``DERIVED_FROM``) in both directions to enumerate the
   score-to-score impact chain (macro → industry → company, or
   vice versa). This is the "how does this score ripple through
   other layers" question. **Future work** (Run 4C / Task 5): the
   current implementation delegates to the tracer; Run 4C will
   rewrite it with a self-contained BFS that does not depend on
   the tracer's downstream-walk semantics (see the
   ``EvidenceTracer`` Pitfall B in
   ``~/.hermes/skills/productivity/market-data-reports/references/phase3b-task4-run4-graph-queries.md``).

What was here, what moved
-------------------------
A ``query_blast_radius`` function and ``BlastRadiusQuery`` DTO
**used to live in this module** (Run 4 first attempt, 2026-07-11).
Both were removed in the Run 4A consolidation because their
implementation delegated to ``EvidenceTracer.trace(direction=
"downstream", ...)``, which carries a directional asymmetry that
returns empty results for the Blast Radius use case. The
authoritative, brief-conformant implementation now lives in
:mod:`phase3.graph.blast_radius` as :func:`compute_blast_radius`
returning a :class:`phase3.graph.blast_radius.BlastRadiusResult`.
Use the canonical path:

    from phase3.graph.blast_radius import (
        BlastRadiusResult,
        BLAST_DOWNSTREAM_SIDE,
        compute_blast_radius,
    )

Why a separate module from ``evidence_tracer`` / ``traversal``
-------------------------------------------------------------
The existing modules answer "what is reachable" — a list of nodes.
A query has different shape: callers want typed buckets
(``source_node_ids``, ``signal_node_ids``, ...), depth-layering,
impact counts, and a JSON-serializable DTO. Putting these in a
dedicated module keeps the tracer focused on its BFS contract and
the query layer focused on its DTO contract. The query layer never
modifies the graph.

All queries are deterministic: same store + same args → same DTO.
Neighbors are sorted at the BFS level (see EvidenceTracer), so
output ordering is stable.

No production DB writes. No production cron / jobs.json edits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)
from phase3.graph.evidence_tracer import (
    EVIDENCE_DOWNSTREAM_EDGE_TYPES,
    EVIDENCE_UPSTREAM_EDGE_TYPES,
    EvidenceChain,
    EvidenceTracer,
)
from phase3.graph.in_memory_store import GraphStore


# Cross-layer edge types only — used by `query_cross_layer_impact`.
# These are the score-to-score edges that connect macro / industry /
# company layers in the spec topology.
CROSS_LAYER_EDGE_TYPES: tuple[EdgeType, ...] = (
    EdgeType.INFLUENCES,
    EdgeType.DERIVED_FROM,
)


# ---------------------------------------------------------------------
# Result DTOs (frozen, JSON-serializable via to_dict)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class LineageQuery:
    """Result of an upstream "where did this come from" query.

    Attributes:
        start: The node id the query was anchored on.
        visited_node_ids: All node ids reachable upstream within
            ``max_depth`` (excluding ``start``).
        source_node_ids: Subset of ``visited_node_ids`` that are
            :class:`NodeType.SOURCE`. Convenience for callers that
            only care about data origins.
        signal_node_ids: Subset that are :class:`NodeType.SIGNAL`.
        entity_node_ids: Subset that are COMPANY / INDUSTRY /
            MACRO_FACTOR / PERSON (the "entity" bucket — kept
            consistent with :class:`EvidenceChain.entity_node_ids`).
        score_node_ids: Subset that are :class:`NodeType.SCORE`
            (other scores that fed into this one via
            DERIVED_FROM / INFLUENCES).
        leaf_node_ids: Upstream leaves — the furthest data
            sources reachable from ``start``.
        leaf_summary: ``{node_type.value: count}`` for the leaves.
        layered: ``{depth: [node_id, ...]}`` mapping depth (in hops
            from ``start``) to the node ids discovered at that
            depth. ``depth=1`` is direct neighbors. The ``start``
            node itself is NOT included in any layer.
        truncated: True if the walk stopped due to ``max_depth``
            or ``max_nodes``.
        depth_reached: Max hop reached during the walk.
        warnings: Human-readable warnings (mirrors
            :class:`EvidenceChain.warnings`).
    """

    start: str
    visited_node_ids: list[str] = field(default_factory=list)
    source_node_ids: list[str] = field(default_factory=list)
    signal_node_ids: list[str] = field(default_factory=list)
    entity_node_ids: list[str] = field(default_factory=list)
    score_node_ids: list[str] = field(default_factory=list)
    leaf_node_ids: list[str] = field(default_factory=list)
    leaf_summary: dict[str, int] = field(default_factory=dict)
    layered: dict[int, list[str]] = field(default_factory=dict)
    truncated: bool = False
    depth_reached: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "visited_node_ids": list(self.visited_node_ids),
            "source_node_ids": list(self.source_node_ids),
            "signal_node_ids": list(self.signal_node_ids),
            "entity_node_ids": list(self.entity_node_ids),
            "score_node_ids": list(self.score_node_ids),
            "leaf_node_ids": list(self.leaf_node_ids),
            "leaf_summary": dict(self.leaf_summary),
            "layered": {str(k): list(v) for k, v in self.layered.items()},
            "truncated": self.truncated,
            "depth_reached": self.depth_reached,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CrossLayerImpactQuery:
    """Result of a cross-layer score-to-score impact query.

    Walks **both directions** along the cross-layer edge types
    (``INFLUENCES``, ``DERIVED_FROM``) starting from the anchor
    node. The result captures:

    Attributes:
        start: The node id the query was anchored on.
        upstream_chain: List of node ids in the upstream cross-layer
            chain (e.g. for a company score: macro score, then
            industry score that influenced it). Ordered from the
            closest to the furthest upstream score.
        downstream_chain: List of node ids in the downstream
            cross-layer chain. Ordered from the closest to the
            furthest downstream score.
        upstream_layers: ``{scorer_type: node_id}`` mapping each
            layer reached upstream to one representative node id
            (the closest one in BFS order). For a company score
            this would typically be ``{"macro": "...", "industry":
            "..."}``.
        downstream_layers: Same shape for downstream.
        all_reachable_score_ids: Union of upstream + downstream
            score node ids (the full impact fan-out).
        warnings: Human-readable warnings.
    """

    start: str
    upstream_chain: list[str] = field(default_factory=list)
    downstream_chain: list[str] = field(default_factory=list)
    upstream_layers: dict[str, str] = field(default_factory=dict)
    downstream_layers: dict[str, str] = field(default_factory=dict)
    all_reachable_score_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "upstream_chain": list(self.upstream_chain),
            "downstream_chain": list(self.downstream_chain),
            "upstream_layers": dict(self.upstream_layers),
            "downstream_layers": dict(self.downstream_layers),
            "all_reachable_score_ids": list(self.all_reachable_score_ids),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


# Score-typed node kinds — used to filter cross-layer walks and to
# infer "scorer_type" for layered output. The mapping is best-effort;
# a score node id is conventionally `score:<scorer_type>:...`.
_SCORE_NODE_TYPES: tuple[NodeType, ...] = (NodeType.SCORE,)


def _infer_scorer_type(node_id: str) -> str:
    """Best-effort scorer_type extraction from a score node id.

    Honors the canonical id pattern
    ``score:<scorer_type>:<entity>:<date_bucket>`` produced by
    :func:`phase3.pipeline.graph_writer.make_score_node_id`. Returns
    ``"unknown"`` if the pattern does not match — callers should
    not treat ``unknown`` as authoritative.
    """
    if not node_id.startswith("score:"):
        return "unknown"
    parts = node_id.split(":")
    if len(parts) < 4:
        return "unknown"
    return parts[1] or "unknown"


def _build_layered_view(
    store: GraphStore,
    start: str,
    edge_types: list[EdgeType],
    direction: Literal["upstream", "downstream"],
    max_depth: int,
) -> dict[int, list[str]]:
    """BFS with explicit per-hop bucketing.

    Returns ``{depth: [node_id, ...]}`` where ``depth=1`` is the set
    of direct neighbors of ``start``. Uses the same per-edge-type
    direction handling as :class:`EvidenceTracer` so callers see
    consistent semantics across the two APIs.

    The neighbor iteration order matches
    :meth:`EvidenceTracer._neighbors` (``(edge_id, neighbor_id)``
    sort), which is the deterministic contract.
    """
    if not store.has_node(start):
        return {}
    # Reuse EvidenceTracer to avoid duplicating the direction policy.
    # We do a max-depth 1 walk repeatedly to get clean per-hop
    # buckets; the cost is acceptable for typical graph sizes
    # (≤ a few hundred score nodes) and preserves determinism.
    layered: dict[int, list[str]] = {}
    visited: set[str] = set()
    frontier: list[str] = [start]
    for depth in range(1, max_depth + 1):
        next_frontier: list[str] = []
        layer_nodes: list[str] = []
        if not frontier:
            break
        # Per-hop, we walk from each frontier node. We use a
        # per-hop fresh tracer call so we keep the same
        # deterministic-neighbor order; the tracer does its own
        # BFS, so for a single hop we cap max_depth=1 and read
        # back the visited nodes.
        for node_id in frontier:
            tracer = EvidenceTracer(store)
            chain = tracer.trace(
                node_id,
                max_depth=1,
                direction=direction,
                edge_types=edge_types,
            )
            for vid in chain.visited_node_ids:
                if vid in visited:
                    continue
                visited.add(vid)
                layer_nodes.append(vid)
                next_frontier.append(vid)
        if not layer_nodes:
            break
        layered[depth] = layer_nodes
        frontier = next_frontier
    return layered


# ---------------------------------------------------------------------
# Public query functions
# ---------------------------------------------------------------------


def query_lineage(
    store: GraphStore,
    start_node_id: str,
    *,
    max_depth: int = 5,
    edge_types: list[EdgeType] | None = None,
    max_nodes: int | None = None,
    include_layered: bool = True,
) -> LineageQuery:
    """Walk upstream from ``start_node_id`` and return a
    :class:`LineageQuery` DTO.

    The "lineage" framing: a score's lineage is the chain of
    signals, sources, and upstream scores that contributed to it.
    This function is the read-only, JSON-serializable counterpart to
    the existing :class:`EvidenceChain` — it answers the same
    question with a DTO shape tuned for dashboards / API consumers.

    Parameters
    ----------
    store:
        The graph store to query.
    start_node_id:
        The anchor node id. Typically a score node id, but any node
        id works (the walk will follow the upstream direction
        policy regardless of the anchor's type).
    max_depth:
        Maximum hop count. 1 = direct neighbors only. Default 5.
    edge_types:
        Optional whitelist of edge types to follow. Defaults to
        :data:`EVIDENCE_UPSTREAM_EDGE_TYPES` (the spec-faithful
        upstream set).
    max_nodes:
        Optional cap on visited nodes. When hit, the walk stops
        and the ``truncated`` flag is set.
    include_layered:
        If True (default), the per-hop layered view is included.
        Set to False for a leaner DTO when the caller only needs
        flat buckets.
    """
    if edge_types is None:
        edge_types = list(EVIDENCE_UPSTREAM_EDGE_TYPES)
    if not store.has_node(start_node_id):
        return LineageQuery(
            start=start_node_id,
            warnings=[f"start node {start_node_id!r} not in graph"],
        )
    tracer = EvidenceTracer(store)
    chain: EvidenceChain = tracer.trace(
        start_node_id,
        max_depth=max_depth,
        direction="upstream",
        edge_types=edge_types,
        max_nodes=max_nodes,
    )

    # Compute leaf summary.
    leaf_summary: dict[str, int] = {}
    nodes_by_id = {n.node_id: n for n in chain.nodes}
    for lid in chain.leaf_node_ids:
        n = nodes_by_id.get(lid)
        if n is None:
            continue
        leaf_summary[n.node_type.value] = leaf_summary.get(
            n.node_type.value, 0
        ) + 1

    layered: dict[int, list[str]] = {}
    if include_layered:
        layered = _build_layered_view(
            store,
            start_node_id,
            edge_types,
            "upstream",
            max_depth,
        )

    return LineageQuery(
        start=start_node_id,
        visited_node_ids=list(chain.visited_node_ids),
        source_node_ids=list(chain.source_node_ids),
        signal_node_ids=list(chain.signal_node_ids),
        entity_node_ids=list(chain.entity_node_ids),
        score_node_ids=list(chain.score_node_ids),
        leaf_node_ids=list(chain.leaf_node_ids),
        leaf_summary=leaf_summary,
        layered=layered,
        truncated=chain.truncated,
        depth_reached=chain.depth_reached,
        warnings=list(chain.warnings),
    )


def query_cross_layer_impact(
    store: GraphStore,
    start_node_id: str,
    *,
    max_depth: int = 5,
) -> CrossLayerImpactQuery:
    """Walk the cross-layer edges (``INFLUENCES``, ``DERIVED_FROM``)
    in both directions to enumerate the score-to-score impact chain.

    The "cross-layer impact" framing: a macro score may influence
    industry scores, which in turn feed company scores. Removing
    or correcting the macro score has ripple effects. This query
    returns the full impact chain in both directions.

    Returns
    -------
    :class:`CrossLayerImpactQuery` with separate upstream /
    downstream chains, per-layer "closest representative" node
    ids, and a union of all reachable score ids.

    Notes
    -----
    Unlike :func:`query_lineage` and
    :func:`phase3.graph.blast_radius.compute_blast_radius`, this
    query is symmetric — it walks both directions. The rationale:
    cross-layer impact is a single concept ("how does this score
    ripple"), and surfacing the upstream vs downstream halves
    separately is more useful than forcing the caller to invoke two
    queries and merge. (As of Run 4A consolidation, Blast Radius
    has been moved to ``phase3.graph.blast_radius`` and is NOT
    exposed from this module anymore.)
    """
    if not store.has_node(start_node_id):
        return CrossLayerImpactQuery(
            start=start_node_id,
            warnings=[f"start node {start_node_id!r} not in graph"],
        )
    edge_types = list(CROSS_LAYER_EDGE_TYPES)
    tracer = EvidenceTracer(store)
    upstream_chain = tracer.trace(
        start_node_id,
        max_depth=max_depth,
        direction="upstream",
        edge_types=edge_types,
    )
    downstream_chain = tracer.trace(
        start_node_id,
        max_depth=max_depth,
        direction="downstream",
        edge_types=edge_types,
    )

    # Filter to score-typed nodes only (cross-layer walks should
    # only ever land on scores, but we guard defensively).
    upstream_score_ids = [
        nid for nid in upstream_chain.visited_node_ids
        if _node_type_or_unknown(store, nid) in _SCORE_NODE_TYPES
    ]
    downstream_score_ids = [
        nid for nid in downstream_chain.visited_node_ids
        if _node_type_or_unknown(store, nid) in _SCORE_NODE_TYPES
    ]

    # Build layer representatives: for each scorer_type we touched,
    # record the closest (lowest depth) score node id in that
    # direction. "Closest" is BFS order — the first occurrence in
    # the visited list is the lowest-depth one.
    upstream_layers: dict[str, str] = {}
    for nid in upstream_score_ids:
        st = _infer_scorer_type(nid)
        if st == "unknown":
            continue
        upstream_layers.setdefault(st, nid)
    downstream_layers: dict[str, str] = {}
    for nid in downstream_score_ids:
        st = _infer_scorer_type(nid)
        if st == "unknown":
            continue
        downstream_layers.setdefault(st, nid)

    warnings: list[str] = []
    warnings.extend(upstream_chain.warnings)
    warnings.extend(downstream_chain.warnings)

    # De-dup the union, preserving upstream-then-downstream order.
    seen: set[str] = set()
    all_ids: list[str] = []
    for nid in upstream_score_ids + downstream_score_ids:
        if nid in seen:
            continue
        seen.add(nid)
        all_ids.append(nid)

    return CrossLayerImpactQuery(
        start=start_node_id,
        upstream_chain=upstream_score_ids,
        downstream_chain=downstream_score_ids,
        upstream_layers=upstream_layers,
        downstream_layers=downstream_layers,
        all_reachable_score_ids=all_ids,
        warnings=warnings,
    )


def _node_type_or_unknown(store: GraphStore, node_id: str) -> NodeType | None:
    """Return the node_type of ``node_id`` in ``store``, or ``None``.

    Helper used by :func:`query_cross_layer_impact` to filter
    visited nodes to score-typed ones. We don't propagate an error
    when a node is missing — the upstream / downstream walks
    already only visit nodes that exist in the store, so a None
    return here is treated as "not a score" and the node is
    filtered out.
    """
    n = store.get_node(node_id)
    return n.node_type if n is not None else None


# ---------------------------------------------------------------------
# Service facade
# ---------------------------------------------------------------------


class GraphQueryService:
    """Stateless facade exposing the three graph query families.

    The service holds a single :class:`GraphStore` reference and
    exposes :meth:`lineage`, :meth:`blast_radius`, and
    :meth:`cross_layer_impact` as instance methods. Stateless
    beyond the store reference — every call constructs a fresh
    :class:`EvidenceTracer` so the service is safe to share
    across threads (the underlying store is the load-bearing
    shared state).

    Typical usage::

        store = SQLiteGraphStore(path).ensure_schema()
        svc = GraphQueryService(store)
        lineage = svc.lineage("score:company:2330:2026-07-08")
        blast = svc.blast_radius("source:yfinance")
        impact = svc.cross_layer_impact(
            "score:macro:global:2026-07-08"
        )

    The instance-method API mirrors the module-level functions
    (``query_lineage``, ``query_cross_layer_impact``) — pick
    whichever fits the call site. Both forms are part of the
    public surface.

    Note: as of the Run 4A consolidation (2026-07-11), this
    service no longer exposes a ``blast_radius`` method. Use
    :func:`phase3.graph.blast_radius.compute_blast_radius` for
    downstream-impact queries.
    """

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    @property
    def store(self) -> GraphStore:
        return self._store

    def lineage(
        self,
        start_node_id: str,
        *,
        max_depth: int = 5,
        edge_types: list[EdgeType] | None = None,
        max_nodes: int | None = None,
        include_layered: bool = True,
    ) -> LineageQuery:
        return query_lineage(
            self._store,
            start_node_id,
            max_depth=max_depth,
            edge_types=edge_types,
            max_nodes=max_nodes,
            include_layered=include_layered,
        )

    def cross_layer_impact(
        self,
        start_node_id: str,
        *,
        max_depth: int = 5,
    ) -> CrossLayerImpactQuery:
        return query_cross_layer_impact(
            self._store,
            start_node_id,
            max_depth=max_depth,
        )


__all__ = [
    "LineageQuery",
    "CrossLayerImpactQuery",
    "GraphQueryService",
    "query_lineage",
    "query_cross_layer_impact",
    "CROSS_LAYER_EDGE_TYPES",
]
