"""Tests for phase3.graph.sqlite_store — SQLite-backed GraphStore.

These tests cover the Phase 3B Task 1 acceptance contract for the
SQLite graph: node and edge upsert (including the canonical edge id
restamping and the (type, from, to) uniqueness), neighbour
traversal in both directions, and stats.
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
    make_graph_edge_id,
)
from phase3.graph.sqlite_store import DEFAULT_DB_PATH, SQLiteGraphStore


def _node(nid: str, ntype: NodeType = NodeType.COMPANY, label: str = "x") -> GraphNode:
    return GraphNode(
        node_id=nid,
        node_type=ntype,
        label=label,
        created_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )


def _edge(etype: EdgeType, frm: str, to: str) -> GraphEdge:
    return GraphEdge(
        edge_id=make_graph_edge_id(etype, frm, to),
        edge_type=etype,
        from_node_id=frm,
        to_node_id=to,
        created_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )


class _GraphTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db_path = os.path.join(self._td.name, "g.db")
        self.store = SQLiteGraphStore(self.db_path)
        self.addCleanup(self.store.close)


class NodeTests(_GraphTestCase):
    def test_add_node_returns_true_for_new(self) -> None:
        self.assertTrue(self.store.add_node(_node("c:1")))

    def test_add_node_returns_false_for_existing(self) -> None:
        self.store.add_node(_node("c:1"))
        self.assertFalse(self.store.add_node(_node("c:1", label="other")))

    def test_add_node_updates_label_on_re_add(self) -> None:
        self.store.add_node(_node("c:1", label="A"))
        self.store.add_node(_node("c:1", label="B"))
        got = self.store.get_node("c:1")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.label, "B")

    def test_get_node_missing(self) -> None:
        self.assertIsNone(self.store.get_node("nope"))

    def test_has_node(self) -> None:
        self.store.add_node(_node("c:1"))
        self.assertTrue(self.store.has_node("c:1"))
        self.assertFalse(self.store.has_node("c:2"))

    def test_node_count(self) -> None:
        self.store.add_node(_node("c:1"))
        self.store.add_node(_node("c:2"))
        self.store.add_node(_node("i:1", NodeType.INDUSTRY, "半導體"))
        self.assertEqual(self.store.node_count(), 3)

    def test_query_nodes_by_type(self) -> None:
        self.store.add_node(_node("c:1"))
        self.store.add_node(_node("c:2"))
        self.store.add_node(_node("i:1", NodeType.INDUSTRY, "半導體"))
        companies = self.store.query_nodes(node_type=NodeType.COMPANY)
        self.assertEqual({n.node_id for n in companies}, {"c:1", "c:2"})

    def test_query_nodes_by_tags(self) -> None:
        n1 = _node("c:1")
        n1.tags.append("focus")
        n2 = _node("c:2")
        n2.tags.append("watch")
        self.store.add_node(n1)
        self.store.add_node(n2)
        out = self.store.query_nodes(tags=["focus"])
        self.assertEqual([n.node_id for n in out], ["c:1"])

    def test_query_nodes_tags_all_of(self) -> None:
        n1 = _node("c:1")
        n1.tags.extend(["a", "b"])
        n2 = _node("c:2")
        n2.tags.append("a")
        self.store.add_node(n1)
        self.store.add_node(n2)
        out = self.store.query_nodes(tags=["a", "b"])
        self.assertEqual([n.node_id for n in out], ["c:1"])


class EdgeTests(_GraphTestCase):
    def setUp(self) -> None:
        super().setUp()
        for nid in ("c:1", "c:2", "i:1"):
            self.store.add_node(_node(nid, NodeType.COMPANY if "c" in nid else NodeType.INDUSTRY, nid))

    def test_add_edge_returns_true_for_new(self) -> None:
        self.assertTrue(
            self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:1", "i:1"))
        )

    def test_add_edge_idempotent(self) -> None:
        self.assertTrue(
            self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:1", "i:1"))
        )
        self.assertFalse(
            self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:1", "i:1"))
        )

    def test_edge_id_is_canonical(self) -> None:
        canonical = make_graph_edge_id(EdgeType.MEMBER_OF, "c:1", "i:1")
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:1", "i:1"))
        got = self.store.get_edge(canonical)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.from_node_id, "c:1")
        self.assertEqual(got.to_node_id, "i:1")

    def test_edges_from(self) -> None:
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:1", "i:1"))
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:2", "i:1"))
        out = self.store.edges_from("c:1")
        self.assertEqual(len(out), 1)
        assert out
        self.assertEqual(out[0].from_node_id, "c:1")

    def test_edges_to(self) -> None:
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:1", "i:1"))
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:2", "i:1"))
        out = self.store.edges_to("i:1")
        self.assertEqual(len(out), 2)

    def test_edge_count(self) -> None:
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:1", "i:1"))
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:2", "i:1"))
        self.assertEqual(self.store.edge_count(), 2)


class NeighborTests(_GraphTestCase):
    def setUp(self) -> None:
        super().setUp()
        for nid in ("c:1", "c:2", "i:1", "m:1"):
            self.store.add_node(
                _node(nid, NodeType.INDUSTRY if "i" in nid else
                      NodeType.MACRO_FACTOR if "m" in nid else NodeType.COMPANY, nid)
            )
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:1", "i:1"))
        self.store.add_edge(_edge(EdgeType.EXPOSED_TO, "c:1", "m:1"))
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:2", "i:1"))

    def test_get_neighbors_out(self) -> None:
        out = self.store.get_neighbors("c:1", direction="out")
        # Two out-edges → two neighbors.
        self.assertEqual(len(out), 2)
        ids = {n.node_id for n, _ in out}
        self.assertEqual(ids, {"i:1", "m:1"})

    def test_get_neighbors_in(self) -> None:
        out = self.store.get_neighbors("i:1", direction="in")
        self.assertEqual(len(out), 2)
        ids = {n.node_id for n, _ in out}
        self.assertEqual(ids, {"c:1", "c:2"})

    def test_get_neighbors_both(self) -> None:
        out = self.store.get_neighbors("i:1", direction="both")
        self.assertEqual(len(out), 2)

    def test_get_neighbors_filter_by_edge_type(self) -> None:
        out = self.store.get_neighbors(
            "c:1", edge_types=[EdgeType.MEMBER_OF], direction="out"
        )
        self.assertEqual(len(out), 1)
        n, e = out[0]
        self.assertEqual(n.node_id, "i:1")
        self.assertEqual(e.edge_type, EdgeType.MEMBER_OF)

    def test_get_neighbors_missing_node_returns_empty(self) -> None:
        self.assertEqual(self.store.get_neighbors("nope"), [])


class StatsTests(_GraphTestCase):
    def test_stats_empty(self) -> None:
        self.assertEqual(
            self.store.stats(),
            {"total_nodes": 0, "total_edges": 0,
             "nodes_by_type": {}, "edges_by_type": {}},
        )

    def test_stats_counts_by_type(self) -> None:
        self.store.add_node(_node("c:1"))
        self.store.add_node(_node("c:2"))
        self.store.add_node(_node("i:1", NodeType.INDUSTRY, "半導體"))
        self.store.add_edge(_edge(EdgeType.MEMBER_OF, "c:1", "i:1"))
        stats = self.store.stats()
        self.assertEqual(stats["total_nodes"], 3)
        self.assertEqual(stats["total_edges"], 1)
        self.assertEqual(stats["nodes_by_type"], {"company": 2, "industry": 1})
        self.assertEqual(stats["edges_by_type"], {"member_of": 1})


class SQLiteGraphStoreInitTests(unittest.TestCase):
    def test_default_path_is_under_phase3_data(self) -> None:
        # Sanity: default path is NOT a production DB.
        self.assertTrue(DEFAULT_DB_PATH.endswith("intelligence.db"))
        self.assertNotIn("macro_history", DEFAULT_DB_PATH)

    def test_default_construction_creates_db(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "init.db")
            self.assertFalse(os.path.exists(db))
            with SQLiteGraphStore(db) as store:
                # Schema is applied.
                self.assertEqual(store.node_count(), 0)
            self.assertTrue(os.path.exists(db))

    def test_auto_migrate_false_does_not_create_schema(self) -> None:
        """Useful for tests that want to drive migration explicitly."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "nomig.db")
            with SQLiteGraphStore(db, auto_migrate=False) as store:
                with self.assertRaises(Exception):
                    # Tables don't exist yet.
                    store.add_node(_node("c:1"))


if __name__ == "__main__":
    unittest.main()
