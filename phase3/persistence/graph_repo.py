"""Repository for ``graph_nodes`` and ``graph_edges``.

Mirrors a subset of the in-memory graph store so the two can be
compared in :mod:`phase3.graph.sqlite_store`. The repo is the SQL
side; the graph store wraps it in
:class:`~phase3.datamodel.graph.GraphNode` /
:class:`~phase3.datamodel.graph.GraphEdge` dataclasses and adds
traversal helpers.

Idempotency
-----------
Both node and edge UPSERTs are keyed on their primary id, with the
``graph_edges`` table also carrying a unique constraint on
``(edge_type, from_node_id, to_node_id)`` so a duplicate logical
edge is silently folded into one row.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from phase3.persistence.sqlite import SQLiteStore


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeRow:
    node_id: str
    node_type: str
    label: str
    created_at: str
    metadata: dict[str, Any]
    tags: list[str]
    schema_version: str = "3.0"


@dataclass(frozen=True)
class EdgeRow:
    edge_id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    weight: float | None
    metadata: dict[str, Any]
    created_at: str
    schema_version: str = "3.0"


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class GraphRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    # ----- writes ----------------------------------------------------------

    def upsert_node(self, node: NodeRow) -> bool:
        sql = """
        INSERT INTO graph_nodes (
            node_id, node_type, label, created_at,
            metadata_json, tags_json, schema_version
        ) VALUES (
            :node_id, :node_type, :label,
            COALESCE(:created_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            :metadata_json, :tags_json, :schema_version
        )
        ON CONFLICT(node_id) DO UPDATE SET
            node_type      = excluded.node_type,
            label          = excluded.label,
            metadata_json  = excluded.metadata_json,
            tags_json      = excluded.tags_json,
            schema_version = excluded.schema_version
        """
        params = {
            "node_id": node.node_id,
            "node_type": node.node_type,
            "label": node.label,
            "created_at": node.created_at,
            "metadata_json": json.dumps(node.metadata, sort_keys=True),
            "tags_json": json.dumps(list(node.tags), sort_keys=True),
            "schema_version": node.schema_version,
        }
        with self._store.transaction():
            existed = (
                self._store.execute(
                    "SELECT 1 FROM graph_nodes WHERE node_id = ?", (node.node_id,)
                ).fetchone()
                is not None
            )
            self._store.execute(sql, params)
        return not existed

    def upsert_edge(self, edge: EdgeRow) -> bool:
        sql = """
        INSERT INTO graph_edges (
            edge_id, edge_type, from_node_id, to_node_id, weight,
            metadata_json, created_at, schema_version
        ) VALUES (
            :edge_id, :edge_type, :from_node_id, :to_node_id, :weight,
            :metadata_json,
            COALESCE(:created_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            :schema_version
        )
        ON CONFLICT(edge_id) DO UPDATE SET
            edge_type      = excluded.edge_type,
            from_node_id   = excluded.from_node_id,
            to_node_id     = excluded.to_node_id,
            weight         = excluded.weight,
            metadata_json  = excluded.metadata_json,
            schema_version = excluded.schema_version
        ON CONFLICT(edge_type, from_node_id, to_node_id) DO UPDATE SET
            weight         = excluded.weight,
            metadata_json  = excluded.metadata_json,
            schema_version = excluded.schema_version
        """
        # Note: SQLite's UPSERT only fires the FIRST ON CONFLICT clause
        # that matches. The edge_id conflict is the primary path; the
        # secondary (edge_type, from, to) match is rare but possible
        # when callers reuse a different edge_id for the same logical
        # edge. The above statement handles both by chaining two
        # ON CONFLICT clauses — but ON CONFLICT can only target the
        # primary key in a single statement. So we handle the
        # secondary conflict by collapsing to a manual UPDATE in the
        # rare case the primary key does not collide but the
        # (type, from, to) triple does.
        params = {
            "edge_id": edge.edge_id,
            "edge_type": edge.edge_type,
            "from_node_id": edge.from_node_id,
            "to_node_id": edge.to_node_id,
            "weight": edge.weight,
            "metadata_json": json.dumps(edge.metadata, sort_keys=True),
            "created_at": edge.created_at,
            "schema_version": edge.schema_version,
        }
        with self._store.transaction():
            existing_by_id = self._store.execute(
                "SELECT edge_id FROM graph_edges WHERE edge_id = ?",
                (edge.edge_id,),
            ).fetchone()
            if existing_by_id is not None:
                self._store.execute(
                    """
                    UPDATE graph_edges SET
                        edge_type = :edge_type,
                        from_node_id = :from_node_id,
                        to_node_id = :to_node_id,
                        weight = :weight,
                        metadata_json = :metadata_json,
                        schema_version = :schema_version
                    WHERE edge_id = :edge_id
                    """,
                    params,
                )
                return False
            existing_by_triple = self._store.execute(
                """
                SELECT edge_id FROM graph_edges
                WHERE edge_type = ? AND from_node_id = ? AND to_node_id = ?
                """,
                (edge.edge_type, edge.from_node_id, edge.to_node_id),
            ).fetchone()
            if existing_by_triple is not None:
                # Fold into the existing row.
                self._store.execute(
                    """
                    UPDATE graph_edges SET
                        weight = :weight,
                        metadata_json = :metadata_json,
                        schema_version = :schema_version
                    WHERE edge_type = :edge_type
                      AND from_node_id = :from_node_id
                      AND to_node_id = :to_node_id
                    """,
                    params,
                )
                return False
            # Fresh insert.
            self._store.execute(
                """
                INSERT INTO graph_edges (
                    edge_id, edge_type, from_node_id, to_node_id, weight,
                    metadata_json, created_at, schema_version
                ) VALUES (
                    :edge_id, :edge_type, :from_node_id, :to_node_id, :weight,
                    :metadata_json,
                    COALESCE(:created_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    :schema_version
                )
                """,
                params,
            )
            return True

    # ----- reads -----------------------------------------------------------

    def get_node(self, node_id: str) -> NodeRow | None:
        row = self._store.execute(
            "SELECT * FROM graph_nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        return _node_row(row) if row is not None else None

    def get_edge(self, edge_id: str) -> EdgeRow | None:
        row = self._store.execute(
            "SELECT * FROM graph_edges WHERE edge_id = ?", (edge_id,)
        ).fetchone()
        return _edge_row(row) if row is not None else None

    def list_nodes(
        self,
        node_type: str | None = None,
        limit: int = 10000,
    ) -> list[NodeRow]:
        if node_type is None:
            rows = self._store.execute(
                "SELECT * FROM graph_nodes ORDER BY node_id LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            rows = self._store.execute(
                "SELECT * FROM graph_nodes WHERE node_type = ? "
                "ORDER BY node_id LIMIT ?",
                (node_type, int(limit)),
            ).fetchall()
        return [_node_row(r) for r in rows]

    def list_edges(
        self,
        edge_type: str | None = None,
        from_node_id: str | None = None,
        to_node_id: str | None = None,
        limit: int = 10000,
    ) -> list[EdgeRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if edge_type is not None:
            clauses.append("edge_type = ?")
            params.append(edge_type)
        if from_node_id is not None:
            clauses.append("from_node_id = ?")
            params.append(from_node_id)
        if to_node_id is not None:
            clauses.append("to_node_id = ?")
            params.append(to_node_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM graph_edges {where} ORDER BY edge_id LIMIT ?"
        params.append(int(limit))
        rows = self._store.execute(sql, tuple(params)).fetchall()
        return [_edge_row(r) for r in rows]

    def node_count(self) -> int:
        cur = self._store.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()
        return int(cur[0]) if cur is not None else 0

    def edge_count(self) -> int:
        cur = self._store.execute("SELECT COUNT(*) FROM graph_edges").fetchone()
        return int(cur[0]) if cur is not None else 0

    def clear(self) -> None:
        """Wipe all nodes/edges. Intended for tests."""
        with self._store.transaction():
            self._store.execute("DELETE FROM graph_edges")
            self._store.execute("DELETE FROM graph_nodes")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _node_row(row: Any) -> NodeRow:
    return NodeRow(
        node_id=str(row["node_id"]),
        node_type=str(row["node_type"]),
        label=str(row["label"]),
        created_at=str(row["created_at"]),
        metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        tags=list(json.loads(row["tags_json"])) if row["tags_json"] else [],
        schema_version=str(row["schema_version"]),
    )


def _edge_row(row: Any) -> EdgeRow:
    return EdgeRow(
        edge_id=str(row["edge_id"]),
        edge_type=str(row["edge_type"]),
        from_node_id=str(row["from_node_id"]),
        to_node_id=str(row["to_node_id"]),
        weight=None if row["weight"] is None else float(row["weight"]),
        metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        created_at=str(row["created_at"]),
        schema_version=str(row["schema_version"]),
    )


__all__ = ["NodeRow", "EdgeRow", "GraphRepository"]
