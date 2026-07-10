"""Tests for Phase 3B Task 4 Run 4A — Blast Radius query.

Authoritative dependency semantics (per Run 4A brief):
- GENERATED (signal -> source): source is upstream provenance of
  signal. From a Source, downstream = its Signals.
- CONTRIBUTES_TO (signal -> score): score depends on signal. From
  a Signal, downstream = its Scores.
- INFLUENCES (score -> score): downstream score impact. From an
  upstream score, downstream = the scores it influences.
- CITES and REFERS_TO are NOT downstream dependency edges (they
  are attribution / reference, not data dependency).

Coverage matrix (per Run 4A brief, 12 cases):
  1. Source -> Signal -> Score
  2. Signal -> Score
  3. Score -> downstream Score through INFLUENCES
  4. Macro score -> Industry score -> Company score chain
  5. CITES ignored for dependency traversal
  6. REFERS_TO ignored for dependency traversal
  7. Mixed edge directions
  8. Cycle protection
  9. Deterministic ordering
 10. max_depth
 11. max_nodes truncation
 12. InMemory / SQLite parity

Plus auxiliary coverage:
  - Policy table sanity (size, completeness, brief conformance)
  - Missing start node
  - Leaf node with no consumers
  - edge_types filter
  - visited_edges
  - impact_counts tally
  - layered view
"""
from __future__ import annotations

import json
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
from phase3.graph.blast_radius import (
    BLAST_DOWNSTREAM_SIDE,
    BlastRadiusResult,
    compute_blast_radius,
)
from phase3.graph.in_memory_store import GraphStore as InMemoryGraphStore
from phase3.graph.sqlite_store import SQLiteGraphStore


_TS = datetime(2026, 7, 10, tzinfo=timezone.utc)


def _node(nid: str, ntype: NodeType, label: str = "") -> GraphNode:
    return GraphNode(
        node_id=nid,
        node_type=ntype,
        label=label or nid,
        created_at=_TS,
    )


def _edge(etype: EdgeType, src: str, dst: str) -> GraphEdge:
    return GraphEdge(
        edge_id="_",  # store re-stamps to canonical id
        edge_type=etype,
        from_node_id=src,
        to_node_id=dst,
        created_at=_TS,
    )


def _populate(store, nodes, edges) -> None:
    for n in nodes:
        store.add_node(n)
    for et, f, t in edges:
        store.add_edge(_edge(et, f, t))


# ---------------------------------------------------------------------------
# 0. Policy table sanity — must align with brief's authoritative list
# ---------------------------------------------------------------------------


