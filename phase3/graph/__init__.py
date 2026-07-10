"""Research graph subpackage.

Phase 3A: pure in-memory graph (no SQLite, no NetworkX). BFS,
shortest_path, and EvidenceTracer are all implemented. Phase 3B
adds SQLiteGraphStore with the same GraphStore interface.
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
    "EVIDENCE_UPSTREAM_EDGE_TYPES",
    "EVIDENCE_DOWNSTREAM_EDGE_TYPES",
    "UPSTREAM_SIDE",
    "DOWNSTREAM_SIDE",
    "SPEC_EDGE_POLICY",
]
