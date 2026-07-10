"""Lineage query — Phase 3B Task 4 Run 4B.

Upstream provenance traversal: given a starting node (typically a
score), return the set of upstream nodes (signals, sources, other
scores) that contributed to it via the lineage edge types.

The "lineage" semantic
----------------------
A score's lineage is the chain of signals, sources, and upstream
scores that contributed to it. Lineage is *upstream* only — it
answers "where did this come from?". This is the read-only,
JSON-serializable counterpart to
:class:`phase3.graph.evidence_tracer.EvidenceChain`.

Lineage is upstream-only
------------------------
Per the Run 4B brief: lineage is upstream provenance. We do not
walk downstream — that is the job of
:func:`phase3.graph.blast_radius.compute_blast_radius` (data
dependency) and
:func:`phase3.graph.cross_layer_impact.compute_cross_layer_impact`
(score-to-score impact). Putting the three queries in three
modules with three explicit policies is the right shape.

Lineage is more inclusive than cross-layer impact
------------------------------------------------
Lineage follows the **evidence-tracer's full upstream set** —
not just cross-layer edges — because the question "where did
this come from" is broader than "what score fed into this
score". The lineage edge types are:

- ``CONTRIBUTES_TO`` (signal -> score): the signal contributed
  to the score. The signal is the upstream data.
- ``GENERATED`` (signal -> source): the source produced the
  signal. The source is the upstream data.
- ``CITES`` (score -> source): the score cites the source. The
  source is the upstream data.
- ``REFERS_TO`` (news -> entity): the news refers to the entity.
  The news is the upstream document.
- ``INFLUENCES`` (score -> score): the influencer is the upstream
  score.
- ``DERIVED_FROM`` (ancestor -> derived): the ancestor is the
  upstream score.
- ``EXPOSED_TO`` (company -> macro): the macro is the upstream
  factor.

These are exactly the edges the evidence tracer treats as
upstream evidence (see :data:`EVIDENCE_UPSTREAM_EDGE_TYPES` in
:mod:`phase3.graph.evidence_tracer`). Lineage and evidence
tracing share the same upstream semantic; lineage just adds a
DTO shape tuned for dashboards / API consumers and a
self-contained BFS that does not depend on the tracer's
downstream-walk semantics (which is fine because lineage is
upstream-only — see Pitfall B / O).

Why a self-contained implementation
----------------------------------
This module is intentionally a small, query-specific implementation.
It does NOT reuse :class:`EvidenceTracer` for the BFS, even though
lineage is upstream-only and the tracer's upstream path is
correct. The reasons:

1. **Module ownership**: lineage has its own DTO contract
   (:class:`LineageQuery`) and its own typed buckets. Putting
   the BFS in a dedicated module keeps the tracer focused on
   its ``EvidenceChain`` contract and the query layer focused
   on its DTO contract.
2. **Defensive independence**: the upstream direction
   (``UPSTREAM_SIDE`` in :mod:`phase3.graph.evidence_tracer`)
   is correct for the current spec topology, but the tracer
   carries a downstream-walk asymmetry (Pitfall B) that has
   bitten every query that delegates to it with
   ``direction="downstream"``. Self-contained lineage code is
   immune to that bug.
3. **Determinism auditability**: the BFS lives in this module,
   so the policy is auditable against the brief without reading
   the tracer module.

Direction policy
----------------
For each edge type, the policy declares which side of the edge
is the **upstream data origin**. The BFS walks: at the current
node X, for each edge of an allowed type, move from X toward
the upstream side of E. If X is already on the upstream side,
do not continue along E.

The policy table is :data:`LINEAGE_UPSTREAM_SIDE`. It mirrors
the evidence-tracer ``UPSTREAM_SIDE`` for the supported edge
types, with the same semantic — the value names the side of
the edge that "produced" the data.

Determinism + cycle protection
------------------------------
Neighbors are visited in stable ``(edge_id, neighbor_id)`` order
at every hop, so two graphs with identical structure and identical
arguments produce identical results. A ``visited`` set ensures
cycles do not produce infinite walks; nodes are recorded in
discovery (BFS) order.

Result shape
------------
:class:`LineageQuery` is a frozen dataclass with:

- ``start``: the anchor node id.
- ``visited_node_ids``: BFS discovery order, excluding ``start``.
- ``source_node_ids`` / ``signal_node_ids`` /
  ``entity_node_ids`` / ``score_node_ids``: typed buckets.
- ``leaf_node_ids``: upstream leaves — the furthest data
  sources reachable from ``start``.
- ``leaf_summary``: ``{node_type.value: count}`` for the leaves.
- ``layered``: ``{depth: [node_id, ...]}`` per-hop bucket
  (``depth=1`` is direct neighbors).
- ``visited_edges``: ``[(edge_id, from_node_id, to_node_id), ...]``
  in BFS discovery order.
- ``truncated`` / ``depth_reached`` / ``warnings``.

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


# Per-edge-type upstream direction policy for the Lineage query.
#
# For each edge type, the value names the side of the edge that is
# the UPSTREAM DATA ORIGIN (the side that "produced" the data).
# The BFS walks: at node X, for each edge of an allowed type, move
# from X toward the upstream side. If X is already on the upstream
# side, do not continue along E.
#
# This is the AUTHORITATIVE Lineage policy per the Run 4B brief.
# It mirrors the evidence-tracer ``UPSTREAM_SIDE`` for the
# supported edge types — lineage and evidence tracing share the
# same upstream semantic.
LINEAGE_UPSTREAM_SIDE: dict[EdgeType, Literal["from", "to"]] = {
    EdgeType.CONTRIBUTES_TO: "from",   # signal is the upstream data
    EdgeType.GENERATED:      "to",     # source is the upstream data
    EdgeType.CITES:          "to",     # source is the upstream data
    EdgeType.REFERS_TO:      "from",   # news is the upstream document
    EdgeType.INFLUENCES:     "from",   # influencer is the upstream score
    EdgeType.DERIVED_FROM:   "to",     # ancestor is the upstream score
    EdgeType.EXPOSED_TO:     "to",     # macro is the upstream factor
}


# Lineage edge types — the set of edge types this query follows.
# Equal to the keys of LINEAGE_UPSTREAM_SIDE.
LINEAGE_EDGE_TYPES: tuple[EdgeType, ...] = (
    EdgeType.CONTRIBUTES_TO,
    EdgeType.GENERATED,
    EdgeType.CITES,
    EdgeType.REFERS_TO,
    EdgeType.INFLUENCES,
    EdgeType.DERIVED_FROM,
    EdgeType.EXPOSED_TO,
)


def _default_edge_types() -> list[EdgeType]:
    return list(LINEAGE_EDGE_TYPES)


@dataclass(frozen=True)
class LineageQuery:
    """Result of an upstream "where did this come from" query.

    Attributes:
        start: The node id the query was anchored on.
        visited_node_ids: All node ids reachable upstream within
            ``max_depth`` (excluding ``start``).
        source_node_ids: Subset of ``visited_node_ids`` that are
            :class:`NodeType.SOURCE`. Convenience for callers
            that only care about data origins.
        signal_node_ids: Subset that are :class:`NodeType.SIGNAL`.
        entity_node_ids: Subset that are COMPANY / INDUSTRY /
            MACRO_FACTOR / PERSON (the "entity" bucket — kept
            consistent with
            :class:`EvidenceChain.entity_node_ids`).
        score_node_ids: Subset that are :class:`NodeType.SCORE`
            (other scores that fed into this one via
            DERIVED_FROM / INFLUENCES).
        leaf_node_ids: Upstream leaves — the furthest data
            sources reachable from ``start``.
        leaf_summary: ``{node_type.value: count}`` for the leaves.
        layered: ``{depth: [node_id, ...]}`` mapping depth (in
            hops from ``start``) to the node ids discovered at
            that depth. ``depth=1`` is direct neighbors. The
            ``start`` node itself is NOT included in any layer.
        visited_edges: ``[(edge_id, from_node_id, to_node_id), ...]``
            in BFS discovery order, so callers can see the actual
            chain that was followed.
        truncated: True if the walk stopped due to ``max_depth``
            or ``max_nodes``.
        depth_reached: Max hop reached during the walk.
        warnings: Human-readable warnings.
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
    visited_edges: list[tuple[str, str, str]] = field(
        default_factory=list
    )
    truncated: bool = False
    depth_reached: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
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
            "visited_edges": [list(t) for t in self.visited_edges],
            "truncated": self.truncated,
            "depth_reached": self.depth_reached,
            "warnings": list(self.warnings),
        }


