"""Blast Radius query — Phase 3B Task 4 Run 4A.

Downstream-dependency traversal: given a starting node, return the
set of consumer nodes (scores, reports, signals) that **transitively
depend** on it. The semantic is "if this node goes away or becomes
wrong, what is impacted" — pure data-dependency, not classification
or attribution.

This module is intentionally a small, query-specific implementation.
It does NOT reuse :class:`EvidenceTracer` — Run 4 discovery found
that ``EvidenceTracer._neighbors`` carries a directional asymmetry
that makes downstream walks from the *from-side* of several common
edge types (CONTRIBUTES_TO, INFLUENCES, INCLUDES, REFERS_TO,
REPORT_BY, WORKS_AT) return empty, which is exactly the Blast Radius
use case ("if a source goes away, what scores break?").

The policy here is derived from the **authoritative spec** for Blast
Radius (see Task 4 Run 4A brief) and not from a mirror of
``UPSTREAM_SIDE`` / ``DOWNSTREAM_SIDE`` from the tracer. The two
modules can disagree on edges that are relevant to evidence tracing
but irrelevant to blast radius (and vice versa); each module owns
its own policy.

Direction policy
----------------
For each edge type the Blast Radius policy declares which side of
the edge is the **downstream consumer** — the side that "depends on"
the other side. The BFS then walks: at the current node X, for each
edge of an allowed type, if X is on the upstream side (the side
opposite the consumer), move to the consumer side.

The policy table is :data:`BLAST_DOWNSTREAM_SIDE`. It is the
authoritative spec for the Blast Radius direction.

What counts as a "downstream dependency edge"
---------------------------------------------
The Run 4A brief is explicit: Blast Radius follows the **pure
data-dependency** edges. The following are DOWNSTREAM DEPENDENCIES
(semantically: removing the upstream breaks the downstream):

- ``GENERATED`` (signal → source): the signal is the consumer of
  the source. From a Source, downstream = its generated Signals.
- ``CONTRIBUTES_TO`` (signal → score): the score is the consumer
  of the signal. From a Signal, downstream = the scores it
  contributes to.
- ``INFLUENCES`` (score → score): the downstream score is the
  consumer. From an upstream score, downstream = the scores it
  influences.
- ``DERIVED_FROM`` (ancestor → derived): the derived score is
  the consumer.
- ``INCLUDES`` (report → score): the report is the consumer of
  the score. From a Score, downstream = the reports that include
  it.

The following are NOT downstream dependencies per the brief and
are deliberately excluded from the default policy:

- ``CITES`` (score → source): attribution, not a downstream
  dependency. A score that cites a source does not break when
  the source goes away; the citation is just provenance.
- ``REFERS_TO`` (news → entity): reference, not a dependency.
- ``MEMBER_OF`` (company → industry): classification.
- ``BELONGS_TO`` (industry → macro): classification.
- ``EXPOSED_TO`` (company → macro): exposure, not data
  dependency.
- ``REPORT_BY`` (report → person): authorship.
- ``WORKS_AT`` (person → company): employment.

Callers that need a different semantic (e.g. a "broad impact"
view that includes attribution) can pass a wider ``edge_types``
whitelist. The default is the strict, brief-faithful dependency
view.

Determinism + cycle protection
------------------------------
Neighbors are visited in stable ``(edge_id, neighbor_id)`` order
at every hop, so two graphs with identical structure and identical
arguments produce identical results. A ``visited`` set ensures
cycles do not produce infinite walks; nodes are recorded in
discovery (BFS) order.

Result shape
------------
:class:`BlastRadiusResult` is a frozen dataclass with:

- ``start``: the anchor node id.
- ``visited_node_ids``: BFS discovery order (excluding ``start``).
- ``score_node_ids`` / ``signal_node_ids`` / ``report_node_ids``:
  typed buckets for dashboard / API consumers.
- ``impact_counts``: ``{node_type.value: count}`` tally.
- ``layered``: ``{depth: [node_id, ...]}`` mapping hop distance
  to the nodes discovered at that depth (``depth=1`` = direct
  neighbors). ``start`` is NOT included in any layer.
- ``truncated``: True if the walk stopped due to ``max_depth``
  or ``max_nodes``.
- ``depth_reached``: max hop reached (0 if the walk is empty).
- ``warnings``: human-readable warnings.
- ``visited_edges``: ``[(edge_id, from_node_id, to_node_id),
  ...]`` in BFS discovery order, so callers can see the actual
  dependency chain that was followed.

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


# Per-edge-type downstream-side policy.
#
# For each edge type, the value names the side of the edge that is
# the DOWNSTREAM CONSUMER — the side that "depends on" the other
# side. The BFS uses this to decide which way to walk from a given
# node: if the current node is on the *upstream* side (the side
# opposite the consumer), walk to the consumer side; if the current
# node is *already* on the consumer side, do not continue past it.
#
# This is the AUTHORITATIVE Blast Radius policy per the Run 4A
# brief. It is intentionally narrower than the evidence-tracer
# ``DOWNSTREAM_SIDE`` (which includes CITES / REFERS_TO /
# classification / authorship edges). The two modules serve
# different queries and own their own policies independently.
BLAST_DOWNSTREAM_SIDE: dict[EdgeType, Literal["from", "to"]] = {
    # signal -> source: the signal is the consumer of the source.
    # At a SOURCE node, downstream = the signal (consumer is "from"
    # = the side the signal is on).
    EdgeType.GENERATED: "from",
    # signal -> score: the score is the consumer of the signal.
    # At a SIGNAL node, downstream = the score.
    EdgeType.CONTRIBUTES_TO: "to",
    # score (influencer) -> score (influenced): the influenced
    # score is the consumer.
    EdgeType.INFLUENCES: "to",
    # score (ancestor) -> score (derived): the derived score is
    # the consumer.
    EdgeType.DERIVED_FROM: "to",
    # report -> score: the report is the consumer of the score
    # (the report depends on the score; removing the score
    # breaks the report). At a SCORE node, downstream = the
    # report (consumer is on the "from" side of the edge).
    EdgeType.INCLUDES: "from",
    # NOTE: CITES, REFERS_TO, MEMBER_OF, BELONGS_TO, EXPOSED_TO,
    # REPORT_BY, WORKS_AT are intentionally NOT in this table.
    # Per the Run 4A brief they are NOT downstream dependency
    # edges. They are still valid edge types in the graph, but
    # Blast Radius does not follow them by default.
}


# Default set of edge types the Blast Radius follows. Equal to the
# keys of BLAST_DOWNSTREAM_SIDE (a copy is made at call time so
# callers cannot mutate the policy table).
def _default_edge_types() -> list[EdgeType]:
    return list(BLAST_DOWNSTREAM_SIDE.keys())


@dataclass(frozen=True)
class BlastRadiusResult:
    """Result of a Blast Radius query — downstream consumer discovery.

    Attributes:
        start: The anchor node id.
        visited_node_ids: BFS discovery order, excluding ``start``.
        score_node_ids: Subset of visited nodes that are
            :class:`NodeType.SCORE`.
        signal_node_ids: Subset that are :class:`NodeType.SIGNAL`
            (downstream signals are legal — generated by a source).
        report_node_ids: Subset that are :class:`NodeType.REPORT`.
        impact_counts: ``{node_type.value: count}`` tally of all
            visited nodes by type. Convenience for dashboards.
        layered: ``{depth: [node_id, ...]}`` mapping hop distance
            to the nodes discovered at that depth. ``depth=1`` is
            direct neighbors. The start node is NOT included in any
            layer.
        visited_edges: ``[(edge_id, from_node_id, to_node_id), ...]``
            in BFS discovery order, so callers can see the actual
            dependency chain that was followed.
        truncated: True if the walk stopped early due to
            ``max_depth`` or ``max_nodes``.
        depth_reached: Max hop reached (0 for an empty walk).
        warnings: Human-readable warnings.
    """

    start: str
    visited_node_ids: list[str] = field(default_factory=list)
    score_node_ids: list[str] = field(default_factory=list)
    signal_node_ids: list[str] = field(default_factory=list)
    report_node_ids: list[str] = field(default_factory=list)
    impact_counts: dict[str, int] = field(default_factory=dict)
    layered: dict[int, list[str]] = field(default_factory=dict)
    visited_edges: list[tuple[str, str, str]] = field(default_factory=list)
    truncated: bool = False
    depth_reached: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "visited_node_ids": list(self.visited_node_ids),
            "score_node_ids": list(self.score_node_ids),
            "signal_node_ids": list(self.signal_node_ids),
            "report_node_ids": list(self.report_node_ids),
            "impact_counts": dict(self.impact_counts),
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
) -> list[tuple[GraphNode, GraphEdge]]:
    """Return the (neighbor, edge) pairs that Blast Radius would walk
    to from ``cur_id``.

    For each edge of an allowed type, check whether ``cur_id`` is on
    the *upstream* side (opposite the consumer side declared in
    :data:`BLAST_DOWNSTREAM_SIDE`). If yes, the neighbor is the
    consumer-side node; if no (cur_id is already the consumer), the
    edge is not followed.

    Neighbors are returned in stable ``(edge_id, neighbor_id)`` order
    so two equal graphs always produce the same result.
    """
    candidates: list[tuple[GraphNode, GraphEdge]] = []

    # cur_id is the from-side of these edges.
    for edge in store.edges_from(cur_id):
        if edge.edge_type not in edge_types_set:
            continue
        downstream_side = BLAST_DOWNSTREAM_SIDE.get(edge.edge_type)
        if downstream_side is None:
            continue
        # cur is the from; walk to to iff consumer is on to-side.
        if downstream_side != "to":
            continue
        other = store.get_node(edge.to_node_id)
        if other is not None:
            candidates.append((other, edge))

    # cur_id is the to-side of these edges.
    for edge in store.edges_to(cur_id):
        if edge.edge_type not in edge_types_set:
            continue
        downstream_side = BLAST_DOWNSTREAM_SIDE.get(edge.edge_type)
        if downstream_side is None:
            continue
        # cur is the to; walk to from iff consumer is on from-side.
        if downstream_side != "from":
            continue
        other = store.get_node(edge.from_node_id)
        if other is not None:
            candidates.append((other, edge))

    # Stable deterministic order.
    candidates.sort(key=lambda ne: (ne[1].edge_id, ne[0].node_id))
    return candidates


def _bucket_visited(
    visited: list[GraphNode],
) -> tuple[list[str], list[str], list[str], dict[str, int]]:
    """Split visited nodes into typed buckets + impact count."""
    score_ids: list[str] = []
    signal_ids: list[str] = []
    report_ids: list[str] = []
    impact_counts: dict[str, int] = {}
    for n in visited:
        impact_counts[n.node_type.value] = impact_counts.get(
            n.node_type.value, 0
        ) + 1
        if n.node_type == NodeType.SCORE:
            score_ids.append(n.node_id)
        elif n.node_type == NodeType.SIGNAL:
            signal_ids.append(n.node_id)
        elif n.node_type == NodeType.REPORT:
            report_ids.append(n.node_id)
    return score_ids, signal_ids, report_ids, impact_counts


def compute_blast_radius(
    store: _GraphStoreLike,
    start_node_id: str,
    *,
    max_depth: int = 5,
    edge_types: list[EdgeType] | None = None,
    max_nodes: int | None = None,
) -> BlastRadiusResult:
    """Compute the Blast Radius (downstream consumers) of
    ``start_node_id``.

    Walks the graph in the downstream direction following the
    per-edge-type policy in :data:`BLAST_DOWNSTREAM_SIDE`. Returns
    a :class:`BlastRadiusResult` with the visited nodes bucketed
    by type and a per-hop layered view.

    Parameters
    ----------
    store:
        The graph store to query.
    start_node_id:
        The anchor node id. Typically a SOURCE, SIGNAL, or SCORE
        node, but any node works.
    max_depth:
        Maximum hop count. ``1`` = direct consumers only.
        Default 5.
    edge_types:
        Optional whitelist of edge types to follow. Defaults to
        all edge types in :data:`BLAST_DOWNSTREAM_SIDE` (the
        brief-faithful strict-dependency set: GENERATED,
        CONTRIBUTES_TO, INFLUENCES, DERIVED_FROM, INCLUDES).
    max_nodes:
        Optional cap on the number of nodes the walk may visit
        (excluding ``start``). When hit, the walk stops and the
        ``truncated`` flag is set.

    Returns
    -------
    :class:`BlastRadiusResult`. When ``start_node_id`` is not in
    the store, the result has empty buckets, ``truncated=False``,
    ``depth_reached=0``, and a single warning.
    """
    warnings: list[str] = []

    if not store.has_node(start_node_id):
        warnings.append(f"start node {start_node_id!r} not in graph")
        return BlastRadiusResult(
            start=start_node_id,
            warnings=warnings,
        )

    if edge_types is None:
        edge_types = _default_edge_types()
    edge_types_set: set[EdgeType] = set(edge_types)

    # BFS state.
    visited_ids: set[str] = {start_node_id}
    visited_nodes: list[GraphNode] = []  # BFS discovery order
    visited_edges: list[tuple[str, str, str]] = []  # (edge_id, from, to)
    layered: dict[int, list[str]] = {}
    frontier: list[str] = [start_node_id]
    depth_reached: int = 0
    truncated: bool = False

    for depth in range(1, max_depth + 1):
        next_frontier: list[str] = []
        layer_nodes: list[str] = []
        for cur_id in frontier:
            for neighbor, edge in _neighbors_at(
                store, cur_id, edge_types_set
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

    score_ids, signal_ids, report_ids, impact_counts = _bucket_visited(
        visited_nodes
    )

    return BlastRadiusResult(
        start=start_node_id,
        visited_node_ids=[n.node_id for n in visited_nodes],
        score_node_ids=score_ids,
        signal_node_ids=signal_ids,
        report_node_ids=report_ids,
        impact_counts=impact_counts,
        layered=layered,
        visited_edges=visited_edges,
        truncated=truncated,
        depth_reached=depth_reached,
        warnings=warnings,
    )


__all__ = [
    "BLAST_DOWNSTREAM_SIDE",
    "BlastRadiusResult",
    "GraphStoreLike",
    "compute_blast_radius",
]
