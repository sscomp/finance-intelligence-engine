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

Phase 3B Task 4 Run 4B (2026-07-11): the canonical Lineage
implementation lives in :mod:`phase3.graph.lineage` and the
canonical Cross-layer Impact implementation lives in
:mod:`phase3.graph.cross_layer_impact`. The previous
``query_lineage`` and ``query_cross_layer_impact`` functions in
:mod:`phase3.graph.queries` (which delegated to
:class:`EvidenceTracer` and inherited the tracer's
downstream-walk asymmetry, "Pitfall O") are now thin facades
over the canonical modules. Use the canonical paths:

    from phase3.graph import (
        LineageQuery,
        compute_lineage,
    )

    from phase3.graph import (
        CrossLayerImpactResult,
        compute_cross_layer_impact,
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
from phase3.graph.cross_layer_impact import (
    CROSS_LAYER_DOWNSTREAM_SIDE,
    CROSS_LAYER_EDGE_TYPES,
    CROSS_LAYER_UPSTREAM_SIDE,
    CrossLayerImpactResult,
    compute_cross_layer_impact,
)
from phase3.graph.lineage import (
    LINEAGE_EDGE_TYPES,
    LINEAGE_UPSTREAM_SIDE,
    LineageQuery,
    compute_lineage,
)
from phase3.graph.queries import (
    CrossLayerImpactQuery,
    GraphQueryService,
    query_cross_layer_impact,
    query_lineage,
)
from phase3.graph.explain_score import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES,
    ExplainedScore,
    GraphStoreLike,
    explain_score,
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
    # Canonical Lineage (Run 4B consolidation, 2026-07-11)
    "LINEAGE_EDGE_TYPES",
    "LINEAGE_UPSTREAM_SIDE",
    "LineageQuery",
    "compute_lineage",
    # Canonical Cross-layer Impact (Run 4B consolidation, 2026-07-11)
    "CROSS_LAYER_EDGE_TYPES",
    "CROSS_LAYER_UPSTREAM_SIDE",
    "CROSS_LAYER_DOWNSTREAM_SIDE",
    "CrossLayerImpactResult",
    "compute_cross_layer_impact",
    # Graph query facade (Run 4B consolidation, 2026-07-11)
    "CrossLayerImpactQuery",
    "GraphQueryService",
    "query_cross_layer_impact",
    "query_lineage",
    # Explain-Score / Decision Trace (Phase 4 Task 3B Run 1,
    # 2026-07-11) — read-only composition of the three canonical
    # graph queries plus score metadata projection.
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES",
    "ExplainedScore",
    "GraphStoreLike",
    "explain_score",
]