class TestBlastPolicyTable(unittest.TestCase):
    """The policy table must match the Run 4A brief exactly: 5 edges."""

    BRIEF_EDGES = frozenset({
        EdgeType.GENERATED,
        EdgeType.CONTRIBUTES_TO,
        EdgeType.INFLUENCES,
        EdgeType.DERIVED_FROM,
        EdgeType.INCLUDES,
    })

    def test_policy_has_exactly_brief_set(self) -> None:
        self.assertEqual(
            frozenset(BLAST_DOWNSTREAM_SIDE.keys()),
            self.BRIEF_EDGES,
            "BLAST_DOWNSTREAM_SIDE must be exactly the brief's "
            "5 dependency edges",
        )

    def test_policy_excludes_attribution_edges(self) -> None:
        # These three must NOT be in the policy per the brief.
        for excluded in (
            EdgeType.CITES,
            EdgeType.REFERS_TO,
            EdgeType.EXPOSED_TO,
            EdgeType.MEMBER_OF,
            EdgeType.BELONGS_TO,
            EdgeType.REPORT_BY,
            EdgeType.WORKS_AT,
        ):
            self.assertNotIn(
                excluded,
                BLAST_DOWNSTREAM_SIDE,
                f"{excluded.value!r} must not be a Blast Radius dep edge",
            )

    def test_policy_values_are_from_or_to(self) -> None:
        for et, side in BLAST_DOWNSTREAM_SIDE.items():
            self.assertIn(
                side, ("from", "to"),
                f"edge {et.value!r} has invalid side {side!r}",
            )

    def test_generated_consumer_is_from(self) -> None:
        # Per brief: GENERATED is signal -> source; source is the
        # upstream data; signal is the consumer. So at a SOURCE
        # node, downstream (the dependent) is the SIGNAL — the
        # consumer is on the "from" side of the edge.
        self.assertEqual(BLAST_DOWNSTREAM_SIDE[EdgeType.GENERATED], "from")

    def test_contributes_to_consumer_is_to(self) -> None:
        # signal -> score; score is the consumer (depends on signal).
        self.assertEqual(
            BLAST_DOWNSTREAM_SIDE[EdgeType.CONTRIBUTES_TO], "to"
        )

    def test_influences_consumer_is_to(self) -> None:
        # score -> score (downstream); downstream score is the
        # consumer.
        self.assertEqual(
            BLAST_DOWNSTREAM_SIDE[EdgeType.INFLUENCES], "to"
        )

    def test_includes_consumer_is_from(self) -> None:
        # report -> score; report is the consumer (depends on the
        # score); consumer is on the "from" side of the edge.
        self.assertEqual(BLAST_DOWNSTREAM_SIDE[EdgeType.INCLUDES], "from")


# ---------------------------------------------------------------------------
# 1. Source -> Signal -> Score
# ---------------------------------------------------------------------------


class TestBlastSourceToSignalToScore(unittest.TestCase):
    """A source is the data origin. The brief's expected behavior:
    From Source, find dependent Signals by traversing GENERATED in
    reverse (Source -> Signal). Then from each Signal, find its
    dependent Scores via CONTRIBUTES_TO forward."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        # Source -> 2 signals (via GENERATED, traversed in reverse
        # at the source). Then 1 of those signals contributes to a
        # score.
        _populate(
            self.store,
            [
                _node("source:yfinance", NodeType.SOURCE),
                _node("signal:pe", NodeType.SIGNAL),
                _node("signal:roe", NodeType.SIGNAL),
                _node("score:company:2330:2026-07-10", NodeType.SCORE),
            ],
            [
                (EdgeType.GENERATED, "signal:pe", "source:yfinance"),
                (EdgeType.GENERATED, "signal:roe", "source:yfinance"),
                (EdgeType.CONTRIBUTES_TO, "signal:pe",
                 "score:company:2330:2026-07-10"),
            ],
        )

    def test_signals_at_depth_1_score_at_depth_2(self) -> None:
        result = compute_blast_radius(self.store, "source:yfinance")
        # depth 1: 2 signals.
        self.assertEqual(
            sorted(result.layered.get(1, [])),
            ["signal:pe", "signal:roe"],
        )
        # depth 2: 1 score (only pe contributes).
        self.assertEqual(
            result.layered.get(2, []),
            ["score:company:2330:2026-07-10"],
        )
        self.assertEqual(
            result.score_node_ids,
            ["score:company:2330:2026-07-10"],
        )
        self.assertEqual(
            sorted(result.signal_node_ids),
            ["signal:pe", "signal:roe"],
        )
        self.assertEqual(result.depth_reached, 2)
        self.assertFalse(result.truncated)

    def test_visited_edges_record_full_chain(self) -> None:
        result = compute_blast_radius(self.store, "source:yfinance")
        # 2 GENERATED edges (source -> signal:pe, source ->
        # signal:roe) and 1 CONTRIBUTES_TO edge (signal:pe -> score).
        edge_types_in_result = [
            e[0].rsplit(":", 1)[0] if ":" in e[0] else e[0]
            for e in result.visited_edges
        ]
        # Each edge_id has the format
        # "<edge_type>:<from_node_id>:<to_node_id>" in the store
        # implementation; we don't assert exact edge_ids, just that
        # we have 3 edges total.
        self.assertEqual(len(result.visited_edges), 3)


# ---------------------------------------------------------------------------
# 2. Signal -> Score
# ---------------------------------------------------------------------------


class TestBlastSignalToScore(unittest.TestCase):
    """From a Signal, find dependent Scores via CONTRIBUTES_TO."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("signal:pe", NodeType.SIGNAL),
                _node("score:company:2330:2026-07-10", NodeType.SCORE),
                _node("score:industry:semi:2026-07-10", NodeType.SCORE),
            ],
            [
                (EdgeType.CONTRIBUTES_TO, "signal:pe",
                 "score:company:2330:2026-07-10"),
                (EdgeType.CONTRIBUTES_TO, "signal:pe",
                 "score:industry:semi:2026-07-10"),
            ],
        )

    def test_two_scores_at_depth_1(self) -> None:
        result = compute_blast_radius(self.store, "signal:pe")
        # Both scores at depth 1.
        self.assertEqual(
            result.score_node_ids,
            [
                "score:company:2330:2026-07-10",
                "score:industry:semi:2026-07-10",
            ],
        )
        self.assertEqual(result.depth_reached, 1)
        self.assertFalse(result.truncated)


