"""Cross-layer Impact query — Phase 3B Task 4 Run 4B.

Score-to-score impact traversal: given a starting SCORE node, return
the set of cross-layer score nodes it can reach in both directions
(upstream and downstream) along the strict cross-layer edge types
(:data:`CROSS_LAYER_UPSTREAM_SIDE` / :data:`CROSS_LAYER_DOWNSTREAM_SIDE`).

The "cross-layer impact" semantic
---------------------------------
A macro score may influence industry scores, which in turn feed
company scores. Removing or correcting the macro score has ripple
effects. Conversely, if a company score is corrected, the upstream
macro / industry scores that influenced it are part of the
"evidence trail" that needs review. This query answers BOTH
questions in one call: ``upstream_chain`` (the scores that fed
into ``start``) and ``downstream_chain`` (the scores that depend
on ``start``).

Strict cross-layer edge set
---------------------------
Per the Run 4B brief, the cross-layer impact query follows ONLY
the cross-layer score-to-score edges:

- ``INFLUENCES`` (score -> score): the influenced score is the
  downstream consumer.
- ``DERIVED_FROM`` (ancestor -> derived): the derived score is
  the downstream consumer.

CITES, REFERS_TO, GENERATED, CONTRIBUTES_TO, INCLUDES and all
classification / authorship edges are NOT cross-layer impact
edges and are NOT followed. (Blast Radius covers a superset
of edges for the "what breaks if I remove this" question; Lineage
covers the "where did this come from" question. Cross-layer
impact is the focused "score ripples through the model" question.)

Why a self-contained implementation
----------------------------------
This module is intentionally a small, query-specific implementation.
It does NOT reuse :class:`EvidenceTracer` — Run 4 / 4A discovery
("Pitfall O" in the Run 4A reference) found that
``EvidenceTracer._neighbors`` carries a directional asymmetry that
makes downstream walks from the *from-side* of common edge types
(CONTRIBUTES_TO, INFLUENCES, INCLUDES, REFERS_TO, REPORT_BY,
WORKS_AT) return empty. INFLUENCES is one of the explicitly
asymmetric edge types (``DOWNSTREAM_SIDE[INFLUENCES] == "to"``,
so a downstream walk starting at the *from* side of an INFLUENCES
edge returns no neighbors). For the cross-layer impact use case
this is fatal: a downstream walk from a macro score (which is the
*from-side* of an INFLUENCES edge) MUST walk to the industry score
on the to-side.

The policy here is derived from the **authoritative spec** for
Cross-layer Impact (see Task 4 Run 4B brief) and not from a mirror
of ``UPSTREAM_SIDE`` / ``DOWNSTREAM_SIDE`` from the tracer. The
two modules can disagree on edges that are relevant to evidence
tracing but irrelevant to cross-layer impact (and vice versa);
each module owns its own policy.

Direction policy
----------------
For each edge type the cross-layer policy declares which side of
the edge is the **upstream data origin** (the side that "produced"
the data) and which side is the **downstream consumer** (the
side that "depends on" the other). The BFS walks: at the current
node X, for each edge of an allowed type, if X is on the
upstream side, move to the downstream side (downstream walk);
if X is on the downstream side, move to the upstream side
(upstream walk). Both walks are performed independently and the
results are reported as separate ``upstream_chain`` /
``downstream_chain`` lists in :class:`CrossLayerImpactQuery`.

The policy tables are :data:`CROSS_LAYER_UPSTREAM_SIDE` (used for
upstream walks) and :data:`CROSS_LAYER_DOWNSTREAM_SIDE` (used
for downstream walks). They are mirrors of each other: for each
edge type, the upstream side and the downstream side are opposite
ends of the edge.

Determinism + cycle protection
------------------------------
Neighbors are visited in stable ``(edge_id, neighbor_id)`` order
at every hop, so two graphs with identical structure and identical
arguments produce identical results. A ``visited`` set ensures
cycles do not produce infinite walks; nodes are recorded in
discovery (BFS) order.

Result shape
------------
:class:`CrossLayerImpactQuery` is a frozen dataclass with:

- ``start``: the anchor node id.
- ``upstream_chain``: list of score node ids in BFS order from
  closest to furthest upstream of ``start``.
- ``downstream_chain``: list of score node ids in BFS order from
  closest to furthest downstream of ``start``.
- ``upstream_layers``: ``{scorer_type: node_id}`` mapping each
  layer reached upstream to the closest representative score.
- ``downstream_layers``: same shape for downstream.
- ``all_reachable_score_ids``: union of upstream + downstream,
  in upstream-first order, with duplicates removed.
- ``layered_upstream`` / ``layered_downstream``:
  ``{depth: [node_id, ...]}`` per-hop bucket (depth=1 is direct
  neighbors).
- ``truncated_upstream`` / ``truncated_downstream`` /
  ``depth_reached_upstream`` / ``depth_reached_downstream``:
  per-direction caps and depth bookkeeping.
- ``warnings``: human-readable warnings.

No production DB writes. No cron / jobs.json edits. No commits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from phase3.datamodel.graph import EdgeType, GraphEdge, GraphNode, NodeType
from phase3.graph.in_memory_store import GraphStore


class _GraphStoreLike(Protocol):
    """Structural protocol for the graph store surface this module
    depends on. Both :class:`phase3.graph.in_memory_store.GraphStore`
    and :class:`phase3.graph.sqlite_store.SQLiteGraphStore` satisfy
    this protocol (duck-typed). Using a Protocol keeps the public
    function callable with either store without coupling this
    module to a concrete class hierarchy.
    """

    def has_node(self, node_id: str) -> bool: ...
    def get_node(self, node_id: str) -> GraphNode | None: ...
    def edges_from(self, node_id: str) -> list[GraphEdge]: ...
    def edges_to(self, node_id: str) -> list[GraphEdge]: ...


# Re-exported alias used as the public type hint. Two classes satisfy
# it today; any future store can satisfy it too without changing
# this module.
GraphStoreLike = _GraphStoreLike


# Per-edge-type upstream / downstream side policy.
#
# For each cross-layer edge type, the value names the side of the
# edge that is the UPSTREAM DATA ORIGIN (the side that "produced"
# the data). The downstream side is the opposite end of the edge.
#
# For UPSTREAM walks: at node X, for each edge E of an allowed type,
# move from X toward the upstream side of E. If X is already on
# the upstream side, do not continue along E.
#
# For DOWNSTREAM walks: at node X, for each edge E of an allowed
# type, move from X toward the downstream side of E. If X is
# already on the downstream side, do not continue along E.
#
# This is the AUTHORITATIVE Cross-layer Impact policy per the Run
# 4B brief. It is intentionally narrower than the evidence-tracer
# ``UPSTREAM_SIDE`` (which covers GENERATED, CONTRIBUTES_TO, CITES,
# etc.) — those are NOT cross-layer impact edges and are NOT
# followed by this module.
CROSS_LAYER_UPSTREAM_SIDE: dict[EdgeType, Literal["from", "to"]] = {
    # INFLUENCES (upstream -> downstream): the influencer is the
    # upstream origin. From an influenced score, upstream = the
    # influencer. From an influencer, no upstream continuation.
    EdgeType.INFLUENCES: "from",
    # DERIVED_FROM (ancestor -> derived): the ancestor is the
    # upstream origin. From a derived score, upstream = the
    # ancestor. From an ancestor, no upstream continuation.
    EdgeType.DERIVED_FROM: "from",
}

CROSS_LAYER_DOWNSTREAM_SIDE: dict[EdgeType, Literal["from", "to"]] = {
    # INFLUENCES: the influenced is the downstream consumer.
    # From an influencer, downstream = the influenced.
    EdgeType.INFLUENCES: "to",
    # DERIVED_FROM: the derived is the downstream consumer.
    # From an ancestor, downstream = the derived.
    EdgeType.DERIVED_FROM: "to",
}


# Cross-layer edge types — the set of edge types this query follows.
# Equal to the keys of CROSS_LAYER_UPSTREAM_SIDE.
CROSS_LAYER_EDGE_TYPES: tuple[EdgeType, ...] = (
    EdgeType.INFLUENCES,
    EdgeType.DERIVED_FROM,
)


def _default_edge_types() -> list[EdgeType]:
    return list(CROSS_LAYER_EDGE_TYPES)


@dataclass(frozen=True)
class CrossLayerImpactResult:
    """Result of a Cross-layer Impact query.

    Attributes:
        start: The anchor node id.
        upstream_chain: Score node ids in BFS order from the
            closest upstream score to the furthest upstream score.
            Order is closest-first (lowest depth first).
        downstream_chain: Score node ids in BFS order from the
            closest downstream score to the furthest downstream
            score.
        upstream_layers: ``{scorer_type: node_id}`` mapping each
            layer reached upstream to the closest representative
            score (the first occurrence in BFS order). For a
            company score this would typically be
            ``{"macro": "...", "industry": "..."}``.
        downstream_layers: Same shape for downstream.
        all_reachable_score_ids: Union of upstream_chain +
            downstream_chain in upstream-first order, deduplicated.
        layered_upstream: ``{depth: [node_id, ...]}`` per-hop
            bucket for the upstream walk. ``depth=1`` is direct
            upstream neighbors.
        layered_downstream: Same shape for downstream.
        visited_edges_upstream: ``[(edge_id, from_node_id,
            to_node_id), ...]`` in upstream BFS discovery order.
        visited_edges_downstream: Same for downstream.
        truncated_upstream: True if the upstream walk stopped due
            to ``max_depth`` or ``max_nodes``.
        truncated_downstream: Same for downstream.
        depth_reached_upstream: Max hop reached in the upstream
            walk (0 for an empty walk).
        depth_reached_downstream: Same for downstream.
        warnings: Human-readable warnings.
    """

    start: str
    upstream_chain: list[str] = field(default_factory=list)
    downstream_chain: list[str] = field(default_factory=list)
    upstream_layers: dict[str, str] = field(default_factory=dict)
    downstream_layers: dict[str, str] = field(default_factory=dict)
    all_reachable_score_ids: list[str] = field(default_factory=list)
    layered_upstream: dict[int, list[str]] = field(default_factory=dict)
    layered_downstream: dict[int, list[str]] = field(default_factory=dict)
    visited_edges_upstream: list[tuple[str, str, str]] = field(
        default_factory=list
    )
    visited_edges_downstream: list[tuple[str, str, str]] = field(
        default_factory=list
    )
    truncated_upstream: bool = False
    truncated_downstream: bool = False
    depth_reached_upstream: int = 0
    depth_reached_downstream: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "upstream_chain": list(self.upstream_chain),
            "downstream_chain": list(self.downstream_chain),
            "upstream_layers": dict(self.upstream_layers),
            "downstream_layers": dict(self.downstream_layers),
            "all_reachable_score_ids": list(self.all_reachable_score_ids),
            "layered_upstream": {
                str(k): list(v) for k, v in self.layered_upstream.items()
            },
            "layered_downstream": {
                str(k): list(v) for k, v in self.layered_downstream.items()
            },
            "visited_edges_upstream": [
                list(t) for t in self.visited_edges_upstream
            ],
            "visited_edges_downstream": [
                list(t) for t in self.visited_edges_downstream
            ],
            "truncated_upstream": self.truncated_upstream,
            "truncated_downstream": self.truncated_downstream,
            "depth_reached_upstream": self.depth_reached_upstream,
            "depth_reached_downstream": self.depth_reached_downstream,
            "warnings": list(self.warnings),
        }


# Score-typed node kinds — used to filter cross-layer walks.
# Cross-layer edges are spec'd as Score -> Score, but we guard
# defensively against accidental non-score neighbors.
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


def _neighbors_at(
    store: _GraphStoreLike,
    cur_id: str,
    edge_types_set: set[EdgeType],
    target_side_table: dict[EdgeType, Literal["from", "to"]],
) -> list[tuple[GraphNode, GraphEdge]]:
    """Return the (neighbor, edge) pairs the cross-layer walk would
    move to from ``cur_id``.

    For each edge of an allowed type, check whether ``cur_id`` is on
    the side of the edge that is OPPOSITE the target side declared
    in ``target_side_table``. If yes, the neighbor is the
    target-side node; if no (cur_id is already on the target
    side), the edge is not followed.

    Neighbors are returned in stable ``(edge_id, neighbor_id)`` order
    so two equal graphs always produce the same result.
    """
    candidates: list[tuple[GraphNode, GraphEdge]] = []

    # cur_id is the from-side of these edges.
    for edge in store.edges_from(cur_id):
        if edge.edge_type not in edge_types_set:
            continue
        target = target_side_table.get(edge.edge_type)
        if target is None:
            continue
        # cur is the from; move to to iff target side is to.
        if target != "to":
            continue
        other = store.get_node(edge.to_node_id)
        if other is not None:
            candidates.append((other, edge))

    # cur_id is the to-side of these edges.
    for edge in store.edges_to(cur_id):
        if edge.edge_type not in edge_types_set:
            continue
        target = target_side_table.get(edge.edge_type)
        if target is None:
            continue
        # cur is the to; move to from iff target side is from.
        if target != "from":
            continue
        other = store.get_node(edge.from_node_id)
        if other is not None:
            candidates.append((other, edge))

    # Stable deterministic order.
    candidates.sort(key=lambda ne: (ne[1].edge_id, ne[0].node_id))
    return candidates


def _bfs_one_direction(
    store: _GraphStoreLike,
    start_node_id: str,
    target_side_table: dict[EdgeType, Literal["from", "to"]],
    edge_types_set: set[EdgeType],
    *,
    max_depth: int,
    max_nodes: int | None,
) -> tuple[
    list[GraphNode],
    list[tuple[str, str, str]],
    dict[int, list[str]],
    bool,
    int,
    list[str],
]:
    """Run a single-direction BFS following ``target_side_table``.

    Returns ``(visited_nodes, visited_edges, layered, truncated,
    depth_reached, warnings)``. ``visited_nodes`` is in BFS
    discovery order, excluding ``start_node_id``.
    """
    warnings: list[str] = []
    visited_ids: set[str] = {start_node_id}
    visited_nodes: list[GraphNode] = []
    visited_edges: list[tuple[str, str, str]] = []
    layered: dict[int, list[str]] = {}
    frontier: list[str] = [start_node_id]
    depth_reached: int = 0
    truncated: bool = False

    for depth in range(1, max_depth + 1):
        next_frontier: list[str] = []
        layer_nodes: list[str] = []
        for cur_id in frontier:
            for neighbor, edge in _neighbors_at(
                store, cur_id, edge_types_set, target_side_table
            ):
                if neighbor.node_id in visited_ids:
                    continue
                if (
                    max_nodes is not None
                    and len(visited_nodes) >= max_nodes
                ):
                    truncated = True
                    break
                visited_ids.add(neighbor.node_id)
                visited_nodes.append(neighbor)
                visited_edges.append(
                    (edge.edge_id, edge.from_node_id, edge.to_node_id)
                )
                layer_nodes.append(neighbor.node_id)
                next_frontier.append(neighbor.node_id)
            if truncated:
                break
        if not layer_nodes:
            break
        layered[depth] = layer_nodes
        depth_reached = depth
        frontier = next_frontier
        if truncated:
            break

    return (
        visited_nodes,
        visited_edges,
        layered,
        truncated,
        depth_reached,
        warnings,
    )


def _filter_to_scores(
    visited_nodes: list[GraphNode],
) -> list[str]:
    """Filter visited nodes to score-typed only and return their ids.

    Cross-layer walks should only ever land on score nodes per
    spec, but we guard defensively in case the graph contains
    mixed-type cross-layer edges.
    """
    return [
        n.node_id
        for n in visited_nodes
        if n.node_type in _SCORE_NODE_TYPES
    ]


def _build_layer_representatives(
    score_ids: list[str],
) -> dict[str, str]:
    """Build ``{scorer_type: closest_score_node_id}``.

    "Closest" is BFS order — the first occurrence in the visited
    list is the lowest-depth one for that layer. Best-effort:
    scores whose id pattern does not start with ``score:`` are
    skipped (scorer_type resolves to ``"unknown"``).
    """
    out: dict[str, str] = {}
    for nid in score_ids:
        st = _infer_scorer_type(nid)
        if st == "unknown":
            continue
        out.setdefault(st, nid)
    return out


def compute_cross_layer_impact(
    store: _GraphStoreLike,
    start_node_id: str,
    *,
    max_depth: int = 5,
    edge_types: list[EdgeType] | None = None,
    max_nodes: int | None = None,
) -> CrossLayerImpactResult:
    """Compute the Cross-layer Impact of ``start_node_id``.

    Walks the graph in BOTH directions along the strict cross-layer
    edge types (:data:`CROSS_LAYER_UPSTREAM_SIDE` /
    :data:`CROSS_LAYER_DOWNSTREAM_SIDE`) and returns a
    :class:`CrossLayerImpactResult` with separate
    ``upstream_chain`` / ``downstream_chain`` lists, per-layer
    closest-representative node ids, and a union of all reachable
    score ids.

    Parameters
    ----------
    store:
        The graph store to query.
    start_node_id:
        The anchor node id. Should be a SCORE node; the query
        returns empty results if the start is not in the store.
    max_depth:
        Maximum hop count in EACH direction. ``1`` = direct
        neighbors only. Default 5.
    edge_types:
        Optional whitelist of edge types to follow. Defaults to
        :data:`CROSS_LAYER_EDGE_TYPES` (the brief-faithful strict
        set: INFLUENCES, DERIVED_FROM). The walk applies the
        same per-edge-type direction semantics to each edge in
        the whitelist; non-cross-layer edges (CITES, GENERATED,
        etc.) are NOT in the default and NOT followed.
    max_nodes:
        Optional cap on the number of nodes the walk may visit in
        EACH direction (excluding ``start``). When hit, the walk
        stops and the corresponding ``truncated_*`` flag is set.

    Returns
    -------
    :class:`CrossLayerImpactResult`. When ``start_node_id`` is
    not in the store, the result has empty chains / layers,
    ``truncated=False`` for both directions, ``depth_reached=0``
    for both, and a single warning.
    """
    warnings: list[str] = []

    if not store.has_node(start_node_id):
        warnings.append(f"start node {start_node_id!r} not in graph")
        return CrossLayerImpactResult(
            start=start_node_id,
            warnings=warnings,
        )

    if edge_types is None:
        edge_types = _default_edge_types()
    edge_types_set: set[EdgeType] = set(edge_types)

    # Validate that the whitelist is a subset of the supported
    # cross-layer edge types. We accept a strict subset silently
    # (the policy table just won't move); an edge type outside
    # the supported set is rejected with a clear warning because
    # silently no-op'ing is a footgun (callers expect us to follow
    # the edges they specified).
    unsupported = edge_types_set - set(CROSS_LAYER_EDGE_TYPES)
    if unsupported:
        warnings.append(
            f"unsupported edge types ignored: "
            f"{sorted(et.value for et in unsupported)}; "
            f"cross-layer impact only follows "
            f"{[et.value for et in CROSS_LAYER_EDGE_TYPES]}"
        )
        edge_types_set = edge_types_set & set(CROSS_LAYER_EDGE_TYPES)
        if not edge_types_set:
            warnings.append(
                "no supported cross-layer edge types remain after "
                "filter; both chains are empty"
            )
            return CrossLayerImpactResult(
                start=start_node_id,
                warnings=warnings,
            )

    # Upstream walk: target side = upstream side.
    up_nodes, up_edges, up_layered, up_trunc, up_depth, up_warn = (
        _bfs_one_direction(
            store,
            start_node_id,
            CROSS_LAYER_UPSTREAM_SIDE,
            edge_types_set,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    )
    warnings.extend(up_warn)

    # Downstream walk: target side = downstream side.
    down_nodes, down_edges, down_layered, down_trunc, down_depth, down_warn = (
        _bfs_one_direction(
            store,
            start_node_id,
            CROSS_LAYER_DOWNSTREAM_SIDE,
            edge_types_set,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    )
    warnings.extend(down_warn)

    # Filter to score-typed nodes only (defensive).
    upstream_score_ids = _filter_to_scores(up_nodes)
    downstream_score_ids = _filter_to_scores(down_nodes)

    upstream_layers = _build_layer_representatives(upstream_score_ids)
    downstream_layers = _build_layer_representatives(downstream_score_ids)

    # Union, dedup, preserve upstream-first order.
    seen: set[str] = set()
    all_ids: list[str] = []
    for nid in upstream_score_ids + downstream_score_ids:
        if nid in seen:
            continue
        seen.add(nid)
        all_ids.append(nid)

    return CrossLayerImpactResult(
        start=start_node_id,
        upstream_chain=upstream_score_ids,
        downstream_chain=downstream_score_ids,
        upstream_layers=upstream_layers,
        downstream_layers=downstream_layers,
        all_reachable_score_ids=all_ids,
        layered_upstream=up_layered,
        layered_downstream=down_layered,
        visited_edges_upstream=up_edges,
        visited_edges_downstream=down_edges,
        truncated_upstream=up_trunc,
        truncated_downstream=down_trunc,
        depth_reached_upstream=up_depth,
        depth_reached_downstream=down_depth,
        warnings=warnings,
    )


__all__ = [
    "CROSS_LAYER_EDGE_TYPES",
    "CROSS_LAYER_UPSTREAM_SIDE",
    "CROSS_LAYER_DOWNSTREAM_SIDE",
    "CrossLayerImpactResult",
    "GraphStoreLike",
    "compute_cross_layer_impact",
]