def _neighbors_at(
    store: _GraphStoreLike,
    cur_id: str,
    edge_types_set: set[EdgeType],
    target_side_table: dict[EdgeType, Literal["from", "to"]],
) -> list[tuple[GraphNode, GraphEdge]]:
    """Return the (neighbor, edge) pairs the lineage walk would move
    to from ``cur_id``.

    For each edge of an allowed type, check whether ``cur_id`` is on
    the side of the edge that is OPPOSITE the target (upstream)
    side. If yes, the neighbor is the upstream-side node; if no
    (cur_id is already on the upstream side), the edge is not
    followed.

    Neighbors are returned in stable ``(edge_id, neighbor_id)``
    order so two equal graphs always produce the same result.
    """
    candidates: list[tuple[GraphNode, GraphEdge]] = []

    # cur_id is the from-side of these edges.
    for edge in store.edges_from(cur_id):
        if edge.edge_type not in edge_types_set:
            continue
        target = target_side_table.get(edge.edge_type)
        if target is None:
            continue
        # cur is the from; move to to iff upstream side is to.
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
        # cur is the to; move to from iff upstream side is from.
        if target != "from":
            continue
        other = store.get_node(edge.from_node_id)
        if other is not None:
            candidates.append((other, edge))

    # Stable deterministic order.
    candidates.sort(key=lambda ne: (ne[1].edge_id, ne[0].node_id))
    return candidates