# ---------------------------------------------------------------------------
# 3. Score -> downstream Score through INFLUENCES
# ---------------------------------------------------------------------------


class TestBlastScoreToScoreInfluences(unittest.TestCase):
    """From a Score, find impacted downstream Scores via INFLUENCES."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:macro:global:2026-07-10", NodeType.SCORE),
                _node("score:industry:semi:2026-07-10", NodeType.SCORE),
                _node("score:industry:financials:2026-07-10",
                      NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:macro:global:2026-07-10",
                 "score:industry:semi:2026-07-10"),
                (EdgeType.INFLUENCES, "score:macro:global:2026-07-10",
                 "score:industry:financials:2026-07-10"),
            ],
        )

    def test_two_industries_at_depth_1(self) -> None:
        result = compute_blast_radius(
            self.store, "score:macro:global:2026-07-10"
        )
        # 2 industry scores at depth 1.
        self.assertEqual(len(result.score_node_ids), 2)
        self.assertIn("score:industry:semi:2026-07-10",
                      result.score_node_ids)
        self.assertIn("score:industry:financials:2026-07-10",
                      result.score_node_ids)
        self.assertEqual(result.depth_reached, 1)


# ---------------------------------------------------------------------------
# 4. Macro score -> Industry score -> Company score chain (via INFLUENCES)
# ---------------------------------------------------------------------------


class TestBlastMacroToIndustryToCompany(unittest.TestCase):
    """Brief Test 4: macro -> industry -> company score chain via
    INFLUENCES. The chain is SCORE -> SCORE -> SCORE. No Entity
    nodes in the result (MEMBER_OF / BELONGS_TO are not dependency
    edges per the brief)."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:macro:global:2026-07-10", NodeType.SCORE),
                _node("score:industry:semi:2026-07-10", NodeType.SCORE),
                _node("score:company:2330:2026-07-10", NodeType.SCORE),
                _node("score:company:2454:2026-07-10", NodeType.SCORE),
                _node("company:2330", NodeType.COMPANY),
                _node("industry:semi", NodeType.INDUSTRY),
                _node("macro:GLOBAL", NodeType.MACRO_FACTOR),
            ],
            [
                # Score chain: macro -> industry -> company.
                (EdgeType.INFLUENCES, "score:macro:global:2026-07-10",
                 "score:industry:semi:2026-07-10"),
                (EdgeType.INFLUENCES, "score:industry:semi:2026-07-10",
                 "score:company:2330:2026-07-10"),
                (EdgeType.INFLUENCES, "score:industry:semi:2026-07-10",
                 "score:company:2454:2026-07-10"),
                # Entity / classification edges (NOT dependency):
                (EdgeType.MEMBER_OF, "company:2330", "industry:semi"),
                (EdgeType.BELONGS_TO, "industry:semi", "macro:GLOBAL"),
            ],
        )

    def test_three_hop_score_chain(self) -> None:
        result = compute_blast_radius(
            self.store, "score:macro:global:2026-07-10", max_depth=5
        )
        # depth 1: industry score
        self.assertEqual(
            result.layered.get(1, []),
            ["score:industry:semi:2026-07-10"],
        )
        # depth 2: two company scores
        self.assertEqual(
            sorted(result.layered.get(2, [])),
            [
                "score:company:2330:2026-07-10",
                "score:company:2454:2026-07-10",
            ],
        )
        self.assertEqual(len(result.score_node_ids), 3)
        self.assertEqual(result.depth_reached, 2)

    def test_no_entity_nodes_in_result(self) -> None:
        """Per brief: Entity nodes should not be included unless a
        future explicit dependency edge is added. MEMBER_OF and
        BELONGS_TO are not dependency edges, so the company /
        industry / macro nodes must not appear."""
        result = compute_blast_radius(
            self.store, "score:macro:global:2026-07-10", max_depth=10
        )
        for entity_id in (
            "company:2330",
            "industry:semi",
            "macro:GLOBAL",
        ):
            self.assertNotIn(
                entity_id,
                result.visited_node_ids,
                f"{entity_id} should not be in blast radius "
                "(MEMBER_OF / BELONGS_TO are not dep edges)",
            )


