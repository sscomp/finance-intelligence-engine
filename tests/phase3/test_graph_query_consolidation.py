"""Graph Query Layer consolidation tests — Phase 3B Task 4 Run 4A.

Covers the Run 4A consolidation (2026-07-11) where the canonical
Blast Radius implementation moved out of ``phase3/graph/queries.py``
(where it delegated to ``EvidenceTracer`` with wrong downstream
semantics — Pitfall B) into ``phase3/graph/blast_radius.py`` (a
self-contained BFS with its own per-edge-type direction policy).

These tests verify:

1. The canonical path (``phase3.graph.blast_radius`` /
   ``phase3.graph`` package re-export) is the only Blast Radius
   implementation.
2. The legacy module-level function ``phase3.graph.queries
   .query_blast_radius`` and DTO ``BlastRadiusQuery`` are gone.
3. The package's public re-exports point to the canonical symbols.
4. CITES / REFERS_TO remain excluded from Blast Radius.
5. Deterministic, idempotent behavior is unchanged.
6. Unrelated queries (lineage, cross-layer impact) are preserved
   in ``phase3.graph.queries``.

All tests use stdlib ``unittest`` only.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

# Ensure macro-report is on the path.
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from phase3.datamodel.graph import (  # noqa: E402
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)
from phase3.graph import (  # noqa: E402
    BLAST_DOWNSTREAM_SIDE,
    BlastRadiusResult,
    GraphQueryService,
    compute_blast_radius,
)
from phase3.graph.blast_radius import (  # noqa: E402
    BLAST_DOWNSTREAM_SIDE as CANONICAL_POLICY,
    BlastRadiusResult as CanonicalResult,
    compute_blast_radius as canonical_compute,
)
from phase3.graph.in_memory_store import (  # noqa: E402
    GraphStore as InMemoryGraphStore,
)
from phase3.graph.sqlite_store import (  # noqa: E402
    SQLiteGraphStore,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_TS = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _node(nid: str, ntype: NodeType) -> GraphNode:
    return GraphNode(
        node_id=nid,
        node_type=ntype,
        label=nid,
        created_at=_TS,
    )


def _populate(store, nodes, edges) -> None:
    for n in nodes:
        store.add_node(n)
    for et, frm, to in edges:
        store.add_edge(GraphEdge(
            edge_id="_",
            edge_type=et,
            from_node_id=frm,
            to_node_id=to,
            created_at=_TS,
        ))


# ---------------------------------------------------------------------------
# 1. The package's public re-exports point to the canonical implementation
# ---------------------------------------------------------------------------


class TestPackageReExports(unittest.TestCase):
    """``phase3.graph`` re-exports must resolve to the canonical
    Blast Radius module, not the legacy queries.py one."""

    def test_blast_radius_result_is_canonical(self) -> None:
        # Identity check: package-level re-export must be the same
        # class object as the canonical module's class.
        self.assertIs(BlastRadiusResult, CanonicalResult)

    def test_compute_blast_radius_is_canonical(self) -> None:
        self.assertIs(compute_blast_radius, canonical_compute)

    def test_canonical_policy_is_re_exported(self) -> None:
        self.assertIs(BLAST_DOWNSTREAM_SIDE, CANONICAL_POLICY)

    def test_legacy_names_removed_from_queries(self) -> None:
        queries = importlib.import_module("phase3.graph.queries")
        self.assertFalse(
            hasattr(queries, "BlastRadiusQuery"),
            "queries.BlastRadiusQuery was removed in Run 4A consolidation",
        )
        self.assertFalse(
            hasattr(queries, "query_blast_radius"),
            "queries.query_blast_radius was removed in Run 4A consolidation",
        )

    def test_legacy_names_removed_from_package(self) -> None:
        # Importing phase3.graph must NOT re-export the legacy
        # names; the canonical module is the only surface.
        from phase3 import graph
        self.assertFalse(
            hasattr(graph, "BlastRadiusQuery"),
            "phase3.graph.BlastRadiusQuery should be gone",
        )
        self.assertFalse(
            hasattr(graph, "query_blast_radius"),
            "phase3.graph.query_blast_radius should be gone",
        )

    def test_canonical_names_in_queries_all(self) -> None:
        # queries.__all__ must NOT include the removed names.
        queries = importlib.import_module("phase3.graph.queries")
        for n in ("BlastRadiusQuery", "query_blast_radius"):
            self.assertNotIn(
                n, queries.__all__,
                f"queries.__all__ must not contain {n!r}",
            )

    def test_canonical_names_in_package_all(self) -> None:
        from phase3 import graph
        for n in ("BlastRadiusQuery", "query_blast_radius"):
            self.assertNotIn(n, graph.__all__)
        # And the canonical ones ARE in __all__.
        for n in (
            "BLAST_DOWNSTREAM_SIDE",
            "BlastRadiusResult",
            "compute_blast_radius",
        ):
            self.assertIn(n, graph.__all__)


# ---------------------------------------------------------------------------
# 2. Unrelated queries (lineage, cross-layer impact) are preserved
# ---------------------------------------------------------------------------


class TestUnrelatedQueriesPreserved(unittest.TestCase):
    """Run 4A only removed Blast Radius. Lineage + cross-layer
    impact must still be importable and functional."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:yfinance", NodeType.SOURCE),
                _node("signal:pe", NodeType.SIGNAL),
                _node("score:industry:semi:2026-07-11", NodeType.SCORE),
                _node("score:company:2330:2026-07-11", NodeType.SCORE),
            ],
            [
                (EdgeType.GENERATED, "signal:pe", "source:yfinance"),
                (EdgeType.CONTRIBUTES_TO, "signal:pe",
                 "score:industry:semi:2026-07-11"),
                (EdgeType.INFLUENCES, "score:industry:semi:2026-07-11",
                 "score:company:2330:2026-07-11"),
            ],
        )

    def test_lineage_still_works(self) -> None:
        from phase3.graph.queries import (
            query_lineage,
            LineageQuery,
        )
        # Lineage goes upstream from a score.
        result = query_lineage(
            self.store, "score:company:2330:2026-07-11"
        )
        self.assertIsInstance(result, LineageQuery)
        # Should reach the industry score (upstream via INFLUENCES
        # upstream direction works correctly in the tracer).
        self.assertIn(
            "score:industry:semi:2026-07-11",
            result.visited_node_ids,
        )

    def test_cross_layer_impact_still_works(self) -> None:
        from phase3.graph.queries import (
            query_cross_layer_impact,
            CrossLayerImpactQuery,
        )
        result = query_cross_layer_impact(
            self.store, "score:industry:semi:2026-07-11"
        )
        self.assertIsInstance(result, CrossLayerImpactQuery)

    def test_graph_query_service_construction(self) -> None:
        svc = GraphQueryService(self.store)
        # Service has lineage and cross_layer_impact.
        self.assertTrue(callable(getattr(svc, "lineage", None)))
        self.assertTrue(
            callable(getattr(svc, "cross_layer_impact", None))
        )
        # But NOT blast_radius (use canonical module instead).
        self.assertFalse(
            hasattr(svc, "blast_radius"),
            "GraphQueryService.blast_radius was removed in Run 4A",
        )


