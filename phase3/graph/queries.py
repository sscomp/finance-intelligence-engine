"""Graph Query Layer — Phase 3B Task 4 Run 4B (post-consolidation).

Reusable, read-only query APIs over the research graph.

Scope (post Run 4B consolidation, 2026-07-11)
---------------------------------------------
This module is a thin facade over the dedicated query modules
created in Run 4B. Each query family has its own module with its
own self-contained BFS and its own per-edge-type direction policy:

- :mod:`phase3.graph.lineage` — :func:`compute_lineage` /
  :class:`LineageQuery` (upstream provenance)
- :mod:`phase3.graph.blast_radius` — :func:`compute_blast_radius` /
  :class:`BlastRadiusResult` (downstream dependency)
- :mod:`phase3.graph.cross_layer_impact` —
  :func:`compute_cross_layer_impact` /
  :class:`CrossLayerImpactResult` (score-to-score ripple)

The thin facade here provides the function aliases and DTO names
that previous Run 4 code exposed (``query_lineage``,
``query_cross_layer_impact``, ``LineageQuery``,
``CrossLayerImpactQuery``, ``GraphQueryService``). All of these
now route to the canonical implementations. There is no
duplicate BFS / leaf logic in this module.

What was here, what moved
-------------------------
Run 4 (first attempt) shipped a ``BlastRadiusQuery`` /
``query_blast_radius`` implementation in this module that
delegated to :class:`EvidenceTracer` with the wrong downstream
semantics (Pitfall B / O). Run 4A (commit ``55cdaf3``) removed
those broken aliases and promoted the strict-dependency
implementation to :mod:`phase3.graph.blast_radius`.

Run 4B (this commit) keeps the facade but rewrites the underlying
``query_cross_layer_impact`` and ``query_lineage`` to delegate
to the dedicated modules
(:mod:`phase3.graph.cross_layer_impact` and
:mod:`phase3.graph.lineage`) which carry their own BFS and
direction policy. This eliminates the dependency on
:class:`EvidenceTracer` from the query layer, which removes the
Pitfall O surface (the tracer's downstream-walk asymmetry) from
this code path.

Why a thin facade rather than direct re-exports
-----------------------------------------------
A class is provided (``:class:`GraphQueryService```) for callers
that prefer an object-oriented entry point; module-level
functions (``query_lineage`` / ``query_cross_layer_impact``) are
provided for callers that prefer functional composition. Both
delegate to the canonical implementations.

All queries are deterministic: same store + same args → same DTO.
Neighbors are sorted at the BFS level so output ordering is
stable.

No production DB writes. No production cron / jobs.json edits.
"""
from __future__ import annotations

from phase3.datamodel.graph import EdgeType

from phase3.graph.blast_radius import (
    BLAST_DOWNSTREAM_SIDE,
    BlastRadiusResult,
    GraphStoreLike,
    compute_blast_radius,
)
from phase3.graph.cross_layer_impact import (
    CROSS_LAYER_DOWNSTREAM_SIDE,
    CROSS_LAYER_EDGE_TYPES,
    CROSS_LAYER_UPSTREAM_SIDE,
    CrossLayerImpactResult,
    compute_cross_layer_impact,
)
from phase3.graph.in_memory_store import GraphStore
from phase3.graph.lineage import (
    LINEAGE_EDGE_TYPES,
    LINEAGE_UPSTREAM_SIDE,
    LineageQuery,
    compute_lineage,
)


# Re-export the canonical DTOs under their legacy ``*Query`` names
# so existing call sites that imported ``LineageQuery`` /
# ``CrossLayerImpactQuery`` from this module keep working. The
# canonical DTOs are imported under their canonical names
# (``LineageQuery`` for lineage, ``CrossLayerImpactResult`` for
# cross-layer impact) above, and the legacy names are exposed via
# module-level re-exports below in __all__.
CrossLayerImpactQuery = CrossLayerImpactResult


# ---------------------------------------------------------------------
# Public query functions — thin delegations to the canonical modules
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

    Thin delegation to
    :func:`phase3.graph.lineage.compute_lineage`. The
    ``include_layered`` flag is accepted for backward compatibility
    (the canonical LineageQuery always includes the layered view;
    ``include_layered=False`` is silently accepted).
    """
    return compute_lineage(
        store,
        start_node_id,
        max_depth=max_depth,
        edge_types=edge_types,
        max_nodes=max_nodes,
    )


def query_cross_layer_impact(
    store: GraphStore,
    start_node_id: str,
    *,
    max_depth: int = 5,
    edge_types: list[EdgeType] | None = None,
    max_nodes: int | None = None,
) -> CrossLayerImpactResult:
    """Walk the cross-layer edges (``INFLUENCES``,
    ``DERIVED_FROM``) in both directions and return a
    :class:`CrossLayerImpactResult` DTO.

    Thin delegation to
    :func:`phase3.graph.cross_layer_impact.compute_cross_layer_impact`.
    """
    return compute_cross_layer_impact(
        store,
        start_node_id,
        max_depth=max_depth,
        edge_types=edge_types,
        max_nodes=max_nodes,
    )


# ---------------------------------------------------------------------
# Service facade
# ---------------------------------------------------------------------


class GraphQueryService:
    """Stateless facade exposing the two graph query families kept in
    this module.

    The service holds a single :class:`GraphStore` reference and
    exposes :meth:`lineage` and :meth:`cross_layer_impact` as
    instance methods. Stateless beyond the store reference —
    every call constructs the relevant BFS state so the service
    is safe to share across threads (the underlying store is the
    load-bearing shared state).

    Typical usage::

        store = SQLiteGraphStore(path).ensure_schema()
        svc = GraphQueryService(store)
        lineage = svc.lineage("score:company:2330:2026-07-08")
        impact = svc.cross_layer_impact(
            "score:macro:global:2026-07-08"
        )

    The instance-method API mirrors the module-level functions
    (``query_lineage``, ``query_cross_layer_impact``) — pick
    whichever fits the call site. For Blast Radius use the
    canonical :func:`phase3.graph.blast_radius.compute_blast_radius`
    function directly (Run 4A removed ``blast_radius`` from this
    service to keep the consolidation on a single home).

    All forms are part of the public surface.
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
        return compute_lineage(
            self._store,
            start_node_id,
            max_depth=max_depth,
            edge_types=edge_types,
            max_nodes=max_nodes,
        )

    def cross_layer_impact(
        self,
        start_node_id: str,
        *,
        max_depth: int = 5,
        edge_types: list[EdgeType] | None = None,
        max_nodes: int | None = None,
    ) -> CrossLayerImpactResult:
        return compute_cross_layer_impact(
            self._store,
            start_node_id,
            max_depth=max_depth,
            edge_types=edge_types,
            max_nodes=max_nodes,
        )


__all__ = [
    # DTOs (re-exported from canonical modules for convenience)
    "LineageQuery",
    "CrossLayerImpactQuery",
    "BlastRadiusResult",
    # Functions
    "query_lineage",
    "query_cross_layer_impact",
    "compute_blast_radius",
    "compute_lineage",
    "compute_cross_layer_impact",
    # Service facade
    "GraphQueryService",
    # Policy tables / edge sets
    "CROSS_LAYER_EDGE_TYPES",
    "CROSS_LAYER_UPSTREAM_SIDE",
    "CROSS_LAYER_DOWNSTREAM_SIDE",
    "BLAST_DOWNSTREAM_SIDE",
    "LINEAGE_EDGE_TYPES",
    "LINEAGE_UPSTREAM_SIDE",
    # Store protocol
    "GraphStoreLike",
]