# ---------------------------------------------------------------------------
# 5. CITES ignored for dependency traversal
# ---------------------------------------------------------------------------


class TestBlastCitesIgnored(unittest.TestCase):
    """Per brief: CITES is attribution, not a downstream dependency
    edge. A score that cites a source should NOT be in the blast
    radius of the source."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:rss", NodeType.SOURCE),
                _node("score:company:2330:2026-07-10", NodeType.SCORE),
            ],
            [
                # Score CITES the source. NOT a dep edge.
                (EdgeType.CITES, "score:company:2330:2026-07-10",
                 "source:rss"),
            ],
        )

    def test_cites_does_not_make_score_a_dependent(self) -> None:
        result = compute_blast_radius(self.store, "source:rss")
        # Source has no downstream consumers (the citing score is
        # NOT a dependent).
        self.assertEqual(result.visited_node_ids, [])
        self.assertEqual(result.score_node_ids, [])
        self.assertEqual(result.depth_reached, 0)

    def test_cites_does_not_make_source_a_dependent(self) -> None:
        # Reverse direction: from a score that cites a source, the
        # source is not downstream of the score.
        result = compute_blast_radius(
            self.store, "score:company:2330:2026-07-10"
        )
        self.assertNotIn("source:rss", result.visited_node_ids)


# ---------------------------------------------------------------------------
# 6. REFERS_TO ignored for dependency traversal
# ---------------------------------------------------------------------------


class TestBlastRefersToIgnored(unittest.TestCase):
    """Per brief: REFERS_TO is reference, not a dependency. A news
    node that refers to an entity is not a downstream dependent of
    that entity."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("company:2330", NodeType.COMPANY),
                _node("news:item1", NodeType.SIGNAL),
            ],
            [
                # News REFERS_TO the company. NOT a dep edge.
                (EdgeType.REFERS_TO, "news:item1", "company:2330"),
            ],
        )

    def test_refers_to_does_not_chain_company_to_news(self) -> None:
        result = compute_blast_radius(self.store, "company:2330")
        # The news signal that refers to the company is NOT
        # downstream of the company.
        self.assertNotIn("news:item1", result.visited_node_ids)
        self.assertEqual(result.visited_node_ids, [])


# ---------------------------------------------------------------------------
# 7. Mixed edge directions from one anchor
# ---------------------------------------------------------------------------


