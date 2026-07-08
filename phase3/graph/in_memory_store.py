"""InMemoryGraphStore — pure in-memory research graph for Phase 3A.

Phase 3B will add SQLiteGraphStore; the GraphStore ABC in this file is
the contract both implementations honor. Phase 3A scope:
  - add_node / add_edge are idempotent (upsert by node_id / edge_id)
  - get_node / get_neighbors support both directions
  - query by node_type for stats / dashboard work

Persistence is intentionally out of scope. The store is created per
session and discarded. Callers (CLI demos, tests) own the lifecycle.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Literal

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    make_graph_edge_id,
)


class GraphStore:
    """Abstract-ish in-memory store. ABC is for downstream SQLite impl.

    All methods are pure-Python and operate on dicts. No locks; not
    thread-safe. Phase 3A is single-process and single-thread.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}
        # Adjacency: from_node_id -> list[edge_id]
        self._out: dict[str, list[str]] = defaultdict(list)
        # Adjacency: to_node_id -> list[edge_id]
        self._in: dict[str, list[str]] = defaultdict(list)

    # ---- nodes ----

    def add_node(self, node: GraphNode) -> bool:
        """Upsert a node. Returns True if a new node was inserted."""
        is_new = node.node_id not in self._nodes
        self._nodes[node.node_id] = node
        return is_new

    def get_node(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def query_nodes(
        self,
        node_type: NodeType | None = None,
        tags: Iterable[str] | None = None,
    ) -> list[GraphNode]:
        out: list[GraphNode] = []
        for n in self._nodes.values():
            if node_type is not None and n.node_type != node_type:
                continue
            if tags and not all(t in n.tags for t in tags):
                continue
            out.append(n)
        return out

    def node_count(self) -> int:
        return len(self._nodes)

    # ---- edges ----

    def add_edge(self, edge: GraphEdge) -> bool:
        """Upsert an edge. Returns True if a new edge was inserted.

        Note: callers can supply their own edge_id, but for the canonical
        Phase 3 contract an edge is identified by (type, from, to) — see
        `make_graph_edge_id()`. We use that as the key, so re-inserting
        the same logical edge is idempotent.
        """
        canonical_id = make_graph_edge_id(
            edge.edge_type, edge.from_node_id, edge.to_node_id
        )
        if edge.edge_id != canonical_id:
            # Re-stamp id; in a real impl this would be a validator, but
            # for Phase 3A we silently adopt the canonical id.
            edge = GraphEdge(
                edge_id=canonical_id,
                edge_type=edge.edge_type,
                from_node_id=edge.from_node_id,
                to_node_id=edge.to_node_id,
                weight=edge.weight,
                metadata=edge.metadata,
                created_at=edge.created_at,
                schema_version=edge.schema_version,
            )
        is_new = canonical_id not in self._edges
        # If the canonical id was previously used by a *different* logical
        # edge (extremely unlikely but possible in collision), drop the
        # old adjacency entries first.
        if not is_new:
            old = self._edges[canonical_id]
            if old.from_node_id != edge.from_node_id or old.to_node_id != edge.to_node_id:
                # Drop the stale adjacency
                try:
                    self._out[old.from_node_id].remove(canonical_id)
                except ValueError:
                    pass
                try:
                    self._in[old.to_node_id].remove(canonical_id)
                except ValueError:
                    pass
                is_new = True
        self._edges[canonical_id] = edge
        if is_new:
            self._out[edge.from_node_id].append(canonical_id)
            self._in[edge.to_node_id].append(canonical_id)
        return is_new

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        return self._edges.get(edge_id)

    def edge_count(self) -> int:
        return len(self._edges)

    def edges_from(self, node_id: str) -> list[GraphEdge]:
        return [self._edges[eid] for eid in self._out.get(node_id, [])]

    def edges_to(self, node_id: str) -> list[GraphEdge]:
        return [self._edges[eid] for eid in self._in.get(node_id, [])]

    def get_neighbors(
        self,
        node_id: str,
        edge_types: list[EdgeType] | None = None,
        direction: Literal["out", "in", "both"] = "both",
    ) -> list[tuple[GraphNode, GraphEdge]]:
        """Yield (neighbor_node, edge) pairs."""
        if not self.has_node(node_id):
            return []
        results: list[tuple[GraphNode, GraphEdge]] = []
        types_set = set(edge_types) if edge_types else None

        def _consider(edge: GraphEdge, other_id: str) -> None:
            if types_set and edge.edge_type not in types_set:
                return
            other = self._nodes.get(other_id)
            if other is None:
                return
            results.append((other, edge))

        if direction in ("out", "both"):
            for e in self.edges_from(node_id):
                _consider(e, e.to_node_id)
        if direction in ("in", "both"):
            for e in self.edges_to(node_id):
                _consider(e, e.from_node_id)
        return results

    # ---- introspection ----

    def stats(self) -> dict[str, Any]:
        """Return counts of nodes by type and edges by type."""
        node_counts: dict[str, int] = defaultdict(int)
        for n in self._nodes.values():
            node_counts[n.node_type.value] += 1
        edge_counts: dict[str, int] = defaultdict(int)
        for e in self._edges.values():
            edge_counts[e.edge_type.value] += 1
        return {
            "total_nodes": len(self._nodes),
            "total_edges": len(self._edges),
            "nodes_by_type": dict(node_counts),
            "edges_by_type": dict(edge_counts),
        }


__all__ = ["GraphStore"]
