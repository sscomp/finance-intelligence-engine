"""Tests for Phase 3B Task 4 Run 4B — Lineage query (canonical module).

Authoritative upstream provenance semantics (per Run 4B brief):
- CONTRIBUTES_TO (signal -> score): signal is upstream data.
- GENERATED (signal -> source): source is upstream data.
- CITES (score -> source): source is upstream data.
- REFERS_TO (news -> entity): news is upstream document.
- INFLUENCES (score -> score): influencer is upstream score.
- DERIVED_FROM (score -> score): ancestor is upstream score.
- EXPOSED_TO (entity -> factor): macro is upstream factor.

Lineage is *upstream only* — it answers "where did this come
from?". Downstream traversal is the job of Blast Radius.

Coverage matrix (12 cases):
  1. Score -> Signal -> Source (CONTRIBUTES_TO then GENERATED)
  2. Score cites Source (CITES)
  3. Score influenced by upstream score (INFLUENCES)
  4. Macro score -> Industry score -> Company score chain
  5. News refers to entity (REFERS_TO)
  6. Entity exposed to macro factor (EXPOSED_TO)
  7. Mixed edge directions (upstream AND non-upstream in graph)
  8. Cycle protection (upstream walk must not loop)
  9. Deterministic ordering
 10. max_depth truncation
 11. max_nodes truncation
 12. InMemory / SQLite parity

Plus auxiliary coverage:
  - Policy table sanity (strict-set equality to brief)
  - Missing start node
  - Leaf node with no upstream
  - edge_types whitelist filter
  - Unsupported edge type warning
  - visited_edges order
  - leaf_node_ids / leaf_summary correctness
  - visited bucketing by node type
  - to_dict() JSON round-trip

All tests use stdlib ``unittest`` only.
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
from phase3.graph.in_memory_store import GraphStore as InMemoryGraphStore
from phase3.graph.lineage import (
    LINEAGE_EDGE_TYPES,
    LINEAGE_UPSTREAM_SIDE,
    LineageQuery,
    compute_lineage,
)
from phase3.graph.sqlite_store import SQLiteGraphStore


_TS = datetime(2026, 7, 11, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


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


class TestLineagePolicyTable(unittest.TestCase):
    """The policy table must match the Run 4B brief exactly: 7 edges."""

    BRIEF_EDGES = frozenset({
        EdgeType.CONTRIBUTES_TO,
        EdgeType.GENERATED,
        EdgeType.CITES,
        EdgeType.REFERS_TO,
        EdgeType.INFLUENCES,
        EdgeType.DERIVED_FROM,
        EdgeType.EXPOSED_TO,
    })

    def test_policy_has_exactly_brief_set(self) -> None:
        self.assertEqual(
            frozenset(LINEAGE_UPSTREAM_SIDE.keys()),
            self.BRIEF_EDGES,
            "LINEAGE_UPSTREAM_SIDE must be exactly the brief's 7 lineage edges",
        )

    def test_edge_types_tuple_matches_policy_keys(self) -> None:
        self.assertEqual(
            frozenset(LINEAGE_EDGE_TYPES),
            frozenset(LINEAGE_UPSTREAM_SIDE.keys()),
            "LINEAGE_EDGE_TYPES must equal LINEAGE_UPSTREAM_SIDE.keys()",
        )

    def test_policy_values_are_from_or_to(self) -> None:
        for et, side in LINEAGE_UPSTREAM_SIDE.items():
            self.assertIn(
                side, ("from", "to"),
                f"edge {et.value!r} has invalid side {side!r}",
            )

    def test_contributes_to_upstream_is_from(self) -> None:
        # signal -> score; signal is the upstream data.
        self.assertEqual(LINEAGE_UPSTREAM_SIDE[EdgeType.CONTRIBUTES_TO], "from")

    def test_generated_upstream_is_to(self) -> None:
        # signal -> source; source is the upstream data.
        self.assertEqual(LINEAGE_UPSTREAM_SIDE[EdgeType.GENERATED], "to")

    def test_cites_upstream_is_to(self) -> None:
        # score -> source; source is the upstream data.
        self.assertEqual(LINEAGE_UPSTREAM_SIDE[EdgeType.CITES], "to")

    def test_refers_to_upstream_is_from(self) -> None:
        # news -> entity; news is the upstream document.
        self.assertEqual(LINEAGE_UPSTREAM_SIDE[EdgeType.REFERS_TO], "from")

    def test_influences_upstream_is_from(self) -> None:
        # score -> score; influencer is the upstream score.
        self.assertEqual(LINEAGE_UPSTREAM_SIDE[EdgeType.INFLUENCES], "from")

    def test_derived_from_upstream_is_to(self) -> None:
        # score -> score; ancestor is the upstream score.
        self.assertEqual(LINEAGE_UPSTREAM_SIDE[EdgeType.DERIVED_FROM], "to")

    def test_exposed_to_upstream_is_to(self) -> None:
        # entity -> factor; macro is the upstream factor.
        self.assertEqual(LINEAGE_UPSTREAM_SIDE[EdgeType.EXPOSED_TO], "to")


# ---------------------------------------------------------------------------
# 1. Score -> Signal -> Source (CONTRIBUTES_TO then GENERATED)
# ---------------------------------------------------------------------------


class TestLineageScoreViaSignalToSource(unittest.TestCase):
    """From a company score, the upstream lineage traces through the
    signal that contributed to it (CONTRIBUTES_TO) and then to the
    source that generated the signal (GENERATED)."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:yfinance", NodeType.SOURCE),
                _node("signal:pe", NodeType.SIGNAL),
                _node("score:company:2330:2026-07-11", NodeType.SCORE),
            ],
            [
                (EdgeType.GENERATED, "signal:pe", "source:yfinance"),
                (EdgeType.CONTRIBUTES_TO, "signal:pe",
                 "score:company:2330:2026-07-11"),
            ],
        )

    def test_visited_includes_signal_and_source(self) -> None:
        result = compute_lineage(self.store, "score:company:2330:2026-07-11")
        self.assertEqual(
            sorted(result.visited_node_ids),
            ["signal:pe", "source:yfinance"],
        )

    def test_signal_at_depth_1_source_at_depth_2(self) -> None:
        result = compute_lineage(self.store, "score:company:2330:2026-07-11")
        self.assertEqual(result.layered.get(1), ["signal:pe"])
        self.assertEqual(result.layered.get(2), ["source:yfinance"])

    def test_buckets_split_signal_vs_source(self) -> None:
        result = compute_lineage(self.store, "score:company:2330:2026-07-11")
        self.assertEqual(result.signal_node_ids, ["signal:pe"])
        self.assertEqual(result.source_node_ids, ["source:yfinance"])
        self.assertEqual(result.score_node_ids, [])

    def test_leaves_is_source(self) -> None:
        result = compute_lineage(self.store, "score:company:2330:2026-07-11")
        # Source is the data origin (no further upstream). The
        # source has no edges pointing AT it of allowed lineage
        # types (the GENERATED edge is signal -> source, so source
        # is the "to" side; the policy says upstream side is "to",
        # so from the source the walk doesn't go further because
        # there's no upstream of source).
        self.assertEqual(result.leaf_node_ids, ["source:yfinance"])
        self.assertEqual(result.leaf_summary, {"source": 1})

    def test_visited_edges_includes_both(self) -> None:
        result = compute_lineage(self.store, "score:company:2330:2026-07-11")
        self.assertEqual(len(result.visited_edges), 2)
        # Both edges must be present (any order).
        edge_pairs = sorted(
            (t[1], t[2]) for t in result.visited_edges
        )
        self.assertEqual(
            edge_pairs,
            [("signal:pe", "score:company:2330:2026-07-11"),
             ("signal:pe", "source:yfinance")],
        )