class TestBlastMixedEdgeDirections(unittest.TestCase):
    """The anchor node participates in multiple edge types.
    Dependency edges (CONTRIBUTES_TO, GENERATED) are followed;
    non-dependency edges (CITES, REFERS_TO, MEMBER_OF) are not."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:yfinance", NodeType.SOURCE),
                _node("signal:pe", NodeType.SIGNAL),
                _node("score:company:2330:2026-07-10", NodeType.SCORE),
                _node("report:daily:2026-07-10", NodeType.REPORT),
                _node("company:2330", NodeType.COMPANY),
            ],
            [
                # Dep edge: signal generated by source.
                (EdgeType.GENERATED, "signal:pe", "source:yfinance"),
                # Dep edge: signal contributes to score.
                (EdgeType.CONTRIBUTES_TO, "signal:pe",
                 "score:company:2330:2026-07-10"),
                # Dep edge: report includes score.
                (EdgeType.INCLUDES, "report:daily:2026-07-10",
                 "score:company:2330:2026-07-10"),
                # NOT dep edges:
                (EdgeType.CITES, "score:company:2330:2026-07-10",
                 "source:yfinance"),
                (EdgeType.REFERS_TO, "news:item1", "company:2330"),
            ],
        )

    def test_only_dep_edges_followed(self) -> None:
        result = compute_blast_radius(self.store, "source:yfinance")
        # Dep path: source -> signal:pe -> score:company:2330 ->
        # report:daily:2026-07-10
        self.assertIn("signal:pe", result.signal_node_ids)
        self.assertIn("score:company:2330:2026-07-10",
                      result.score_node_ids)
        self.assertIn("report:daily:2026-07-10", result.report_node_ids)
        # company is NOT reached (REFERS_TO and MEMBER_OF are not
        # followed). CITES doesn't matter since source is the start.
        self.assertNotIn("company:2330", result.visited_node_ids)
        self.assertEqual(result.depth_reached, 3)


# ---------------------------------------------------------------------------
# 8. Cycle protection
# ---------------------------------------------------------------------------


class TestBlastCycleProtection(unittest.TestCase):
    """A graph with a cycle must not infinite-loop. Each node is
    visited at most once."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("a:score", NodeType.SCORE),
                _node("b:score", NodeType.SCORE),
                _node("c:score", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "a:score", "b:score"),
                (EdgeType.INFLUENCES, "b:score", "c:score"),
                (EdgeType.INFLUENCES, "c:score", "a:score"),
            ],
        )

    def test_cycle_terminates_each_node_visited_once(self) -> None:
        # Graph: a -> b, b -> c, c -> a (INFLUENCES cycle). Start
        # at a:score. a is the start and not in any layer.
        # BFS: depth 1 from a = b. depth 2 from b = c. c -> a is
        # followed but a is already visited (cycle protection).
        # So visited = [b, c] = 2 nodes, not 3.
        result = compute_blast_radius(
            self.store, "a:score", max_depth=10, max_nodes=50
        )
        self.assertEqual(len(result.visited_node_ids), 2)
        self.assertEqual(
            set(result.visited_node_ids),
            {"b:score", "c:score"},
        )
        for layer_nodes in result.layered.values():
            self.assertNotIn("a:score", layer_nodes)
        self.assertEqual(result.layered[1], ["b:score"])
        self.assertEqual(result.layered[2], ["c:score"])
        self.assertFalse(result.truncated)


class TestBlastSelfLoop(unittest.TestCase):
    """A self-loop (a score that influences itself) must not
    infinite-loop."""

    def test_self_loop_terminates(self) -> None:
        store = InMemoryGraphStore()
        _populate(
            store,
            [_node("s", NodeType.SCORE)],
            [(EdgeType.INFLUENCES, "s", "s")],
        )
        result = compute_blast_radius(store, "s", max_depth=10)
        self.assertEqual(result.visited_node_ids, [])


# ---------------------------------------------------------------------------
# 9. Deterministic ordering
# ---------------------------------------------------------------------------


