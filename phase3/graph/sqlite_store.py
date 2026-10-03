"""SQLite-backed graph store for Phase 3B.

Mirrors the public surface of
:class:`phase3.graph.in_memory_store.GraphStore` so the two can be
swapped in any consumer (traversal helpers, evidence tracer, etc.).
Test parity is asserted in ``tests/phase3/test_graph_parity.py``.

Implementation
--------------
* One :class:`~phase3.persistence.sqlite.SQLiteStore` for the
  whole graph. The store is owned by the graph store but callers
  decide the path — the default is ``phase3/data/intelligence.db``.
* Node and edge round-tripping goes through
  :class:`~phase3.persistence.graph_repo.GraphRepository`.
* Edge upserts always use the *canonical* id from
  :func:`phase3.datamodel.graph.make_graph_edge_id` (matches the
  in-memory store's contract). The schema's unique constraint on
  ``(edge_type, from_node_id, to_node_id)`` is a second safety net
  against duplicate logical edges.
* This module is import-time side-effect free: nothing touches the
  filesystem until :class:`SQLiteGraphStore` is instantiated.
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any, Iterable, Literal

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    make_graph_edge_id,
)
from phase3.persistence.graph_repo import EdgeRow, GraphRepository, NodeRow
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.sqlite import SQLiteStore
from phase3.persistence import schema_v1


#: Default database path for Phase 3B. Resolved relative to the
#: ``phase3`` package, never relative to the caller's CWD, so the
#: same code path always lands in the same file.
DEFAULT_DB_PATH: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "intelligence.db",
)


class SQLiteGraphStore:
    """SQLite-backed :class:`GraphStore` for Phase 3B.

    Parameters
    ----------
    db_path:
        Where to store the database. Pass a temp path in tests. The
        default is :data:`DEFAULT_DB_PATH`, which is a file under
        ``phase3/data/`` that the path guard allows.
    auto_migrate:
        If True (default), :meth:`ensure_schema` runs at
        construction so first use Just Works. Set False in tests that
        want to drive migration explicitly.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
        auto_migrate: bool = True,
    ) -> None:
        self._store = SQLiteStore(db_path)
        self._repo = GraphRepository(self._store)
        if auto_migrate:
            self.ensure_schema()

    # ----- lifecycle -------------------------------------------------------

    def ensure_schema(self) -> tuple[list[Any], int]:
        """Run the v1 migration. Idempotent.

        Returns ``(newly_applied, current_version)``; ``newly_applied``
        is empty when the schema is already up to date.
        """
        mgr = MigrationManager(self._store, [schema_v1.build()])
        applied = mgr.apply()
        return applied, mgr.current_version()

    @property
    def path(self) -> str:
        """Resolved path to the database file."""
        return self._store.path

    def set_query_only(self) -> None:
        """Flip the underlying connection to read-only (Phase 6.3 seam).

        Delegates to :meth:`SQLiteStore.set_query_only` so read-only
        CLI paths stop digging at ``store._store._conn``.
        """
        self._store.set_query_only()

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "SQLiteGraphStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----- nodes -----------------------------------------------------------

    def add_node(self, node: GraphNode) -> bool:
        """Upsert a node. Returns True if a new node was inserted."""
        return self._repo.upsert_node(
            NodeRow(
                node_id=node.node_id,
                node_type=node.node_type.value,
                label=node.label,
                created_at=node.created_at.isoformat(),
                metadata=dict(node.metadata),
                tags=list(node.tags),
                schema_version=node.schema_version,
            )
        )

    def get_node(self, node_id: str) -> GraphNode | None:
        row = self._repo.get_node(node_id)
        return _node_from_row(row) if row is not None else None

    def has_node(self, node_id: str) -> bool:
        return self._repo.get_node(node_id) is not None

    def query_nodes(
        self,
        node_type: NodeType | None = None,
        tags: Iterable[str] | None = None,
    ) -> list[GraphNode]:
        """Filter by ``node_type`` and ``tags`` (all-of).

        ``tags`` filtering happens in Python because SQLite has no
        array containment operator in stock builds; the row list
        is small enough that this is not a hot path.
        """
        type_filter = node_type.value if node_type is not None else None
        rows = self._repo.list_nodes(node_type=type_filter, limit=100000)
        out: list[GraphNode] = []
        for r in rows:
            node = _node_from_row(r)
            if tags and not all(t in node.tags for t in tags):
                continue
            out.append(node)
        return out

    def node_count(self) -> int:
        return self._repo.node_count()

    # ----- edges -----------------------------------------------------------

    def add_edge(self, edge: GraphEdge) -> bool:
        """Upsert an edge. Returns True if a new edge was inserted.

        The id is restamped to the canonical
        :func:`make_graph_edge_id` value to match the in-memory
        store's contract.
        """
        canonical = make_graph_edge_id(
            edge.edge_type, edge.from_node_id, edge.to_node_id
        )
        # If the caller's id disagreed we restamp — same as the
        # in-memory store. We do this in Python rather than via a SQL
        # UPDATE because the schema unique constraint already
        # prevents the worst outcome (a duplicate logical edge).
        if edge.edge_id != canonical:
            edge = GraphEdge(
                edge_id=canonical,
                edge_type=edge.edge_type,
                from_node_id=edge.from_node_id,
                to_node_id=edge.to_node_id,
                weight=edge.weight,
                metadata=edge.metadata,
                created_at=edge.created_at,
                schema_version=edge.schema_version,
            )
        return self._repo.upsert_edge(
            EdgeRow(
                edge_id=edge.edge_id,
                edge_type=edge.edge_type.value,
                from_node_id=edge.from_node_id,
                to_node_id=edge.to_node_id,
                weight=edge.weight,
                metadata=dict(edge.metadata),
                created_at=edge.created_at.isoformat(),
                schema_version=edge.schema_version,
            )
        )

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        row = self._repo.get_edge(edge_id)
        return _edge_from_row(row) if row is not None else None

    def edge_count(self) -> int:
        return self._repo.edge_count()

    def edges_from(self, node_id: str) -> list[GraphEdge]:
        rows = self._repo.list_edges(from_node_id=node_id, limit=100000)
        return [_edge_from_row(r) for r in rows]

    def edges_to(self, node_id: str) -> list[GraphEdge]:
        rows = self._repo.list_edges(to_node_id=node_id, limit=100000)
        return [_edge_from_row(r) for r in rows]

    def get_neighbors(
        self,
        node_id: str,
        edge_types: list[EdgeType] | None = None,
        direction: Literal["out", "in", "both"] = "both",
    ) -> list[tuple[GraphNode, GraphEdge]]:
        """Return ``(neighbor, edge)`` pairs, filtered by direction
        and edge type. Mirrors the in-memory store's contract
        exactly (returns ``[]`` if the node is missing).
        """
        if not self.has_node(node_id):
            return []
        types_set = set(edge_types) if edge_types else None
        results: list[tuple[GraphNode, GraphEdge]] = []

        def _consider(edge: GraphEdge, other_id: str) -> None:
            if types_set and edge.edge_type not in types_set:
                return
            other_row = self._repo.get_node(other_id)
            if other_row is None:
                return
            results.append((_node_from_row(other_row), edge))

        if direction in ("out", "both"):
            for e in self.edges_from(node_id):
                _consider(e, e.to_node_id)
        if direction in ("in", "both"):
            for e in self.edges_to(node_id):
                _consider(e, e.from_node_id)
        return results

    # ----- introspection ---------------------------------------------------

    def stats(self) -> dict[str, Any]:
        node_counts: dict[str, int] = defaultdict(int)
        for r in self._repo.list_nodes(limit=1000000):
            node_counts[r.node_type] += 1
        edge_counts: dict[str, int] = defaultdict(int)
        for r in self._repo.list_edges(limit=1000000):
            edge_counts[r.edge_type] += 1
        return {
            "total_nodes": self._repo.node_count(),
            "total_edges": self._repo.edge_count(),
            "nodes_by_type": dict(node_counts),
            "edges_by_type": dict(edge_counts),
        }


# ---------------------------------------------------------------------------
# Row -> dataclass helpers
# ---------------------------------------------------------------------------


def _node_from_row(row: NodeRow) -> GraphNode:
    from datetime import datetime

    return GraphNode(
        node_id=row.node_id,
        node_type=NodeType(row.node_type),
        label=row.label,
        created_at=datetime.fromisoformat(row.created_at),
        metadata=dict(row.metadata),
        tags=list(row.tags),
        schema_version=row.schema_version,
    )


def _edge_from_row(row: EdgeRow) -> GraphEdge:
    from datetime import datetime

    return GraphEdge(
        edge_id=row.edge_id,
        edge_type=EdgeType(row.edge_type),
        from_node_id=row.from_node_id,
        to_node_id=row.to_node_id,
        weight=row.weight,
        metadata=dict(row.metadata),
        created_at=datetime.fromisoformat(row.created_at),
        schema_version=row.schema_version,
    )


__all__ = ["SQLiteGraphStore", "DEFAULT_DB_PATH"]