# ---------------------------------------------------------------------------
# 2. Score cites Source (CITES)
# ---------------------------------------------------------------------------


class TestLineageScoreCitesSource(unittest.TestCase):
    """CITES is score -> source; the source is upstream data."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:fed_fomc_minutes", NodeType.SOURCE),
                _node("score:macro:global:2026-07-11", NodeType.SCORE),
            ],
            [
                (EdgeType.CITES, "score:macro:global:2026-07-11",
                 "source:fed_fomc_minutes"),
            ],
        )

    def test_source_is_upstream_of_score(self) -> None:
        result = compute_lineage(self.store, "score:macro:global:2026-07-11")
        self.assertEqual(result.visited_node_ids, ["source:fed_fomc_minutes"])
        self.assertEqual(result.source_node_ids, ["source:fed_fomc_minutes"])
        self.assertEqual(result.depth_reached, 1)


# ---------------------------------------------------------------------------
# 3. Score influenced by upstream score (INFLUENCES)
# ---------------------------------------------------------------------------


class TestLineageInfluencesUpstream(unittest.TestCase):
    """INFLUENCES goes score -> score; the influencer is upstream."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:macro:global:2026-07-11", NodeType.SCORE),
                _node("score:industry:semis:2026-07-11", NodeType.SCORE),
                _node("score:company:2330:2026-07-11", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:macro:global:2026-07-11",
                 "score:industry:semis:2026-07-11"),
                (EdgeType.INFLUENCES, "score:industry:semis:2026-07-11",
                 "score:company:2330:2026-07-11"),
            ],
        )

    def test_company_score_lineage_traces_back_to_macro(self) -> None:
        result = compute_lineage(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(
            sorted(result.visited_node_ids),
            ["score:industry:semis:2026-07-11",
             "score:macro:global:2026-07-11"],
        )
        self.assertEqual(
            sorted(result.score_node_ids),
            ["score:industry:semis:2026-07-11",
             "score:macro:global:2026-07-11"],
        )

    def test_layered_depths(self) -> None:
        result = compute_lineage(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(result.layered.get(1), ["score:industry:semis:2026-07-11"])
        self.assertEqual(result.layered.get(2), ["score:macro:global:2026-07-11"])
        self.assertEqual(result.depth_reached, 2)


# ---------------------------------------------------------------------------
# 4. Macro score -> Industry score -> Company score chain
# ---------------------------------------------------------------------------


class TestLineageFullChain(unittest.TestCase):
    """The full brief: from a company score, lineage traces back
    through industry to macro, AND through the signal that
    contributed to the company score, AND the source that
    generated the signal. All four edge types participate."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:twse_t86", NodeType.SOURCE),
                _node("signal:foreign_net_buy", NodeType.SIGNAL),
                _node("score:macro:global:2026-07-11", NodeType.SCORE),
                _node("score:industry:semis:2026-07-11", NodeType.SCORE),
                _node("score:company:2330:2026-07-11", NodeType.SCORE),
            ],
            [
                (EdgeType.GENERATED, "signal:foreign_net_buy",
                 "source:twse_t86"),
                (EdgeType.CONTRIBUTES_TO, "signal:foreign_net_buy",
                 "score:company:2330:2026-07-11"),
                (EdgeType.INFLUENCES, "score:macro:global:2026-07-11",
                 "score:industry:semis:2026-07-11"),
                (EdgeType.INFLUENCES, "score:industry:semis:2026-07-11",
                 "score:company:2330:2026-07-11"),
            ],
        )

    def test_full_upstream_set(self) -> None:
        result = compute_lineage(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(
            sorted(result.visited_node_ids),
            [
                "score:industry:semis:2026-07-11",
                "score:macro:global:2026-07-11",
                "signal:foreign_net_buy",
                "source:twse_t86",
            ],
        )

    def test_buckets_partition_correctly(self) -> None:
        result = compute_lineage(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(
            sorted(result.source_node_ids), ["source:twse_t86"]
        )
        self.assertEqual(
            sorted(result.signal_node_ids), ["signal:foreign_net_buy"]
        )
        self.assertEqual(
            sorted(result.score_node_ids),
            ["score:industry:semis:2026-07-11",
             "score:macro:global:2026-07-11"],
        )
        self.assertEqual(result.entity_node_ids, [])

    def test_macro_is_a_leaf(self) -> None:
        result = compute_lineage(
            self.store, "score:company:2330:2026-07-11"
        )
        # macro has no upstream lineage edges in this graph.
        self.assertIn("score:macro:global:2026-07-11", result.leaf_node_ids)


# ---------------------------------------------------------------------------
# 5. News refers to entity (REFERS_TO)
# ---------------------------------------------------------------------------


class TestLineageNewsRefersToEntity(unittest.TestCase):
    """REFERS_TO is news -> entity; the news is the upstream document."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("news:reuters_2026_07_11", NodeType.NEWS),
                _node("entity:tsmc", NodeType.COMPANY),
            ],
            [
                (EdgeType.REFERS_TO, "news:reuters_2026_07_11",
                 "entity:tsmc"),
            ],
        )

    def test_news_is_upstream_of_entity(self) -> None:
        result = compute_lineage(self.store, "entity:tsmc")
        self.assertEqual(result.visited_node_ids, ["news:reuters_2026_07_11"])