class TestBlastDeterminism(unittest.TestCase):
    """Two consecutive runs on the same graph must produce byte-equal
    results. Result must be stable under different insertion orders
    of the same edges."""

    def _build(self) -> InMemoryGraphStore:
        s = InMemoryGraphStore()
        _populate(
            s,
            [
                _node("source:yfinance", NodeType.SOURCE),
                _node("signal:A", NodeType.SIGNAL),
                _node("signal:B", NodeType.SIGNAL),
                _node("signal:C", NodeType.SIGNAL),
                _node("score:1", NodeType.SCORE),
                _node("score:2", NodeType.SCORE),
            ],
            [
                (EdgeType.GENERATED, "signal:A", "source:yfinance"),
                (EdgeType.GENERATED, "signal:B", "source:yfinance"),
                (EdgeType.GENERATED, "signal:C", "source:yfinance"),
                (EdgeType.CONTRIBUTES_TO, "signal:A", "score:1"),
                (EdgeType.CONTRIBUTES_TO, "signal:B", "score:2"),
            ],
        )
        return s

    def test_two_runs_byte_equal(self) -> None:
        a = compute_blast_radius(self._build(), "source:yfinance")
        b = compute_blast_radius(self._build(), "source:yfinance")
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_reversed_insertion_order_byte_equal(self) -> None:
        s = InMemoryGraphStore()
        # Insert in reverse order.
        _populate(
            s,
            [
                _node("score:2", NodeType.SCORE),
                _node("score:1", NodeType.SCORE),
                _node("signal:C", NodeType.SIGNAL),
                _node("signal:B", NodeType.SIGNAL),
                _node("signal:A", NodeType.SIGNAL),
                _node("source:yfinance", NodeType.SOURCE),
            ],
            [
                (EdgeType.CONTRIBUTES_TO, "signal:B", "score:2"),
                (EdgeType.CONTRIBUTES_TO, "signal:A", "score:1"),
                (EdgeType.GENERATED, "signal:C", "source:yfinance"),
                (EdgeType.GENERATED, "signal:B", "source:yfinance"),
                (EdgeType.GENERATED, "signal:A", "source:yfinance"),
            ],
        )
        a = compute_blast_radius(self._build(), "source:yfinance")
        b = compute_blast_radius(s, "source:yfinance")
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_dto_is_json_serializable(self) -> None:
        result = compute_blast_radius(self._build(), "source:yfinance")
        d = result.to_dict()
        # Round-trip through JSON.
        s = json.dumps(d)
        loaded = json.loads(s)
        self.assertEqual(loaded, d)


# ---------------------------------------------------------------------------
# 10. max_depth
# ---------------------------------------------------------------------------


