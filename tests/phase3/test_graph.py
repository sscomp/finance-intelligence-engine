"""Tests for Phase 3A research graph: in-memory store, traversal, evidence.

Coverage:
  - Node / edge upsert idempotency (re-insert = no-op).
  - BFS traversal in all 3 directions.
  - Shortest path (unweighted BFS).
  - Disconnected path returns None.
  - Evidence tracer follows upstream edges to data-source leaves.
"""
from __future__ import annotations

import unittest

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    make_graph_edge_id,
)
from phase3.graph.evidence_tracer import (
    EVIDENCE_UPSTREAM_EDGE_TYPES,
    EvidenceTracer,
)
from phase3.graph.in_memory_store import GraphStore
from phase3.graph.traversal import bfs, shortest_path


def _node(nid: str, ntype: NodeType, label: str = "") -> GraphNode:
    return GraphNode(node_id=nid, node_type=ntype, label=label or nid)


def _edge(etype: EdgeType, src: str, dst: str, weight: float | None = None) -> GraphEdge:
    return GraphEdge(
        edge_id=make_graph_edge_id(etype, src, dst),
        edge_type=etype, from_node_id=src, to_node_id=dst, weight=weight,
    )


# ---------- in_memory_store: node/edge upsert ----------

class TestGraphNodeUpsert(unittest.TestCase):
    def test_add_node_new_returns_true(self):
        s = GraphStore()
        self.assertTrue(s.add_node(_node("company:2330", NodeType.COMPANY, "TSMC")))

    def test_add_node_duplicate_returns_false(self):
        s = GraphStore()
        s.add_node(_node("company:2330", NodeType.COMPANY, "TSMC"))
        # Same node_id → upsert → False
        self.assertFalse(s.add_node(_node("company:2330", NodeType.COMPANY, "TSMC")))

    def test_add_node_duplicate_replaces(self):
        s = GraphStore()
        s.add_node(_node("company:2330", NodeType.COMPANY, "TSMC"))
        # Re-insert with different label → still upsert (idempotent on id)
        s.add_node(_node("company:2330", NodeType.COMPANY, "TSMC-new"))
        n = s.get_node("company:2330")
        self.assertEqual(n.label, "TSMC-new")
        self.assertEqual(s.node_count(), 1)

    def test_get_node_missing_returns_none(self):
        s = GraphStore()
        self.assertIsNone(s.get_node("missing"))

    def test_has_node(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        self.assertTrue(s.has_node("a"))
        self.assertFalse(s.has_node("b"))

    def test_query_nodes_by_type(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.COMPANY))
        s.add_node(_node("c", NodeType.INDUSTRY))
        companies = s.query_nodes(node_type=NodeType.COMPANY)
        self.assertEqual(len(companies), 2)

    def test_query_nodes_by_tag(self):
        s = GraphStore()
        s.add_node(GraphNode(
            node_id="a", node_type=NodeType.COMPANY, label="A",
            tags=["tech", "twse"],
        ))
        s.add_node(GraphNode(
            node_id="b", node_type=NodeType.COMPANY, label="B",
            tags=["finance"],
        ))
        self.assertEqual(len(s.query_nodes(tags=["tech"])), 1)
        self.assertEqual(len(s.query_nodes(tags=["twse"])), 1)
        self.assertEqual(len(s.query_nodes(tags=["nonexistent"])), 0)

    def test_node_count(self):
        s = GraphStore()
        self.assertEqual(s.node_count(), 0)
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        self.assertEqual(s.node_count(), 2)