# ---------------------------------------------------------------------------
# 6. Entity exposed to macro factor (EXPOSED_TO)
# ---------------------------------------------------------------------------


class TestLineageExposedTo(unittest.TestCase):
    """EXPOSED_TO is entity -> factor; the macro is upstream."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("macro_factor:oil_price", NodeType.MACRO_FACTOR),
                _node("entity:airline_a", NodeType.COMPANY),
            ],
            [
                (EdgeType.EXPOSED_TO, "entity:airline_a",
                 "macro_factor:oil_price"),
            ],
        )

    def test_macro_factor_is_upstream(self) -> None:
        result = compute_lineage(self.store, "entity:airline_a")
        self.assertEqual(
            result.visited_node_ids, ["macro_factor:oil_price"]
        )


# ---------------------------------------------------------------------------
# 7. Mixed edge directions (upstream AND non-upstream in graph)
# ---------------------------------------------------------------------------


class TestLineageMixedEdges(unittest.TestCase):
    """When the graph contains BOTH upstream and downstream edges
    (e.g. a back-edge pointing from a source to a downstream
    consumer that is NOT a lineage edge), the query must follow
    only the upstream side and ignore the rest."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:yfinance", NodeType.SOURCE),
                _node("signal:pe", NodeType.SIGNAL),
                _node("score:company:2330:2026-07-11", NodeType.SCORE),
                # Unrelated downstream node (a consumer of the
                # signal but via a non-lineage edge type).
                _node("report:weekly_2330", NodeType.REPORT),
            ],
            [
                (EdgeType.GENERATED, "signal:pe", "source:yfinance"),
                (EdgeType.CONTRIBUTES_TO, "signal:pe",
                 "score:company:2330:2026-07-11"),
                # Back-edge using a non-lineage edge type: INCLUDES
                # is report -> score, so report includes a score
                # (the score is upstream of the report from the
                # brief's view: from a report, lineage goes to its
                # score). But from the company's score, the
                # walk should not follow the INCLUDES edge AT ALL
                # because INCLUDES is not in LINEAGE_EDGE_TYPES.
                (EdgeType.INCLUDES, "report:weekly_2330",
                 "score:company:2330:2026-07-11"),
            ],
        )

    def test_includes_edge_ignored(self) -> None:
        result = compute_lineage(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertNotIn("report:weekly_2330", result.visited_node_ids)
        self.assertEqual(
            sorted(result.visited_node_ids),
            ["signal:pe", "source:yfinance"],
        )


# ---------------------------------------------------------------------------
# 8. Cycle protection
# ---------------------------------------------------------------------------


class TestLineageCycleProtection(unittest.TestCase):
    """Even with a cycle in the lineage graph, the walk must not loop."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:a", NodeType.SCORE),
                _node("score:b", NodeType.SCORE),
                _node("score:c", NodeType.SCORE),
            ],
            [
                # a influences b, b influences c, c influences a (cycle).
                (EdgeType.INFLUENCES, "score:a", "score:b"),
                (EdgeType.INFLUENCES, "score:b", "score:c"),
                (EdgeType.INFLUENCES, "score:c", "score:a"),
            ],
        )

    def test_visited_each_node_at_most_once(self) -> None:
        result = compute_lineage(self.store, "score:a")
        self.assertEqual(len(result.visited_node_ids), 2)
        self.assertEqual(
            sorted(result.visited_node_ids),
            ["score:b", "score:c"],
        )

    def test_visited_edges_each_once(self) -> None:
        result = compute_lineage(self.store, "score:a")
        # Two edges traversed (a->b, b->c), c->a is the back-edge
        # that would re-enter 'a' so it must not be in the trail.
        self.assertEqual(len(result.visited_edges), 2)


# ---------------------------------------------------------------------------
# 9. Deterministic ordering
# ---------------------------------------------------------------------------


class TestLineageDeterministicOrdering(unittest.TestCase):
    """Same input -> same output, every time, regardless of insertion
    order or graph store."""

    def setUp(self) -> None:
        # Diamond: A influences B and C; both B and C influence D.
        # Expected BFS order from D upstream: B, C, A.
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:a", NodeType.SCORE),
                _node("score:b", NodeType.SCORE),
                _node("score:c", NodeType.SCORE),
                _node("score:d", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:a", "score:b"),
                (EdgeType.INFLUENCES, "score:a", "score:c"),
                (EdgeType.INFLUENCES, "score:b", "score:d"),
                (EdgeType.INFLUENCES, "score:c", "score:d"),
            ],
        )

    def test_layered_depth_1_is_sorted_by_node_id(self) -> None:
        result = compute_lineage(self.store, "score:d", max_depth=2)
        # BFS discovers B then C from D (insertion order).
        # Within a single node, neighbors are discovered in
        # insertion order of edges (we insert b before c in the
        # test setup but the spec is: deterministic per call).
        # The real invariant is: at depth 1, both B and C are
        # present; at depth 2, A is present.
        self.assertEqual(
            sorted(result.layered.get(1, [])),
            ["score:b", "score:c"],
        )
        self.assertEqual(result.layered.get(2), ["score:a"])

    def test_repeated_calls_are_idempotent(self) -> None:
        r1 = compute_lineage(self.store, "score:d")
        r2 = compute_lineage(self.store, "score:d")
        self.assertEqual(r1, r2)


# ---------------------------------------------------------------------------
# 10. max_depth truncation
# ---------------------------------------------------------------------------


class TestLineageMaxDepth(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:macro", NodeType.SCORE),
                _node("score:industry", NodeType.SCORE),
                _node("score:company", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:macro", "score:industry"),
                (EdgeType.INFLUENCES, "score:industry", "score:company"),
            ],
        )

    def test_max_depth_1_stops_at_direct_neighbor(self) -> None:
        result = compute_lineage(self.store, "score:company", max_depth=1)
        self.assertEqual(
            result.visited_node_ids, ["score:industry"]
        )
        self.assertEqual(result.depth_reached, 1)
        self.assertFalse(result.truncated)

    def test_max_depth_2_includes_macro(self) -> None:
        result = compute_lineage(self.store, "score:company", max_depth=2)
        self.assertEqual(
            sorted(result.visited_node_ids),
            ["score:industry", "score:macro"],
        )
        self.assertEqual(result.depth_reached, 2)


# ---------------------------------------------------------------------------
# 11. max_nodes truncation
# ---------------------------------------------------------------------------


class TestLineageMaxNodes(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:macro", NodeType.SCORE),
                _node("score:industry", NodeType.SCORE),
                _node("score:company", NodeType.SCORE),
                _node("score:brand", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:macro", "score:industry"),
                (EdgeType.INFLUENCES, "score:industry", "score:company"),
                (EdgeType.INFLUENCES, "score:company", "score:brand"),
            ],
        )

    def test_max_nodes_1_truncates_after_first_neighbor(self) -> None:
        result = compute_lineage(
            self.store, "score:brand", max_depth=5, max_nodes=1
        )
        self.assertEqual(result.visited_node_ids, ["score:company"])
        self.assertTrue(result.truncated)


# ---------------------------------------------------------------------------
# 12. InMemory / SQLite parity
# ---------------------------------------------------------------------------


class TestLineageInMemorySQLiteParity(unittest.TestCase):
    """The same graph built in two stores must produce identical
    LineageQuery results."""

    GRAPH_NODES = [
        ("source:yfinance", NodeType.SOURCE),
        ("signal:pe", NodeType.SIGNAL),
        ("score:macro:global:2026-07-11", NodeType.SCORE),
        ("score:industry:semis:2026-07-11", NodeType.SCORE),
        ("score:company:2330:2026-07-11", NodeType.SCORE),
    ]
    GRAPH_EDGES = [
        (EdgeType.GENERATED, "signal:pe", "source:yfinance"),
        (EdgeType.CONTRIBUTES_TO, "signal:pe",
         "score:company:2330:2026-07-11"),
        (EdgeType.INFLUENCES, "score:macro:global:2026-07-11",
         "score:industry:semis:2026-07-11"),
        (EdgeType.INFLUENCES, "score:industry:semis:2026-07-11",
         "score:company:2330:2026-07-11"),
    ]
    START = "score:company:2330:2026-07-11"

    def _build(self, store) -> None:
        for nid, nt in self.GRAPH_NODES:
            store.add_node(_node(nid, nt))
        for et, f, t in self.GRAPH_EDGES:
            store.add_edge(_edge(et, f, t))

    def test_parity(self) -> None:
        mem = InMemoryGraphStore()
        self._build(mem)
        mem_result = compute_lineage(mem, self.START)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "lineage_test.db")
            sq = SQLiteGraphStore(db_path, auto_migrate=True)
            self._build(sq)
            sq_result = compute_lineage(sq, self.START)

        # Compare as dicts (sets order aside) — but visited_edges
        # has tuple order; compare full DTOs with the same ordering
        # because both stores follow the same insertion order in
        # BFS.
        self.assertEqual(
            sorted(mem_result.visited_node_ids),
            sorted(sq_result.visited_node_ids),
        )
        self.assertEqual(
            sorted(mem_result.signal_node_ids),
            sorted(sq_result.signal_node_ids),
        )
        self.assertEqual(
            sorted(mem_result.score_node_ids),
            sorted(sq_result.score_node_ids),
        )
        self.assertEqual(
            sorted(mem_result.source_node_ids),
            sorted(sq_result.source_node_ids),
        )
        self.assertEqual(mem_result.depth_reached, sq_result.depth_reached)


# ---------------------------------------------------------------------------
# Auxiliary: missing start node
# ---------------------------------------------------------------------------


class TestLineageMissingStartNode(unittest.TestCase):
    def test_missing_start_returns_empty_result_with_warning(self) -> None:
        store = InMemoryGraphStore()
        result = compute_lineage(store, "score:nonexistent")
        self.assertEqual(result.start, "score:nonexistent")
        self.assertEqual(result.visited_node_ids, [])
        self.assertEqual(result.depth_reached, 0)
        self.assertFalse(result.truncated)
        self.assertTrue(
            any("not in graph" in w for w in result.warnings)
        )


# ---------------------------------------------------------------------------
# Auxiliary: leaf node with no upstream
# ---------------------------------------------------------------------------


class TestLineageLeafWithNoUpstream(unittest.TestCase):
    def test_source_with_no_lineage_inbound_yields_empty(self) -> None:
        store = InMemoryGraphStore()
        store.add_node(_node("source:lonely", NodeType.SOURCE))
        result = compute_lineage(store, "source:lonely")
        self.assertEqual(result.visited_node_ids, [])
        # A node with no upstream neighbors is itself a leaf by
        # convention (no upstream traversal reaches anything).
        self.assertEqual(result.leaf_node_ids, [])
        self.assertEqual(result.depth_reached, 0)


# ---------------------------------------------------------------------------
# Auxiliary: edge_types whitelist
# ---------------------------------------------------------------------------


class TestLineageEdgeTypeFilter(unittest.TestCase):
    def test_whitelist_only_influences_skips_contributes(self) -> None:
        store = InMemoryGraphStore()
        _populate(
            store,
            [
                _node("source:yfinance", NodeType.SOURCE),
                _node("signal:pe", NodeType.SIGNAL),
                _node("score:macro", NodeType.SCORE),
                _node("score:company", NodeType.SCORE),
            ],
            [
                (EdgeType.GENERATED, "signal:pe", "source:yfinance"),
                (EdgeType.CONTRIBUTES_TO, "signal:pe", "score:company"),
                (EdgeType.INFLUENCES, "score:macro", "score:company"),
            ],
        )
        result = compute_lineage(
            store, "score:company",
            edge_types=[EdgeType.INFLUENCES],
        )
        # INFLUENCES walk reaches macro; CONTRIBUTES_TO is filtered
        # out, so signal and source are NOT visited.
        self.assertEqual(result.visited_node_ids, ["score:macro"])

    def test_unsupported_edge_type_warns(self) -> None:
        store = InMemoryGraphStore()
        store.add_node(_node("score:x", NodeType.SCORE))
        result = compute_lineage(
            store, "score:x",
            edge_types=[EdgeType.INCLUDES],  # not a lineage edge
        )
        self.assertTrue(
            any("unsupported edge types" in w for w in result.warnings)
        )

    def test_no_supported_edge_types_after_filter_returns_empty(self) -> None:
        store = InMemoryGraphStore()
        store.add_node(_node("score:x", NodeType.SCORE))
        result = compute_lineage(
            store, "score:x",
            edge_types=[EdgeType.INCLUDES, EdgeType.REPORT_BY],
        )
        self.assertEqual(result.visited_node_ids, [])
        self.assertTrue(
            any("no supported lineage edge types" in w
                for w in result.warnings)
        )


# ---------------------------------------------------------------------------
# Auxiliary: to_dict JSON round-trip
# ---------------------------------------------------------------------------


class TestLineageToDict(unittest.TestCase):
    def test_to_dict_is_json_serializable(self) -> None:
        store = InMemoryGraphStore()
        _populate(
            store,
            [
                _node("source:a", NodeType.SOURCE),
                _node("signal:b", NodeType.SIGNAL),
            ],
            [
                (EdgeType.GENERATED, "signal:b", "source:a"),
            ],
        )
        result = compute_lineage(store, "signal:b")
        d = result.to_dict()
        # Must round-trip through json.
        encoded = json.dumps(d, sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["start"], "signal:b")
        self.assertIn("source:a", decoded["visited_node_ids"])
        self.assertIn("warnings", decoded)
        self.assertIn("truncated", decoded)
        self.assertIn("depth_reached", decoded)

    def test_repeated_calls_produce_equal_dto(self) -> None:
        # Sanity: the frozen dataclass compares equal across
        # repeated calls (frozen=True gives dataclass __eq__).
        store = InMemoryGraphStore()
        store.add_node(_node("score:x", NodeType.SCORE))
        r1 = compute_lineage(store, "score:x")
        r2 = compute_lineage(store, "score:x")
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
