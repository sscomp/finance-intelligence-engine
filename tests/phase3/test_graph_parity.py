"""Tests asserting that the SQLiteGraphStore and the in-memory
GraphStore yield equivalent results for a representative research
graph.

We do not require byte-equal internal state — the SQLite store
serialises datetimes as ISO strings, etc. — but for a fixed
construction sequence the observable behaviour (node count, edge
count, neighbor sets, stats) must match.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)
from phase3.graph.in_memory_store import GraphStore as InMemoryGraphStore
from phase3.graph.sqlite_store import SQLiteGraphStore


# A small representative research graph: 1 company, 1 industry, 1 macro
# factor, 2 signals, 1 score, 1 source, with the 7 edges connecting
# them.
SAMPLE_NODES: list[GraphNode] = [
    GraphNode(node_id="company:2330", node_type=NodeType.COMPANY,
              label="台積電",
              created_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
              tags=["focus"]),
    GraphNode(node_id="industry:半導體", node_type=NodeType.INDUSTRY,
              label="半導體",
              created_at=datetime(2026, 7, 8, tzinfo=timezone.utc)),
    GraphNode(node_id="macro:FED_RATE", node_type=NodeType.MACRO_FACTOR,
              label="Fed利率",
              created_at=datetime(2026, 7, 8, tzinfo=timezone.utc)),
    GraphNode(node_id="signal:2330:pe", node_type=NodeType.SIGNAL,
              label="PE=22",
              created_at=datetime(2026, 7, 8, tzinfo=timezone.utc)),
    GraphNode(node_id="signal:2330:roe", node_type=NodeType.SIGNAL,
              label="ROE=30%",
              created_at=datetime(2026, 7, 8, tzinfo=timezone.utc)),
    GraphNode(node_id="score:company:2330:2026-07-08", node_type=NodeType.SCORE,
              label="+34.04",
              created_at=datetime(2026, 7, 8, tzinfo=timezone.utc)),
    GraphNode(node_id="source:yfinance", node_type=NodeType.SOURCE,
              label="yfinance",
              created_at=datetime(2026, 7, 8, tzinfo=timezone.utc)),
]

SAMPLE_EDGES: list[tuple[EdgeType, str, str]] = [
    (EdgeType.MEMBER_OF, "company:2330", "industry:半導體"),
    (EdgeType.EXPOSED_TO, "company:2330", "macro:FED_RATE"),
    (EdgeType.CONTRIBUTES_TO, "signal:2330:pe",
     "score:company:2330:2026-07-08"),
    (EdgeType.CONTRIBUTES_TO, "signal:2330:roe",
     "score:company:2330:2026-07-08"),
    (EdgeType.GENERATED, "signal:2330:pe", "source:yfinance"),
    (EdgeType.GENERATED, "signal:2330:roe", "source:yfinance"),
    (EdgeType.CITES, "score:company:2330:2026-07-08", "source:yfinance"),
]


def _populate(store, nodes, edges) -> None:
    for n in nodes:
        store.add_node(n)
    for et, f, t in edges:
        # Use the edge id "_" — both stores restamp to canonical id.
        store.add_edge(GraphEdge(
            edge_id="_", edge_type=et, from_node_id=f, to_node_id=t,
            created_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        ))


class GraphParityTests(unittest.TestCase):
    """Both stores must give the same answers to the same queries."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.in_mem = InMemoryGraphStore()
        _populate(cls.in_mem, SAMPLE_NODES, SAMPLE_EDGES)

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        db = os.path.join(self._td.name, "parity.db")
        self.sql = SQLiteGraphStore(db)
        self.addCleanup(self.sql.close)
        _populate(self.sql, SAMPLE_NODES, SAMPLE_EDGES)

    def test_node_count_matches(self) -> None:
        self.assertEqual(
            self.in_mem.node_count(), self.sql.node_count(),
        )

    def test_edge_count_matches(self) -> None:
        self.assertEqual(
            self.in_mem.edge_count(), self.sql.edge_count(),
        )

    def test_stats_match(self) -> None:
        self.assertEqual(self.in_mem.stats(), self.sql.stats())

    def test_get_neighbors_out_matches(self) -> None:
        im_n = self.in_mem.get_neighbors(
            "company:2330", direction="out"
        )
        sql_n = self.sql.get_neighbors(
            "company:2330", direction="out"
        )
        # Compare on (node_id, edge_type) tuples — both stores produce
        # stable canonical edge_ids and equivalent node types.
        im_pairs = sorted((n.node_id, e.edge_type.value) for n, e in im_n)
        sql_pairs = sorted((n.node_id, e.edge_type.value) for n, e in sql_n)
        self.assertEqual(im_pairs, sql_pairs)

    def test_get_neighbors_in_matches(self) -> None:
        im_n = self.in_mem.get_neighbors("source:yfinance", direction="in")
        sql_n = self.sql.get_neighbors("source:yfinance", direction="in")
        im_pairs = sorted((n.node_id, e.edge_type.value) for n, e in im_n)
        sql_pairs = sorted((n.node_id, e.edge_type.value) for n, e in sql_n)
        self.assertEqual(im_pairs, sql_pairs)

    def test_get_neighbors_both_matches(self) -> None:
        im_n = self.in_mem.get_neighbors("signal:2330:pe", direction="both")
        sql_n = self.sql.get_neighbors("signal:2330:pe", direction="both")
        im_pairs = sorted((n.node_id, e.edge_type.value) for n, e in im_n)
        sql_pairs = sorted((n.node_id, e.edge_type.value) for n, e in sql_n)
        self.assertEqual(im_pairs, sql_pairs)

    def test_get_neighbors_filter_matches(self) -> None:
        im_n = self.in_mem.get_neighbors(
            "company:2330", edge_types=[EdgeType.MEMBER_OF],
            direction="out",
        )
        sql_n = self.sql.get_neighbors(
            "company:2330", edge_types=[EdgeType.MEMBER_OF],
            direction="out",
        )
        im_pairs = sorted((n.node_id, e.edge_type.value) for n, e in im_n)
        sql_pairs = sorted((n.node_id, e.edge_type.value) for n, e in sql_n)
        self.assertEqual(im_pairs, sql_pairs)

    def test_query_nodes_by_type_matches(self) -> None:
        im = self.in_mem.query_nodes(node_type=NodeType.SIGNAL)
        sql = self.sql.query_nodes(node_type=NodeType.SIGNAL)
        self.assertEqual(
            sorted(n.node_id for n in im),
            sorted(n.node_id for n in sql),
        )

    def test_query_nodes_by_tags_matches(self) -> None:
        im = self.in_mem.query_nodes(tags=["focus"])
        sql = self.sql.query_nodes(tags=["focus"])
        self.assertEqual(
            sorted(n.node_id for n in im),
            sorted(n.node_id for n in sql),
        )

    def test_missing_node_returns_empty_both(self) -> None:
        self.assertEqual(
            self.in_mem.get_neighbors("nope"),
            self.sql.get_neighbors("nope"),
        )

    def test_get_node_round_trip(self) -> None:
        im = self.in_mem.get_node("company:2330")
        sql = self.sql.get_node("company:2330")
        self.assertIsNotNone(im)
        self.assertIsNotNone(sql)
        assert im is not None and sql is not None
        self.assertEqual(im.node_id, sql.node_id)
        self.assertEqual(im.node_type, sql.node_type)
        self.assertEqual(im.label, sql.label)
        self.assertEqual(im.tags, sql.tags)


if __name__ == "__main__":
    unittest.main()