class TestGraphEdgeUpsert(unittest.TestCase):
    def test_add_edge_new(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        self.assertTrue(
            s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        )

    def test_add_edge_idempotent(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        e1 = _edge(EdgeType.MEMBER_OF, "a", "b")
        e2 = _edge(EdgeType.MEMBER_OF, "a", "b")
        s.add_edge(e1)
        # Same canonical id → not new
        self.assertFalse(s.add_edge(e2))
        self.assertEqual(s.edge_count(), 1)

    def test_get_edge(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        e = _edge(EdgeType.MEMBER_OF, "a", "b")
        s.add_edge(e)
        eid = make_graph_edge_id(EdgeType.MEMBER_OF, "a", "b")
        self.assertIsNotNone(s.get_edge(eid))

    def test_edges_from(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        s.add_node(_node("c", NodeType.INDUSTRY))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "c"))
        self.assertEqual(len(s.edges_from("a")), 2)
        self.assertEqual(len(s.edges_from("b")), 0)

    def test_edges_to(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        self.assertEqual(len(s.edges_to("b")), 1)
        self.assertEqual(len(s.edges_to("a")), 0)

    def test_get_neighbors_out(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        neighbors = s.get_neighbors("a", direction="out")
        self.assertEqual(len(neighbors), 1)
        self.assertEqual(neighbors[0][0].node_id, "b")

    def test_get_neighbors_in(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        neighbors = s.get_neighbors("b", direction="in")
        self.assertEqual(len(neighbors), 1)
        self.assertEqual(neighbors[0][0].node_id, "a")

    def test_get_neighbors_both(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        s.add_node(_node("c", NodeType.MACRO_FACTOR))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        s.add_edge(_edge(EdgeType.BELONGS_TO, "b", "c"))
        # From "b": out to c, in from a
        neighbors = s.get_neighbors("b", direction="both")
        self.assertEqual(len(neighbors), 2)
        ids = {n[0].node_id for n in neighbors}
        self.assertEqual(ids, {"a", "c"})

    def test_get_neighbors_with_edge_type_filter(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.INDUSTRY))
        s.add_node(_node("c", NodeType.MACRO_FACTOR))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        s.add_edge(_edge(EdgeType.BELONGS_TO, "b", "c"))
        # From b going out, only BELONGS_TO → c
        neighbors = s.get_neighbors("b", edge_types=[EdgeType.BELONGS_TO], direction="out")
        self.assertEqual(len(neighbors), 1)
        self.assertEqual(neighbors[0][0].node_id, "c")

    def test_get_neighbors_missing_node_returns_empty(self):
        s = GraphStore()
        self.assertEqual(s.get_neighbors("missing"), [])

    def test_stats(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("b", NodeType.COMPANY))
        s.add_node(_node("c", NodeType.INDUSTRY))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "c"))
        st = s.stats()
        self.assertEqual(st["total_nodes"], 3)
        self.assertEqual(st["total_edges"], 2)
        self.assertEqual(st["nodes_by_type"].get("company"), 2)
        self.assertEqual(st["nodes_by_type"].get("industry"), 1)
        self.assertEqual(st["edges_by_type"].get("member_of"), 2)


# ---------- BFS ----------

class TestBFS(unittest.TestCase):
    def _build_line(self) -> GraphStore:
        # a -> b -> c -> d -> e
        s = GraphStore()
        for x in "abcde":
            s.add_node(_node(x, NodeType.COMPANY, x))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "b", "c"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "c", "d"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "d", "e"))
        return s

    def test_bfs_start_missing_returns_empty(self):
        s = GraphStore()
        self.assertEqual(bfs(s, "missing"), [])

    def test_bfs_max_depth_1(self):
        s = self._build_line()
        result = bfs(s, "a", max_depth=1)
        ids = [n.node_id for n in result]
        self.assertEqual(ids, ["b"])

    def test_bfs_max_depth_2(self):
        s = self._build_line()
        result = bfs(s, "a", max_depth=2)
        ids = sorted(n.node_id for n in result)
        self.assertEqual(ids, ["b", "c"])

    def test_bfs_full_chain(self):
        s = self._build_line()
        result = bfs(s, "a", max_depth=10)
        ids = sorted(n.node_id for n in result)
        self.assertEqual(ids, ["b", "c", "d", "e"])

    def test_bfs_no_neighbors(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        self.assertEqual(bfs(s, "a"), [])

    def test_bfs_excludes_start(self):
        s = self._build_line()
        # From "c" with direction="both", the BFS visits d (out),
        # b (in), and then e (from d).
        result = bfs(s, "c", max_depth=10, direction="both")
        ids = [n.node_id for n in result]
        self.assertNotIn("c", ids)
        self.assertIn("d", ids)
        self.assertIn("b", ids)

    def test_bfs_handles_diamond(self):
        # a -> b, a -> c, b -> d, c -> d
        s = GraphStore()
        for x in "abcd":
            s.add_node(_node(x, NodeType.COMPANY, x))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "c"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "b", "d"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "c", "d"))
        result = bfs(s, "a", max_depth=10)
        ids = sorted(n.node_id for n in result)
        # d should appear once (first reach wins)
        self.assertEqual(ids, ["b", "c", "d"])
        self.assertEqual(len([x for x in ids if x == "d"]), 1)


