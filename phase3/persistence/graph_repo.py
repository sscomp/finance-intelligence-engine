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

Dialect notes (Phase 6.3)
-------------------------
SQL here uses ``%s`` placeholders and positional params only, so the
repo runs identically on :class:`SQLiteStore` and
:class:`PostgresStore`. All ``created_at`` stamps come from
:func:`phase3.persistence.timeutil.utc_now_iso` (or the caller's
explicit value) — no ``strftime`` in the statement text. Note both
backends only let ``ON CONFLICT`` target the primary key, so the
historical dual-clause edge UPSERT (dead SQL) is replaced by an
explicit check-then-insert sequence that folds on either key.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from phase3.persistence.sqlite import SQLiteStore
from phase3.persistence.timeutil import utc_now_iso


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


def _created_at(value: str | None) -> str:
    """Normalize a caller-supplied ``created_at`` (''/None → now)."""
    return value if value else utc_now_iso()


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
            %s, %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT(node_id) DO UPDATE SET
            node_type      = excluded.node_type,
            label          = excluded.label,
            metadata_json  = excluded.metadata_json,
            tags_json      = excluded.tags_json,
            schema_version = excluded.schema_version
        """
        params = (
            node.node_id,
            node.node_type,
            node.label,
            _created_at(node.created_at),
            json.dumps(node.metadata, sort_keys=True),
            json.dumps(list(node.tags), sort_keys=True),
            node.schema_version,
        )
        with self._store.transaction():
            existed = (
                self._store.execute(
                    "SELECT 1 FROM graph_nodes WHERE node_id = %s",
                    (node.node_id,),
                ).fetchone()
                is not None
            )
            self._store.execute(sql, params)
        return not existed

    def upsert_edge(self, edge: EdgeRow) -> bool:
        params = (
            edge.edge_id,
            edge.edge_type,
            edge.from_node_id,
            edge.to_node_id,
            edge.weight,
            json.dumps(edge.metadata, sort_keys=True),
            _created_at(edge.created_at),
            edge.schema_version,
        )
        with self._store.transaction():
            existing_by_id = self._store.execute(
                "SELECT edge_id FROM graph_edges WHERE edge_id = %s",
                (edge.edge_id,),
            ).fetchone()
            if existing_by_id is not None:
                self._store.execute(
                    """
                    UPDATE graph_edges SET
                        edge_type = %s,
                        from_node_id = %s,
                        to_node_id = %s,
                        weight = %s,
                        metadata_json = %s,
                        schema_version = %s
                    WHERE edge_id = %s
                    """,
                    (
                        edge.edge_type,
                        edge.from_node_id,
                        edge.to_node_id,
                        edge.weight,
                        json.dumps(edge.metadata, sort_keys=True),
                        edge.schema_version,
                        edge.edge_id,
                    ),
                )
                return False
            existing_by_triple = self._store.execute(
                """
                SELECT edge_id FROM graph_edges
                WHERE edge_type = %s AND from_node_id = %s AND to_node_id = %s
                """,
                (edge.edge_type, edge.from_node_id, edge.to_node_id),
            ).fetchone()
            if existing_by_triple is not None:
                # Fold into the existing row: keep the existing edge_id
                # (it is content-derived from the triple) and update
                # only the mutable columns.
                self._store.execute(
                    """
                    UPDATE graph_edges SET
                        weight = %s,
                        metadata_json = %s,
                        schema_version = %s
                    WHERE edge_type = %s
                      AND from_node_id = %s
                      AND to_node_id = %s
                    """,
                    (
                        edge.weight,
                        json.dumps(edge.metadata, sort_keys=True),
                        edge.schema_version,
                        edge.edge_type,
                        edge.from_node_id,
                        edge.to_node_id,
                    ),
                )
                return False
            # Fresh insert.
            self._store.execute(
                """
                INSERT INTO graph_edges (
                    edge_id, edge_type, from_node_id, to_node_id, weight,
                    metadata_json, created_at, schema_version
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                """,
                params,
            )
            return True

    # ----- reads -----------------------------------------------------------

    def get_node(self, node_id: str) -> NodeRow | None:
        row = self._store.execute(
            "SELECT * FROM graph_nodes WHERE node_id = %s", (node_id,)
        ).fetchone()
        return _node_row(row) if row is not None else None

    def get_edge(self, edge_id: str) -> EdgeRow | None:
        row = self._store.execute(
            "SELECT * FROM graph_edges WHERE edge_id = %s", (edge_id,)
        ).fetchone()
        return _edge_row(row) if row is not None else None

    def list_nodes(
        self,
        node_type: str | None = None,
        limit: int = 10000,
    ) -> list[NodeRow]:
        if node_type is None:
            rows = self._store.execute(
                "SELECT * FROM graph_nodes ORDER BY node_id LIMIT %s",
                (int(limit),),
            ).fetchall()
        else:
            rows = self._store.execute(
                "SELECT * FROM graph_nodes WHERE node_type = %s "
                "ORDER BY node_id LIMIT %s",
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
            clauses.append("edge_type = %s")
            params.append(edge_type)
        if from_node_id is not None:
            clauses.append("from_node_id = %s")
            params.append(from_node_id)
        if to_node_id is not None:
            clauses.append("to_node_id = %s")
            params.append(to_node_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM graph_edges {where} ORDER BY edge_id LIMIT %s"
        params.append(int(limit))
        rows = self._store.execute(sql, tuple(params)).fetchall()
        return [_edge_row(r) for r in rows]

    def fetch_edges_by_nodes(
        self,
        node_ids: Sequence[str],
        *,
        direction: str = "both",
        edge_types: Sequence[str] | None = None,
    ) -> list[EdgeRow]:
        """Batched edge fetch for BFS frontiers.

        Returns every edge with ``from_node_id`` or ``to_node_id`` in
        ``node_ids``, ordered by ``edge_id`` (identical ordering to a
        per-node ``list_edges`` union). This is the seam the
        :mod:`phase3.graph.optimization` batched lookups use instead
        of digging at the raw driver connection.
        """
        if not node_ids:
            return []
        node_ph = ",".join("%s" for _ in node_ids)

        def _fetch(col: str) -> list[Any]:
            params: list[Any] = list(node_ids)
            if edge_types is not None:
                type_ph = ",".join("%s" for _ in edge_types)
                sql = (
                    f"SELECT * FROM graph_edges "
                    f"WHERE {col} IN ({node_ph}) "
                    f"AND edge_type IN ({type_ph}) "
                    f"ORDER BY edge_id"
                )
                params.extend(edge_types)
            else:
                sql = (
                    f"SELECT * FROM graph_edges "
                    f"WHERE {col} IN ({node_ph}) "
                    f"ORDER BY edge_id"
                )
            return self._store.execute(sql, tuple(params)).fetchall()

        results: list[Any] = []
        if direction in ("from", "both"):
            results.extend(_fetch("from_node_id"))
        if direction in ("to", "both"):
            results.extend(_fetch("to_node_id"))
        return [_edge_row(r) for r in results]

    def fetch_nodes_by_ids(self, node_ids: Sequence[str]) -> list[NodeRow]:
        """Batched node fetch; missing ids are simply absent."""
        if not node_ids:
            return []
        placeholders = ",".join("%s" for _ in node_ids)
        rows = self._store.execute(
            f"SELECT * FROM graph_nodes WHERE node_id IN ({placeholders}) "
            "ORDER BY node_id",
            tuple(node_ids),
        ).fetchall()
        return [_node_row(r) for r in rows]

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