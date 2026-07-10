"""Tests for Phase 3B Task 4 Run 4B — Cross-layer Impact query.

Authoritative cross-layer impact semantics (per Run 4B brief):
- Only the strict cross-layer edges are followed:
  * INFLUENCES (score -> score): the influencer is upstream,
    the influenced is downstream.
  * DERIVED_FROM (ancestor -> derived): the ancestor is upstream,
    the derived is downstream.
- ALL other edge types (CONTRIBUTES_TO, GENERATED, CITES,
  REFERS_TO, EXPOSED_TO, INCLUDES, MEMBER_OF, BELONGS_TO,
  REPORT_BY, WORKS_AT) are NOT cross-layer impact edges and
  must be ignored.
- The walk goes in BOTH directions: upstream chain and
  downstream chain are computed independently.
- Result filters to score-typed nodes defensively
  (``_filter_to_scores``).
- Layer representatives: for each scorer_type, the first node
  reached in BFS order is the representative.

This module is the canonical location (Run 4B). The legacy
in-queries.py implementation was removed in Run 4B because it
delegated to ``EvidenceTracer`` (Pitfall O directional
asymmetry).

Coverage matrix (12 cases):
  1. Simple INFLUENCES upstream chain (company <- industry)
  2. Simple INFLUENCES downstream chain (macro -> industry)
  3. Macro -> Industry -> Company full chain (upstream + downstream)
  4. DERIVED_FROM upstream / downstream
  5. Mixed INFLUENCES + DERIVED_FROM
  6. Non-cross-layer edges (CITES, GENERATED, CONTRIBUTES_TO,
     REFERS_TO, EXPOSED_TO, INCLUDES) ignored
  7. Cycle protection in upstream walk
  8. Cycle protection in downstream walk
  9. Deterministic ordering
 10. max_depth truncation
 11. max_nodes truncation
 12. InMemory / SQLite parity

Plus auxiliary coverage:
  - Policy table sanity (strict-set equality to brief)
  - Missing start node
  - Non-score start node yields empty chains
  - edge_types whitelist filter
  - Unsupported edge type warning
  - upstream_layers / downstream_layers correctness
  - to_dict() JSON round-trip
  - CrossLayerImpactResult fields (truncated_*, depth_reached_*,
    visited_edges_*, layered_*)

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
from phase3.graph.cross_layer_impact import (
    CROSS_LAYER_DOWNSTREAM_SIDE,
    CROSS_LAYER_EDGE_TYPES,
    CROSS_LAYER_UPSTREAM_SIDE,
    CrossLayerImpactResult,
    compute_cross_layer_impact,
)
from phase3.graph.in_memory_store import GraphStore as InMemoryGraphStore
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


class TestCrossLayerPolicyTable(unittest.TestCase):
    """The policy table must match the Run 4B brief exactly: 2 edges."""

    BRIEF_EDGES = frozenset({
        EdgeType.INFLUENCES,
        EdgeType.DERIVED_FROM,
    })

    def test_policy_has_exactly_brief_set(self) -> None:
        self.assertEqual(
            frozenset(CROSS_LAYER_UPSTREAM_SIDE.keys()),
            self.BRIEF_EDGES,
            "CROSS_LAYER_UPSTREAM_SIDE must be exactly the brief's 2 edges",
        )

    def test_downstream_policy_has_same_keys(self) -> None:
        self.assertEqual(
            frozenset(CROSS_LAYER_DOWNSTREAM_SIDE.keys()),
            self.BRIEF_EDGES,
            "CROSS_LAYER_DOWNSTREAM_SIDE must have the same 2 edge keys",
        )

    def test_edge_types_tuple_matches_policy_keys(self) -> None:
        self.assertEqual(
            frozenset(CROSS_LAYER_EDGE_TYPES),
            self.BRIEF_EDGES,
            "CROSS_LAYER_EDGE_TYPES must be the brief's 2 edges",
        )

    def test_upstream_and_downstream_sides_are_opposite(self) -> None:
        # Per the brief: an edge that goes "from upstream to
        # downstream" has the upstream side on the from-side and
        # the downstream side on the to-side. So the policy
        # tables must be exact opposites: if upstream says "from"
        # for an edge type, downstream must say "to" for the
        # same edge type.
        for et in self.BRIEF_EDGES:
            self.assertEqual(
                CROSS_LAYER_UPSTREAM_SIDE[et],
                "from",
                f"{et.value}: upstream side must be 'from' (the "
                f"influencer/ancestor is on the from-side)",
            )
            self.assertEqual(
                CROSS_LAYER_DOWNSTREAM_SIDE[et],
                "to",
                f"{et.value}: downstream side must be 'to' (the "
                f"influenced/derived is on the to-side)",
            )

    def test_non_cross_layer_edges_excluded(self) -> None:
        excluded = (
            EdgeType.GENERATED,
            EdgeType.CONTRIBUTES_TO,
            EdgeType.CITES,
            EdgeType.REFERS_TO,
            EdgeType.EXPOSED_TO,
            EdgeType.INCLUDES,
            EdgeType.MEMBER_OF,
            EdgeType.BELONGS_TO,
            EdgeType.REPORT_BY,
            EdgeType.WORKS_AT,
        )
        for et in excluded:
            self.assertNotIn(
                et, CROSS_LAYER_UPSTREAM_SIDE,
                f"{et.value!r} must not be a cross-layer edge",
            )
            self.assertNotIn(
                et, CROSS_LAYER_DOWNSTREAM_SIDE,
                f"{et.value!r} must not be a cross-layer edge",
            )


# ---------------------------------------------------------------------------
# 1. Simple INFLUENCES upstream chain (company <- industry)
# ---------------------------------------------------------------------------


class TestCrossLayerUpstreamInfluences(unittest.TestCase):
    """From a company score, the upstream chain is the industry
    score that influenced it via INFLUENCES."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:industry:semis:2026-07-11", NodeType.SCORE),
                _node("score:company:2330:2026-07-11", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:industry:semis:2026-07-11",
                 "score:company:2330:2026-07-11"),
            ],
        )

    def test_upstream_chain_has_industry(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(
            result.upstream_chain, ["score:industry:semis:2026-07-11"]
        )

    def test_downstream_chain_is_empty(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(result.downstream_chain, [])

    def test_upstream_layers_industry_representative(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(
            result.upstream_layers,
            {"industry": "score:industry:semis:2026-07-11"},
        )
        self.assertEqual(result.downstream_layers, {})

    def test_all_reachable_only_upstream(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(
            result.all_reachable_score_ids,
            ["score:industry:semis:2026-07-11"],
        )


# ---------------------------------------------------------------------------
# 2. Simple INFLUENCES downstream chain (macro -> industry)
# ---------------------------------------------------------------------------


class TestCrossLayerDownstreamInfluences(unittest.TestCase):
    """From a macro score, the downstream chain is the industry
    score it influences."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:macro:global:2026-07-11", NodeType.SCORE),
                _node("score:industry:semis:2026-07-11", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:macro:global:2026-07-11",
                 "score:industry:semis:2026-07-11"),
            ],
        )

    def test_downstream_chain_has_industry(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:macro:global:2026-07-11"
        )
        self.assertEqual(
            result.downstream_chain, ["score:industry:semis:2026-07-11"]
        )

    def test_upstream_chain_is_empty(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:macro:global:2026-07-11"
        )
        self.assertEqual(result.upstream_chain, [])

    def test_downstream_layers_industry_representative(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:macro:global:2026-07-11"
        )
        self.assertEqual(
            result.downstream_layers,
            {"industry": "score:industry:semis:2026-07-11"},
        )


# ---------------------------------------------------------------------------
# 3. Macro -> Industry -> Company full chain
# ---------------------------------------------------------------------------


class TestCrossLayerFullChain(unittest.TestCase):
    """Industry sits between macro and company. From industry, the
    upstream chain is macro and the downstream chain is company."""

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

    def test_from_industry_upstream_macro_downstream_company(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:industry:semis:2026-07-11"
        )
        self.assertEqual(
            result.upstream_chain, ["score:macro:global:2026-07-11"]
        )
        self.assertEqual(
            result.downstream_chain, ["score:company:2330:2026-07-11"]
        )

    def test_from_company_full_upstream_chain(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(
            result.upstream_chain,
            ["score:industry:semis:2026-07-11",
             "score:macro:global:2026-07-11"],
        )
        self.assertEqual(result.downstream_chain, [])

    def test_from_macro_full_downstream_chain(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:macro:global:2026-07-11"
        )
        self.assertEqual(
            result.downstream_chain,
            ["score:industry:semis:2026-07-11",
             "score:company:2330:2026-07-11"],
        )
        self.assertEqual(result.upstream_chain, [])

    def test_layer_representatives_from_industry(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:industry:semis:2026-07-11"
        )
        self.assertEqual(
            result.upstream_layers,
            {"macro": "score:macro:global:2026-07-11"},
        )
        self.assertEqual(
            result.downstream_layers,
            {"company": "score:company:2330:2026-07-11"},
        )

    def test_all_reachable_from_industry_preserves_upstream_first_order(
        self,
    ) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:industry:semis:2026-07-11"
        )
        # Union, dedup, preserve upstream-first order.
        self.assertEqual(
            result.all_reachable_score_ids,
            ["score:macro:global:2026-07-11",
             "score:company:2330:2026-07-11"],
        )


# ---------------------------------------------------------------------------
# 4. DERIVED_FROM upstream / downstream
# ---------------------------------------------------------------------------


class TestCrossLayerDerivedFrom(unittest.TestCase):
    """DERIVED_FROM is ancestor -> derived; the ancestor is
    upstream."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("score:company:2330:base", NodeType.SCORE),
                _node("score:company:2330:adjusted", NodeType.SCORE),
            ],
            [
                (EdgeType.DERIVED_FROM, "score:company:2330:base",
                 "score:company:2330:adjusted"),
            ],
        )

    def test_base_is_upstream_of_adjusted(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:company:2330:adjusted"
        )
        self.assertEqual(
            result.upstream_chain, ["score:company:2330:base"]
        )
        self.assertEqual(result.downstream_chain, [])

    def test_adjusted_is_downstream_of_base(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:company:2330:base"
        )
        self.assertEqual(
            result.downstream_chain, ["score:company:2330:adjusted"]
        )


# ---------------------------------------------------------------------------
# 5. Mixed INFLUENCES + DERIVED_FROM
# ---------------------------------------------------------------------------


class TestCrossLayerMixedEdgeTypes(unittest.TestCase):
    """When the graph has both INFLUENCES and DERIVED_FROM edges
    connected to the same anchor, the walk must follow both edge
    types — INFLUENCES upstream, DERIVED_FROM downstream."""

    def test_both_edge_types_respected_from_anchor(self) -> None:
        store = InMemoryGraphStore()
        # Anchor has INFLUENCES upstream (b) and DERIVED_FROM
        # downstream (c).
        _populate(
            store,
            [
                _node("score:b", NodeType.SCORE),
                _node("score:anchor", NodeType.SCORE),
                _node("score:c", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:b", "score:anchor"),
                (EdgeType.DERIVED_FROM, "score:anchor", "score:c"),
            ],
        )
        result = compute_cross_layer_impact(store, "score:anchor")
        self.assertEqual(result.upstream_chain, ["score:b"])
        self.assertEqual(result.downstream_chain, ["score:c"])

    def test_derived_from_upstream_and_influences_downstream(self) -> None:
        # Same anchor but mirrored: DERIVED_FROM upstream
        # (ancestor -> anchor), INFLUENCES downstream
        # (anchor -> influenced).
        store = InMemoryGraphStore()
        _populate(
            store,
            [
                _node("score:ancestor", NodeType.SCORE),
                _node("score:anchor", NodeType.SCORE),
                _node("score:influenced", NodeType.SCORE),
            ],
            [
                (EdgeType.DERIVED_FROM, "score:ancestor", "score:anchor"),
                (EdgeType.INFLUENCES, "score:anchor",
                 "score:influenced"),
            ],
        )
        result = compute_cross_layer_impact(store, "score:anchor")
        self.assertEqual(result.upstream_chain, ["score:ancestor"])
        self.assertEqual(result.downstream_chain, ["score:influenced"])


# ---------------------------------------------------------------------------
# 6. Non-cross-layer edges ignored
# ---------------------------------------------------------------------------


class TestCrossLayerIgnoresNonCrossLayerEdges(unittest.TestCase):
    """CITES, GENERATED, CONTRIBUTES_TO, REFERS_TO, EXPOSED_TO, and
    INCLUDES are not cross-layer edges and must be ignored even
    when they connect score-typed nodes."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:yfinance", NodeType.SOURCE),
                _node("signal:pe", NodeType.SIGNAL),
                _node("score:industry:semis:2026-07-11", NodeType.SCORE),
                _node("score:company:2330:2026-07-11", NodeType.SCORE),
                _node("entity:tsmc", NodeType.COMPANY),
                _node("report:weekly", NodeType.REPORT),
                _node("macro_factor:oil", NodeType.MACRO_FACTOR),
            ],
            [
                # Non-cross-layer edges that should be ignored.
                (EdgeType.GENERATED, "signal:pe", "source:yfinance"),
                (EdgeType.CONTRIBUTES_TO, "signal:pe",
                 "score:company:2330:2026-07-11"),
                (EdgeType.CITES, "score:company:2330:2026-07-11",
                 "source:yfinance"),
                (EdgeType.REFERS_TO, "report:weekly", "entity:tsmc"),
                (EdgeType.EXPOSED_TO, "entity:tsmc", "macro_factor:oil"),
                (EdgeType.INCLUDES, "report:weekly",
                 "score:company:2330:2026-07-11"),
            ],
        )

    def test_company_score_yields_empty_when_no_cross_layer(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertEqual(result.upstream_chain, [])
        self.assertEqual(result.downstream_chain, [])
        self.assertEqual(result.upstream_layers, {})
        self.assertEqual(result.downstream_layers, {})
        self.assertEqual(result.all_reachable_score_ids, [])


# ---------------------------------------------------------------------------
# 7. Cycle protection in upstream walk
# ---------------------------------------------------------------------------


class TestCrossLayerCycleProtectionUpstream(unittest.TestCase):
    """Even with a cycle in the upstream cross-layer graph, the
    walk must not loop."""

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
                (EdgeType.INFLUENCES, "score:a", "score:b"),
                (EdgeType.INFLUENCES, "score:b", "score:c"),
                (EdgeType.INFLUENCES, "score:c", "score:a"),
            ],
        )

    def test_upstream_visited_each_score_at_most_once(self) -> None:
        result = compute_cross_layer_impact(self.store, "score:a")
        self.assertEqual(
            sorted(result.upstream_chain), ["score:b", "score:c"]
        )
        self.assertEqual(result.upstream_chain.__len__(), 2)


# ---------------------------------------------------------------------------
# 8. Cycle protection in downstream walk
# ---------------------------------------------------------------------------


class TestCrossLayerCycleProtectionDownstream(unittest.TestCase):
    def test_downstream_visited_each_score_at_most_once(self) -> None:
        store = InMemoryGraphStore()
        _populate(
            store,
            [
                _node("score:a", NodeType.SCORE),
                _node("score:b", NodeType.SCORE),
                _node("score:c", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:a", "score:b"),
                (EdgeType.INFLUENCES, "score:b", "score:c"),
                (EdgeType.INFLUENCES, "score:c", "score:a"),
            ],
        )
        result = compute_cross_layer_impact(store, "score:a")
        self.assertEqual(
            sorted(result.downstream_chain), ["score:b", "score:c"]
        )


# ---------------------------------------------------------------------------
# 9. Deterministic ordering
# ---------------------------------------------------------------------------


class TestCrossLayerDeterministicOrdering(unittest.TestCase):
    def setUp(self) -> None:
        # Diamond downstream: A influences B and C; both B and C
        # influence D. From A downstream, BFS order is B, C, D.
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

    def test_downstream_layered_has_b_and_c_at_depth_1(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:a", max_depth=3
        )
        # depth 1: b and c. depth 2: d (via either b or c).
        # The implementation visits neighbors in stable
        # (edge_id, neighbor_id) order, so order is determined
        # by edge id stamping in the store.
        self.assertEqual(
            sorted(result.layered_downstream.get(1, [])),
            ["score:b", "score:c"],
        )
        self.assertEqual(
            result.layered_downstream.get(2), ["score:d"]
        )

    def test_repeated_calls_are_idempotent(self) -> None:
        r1 = compute_cross_layer_impact(self.store, "score:a")
        r2 = compute_cross_layer_impact(self.store, "score:a")
        self.assertEqual(r1, r2)


# ---------------------------------------------------------------------------
# 10. max_depth truncation
# ---------------------------------------------------------------------------


class TestCrossLayerMaxDepth(unittest.TestCase):
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

    def test_max_depth_1_stops_at_industry_downstream(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:macro", max_depth=1
        )
        self.assertEqual(
            result.downstream_chain, ["score:industry"]
        )
        self.assertEqual(result.depth_reached_downstream, 1)
        self.assertFalse(result.truncated_downstream)

    def test_max_depth_2_includes_company_downstream(self) -> None:
        result = compute_cross_layer_impact(
            self.store, "score:macro", max_depth=2
        )
        self.assertEqual(
            result.downstream_chain,
            ["score:industry", "score:company"],
        )
        self.assertEqual(result.depth_reached_downstream, 2)


# ---------------------------------------------------------------------------
# 11. max_nodes truncation
# ---------------------------------------------------------------------------


class TestCrossLayerMaxNodes(unittest.TestCase):
    def test_max_nodes_truncates_one_direction_only(self) -> None:
        store = InMemoryGraphStore()
        _populate(
            store,
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
        result = compute_cross_layer_impact(
            store, "score:macro", max_depth=5, max_nodes=1
        )
        # Downstream: industry is the first node; max_nodes=1
        # truncates before reaching company.
        self.assertEqual(
            result.downstream_chain, ["score:industry"]
        )
        self.assertTrue(result.truncated_downstream)


# ---------------------------------------------------------------------------
# 12. InMemory / SQLite parity
# ---------------------------------------------------------------------------


class TestCrossLayerInMemorySQLiteParity(unittest.TestCase):
    """The same graph built in two stores must produce identical
    CrossLayerImpactResult results."""

    GRAPH_NODES = [
        ("score:macro:global:2026-07-11", NodeType.SCORE),
        ("score:industry:semis:2026-07-11", NodeType.SCORE),
        ("score:company:2330:2026-07-11", NodeType.SCORE),
    ]
    GRAPH_EDGES = [
        (EdgeType.INFLUENCES, "score:macro:global:2026-07-11",
         "score:industry:semis:2026-07-11"),
        (EdgeType.INFLUENCES, "score:industry:semis:2026-07-11",
         "score:company:2330:2026-07-11"),
    ]
    START = "score:industry:semis:2026-07-11"

    def _build(self, store) -> None:
        for nid, nt in self.GRAPH_NODES:
            store.add_node(_node(nid, nt))
        for et, f, t in self.GRAPH_EDGES:
            store.add_edge(_edge(et, f, t))

    def test_parity(self) -> None:
        mem = InMemoryGraphStore()
        self._build(mem)
        mem_result = compute_cross_layer_impact(mem, self.START)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "cross_layer_test.db")
            sq = SQLiteGraphStore(db_path, auto_migrate=True)
            self._build(sq)
            sq_result = compute_cross_layer_impact(sq, self.START)

        self.assertEqual(mem_result.upstream_chain, sq_result.upstream_chain)
        self.assertEqual(
            mem_result.downstream_chain, sq_result.downstream_chain
        )
        self.assertEqual(mem_result.upstream_layers, sq_result.upstream_layers)
        self.assertEqual(
            mem_result.downstream_layers, sq_result.downstream_layers
        )
        self.assertEqual(
            mem_result.all_reachable_score_ids,
            sq_result.all_reachable_score_ids,
        )
        self.assertEqual(
            mem_result.depth_reached_upstream, sq_result.depth_reached_upstream
        )
        self.assertEqual(
            mem_result.depth_reached_downstream,
            sq_result.depth_reached_downstream,
        )


# ---------------------------------------------------------------------------
# Auxiliary: missing start node
# ---------------------------------------------------------------------------


class TestCrossLayerMissingStartNode(unittest.TestCase):
    def test_missing_start_returns_empty_with_warning(self) -> None:
        store = InMemoryGraphStore()
        result = compute_cross_layer_impact(store, "score:nonexistent")
        self.assertEqual(result.start, "score:nonexistent")
        self.assertEqual(result.upstream_chain, [])
        self.assertEqual(result.downstream_chain, [])
        self.assertEqual(result.depth_reached_upstream, 0)
        self.assertEqual(result.depth_reached_downstream, 0)
        self.assertFalse(result.truncated_upstream)
        self.assertFalse(result.truncated_downstream)
        self.assertTrue(
            any("not in graph" in w for w in result.warnings)
        )


# ---------------------------------------------------------------------------
# Auxiliary: non-score start node yields empty chains
# ---------------------------------------------------------------------------


class TestCrossLayerNonScoreStart(unittest.TestCase):
    """A non-score start node is not a candidate for cross-layer
    impact; the walk may still find score-typed neighbors but the
    start itself is excluded. With no cross-layer edges, the
    result is empty chains with no error."""

    def test_source_start_yields_empty(self) -> None:
        store = InMemoryGraphStore()
        store.add_node(_node("source:yfinance", NodeType.SOURCE))
        result = compute_cross_layer_impact(store, "source:yfinance")
        self.assertEqual(result.upstream_chain, [])
        self.assertEqual(result.downstream_chain, [])


# ---------------------------------------------------------------------------
# Auxiliary: edge_types whitelist
# ---------------------------------------------------------------------------


class TestCrossLayerEdgeTypeFilter(unittest.TestCase):
    def test_whitelist_omits_influences_only_derives_walks(self) -> None:
        store = InMemoryGraphStore()
        _populate(
            store,
            [
                _node("score:a", NodeType.SCORE),
                _node("score:b", NodeType.SCORE),
                _node("score:c", NodeType.SCORE),
                _node("score:d", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:a", "score:b"),
                (EdgeType.DERIVED_FROM, "score:c", "score:d"),
            ],
        )
        result = compute_cross_layer_impact(
            store, "score:a",
            edge_types=[EdgeType.DERIVED_FROM],
        )
        # With only DERIVED_FROM in the whitelist, INFLUENCES is
        # not followed. From "score:a" there are no DERIVED_FROM
        # edges out. So both chains are empty.
        self.assertEqual(result.upstream_chain, [])
        self.assertEqual(result.downstream_chain, [])

    def test_unsupported_edge_type_warns(self) -> None:
        store = InMemoryGraphStore()
        store.add_node(_node("score:x", NodeType.SCORE))
        result = compute_cross_layer_impact(
            store, "score:x",
            edge_types=[EdgeType.INCLUDES],  # not cross-layer
        )
        self.assertTrue(
            any("unsupported edge types" in w for w in result.warnings)
        )

    def test_no_supported_edge_types_after_filter_returns_empty(self) -> None:
        store = InMemoryGraphStore()
        store.add_node(_node("score:x", NodeType.SCORE))
        result = compute_cross_layer_impact(
            store, "score:x",
            edge_types=[EdgeType.INCLUDES, EdgeType.GENERATED],
        )
        self.assertEqual(result.upstream_chain, [])
        self.assertEqual(result.downstream_chain, [])
        self.assertTrue(
            any("no supported cross-layer edge types" in w
                for w in result.warnings)
        )


# ---------------------------------------------------------------------------
# Auxiliary: to_dict JSON round-trip
# ---------------------------------------------------------------------------


class TestCrossLayerToDict(unittest.TestCase):
    def test_to_dict_is_json_serializable(self) -> None:
        store = InMemoryGraphStore()
        _populate(
            store,
            [
                _node("score:a", NodeType.SCORE),
                _node("score:b", NodeType.SCORE),
            ],
            [
                (EdgeType.INFLUENCES, "score:a", "score:b"),
            ],
        )
        result = compute_cross_layer_impact(store, "score:a")
        d = result.to_dict()
        encoded = json.dumps(d, sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["start"], "score:a")
        self.assertEqual(
            decoded["downstream_chain"], ["score:b"]
        )
        self.assertEqual(decoded["upstream_chain"], [])
        self.assertIn("downstream_layers", decoded)
        self.assertIn("upstream_layers", decoded)
        self.assertIn("all_reachable_score_ids", decoded)
        self.assertIn("warnings", decoded)
        self.assertIn("truncated_upstream", decoded)
        self.assertIn("truncated_downstream", decoded)
        self.assertIn("depth_reached_upstream", decoded)
        self.assertIn("depth_reached_downstream", decoded)

    def test_repeated_calls_produce_equal_dto(self) -> None:
        store = InMemoryGraphStore()
        store.add_node(_node("score:x", NodeType.SCORE))
        r1 = compute_cross_layer_impact(store, "score:x")
        r2 = compute_cross_layer_impact(store, "score:x")
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