# ---------- shortest_path ----------

class TestShortestPath(unittest.TestCase):
    def _build_line(self) -> GraphStore:
        s = GraphStore()
        for x in "abcde":
            s.add_node(_node(x, NodeType.COMPANY, x))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "b", "c"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "c", "d"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "d", "e"))
        return s

    def test_shortest_path_direct(self):
        s = self._build_line()
        path = shortest_path(s, "a", "b")
        self.assertEqual(path, ["a", "b"])

    def test_shortest_path_three_hops(self):
        s = self._build_line()
        path = shortest_path(s, "a", "d")
        self.assertEqual(path, ["a", "b", "c", "d"])

    def test_shortest_path_self(self):
        s = self._build_line()
        path = shortest_path(s, "a", "a")
        self.assertEqual(path, ["a"])

    def test_shortest_path_unreachable_returns_none(self):
        s = self._build_line()
        # e has no outgoing edges
        self.assertIsNone(shortest_path(s, "e", "a"))

    def test_shortest_path_disconnected_components(self):
        s = GraphStore()
        s.add_node(_node("a", NodeType.COMPANY))
        s.add_node(_node("x", NodeType.COMPANY))
        # a and x are not connected
        self.assertIsNone(shortest_path(s, "a", "x"))

    def test_shortest_path_missing_node(self):
        s = self._build_line()
        self.assertIsNone(shortest_path(s, "a", "missing"))
        self.assertIsNone(shortest_path(s, "missing", "a"))

    def test_shortest_path_diamond_picks_shorter(self):
        # a -> b -> d, a -> c -> d (both length 3, BFS picks one)
        s = GraphStore()
        for x in "abcd":
            s.add_node(_node(x, NodeType.COMPANY, x))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "b"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "a", "c"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "b", "d"))
        s.add_edge(_edge(EdgeType.MEMBER_OF, "c", "d"))
        path = shortest_path(s, "a", "d")
        # Path length is 3: a -> (b or c) -> d
        self.assertEqual(len(path), 3)
        self.assertEqual(path[0], "a")
        self.assertEqual(path[-1], "d")


# ---------- evidence_tracer ----------