def _compute_leaves(
    store: _GraphStoreLike,
    visited: list[GraphNode],
    edge_types_set: set[EdgeType],
    target_side_table: dict[EdgeType, Literal["from", "to"]],
) -> list[str]:
    """Compute upstream leaves: visited nodes with no further
    upstream walks in the allowed direction.

    A node is an upstream leaf if, for every edge of an allowed
    type touching it, the node is on the upstream side already
    (i.e. further walks would go downstream, which we don't do).
    """
    leaves: list[str] = []
    for n in visited:
        has_walk = False
        # Outgoing edges: would walk iff upstream side is to.
        for edge in store.edges_from(n.node_id):
            if edge.edge_type in edge_types_set and \
                    target_side_table.get(edge.edge_type) == "to":
                has_walk = True
                break
        if not has_walk:
            # Incoming edges: would walk iff upstream side is from.
            for edge in store.edges_to(n.node_id):
                if edge.edge_type in edge_types_set and \
                        target_side_table.get(edge.edge_type) == "from":
                    has_walk = True
                    break
        if not has_walk:
            leaves.append(n.node_id)
    return leaves


def _bucket_visited(
    visited: list[GraphNode],
) -> dict[str, list[str]]:
    """Split visited nodes into typed buckets."""
    out: dict[str, list[str]] = {
        "source": [],
        "signal": [],
        "score": [],
        "entity": [],
    }
    for n in visited:
        if n.node_type == NodeType.SOURCE:
            out["source"].append(n.node_id)
        elif n.node_type == NodeType.SIGNAL:
            out["signal"].append(n.node_id)
        elif n.node_type == NodeType.SCORE:
            out["score"].append(n.node_id)
        elif n.node_type in (
            NodeType.COMPANY,
            NodeType.INDUSTRY,
            NodeType.MACRO_FACTOR,
            NodeType.PERSON,
        ):
            out["entity"].append(n.node_id)
    return out