class TestBlastMaxDepth(unittest.TestCase):
    """max_depth caps the number of BFS layers explored."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source", NodeType.SOURCE),
                _node("sig:1", NodeType.SIGNAL),
                _node("sig:2", NodeType.SIGNAL),
                _node("score:1", NodeType.SCORE),
                _node("score:2", NodeType.SCORE),
            ],
            [
                # Two signals generated by source.
                (EdgeType.GENERATED, "sig:1", "source"),
                (EdgeType.GENERATED, "sig:2", "source"),
                # Each signal contributes to a score.
                (EdgeType.CONTRIBUTES_TO, "sig:1", "score:1"),
                (EdgeType.CONTRIBUTES_TO, "sig:2", "score:2"),
            ],
        )

    def test_max_depth_1_only_signals(self) -> None:
        result = compute_blast_radius(self.store, "source", max_depth=1)
        # Only direct neighbors (signals) — scores at depth 2
        # are NOT visited.
        self.assertEqual(
            sorted(result.visited_node_ids),
            ["sig:1", "sig:2"],
        )
        self.assertEqual(result.score_node_ids, [])
        self.assertEqual(result.depth_reached, 1)

    def test_max_depth_2_includes_scores(self) -> None:
        result = compute_blast_radius(self.store, "source", max_depth=2)
        # depth 1: 2 signals; depth 2: 2 scores.
        self.assertEqual(
            sorted(result.visited_node_ids),
            ["score:1", "score:2", "sig:1", "sig:2"],
        )
        self.assertEqual(result.depth_reached, 2)
        # No more nodes — BFS ended via empty layer.
        self.assertFalse(result.truncated)


# ---------------------------------------------------------------------------
# 11. max_nodes truncation
# ---------------------------------------------------------------------------


class TestBlastMaxNodes(unittest.TestCase):
    """max_nodes caps the number of visited nodes."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source", NodeType.SOURCE),
                _node("sig:a", NodeType.SIGNAL),
                _node("sig:b", NodeType.SIGNAL),
                _node("sig:c", NodeType.SIGNAL),
                _node("sig:d", NodeType.SIGNAL),
            ],
            [
                (EdgeType.GENERATED, "sig:a", "source"),
                (EdgeType.GENERATED, "sig:b", "source"),
                (EdgeType.GENERATED, "sig:c", "source"),
                (EdgeType.GENERATED, "sig:d", "source"),
            ],
        )

    def test_max_nodes_1_truncates(self) -> None:
        result = compute_blast_radius(
            self.store, "source", max_depth=10, max_nodes=1
        )
        # Only the first discovered node (deterministic order).
        self.assertEqual(len(result.visited_node_ids), 1)
        self.assertTrue(result.truncated)

    def test_max_nodes_3_caps_to_3(self) -> None:
        result = compute_blast_radius(
            self.store, "source", max_depth=10, max_nodes=3
        )
        self.assertEqual(len(result.visited_node_ids), 3)
        self.assertTrue(result.truncated)


# ---------------------------------------------------------------------------
# 12. InMemory / SQLite parity
# ---------------------------------------------------------------------------


class TestBlastSQLiteParity(unittest.TestCase):
    """A representative graph traversed against both stores must
    yield identical BlastRadiusResult dicts."""

    def setUp(self) -> None:
        self.in_mem = InMemoryGraphStore()
        nodes = [
            _node("source:yfinance", NodeType.SOURCE),
            _node("signal:2330:pe", NodeType.SIGNAL),
            _node("signal:2330:roe", NodeType.SIGNAL),
            _node("score:company:2330:2026-07-10", NodeType.SCORE),
            _node("score:industry:semi:2026-07-10", NodeType.SCORE),
            _node("report:daily:2026-07-10", NodeType.REPORT),
        ]
        edges = [
            (EdgeType.GENERATED, "signal:2330:pe", "source:yfinance"),
            (EdgeType.GENERATED, "signal:2330:roe", "source:yfinance"),
            (EdgeType.CONTRIBUTES_TO, "signal:2330:pe",
             "score:company:2330:2026-07-10"),
            (EdgeType.CONTRIBUTES_TO, "signal:2330:roe",
             "score:industry:semi:2026-07-10"),
            (EdgeType.INFLUENCES, "score:industry:semi:2026-07-10",
             "score:company:2330:2026-07-10"),
            (EdgeType.INCLUDES, "report:daily:2026-07-10",
             "score:company:2330:2026-07-10"),
            # Non-dep edges — must NOT be traversed in either store.
            (EdgeType.CITES, "score:company:2330:2026-07-10",
             "source:yfinance"),
        ]
        _populate(self.in_mem, nodes, edges)

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        db = os.path.join(self._td.name, "parity.db")
        self.sql = SQLiteGraphStore(db)
        self.addCleanup(self.sql.close)
        _populate(self.sql, nodes, edges)

    def test_parity_from_source(self) -> None:
        a = compute_blast_radius(self.in_mem, "source:yfinance")
        b = compute_blast_radius(self.sql, "source:yfinance")
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_parity_from_signal(self) -> None:
        a = compute_blast_radius(self.in_mem, "signal:2330:pe")
        b = compute_blast_radius(self.sql, "signal:2330:pe")
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_parity_from_score(self) -> None:
        a = compute_blast_radius(
            self.in_mem, "score:industry:semi:2026-07-10"
        )
        b = compute_blast_radius(
            self.sql, "score:industry:semi:2026-07-10"
        )
        self.assertEqual(a.to_dict(), b.to_dict())