class TestEvidenceTracer(unittest.TestCase):
    def _build_score_chain(self) -> GraphStore:
        """Build a small graph: source -> signal -> score.

        The score 'score:company:2330' is derived from a signal
        'sig:pe:2330' which in turn was generated from a source
        'src:yfinance:2330'.
        """
        s = GraphStore()
        s.add_node(_node("score:company:2330", NodeType.SCORE, "Score 2330"))
        s.add_node(_node("sig:pe:2330", NodeType.SIGNAL, "PE signal 2330"))
        s.add_node(_node("src:yfinance:2330", NodeType.SOURCE, "yfinance 2330"))
        s.add_edge(_edge(EdgeType.CONTRIBUTES_TO, "sig:pe:2330", "score:company:2330"))
        s.add_edge(_edge(EdgeType.GENERATED, "sig:pe:2330", "src:yfinance:2330"))
        return s

    def test_trace_upstream_finds_signal(self):
        s = self._build_score_chain()
        tracer = EvidenceTracer(s)
        chain = tracer.trace("score:company:2330", max_depth=5, direction="upstream")
        ids = [n.node_id for n in chain.nodes]
        # The upstream walk from score should reach the contributing signal
        self.assertIn("sig:pe:2330", ids)
        # Score itself is NOT in nodes (BFS excludes start)
        self.assertNotIn("score:company:2330", ids)

    def test_trace_upstream_with_specific_edge_types(self):
        s = self._build_score_chain()
        tracer = EvidenceTracer(s)
        # Only follow CONTRIBUTES_TO → only signal
        chain = tracer.trace(
            "score:company:2330", max_depth=5, direction="upstream",
            edge_types=[EdgeType.CONTRIBUTES_TO],
        )
        ids = [n.node_id for n in chain.nodes]
        self.assertIn("sig:pe:2330", ids)
        # Source shouldn't appear because the GENERATED edge type
        # was filtered out.
        self.assertNotIn("src:yfinance:2330", ids)

    def test_trace_signal_finds_source_via_generated_in(self):
        """BFS in-direction from a signal: a source that the signal
        points to is NOT upstream; we need a chain like src -> signal
        to make src upstream of signal."""
        s = GraphStore()
        s.add_node(_node("sig:pe:2330", NodeType.SIGNAL, "Signal"))
        s.add_node(_node("src:yfinance:2330", NodeType.SOURCE, "Source"))
        # Direction matters: src -> sig (incoming to sig) means src is
        # upstream of sig.
        s.add_edge(_edge(EdgeType.GENERATED, "src:yfinance:2330", "sig:pe:2330"))
        tracer = EvidenceTracer(s)
        chain = tracer.trace("sig:pe:2330", max_depth=5, direction="upstream")
        ids = [n.node_id for n in chain.nodes]
        self.assertIn("src:yfinance:2330", ids)
        self.assertIn("src:yfinance:2330", chain.leaf_node_ids)

    def test_trace_missing_node_returns_empty(self):
        s = GraphStore()
        tracer = EvidenceTracer(s)
        chain = tracer.trace("missing", max_depth=5)
        self.assertEqual(chain.nodes, [])
        self.assertEqual(chain.leaf_node_ids, [])

    def test_trace_downstream(self):
        # Build a graph where one score derives from another, with the
        # convention DERIVED_FROM: from = derived, to = ancestor.
        s = GraphStore()
        s.add_node(_node("score:derived:2330", NodeType.SCORE, "Derived"))
        s.add_node(_node("score:company:2330", NodeType.SCORE, "Company Score"))
        s.add_node(_node("report:2026-07-08", NodeType.REPORT, "Daily Report"))
        # derived -> company (the derived score derives from the company score)
        s.add_edge(_edge(EdgeType.DERIVED_FROM, "score:derived:2330",
                         "score:company:2330"))
        # company -> report (the company score is included in the report)
        s.add_edge(_edge(EdgeType.INCLUDES, "score:company:2330",
                         "report:2026-07-08"))
        tracer = EvidenceTracer(s)
        # From derived, downstream (out) → company. From company,
        # downstream (out) → report.
        chain_derived = tracer.trace(
            "score:derived:2330", max_depth=5, direction="downstream",
        )
        ids_d = [n.node_id for n in chain_derived.nodes]
        self.assertIn("score:company:2330", ids_d)

        chain_company = tracer.trace(
            "score:company:2330", max_depth=5, direction="downstream",
        )
        ids_c = [n.node_id for n in chain_company.nodes]
        self.assertIn("report:2026-07-08", ids_c)

    def test_evidence_chain_to_text(self):
        s = self._build_score_chain()
        tracer = EvidenceTracer(s)
        chain = tracer.trace("score:company:2330", max_depth=5)
        text = chain.to_text()
        # Should mention the start
        self.assertIn("score:company:2330", text)
        # Should mention at least one upstream node label
        self.assertIn("PE signal 2330", text)

    def test_evidence_upstream_edge_types_is_nonempty(self):
        self.assertGreater(len(EVIDENCE_UPSTREAM_EDGE_TYPES), 0)
        # Includes CONTRIBUTES_TO and GENERATED
        self.assertIn(EdgeType.CONTRIBUTES_TO, EVIDENCE_UPSTREAM_EDGE_TYPES)
        self.assertIn(EdgeType.GENERATED, EVIDENCE_UPSTREAM_EDGE_TYPES)


if __name__ == "__main__":
    unittest.main()