# ---------------------------------------------------------------------------
# 3. CITES / REFERS_TO remain excluded from canonical Blast Radius
# ---------------------------------------------------------------------------


class TestBlastRadiusExcludesAttributionEdges(unittest.TestCase):
    """Per the brief: CITES (attribution) and REFERS_TO (reference)
    are NOT downstream dependency edges. The canonical policy and
    BFS must continue to exclude them."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:rss", NodeType.SOURCE),
                _node("score:co:2026-07-11", NodeType.SCORE),
                _node("company:2330", NodeType.COMPANY),
                _node("news:item1", NodeType.SIGNAL),
            ],
            [
                # CITES: score -> source. NOT a dep edge.
                (EdgeType.CITES, "score:co:2026-07-11", "source:rss"),
                # REFERS_TO: news -> entity. NOT a dep edge.
                (EdgeType.REFERS_TO, "news:item1", "company:2330"),
            ],
        )

    def test_cites_not_in_policy(self) -> None:
        self.assertNotIn(EdgeType.CITES, BLAST_DOWNSTREAM_SIDE)

    def test_refers_to_not_in_policy(self) -> None:
        self.assertNotIn(EdgeType.REFERS_TO, BLAST_DOWNSTREAM_SIDE)

    def test_cites_excluded_from_walk(self) -> None:
        result = compute_blast_radius(self.store, "source:rss")
        # Score that cites the source must NOT be in blast radius.
        self.assertNotIn("score:co:2026-07-11", result.visited_node_ids)

    def test_refers_to_excluded_from_walk(self) -> None:
        result = compute_blast_radius(self.store, "company:2330")
        # News that refers to the company must NOT be downstream.
        self.assertNotIn("news:item1", result.visited_node_ids)


# ---------------------------------------------------------------------------
# 4. Determinism / idempotency after consolidation
# ---------------------------------------------------------------------------


class TestBlastRadiusDeterminism(unittest.TestCase):
    """Two consecutive canonical calls on the same graph must
    produce byte-equal DTOs, even after the consolidation removed
    the broken alternate path."""

    def setUp(self) -> None:
        self.store = InMemoryGraphStore()
        _populate(
            self.store,
            [
                _node("source:yfinance", NodeType.SOURCE),
                _node("signal:A", NodeType.SIGNAL),
                _node("signal:B", NodeType.SIGNAL),
                _node("score:1", NodeType.SCORE),
                _node("score:2", NodeType.SCORE),
            ],
            [
                (EdgeType.GENERATED, "signal:A", "source:yfinance"),
                (EdgeType.GENERATED, "signal:B", "source:yfinance"),
                (EdgeType.CONTRIBUTES_TO, "signal:A", "score:1"),
                (EdgeType.CONTRIBUTES_TO, "signal:B", "score:2"),
            ],
        )

    def test_two_runs_byte_equal(self) -> None:
        a = compute_blast_radius(self.store, "source:yfinance")
        b = compute_blast_radius(self.store, "source:yfinance")
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_dto_is_json_serializable(self) -> None:
        result = compute_blast_radius(self.store, "source:yfinance")
        loaded = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(loaded, result.to_dict())


# ---------------------------------------------------------------------------
# 5. Brief policy is EXACTLY the 5 dependency edges (strict-set)
# ---------------------------------------------------------------------------


class TestCanonicalPolicyStrictSet(unittest.TestCase):
    """Per Pitfall I in the Run 4A reference: the policy must be
    EXACTLY the brief's set. Use a strict-set assertion to catch
    both additions (extra edges) and removals (missing edges)."""

    BRIEF_EDGES = frozenset({
        EdgeType.GENERATED,
        EdgeType.CONTRIBUTES_TO,
        EdgeType.INFLUENCES,
        EdgeType.DERIVED_FROM,
        EdgeType.INCLUDES,
    })

    def test_policy_is_exactly_brief_set(self) -> None:
        self.assertEqual(
            frozenset(BLAST_DOWNSTREAM_SIDE.keys()),
            self.BRIEF_EDGES,
            "policy must be EXACTLY the brief's 5 dependency edges",
        )

    def test_no_entity_classification_edges(self) -> None:
        # MEMBER_OF, BELONGS_TO, EXPOSED_TO are classification,
        # not dependency.
        for et in (
            EdgeType.MEMBER_OF,
            EdgeType.BELONGS_TO,
            EdgeType.EXPOSED_TO,
        ):
            self.assertNotIn(et, BLAST_DOWNSTREAM_SIDE)

    def test_no_attribution_or_reference_edges(self) -> None:
        # CITES, REFERS_TO are attribution/reference, not
        # dependency.
        for et in (EdgeType.CITES, EdgeType.REFERS_TO):
            self.assertNotIn(et, BLAST_DOWNSTREAM_SIDE)

    def test_no_authorship_or_employment_edges(self) -> None:
        # REPORT_BY, WORKS_AT are authorship/employment, not
        # dependency.
        for et in (EdgeType.REPORT_BY, EdgeType.WORKS_AT):
            self.assertNotIn(et, BLAST_DOWNSTREAM_SIDE)


# ---------------------------------------------------------------------------
# 6. DTO shape: no entity_node_ids bucket (per Pitfall K)
# ---------------------------------------------------------------------------


class TestBlastRadiusDTOShape(unittest.TestCase):
    """The canonical DTO must NOT have an entity_node_ids bucket
    (per Pitfall K — the brief's "what may appear" list excludes
    entity nodes)."""

    def test_no_entity_bucket_in_dto(self) -> None:
        import dataclasses
        fields = {f.name for f in dataclasses.fields(BlastRadiusResult)}
        self.assertNotIn("entity_node_ids", fields)

    def test_dto_to_dict_keys_match_documented_shape(self) -> None:
        result = BlastRadiusResult(start="x")
        d = result.to_dict()
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


# ---------------------------------------------------------------------------
# 7. Canonical implementation is what other modules import
# ---------------------------------------------------------------------------


class TestCanonicalSoleImport(unittest.TestCase):
    """Search the package for any leftover imports of the removed
    names. If anyone still references them, the consolidation is
    incomplete."""

    def test_no_module_imports_legacy_blast_radius_query(self) -> None:
        import ast
        import pathlib

        root = pathlib.Path(_PROJECT_ROOT)
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            # Skip generated / migration / archive / tests that
            # legitimately test the *absence* of the name.
            rel = path.relative_to(root)
            parts = rel.parts
            if any(
                p in parts
                for p in (
                    "__pycache__",
                    "migrations",
                    "test_graph_query_consolidation.py",
                )
            ):
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except Exception:
                continue
            try:
                tree = ast.parse(src, filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name in (
                            "BlastRadiusQuery",
                            "query_blast_radius",
                        ):
                            offenders.append(
                                f"{rel}: from {node.module or '?'} "
                                f"import {alias.name}"
                            )
        if offenders:
            self.fail(
                "Leftover imports of removed legacy symbols:\n  "
                + "\n  ".join(offenders)
            )


# ---------------------------------------------------------------------------
# 8. SQLite parity after consolidation
# ---------------------------------------------------------------------------


class TestBlastRadiusStoresParity(unittest.TestCase):
    """The canonical implementation must produce identical results
    on both in-memory and SQLite stores. The parity test from
    test_blast_radius covers this end-to-end; here we run a quick
    subset to confirm consolidation didn't break the contract."""

    def test_parity_subset(self) -> None:
        nodes = [
            _node("source:yfinance", NodeType.SOURCE),
            _node("signal:2330:pe", NodeType.SIGNAL),
            _node("score:co:2330:2026-07-11", NodeType.SCORE),
        ]
        edges = [
            (EdgeType.GENERATED, "signal:2330:pe", "source:yfinance"),
            (EdgeType.CONTRIBUTES_TO, "signal:2330:pe",
             "score:co:2330:2026-07-11"),
        ]

        in_mem = InMemoryGraphStore()
        _populate(in_mem, nodes, edges)

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        sql = SQLiteGraphStore(os.path.join(td.name, "p.db"))
        self.addCleanup(sql.close)
        _populate(sql, nodes, edges)

        a = compute_blast_radius(in_mem, "source:yfinance").to_dict()
        b = compute_blast_radius(sql, "source:yfinance").to_dict()
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