# ---------------------------------------------------------------------------
# Auxiliary: missing start, leaf, edge-types filter
# ---------------------------------------------------------------------------


class TestBlastAuxiliary(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("a", NodeType.SOURCE),
                _node("b", NodeType.SIGNAL),
                _node("c", NodeType.SCORE),
                _node("d", NodeType.REPORT),
            ],
            [
                (EdgeType.GENERATED, "b", "a"),
                (EdgeType.CONTRIBUTES_TO, "b", "c"),
                (EdgeType.INCLUDES, "d", "c"),
            ],
        )

    def test_missing_start_node(self) -> None:
        result = compute_blast_radius(self.store, "does:not:exist")
        self.assertEqual(result.visited_node_ids, [])
        self.assertEqual(result.score_node_ids, [])
        self.assertEqual(result.depth_reached, 0)
        self.assertFalse(result.truncated)
        self.assertTrue(any("not in graph" in w for w in result.warnings))

    def test_leaf_node_no_consumers(self) -> None:
        # 'd' is a report with no downstream consumers (no report
        # that includes d).
        result = compute_blast_radius(self.store, "d")
        self.assertEqual(result.visited_node_ids, [])

    def test_max_depth_caps_layered(self) -> None:
        result = compute_blast_radius(self.store, "a", max_depth=1)
        self.assertEqual(result.visited_node_ids, ["b"])
        self.assertEqual(result.depth_reached, 1)
        self.assertNotIn(2, result.layered)

    def test_max_nodes_caps_walk(self) -> None:
        result = compute_blast_radius(
            self.store, "a", max_depth=10, max_nodes=1
        )
        self.assertEqual(result.visited_node_ids, ["b"])
        self.assertTrue(result.truncated)

    def test_edge_type_filter(self) -> None:
        result = compute_blast_radius(
            self.store, "a", edge_types=[EdgeType.GENERATED]
        )
        self.assertEqual(result.visited_node_ids, ["b"])
        self.assertNotIn("c", result.visited_node_ids)

    def test_impact_counts_tally(self) -> None:
        result = compute_blast_radius(self.store, "a")
        self.assertEqual(result.impact_counts.get("signal"), 1)
        self.assertEqual(result.impact_counts.get("score"), 1)
        self.assertEqual(result.impact_counts.get("report"), 1)


# ---------------------------------------------------------------------------
# DTO contract
# ---------------------------------------------------------------------------


class TestBlastResultDTO(unittest.TestCase):
    """BlastRadiusResult must be a frozen, JSON-serializable DTO."""

    def test_dto_is_frozen(self) -> None:
        r = BlastRadiusResult(start="x")
        with self.assertRaises(Exception):
            r.start = "y"  # type: ignore[misc]

    def test_dto_to_dict_keys(self) -> None:
        r = BlastRadiusResult(start="x")
        d = r.to_dict()
        for k in (
            "start",
            "visited_node_ids",
            "score_node_ids",
            "signal_node_ids",
            "report_node_ids",
            "impact_counts",
            "layered",
            "visited_edges",
            "truncated",
            "depth_reached",
            "warnings",
        ):
            self.assertIn(k, d)

    def test_dto_no_entity_bucket_in_result(self) -> None:
        """Per brief: Entity nodes are not included in blast radius
        by default. The DTO therefore has NO entity_node_ids
        field — it is not a current concern of the result shape."""
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(BlastRadiusResult)}
        self.assertNotIn("entity_node_ids", field_names)


if __name__ == "__main__":
    unittest.main()