def compute_lineage(
    store: _GraphStoreLike,
    start_node_id: str,
    *,
    max_depth: int = 5,
    edge_types: list[EdgeType] | None = None,
    max_nodes: int | None = None,
) -> LineageQuery:
    """Walk upstream from ``start_node_id`` and return a
    :class:`LineageQuery` DTO.

    The "lineage" framing: a score's lineage is the chain of
    signals, sources, and upstream scores that contributed to it.
    This function is the read-only, JSON-serializable counterpart
    to the existing :class:`EvidenceChain` — it answers the same
    question with a DTO shape tuned for dashboards / API consumers.

    Parameters
    ----------
    store:
        The graph store to query.
    start_node_id:
        The anchor node id. Typically a score node id, but any
        node id works (the walk will follow the upstream
        direction policy regardless of the anchor's type).
    max_depth:
        Maximum hop count. 1 = direct neighbors only. Default 5.
    edge_types:
        Optional whitelist of edge types to follow. Defaults to
        :data:`LINEAGE_EDGE_TYPES` (the full lineage set).
    max_nodes:
        Optional cap on visited nodes. When hit, the walk stops
        and the ``truncated`` flag is set.
    """
    warnings: list[str] = []

    if not store.has_node(start_node_id):
        warnings.append(f"start node {start_node_id!r} not in graph")
        return LineageQuery(
            start=start_node_id,
            warnings=warnings,
        )

    if edge_types is None:
        edge_types = _default_edge_types()
    edge_types_set: set[EdgeType] = set(edge_types)

    # Validate that the whitelist is a subset of the supported
    # lineage edge types. We accept a strict subset silently
    # (the policy table just won't move); an edge type outside
    # the supported set is rejected with a clear warning because
    # silently no-op'ing is a footgun.
    unsupported = edge_types_set - set(LINEAGE_EDGE_TYPES)
    if unsupported:
        warnings.append(
            f"unsupported edge types ignored: "
            f"{sorted(et.value for et in unsupported)}; "
            f"lineage only follows "
            f"{[et.value for et in LINEAGE_EDGE_TYPES]}"
        )
        edge_types_set = edge_types_set & set(LINEAGE_EDGE_TYPES)
        if not edge_types_set:
            warnings.append(
                "no supported lineage edge types remain after filter; "
                "the result is empty"
            )
            return LineageQuery(
                start=start_node_id,
                warnings=warnings,
            )

    # BFS state.
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
                store, cur_id, edge_types_set, LINEAGE_UPSTREAM_SIDE
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

    leaves = _compute_leaves(
        store, visited_nodes, edge_types_set, LINEAGE_UPSTREAM_SIDE
    )
    buckets = _bucket_visited(visited_nodes)

    leaf_summary: dict[str, int] = {}
    nodes_by_id = {n.node_id: n for n in visited_nodes}
    for lid in leaves:
        n = nodes_by_id.get(lid)
        if n is None:
            continue
        leaf_summary[n.node_type.value] = leaf_summary.get(
            n.node_type.value, 0
        ) + 1

    return LineageQuery(
        start=start_node_id,
        visited_node_ids=[n.node_id for n in visited_nodes],
        source_node_ids=buckets["source"],
        signal_node_ids=buckets["signal"],
        entity_node_ids=buckets["entity"],
        score_node_ids=buckets["score"],
        leaf_node_ids=leaves,
        leaf_summary=leaf_summary,
        layered=layered,
        visited_edges=visited_edges,
        truncated=truncated,
        depth_reached=depth_reached,
        warnings=warnings,
    )


__all__ = [
    "LINEAGE_EDGE_TYPES",
    "LINEAGE_UPSTREAM_SIDE",
    "GraphStoreLike",
    "LineageQuery",
    "compute_lineage",
]
