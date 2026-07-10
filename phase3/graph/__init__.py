"""Research graph subpackage.

Phase 3A: pure in-memory graph (no SQLite, no NetworkX). BFS,
shortest_path, and EvidenceTracer are all implemented. Phase 3B
adds SQLiteGraphStore with the same GraphStore interface.

Phase 3B Task 4 Run 4A (2026-07-11): the canonical Blast Radius
implementation lives in :mod:`phase3.graph.blast_radius` and is
re-exported from this package. The prior ``BlastRadiusQuery`` /
``query_blast_radius`` symbols that used to live in
:mod:`phase3.graph.queries` (Run 4 first attempt) have been
removed because their implementation delegated to
:class:`EvidenceTracer` with the wrong direction semantics
("Pitfall B" in the Run 4 reference). Use the canonical path:

    from phase3.graph import (
        BlastRadiusResult,
        BLAST_DOWNSTREAM_SIDE,
        compute_blast_radius,
    )
"""
from __future__ import annotations

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    make_graph_edge_id,
)
from phase3.graph.evidence_tracer import (
    DOWNSTREAM_SIDE,
    EVIDENCE_DOWNSTREAM_EDGE_TYPES,
    EVIDENCE_UPSTREAM_EDGE_TYPES,
    EvidenceChain,
    EvidenceTracer,
    SPEC_EDGE_POLICY,
    UPSTREAM_SIDE,
)
from phase3.graph.in_memory_store import GraphStore
from phase3.graph.traversal import bfs, shortest_path
from phase3.graph.evidence_trace_export import (
    EvidenceChainAdapter,
    chain_to_dict,
    chain_to_json,
    score_node_id_for_result,
)
from phase3.graph.blast_radius import (
    BLAST_DOWNSTREAM_SIDE,
    BlastRadiusResult,
    GraphStoreLike,
    compute_blast_radius,
)
from phase3.graph.queries import (
    CROSS_LAYER_EDGE_TYPES,
    CrossLayerImpactQuery,
    GraphQueryService,
    LineageQuery,
    query_cross_layer_impact,
    query_lineage,
)

__all__ = [
    "GraphStore",
    "GraphNode",
    "GraphEdge",
    "NodeType",
    "EdgeType",
    "make_graph_edge_id",
    "bfs",
    "shortest_path",
    "EvidenceTracer",
    "EvidenceChain",
    "EvidenceChainAdapter",
    "chain_to_dict",
    "chain_to_json",
    "score_node_id_for_result",
    "EVIDENCE_UPSTREAM_EDGE_TYPES",
    "EVIDENCE_DOWNSTREAM_EDGE_TYPES",
    "UPSTREAM_SIDE",
    "DOWNSTREAM_SIDE",
    "SPEC_EDGE_POLICY",
    # Canonical Blast Radius (Run 4A consolidation, 2026-07-11)
    "BLAST_DOWNSTREAM_SIDE",
    "BlastRadiusResult",
    "GraphStoreLike",
    "compute_blast_radius",
    # Graph query layer (Run 4 first attempt, post-consolidation)
    "CROSS_LAYER_EDGE_TYPES",
    "CrossLayerImpactQuery",
    "GraphQueryService",
    "LineageQuery",
    "query_cross_layer_impact",
    "query_lineage",
]
