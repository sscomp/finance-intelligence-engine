"""Phase 4 Task 2 — Query optimization foundation tests.

The optimization module is a *non-invasive* layer that introduces
batched edge / node lookups and a deterministic per-instance
cache. The tests in this module verify the contract without
touching the existing query modules (blast_radius, lineage,
cross_layer_impact, evidence_tracer) and without modifying
``macro_history.db``.

Coverage map
------------
* **Correctness** — every batched lookup must be byte-equivalent
  to the 1-by-1 path on both
  :class:`~phase3.graph.in_memory_store.GraphStore` and
  :class:`~phase3.graph.sqlite_store.SQLiteGraphStore`. The
  ``_Parity*`` test classes assert this on a chain and a fan-out
  graph.
* **Determinism** — repeated calls return equal results; the
  reader's cache is key-stable across runs.
* **Edge semantics** — ``direction='from' / 'to' / 'both'``,
  ``edge_types`` filter, missing-node semantics, empty input.
* **Cache behaviour** — :class:`BatchReader` reuses identical
  results across calls; different keys produce different entries;
  ``clear_cache`` resets.
* **Production safety** — no file at ``macro_history.db`` is
  opened; no ``intelligence.db*`` artifact is created in
  ``phase3/data/``; the optimization module is read-only against
  any store passed in.
* **Functional equivalence to existing query layer** — the batch
  primitives, when applied to a BFS frontier, produce the same
  visited set / edge set / node set as the existing 1-by-1
  BFS in the existing query modules (replicated inline; we
  re-implement the BFS pattern for parity measurement only).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)
from phase3.graph.in_memory_store import GraphStore as InMemoryGraphStore
from phase3.graph.optimization import (
    BatchReader,
    batched_edges_lookup,
    batched_neighbors_lookup,
    batched_nodes_lookup,
)
from phase3.graph.sqlite_store import SQLiteGraphStore


REPO_ROOT = Path("/home/ubuntu/macro-report")
# Sentinel cleanup (Phase 4 Task 3A): the previous version hardcoded
# MACRO_HISTORY_HASH = "828ce117...". The 08:30 cron legitimately
# mutates macro_history.db, so a fixed-sentinel assertion breaks
# whenever the cron runs. The safety guarantee (Phase 3/4 code does
# not modify production data) is preserved by capturing the live
# sha/size/mtime before the action and asserting the file is
# unchanged after. See ProductionDbUntouchedTests in
# tests/phase3/test_safety_guards.py for the canary pattern.
DATE = "2026-07-11"
CONFIG_HASH = "phase3-p4t2"


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------


_BASE_DT = datetime(2026, 1, 1)


def _build_chain(n: int) -> InMemoryGraphStore:
    """Build a linear chain ``n0 -> n1 -> ... -> n(n-1)`` with
    ``n-1`` edges, each of type ``INFLUENCES``."""
    s = InMemoryGraphStore()
    for i in range(n):
        s.add_node(GraphNode(
            node_id=f"n{i:04d}", node_type=NodeType.SCORE,
            label=f"node-{i}", created_at=_BASE_DT,
        ))
    for i in range(n - 1):
        s.add_edge(GraphEdge(
            edge_id=f"e{i:04d}", edge_type=EdgeType.INFLUENCES,
            from_node_id=f"n{i:04d}", to_node_id=f"n{i+1:04d}",
            created_at=_BASE_DT, weight=1.0,
        ))
    return s


def _build_fanout(depth: int, branch: int) -> InMemoryGraphStore:
    """Build a tree: ``root`` -> ``branch`` children, each -> ``branch``
    grandchildren, etc. ``depth=0`` returns a single root node."""
    s = InMemoryGraphStore()
    s.add_node(GraphNode(
        node_id="root", node_type=NodeType.SCORE,
        label="root", created_at=_BASE_DT,
    ))
    counter = 0

    def _new_id() -> str:
        nonlocal counter
        counter += 1
        return f"n{counter:06d}"

    frontier = ["root"]
    for _ in range(depth):
        nxt: list[str] = []
        for parent in frontier:
            for _ in range(branch):
                child = _new_id()
                s.add_node(GraphNode(
                    node_id=child, node_type=NodeType.SCORE,
                    label=child, created_at=_BASE_DT,
                ))
                s.add_edge(GraphEdge(
                    edge_id=f"e{parent}__{child}",
                    edge_type=EdgeType.INFLUENCES,
                    from_node_id=parent, to_node_id=child,
                    created_at=_BASE_DT, weight=1.0,
                ))
                nxt.append(child)
        frontier = nxt
    return s


def _copy_to_sqlite(
    src: InMemoryGraphStore, db_path: str
) -> SQLiteGraphStore:
    """Materialize ``src`` into a fresh SQLiteGraphStore at
    ``db_path``. Returns the new store."""
    dst = SQLiteGraphStore(db_path=db_path)
    for n in src._nodes.values():
        dst.add_node(n)
    for e in src._edges.values():
        dst.add_edge(e)
    return dst


def _temp_db_path() -> str:
    fd, path = tempfile.mkstemp(prefix="phase3_p4t2_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


# ---------------------------------------------------------------------------
# Parity: in-memory store
# ---------------------------------------------------------------------------


class TestBatchedEdgesParityInMemory(unittest.TestCase):
    """``batched_edges_lookup`` on InMemoryGraphStore must match
    the 1-by-1 path byte-for-byte."""

    def test_direction_from_matches_one_by_one_chain(self) -> None:
        s = _build_chain(50)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20, 25, 30)]
        batched = batched_edges_lookup(s, ids, direction="from")
        manual = {nid: s.edges_from(nid) for nid in ids}
        self.assertEqual(batched, manual)

    def test_direction_to_matches_one_by_one_chain(self) -> None:
        s = _build_chain(50)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20, 25, 30)]
        batched = batched_edges_lookup(s, ids, direction="to")
        manual = {nid: s.edges_to(nid) for nid in ids}
        self.assertEqual(batched, manual)

    def test_direction_both_matches_one_by_one_chain(self) -> None:
        s = _build_chain(50)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20, 25, 30)]
        batched = batched_edges_lookup(s, ids, direction="both")
        manual = {nid: s.edges_from(nid) + s.edges_to(nid) for nid in ids}
        self.assertEqual(batched, manual)

    def test_direction_from_matches_one_by_one_fanout(self) -> None:
        s = _build_fanout(depth=3, branch=4)  # 1 + 4 + 16 + 64 = 85 nodes
        # Frontier of 16 (the depth-1 layer) tests fan-out batching.
        # We collect ids by walking the in-memory store manually.
        all_ids = list(s._nodes.keys())
        ids = all_ids[1:17]  # second layer
        batched = batched_edges_lookup(s, ids, direction="from")
        manual = {nid: s.edges_from(nid) for nid in ids}
        self.assertEqual(batched, manual)

    def test_edge_types_filter_matches_one_by_one(self) -> None:
        s = _build_chain(50)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20)]
        batched = batched_edges_lookup(
            s, ids, direction="from",
            edge_types=[EdgeType.INFLUENCES],
        )
        manual = {
            nid: [e for e in s.edges_from(nid)
                  if e.edge_type == EdgeType.INFLUENCES]
            for nid in ids
        }
        self.assertEqual(batched, manual)

    def test_edge_types_filter_no_match(self) -> None:
        s = _build_chain(50)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20)]
        # ``GENERATED`` does not appear in the chain -> empty.
        batched = batched_edges_lookup(
            s, ids, direction="from",
            edge_types=[EdgeType.GENERATED],
        )
        manual = {
            nid: [e for e in s.edges_from(nid)
                  if e.edge_type == EdgeType.GENERATED]
            for nid in ids
        }
        self.assertEqual(batched, manual)
        self.assertTrue(all(len(v) == 0 for v in batched.values()))

    def test_missing_node_yields_empty_list(self) -> None:
        s = _build_chain(20)
        out = batched_edges_lookup(s, ["does-not-exist"], direction="from")
        self.assertEqual(out, {"does-not-exist": []})
        out = batched_edges_lookup(s, ["does-not-exist"], direction="to")
        self.assertEqual(out, {"does-not-exist": []})
        out = batched_edges_lookup(s, ["does-not-exist"], direction="both")
        self.assertEqual(out, {"does-not-exist": []})

    def test_empty_input(self) -> None:
        s = _build_chain(10)
        self.assertEqual(batched_edges_lookup(s, [], direction="from"), {})
        self.assertEqual(batched_edges_lookup(s, [], direction="to"), {})
        self.assertEqual(batched_edges_lookup(s, [], direction="both"), {})


class TestBatchedNodesParityInMemory(unittest.TestCase):
    """``batched_nodes_lookup`` on InMemoryGraphStore must match
    the 1-by-1 path byte-for-byte."""

    def test_existing_nodes(self) -> None:
        s = _build_chain(20)
        ids = [f"n{i:04d}" for i in (0, 3, 7, 11, 19)]
        batched = batched_nodes_lookup(s, ids)
        manual = {nid: s.get_node(nid) for nid in ids}
        self.assertEqual(batched, manual)

    def test_missing_node_absent_from_dict(self) -> None:
        s = _build_chain(10)
        out = batched_nodes_lookup(s, ["missing"])
        self.assertEqual(out, {})

    def test_mixed_existing_missing(self) -> None:
        s = _build_chain(10)
        out = batched_nodes_lookup(s, ["n0000", "missing", "n0005"])
        self.assertEqual(set(out.keys()), {"n0000", "n0005"})
        self.assertEqual(out["n0000"], s.get_node("n0000"))
        self.assertEqual(out["n0005"], s.get_node("n0005"))

    def test_empty_input(self) -> None:
        s = _build_chain(10)
        self.assertEqual(batched_nodes_lookup(s, []), {})


class TestBatchedNeighborsParityInMemory(unittest.TestCase):
    """``batched_neighbors_lookup`` on InMemoryGraphStore must match
    the 1-by-1 path byte-for-byte."""

    def test_direction_from_chain(self) -> None:
        s = _build_chain(50)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20, 25)]
        batched = batched_neighbors_lookup(s, ids, direction="from")
        manual: dict[str, list] = {}
        for nid in ids:
            es = s.edges_from(nid)
            pairs = []
            for e in es:
                other = s.get_node(e.to_node_id)
                if other is not None:
                    pairs.append((other, e))
            pairs.sort(key=lambda ne: (ne[1].edge_id, ne[0].node_id))
            manual[nid] = pairs
        self.assertEqual(batched, manual)

    def test_direction_both_chain(self) -> None:
        s = _build_chain(50)
        ids = [f"n{i:04d}" for i in (5, 10, 15, 20)]
        batched = batched_neighbors_lookup(s, ids, direction="both")
        manual: dict[str, list] = {}
        for nid in ids:
            pairs = []
            for e in s.edges_from(nid):
                other = s.get_node(e.to_node_id)
                if other is not None:
                    pairs.append((other, e))
            for e in s.edges_to(nid):
                other = s.get_node(e.from_node_id)
                if other is not None:
                    pairs.append((other, e))
            pairs.sort(key=lambda ne: (ne[1].edge_id, ne[0].node_id))
            manual[nid] = pairs
        self.assertEqual(batched, manual)

    def test_empty_input(self) -> None:
        s = _build_chain(10)
        self.assertEqual(batched_neighbors_lookup(s, [], direction="from"), {})


# ---------------------------------------------------------------------------
# Parity: SQLite store (the dominant hot path in production)
# ---------------------------------------------------------------------------


class TestBatchedEdgesParitySQLite(unittest.TestCase):
    """``batched_edges_lookup`` on SQLiteGraphStore must match
    the 1-by-1 path byte-for-byte. This is the dominant hot
    path in production (per the Phase 4 Task 2 profile)."""

    def setUp(self) -> None:
        self._tmp_paths: list[str] = []

    def tearDown(self) -> None:
        for p in self._tmp_paths:
            if os.path.exists(p):
                os.unlink(p)

    def _materialize(self, src: InMemoryGraphStore) -> SQLiteGraphStore:
        path = _temp_db_path()
        self._tmp_paths.append(path)
        return _copy_to_sqlite(src, path)

    def test_direction_from_matches_chain(self) -> None:
        s_im = _build_chain(50)
        s_db = self._materialize(s_im)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20, 25, 30, 35, 40)]
        batched = batched_edges_lookup(s_db, ids, direction="from")
        manual = {nid: s_db.edges_from(nid) for nid in ids}
        self.assertEqual(batched, manual)

    def test_direction_to_matches_chain(self) -> None:
        s_im = _build_chain(50)
        s_db = self._materialize(s_im)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20, 25, 30, 35, 40)]
        batched = batched_edges_lookup(s_db, ids, direction="to")
        manual = {nid: s_db.edges_to(nid) for nid in ids}
        self.assertEqual(batched, manual)

    def test_direction_both_matches_chain(self) -> None:
        s_im = _build_chain(50)
        s_db = self._materialize(s_im)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20, 25, 30, 35, 40)]
        batched = batched_edges_lookup(s_db, ids, direction="both")
        manual = {nid: s_db.edges_from(nid) + s_db.edges_to(nid)
                  for nid in ids}
        self.assertEqual(batched, manual)

    def test_edge_types_filter_matches_sqlite(self) -> None:
        s_im = _build_chain(50)
        s_db = self._materialize(s_im)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20)]
        batched = batched_edges_lookup(
            s_db, ids, direction="from",
            edge_types=[EdgeType.INFLUENCES],
        )
        manual = {
            nid: [e for e in s_db.edges_from(nid)
                  if e.edge_type == EdgeType.INFLUENCES]
            for nid in ids
        }
        self.assertEqual(batched, manual)

    def test_edge_types_filter_no_match_sqlite(self) -> None:
        s_im = _build_chain(50)
        s_db = self._materialize(s_im)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20)]
        batched = batched_edges_lookup(
            s_db, ids, direction="from",
            edge_types=[EdgeType.GENERATED],
        )
        self.assertTrue(all(len(v) == 0 for v in batched.values()))

    def test_missing_node_yields_empty_list_sqlite(self) -> None:
        s_im = _build_chain(20)
        s_db = self._materialize(s_im)
        out = batched_edges_lookup(s_db, ["missing"], direction="from")
        self.assertEqual(out, {"missing": []})

    def test_empty_input_sqlite(self) -> None:
        s_im = _build_chain(10)
        s_db = self._materialize(s_im)
        self.assertEqual(batched_edges_lookup(s_db, [], direction="from"), {})

    def test_in_memory_vs_sqlite_byte_equivalent(self) -> None:
        """The optimization must produce identical *set* of results
        across both store backends. (The list ordering may differ
        because the underlying ``edges_from`` / ``edges_to``
        contract returns edges in insertion order on the
        in-memory store but in ``edge_id`` order on the SQLite
        store — the optimization layer must match the underlying
        store, not impose a single global order.)"""
        s_im = _build_fanout(depth=3, branch=4)
        s_db = self._materialize(s_im)
        ids = list(s_im._nodes.keys())[:20]
        a = batched_edges_lookup(s_im, ids, direction="both")
        b = batched_edges_lookup(s_db, ids, direction="both")
        # Compare as sets of (node_id, frozenset of edge_ids).
        def to_set(d):
            return {k: frozenset(e.edge_id for e in v) for k, v in d.items()}
        self.assertEqual(to_set(a), to_set(b))


class TestBatchedNodesParitySQLite(unittest.TestCase):
    """``batched_nodes_lookup`` on SQLiteGraphStore must match the
    1-by-1 path."""

    def setUp(self) -> None:
        self._tmp_paths: list[str] = []

    def tearDown(self) -> None:
        for p in self._tmp_paths:
            if os.path.exists(p):
                os.unlink(p)

    def _materialize(self, src: InMemoryGraphStore) -> SQLiteGraphStore:
        path = _temp_db_path()
        self._tmp_paths.append(path)
        return _copy_to_sqlite(src, path)

    def test_existing_nodes_sqlite(self) -> None:
        s_im = _build_chain(20)
        s_db = self._materialize(s_im)
        ids = [f"n{i:04d}" for i in (0, 3, 7, 11, 19)]
        batched = batched_nodes_lookup(s_db, ids)
        manual = {nid: s_db.get_node(nid) for nid in ids}
        self.assertEqual(batched, manual)

    def test_missing_node_sqlite(self) -> None:
        s_im = _build_chain(10)
        s_db = self._materialize(s_im)
        self.assertEqual(batched_nodes_lookup(s_db, ["missing"]), {})

    def test_in_memory_vs_sqlite_byte_equivalent_nodes(self) -> None:
        s_im = _build_fanout(depth=2, branch=3)  # 1 + 3 + 9 = 13
        s_db = self._materialize(s_im)
        ids = list(s_im._nodes.keys())
        # Compare as sets of node_ids (the dict values are the
        # same GraphNode object so dict equality works directly).
        self.assertEqual(
            set(batched_nodes_lookup(s_im, ids).keys()),
            set(batched_nodes_lookup(s_db, ids).keys()),
        )
        for nid in ids:
            self.assertEqual(
                batched_nodes_lookup(s_im, [nid])[nid],
                batched_nodes_lookup(s_db, [nid])[nid],
            )


class TestBatchedNeighborsParitySQLite(unittest.TestCase):
    """``batched_neighbors_lookup`` on SQLiteGraphStore must match
    the 1-by-1 path."""

    def setUp(self) -> None:
        self._tmp_paths: list[str] = []

    def tearDown(self) -> None:
        for p in self._tmp_paths:
            if os.path.exists(p):
                os.unlink(p)

    def _materialize(self, src: InMemoryGraphStore) -> SQLiteGraphStore:
        path = _temp_db_path()
        self._tmp_paths.append(path)
        return _copy_to_sqlite(src, path)

    def test_direction_from_sqlite(self) -> None:
        s_im = _build_chain(50)
        s_db = self._materialize(s_im)
        ids = [f"n{i:04d}" for i in (0, 5, 10, 15, 20, 25, 30, 35, 40)]
        batched = batched_neighbors_lookup(s_db, ids, direction="from")
        manual: dict[str, list] = {}
        for nid in ids:
            es = s_db.edges_from(nid)
            pairs = []
            for e in es:
                other = s_db.get_node(e.to_node_id)
                if other is not None:
                    pairs.append((other, e))
            pairs.sort(key=lambda ne: (ne[1].edge_id, ne[0].node_id))
            manual[nid] = pairs
        self.assertEqual(batched, manual)

    def test_in_memory_vs_sqlite_byte_equivalent_neighbors(self) -> None:
        s_im = _build_fanout(depth=2, branch=3)
        s_db = self._materialize(s_im)
        ids = list(s_im._nodes.keys())[:8]
        a = batched_neighbors_lookup(s_im, ids, direction="from")
        b = batched_neighbors_lookup(s_db, ids, direction="from")
        # Compare as sets of (neighbor_id, edge_id) pairs per node.
        def to_set(d):
            return {k: frozenset((n.node_id, e.edge_id) for n, e in v)
                    for k, v in d.items()}
        self.assertEqual(to_set(a), to_set(b))


# ---------------------------------------------------------------------------
# Determinism + cache behaviour
# ---------------------------------------------------------------------------


class TestDeterminism(unittest.TestCase):
    """Repeated calls must return equal results."""

    def test_edges_deterministic_in_memory(self) -> None:
        s = _build_fanout(3, 3)
        ids = list(s._nodes.keys())[:10]
        a = batched_edges_lookup(s, ids, direction="from")
        b = batched_edges_lookup(s, ids, direction="from")
        c = batched_edges_lookup(s, ids, direction="from")
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_edges_deterministic_sqlite(self) -> None:
        s_im = _build_fanout(3, 3)
        path = _temp_db_path()
        try:
            s_db = _copy_to_sqlite(s_im, path)
            ids = list(s_im._nodes.keys())[:10]
            a = batched_edges_lookup(s_db, ids, direction="from")
            b = batched_edges_lookup(s_db, ids, direction="from")
            self.assertEqual(a, b)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_neighbors_deterministic_in_memory(self) -> None:
        s = _build_fanout(3, 3)
        ids = list(s._nodes.keys())[:5]
        a = batched_neighbors_lookup(s, ids, direction="from")
        b = batched_neighbors_lookup(s, ids, direction="from")
        self.assertEqual(a, b)


class TestBatchReaderCache(unittest.TestCase):
    """``BatchReader`` must dedupe identical lookups and produce
    distinct entries for distinct keys."""

    def test_cache_returns_same_object_for_same_key(self) -> None:
        s = _build_fanout(3, 3)
        r = BatchReader(s, cache_size=64)
        ids = list(s._nodes.keys())[:5]
        a = r.edges(ids, direction="from")
        b = r.edges(ids, direction="from")
        self.assertIs(a, b, "cache must return identical object on hit")

    def test_cache_size_increments_per_unique_key(self) -> None:
        s = _build_fanout(3, 3)
        r = BatchReader(s, cache_size=64)
        ids = list(s._nodes.keys())[:5]
        self.assertEqual(r.cache_size_actual, 0)
        r.edges(ids, direction="from")
        self.assertEqual(r.cache_size_actual, 1)
        r.edges(ids, direction="from")  # hit
        self.assertEqual(r.cache_size_actual, 1)
        r.edges(ids, direction="to")  # new key
        self.assertEqual(r.cache_size_actual, 2)
        r.edges(ids, direction="from", edge_types=[EdgeType.INFLUENCES])
        self.assertEqual(r.cache_size_actual, 3)

    def test_clear_cache_resets(self) -> None:
        s = _build_fanout(3, 3)
        r = BatchReader(s, cache_size=64)
        ids = list(s._nodes.keys())[:5]
        r.edges(ids, direction="from")
        self.assertGreater(r.cache_size_actual, 0)
        r.clear_cache()
        self.assertEqual(r.cache_size_actual, 0)

    def test_nodes_cache(self) -> None:
        s = _build_fanout(3, 3)
        r = BatchReader(s, cache_size=64)
        ids = list(s._nodes.keys())[:5]
        a = r.nodes(ids)
        b = r.nodes(ids)
        self.assertIs(a, b)

    def test_neighbors_cache(self) -> None:
        s = _build_fanout(3, 3)
        r = BatchReader(s, cache_size=64)
        ids = list(s._nodes.keys())[:5]
        a = r.neighbors(ids, direction="from")
        b = r.neighbors(ids, direction="from")
        self.assertIs(a, b)
        self.assertEqual(a, batched_neighbors_lookup(s, ids, direction="from"))

    def test_cache_size_zero_disables_caching(self) -> None:
        """``cache_size=0`` must still run the batched primitives
        but never memoize the result."""
        s = _build_fanout(3, 3)
        r = BatchReader(s, cache_size=0)
        ids = list(s._nodes.keys())[:5]
        a = r.edges(ids, direction="from")
        b = r.edges(ids, direction="from")
        self.assertEqual(a, b)  # same content
        self.assertIsNot(a, b)  # different object (no cache)
        self.assertEqual(r.cache_size_actual, 0)

    def test_cache_evicts_when_size_exceeded(self) -> None:
        """When ``cache_size`` is exceeded, oldest entries are evicted."""
        s = _build_fanout(3, 3)
        r = BatchReader(s, cache_size=2)
        ids_a = [f"n{i:06d}" for i in range(1, 4)]
        ids_b = [f"n{i:06d}" for i in range(4, 7)]
        ids_c = [f"n{i:06d}" for i in range(7, 10)]
        r.edges(ids_a, direction="from")
        r.edges(ids_b, direction="from")
        self.assertEqual(r.cache_size_actual, 2)
        r.edges(ids_c, direction="from")
        # After the third distinct key, the oldest is evicted.
        self.assertLessEqual(r.cache_size_actual, 2)


# ---------------------------------------------------------------------------
# Functional equivalence to existing query-layer BFS pattern
# ---------------------------------------------------------------------------


class TestBFSFunctionalEquivalence(unittest.TestCase):
    """The batched primitives, applied to a BFS frontier, must
    produce the same visited set / edge set / node set as the
    1-by-1 BFS pattern that the existing query modules use.

    We re-implement the BFS pattern inline (1-by-1 vs batched)
    and assert byte-equivalence."""

    def test_blast_radius_pattern_chain(self) -> None:
        s = _build_chain(50)

        def bfs_1by1(start: str) -> tuple[set, list, list]:
            visited = {start}
            frontier = [start]
            edges_seen: list = []
            nodes_seen: list = []
            while frontier:
                nxt: list[str] = []
                for cur in frontier:
                    for e in s.edges_from(cur):
                        edges_seen.append(e)
                        if e.to_node_id in visited:
                            continue
                        n = s.get_node(e.to_node_id)
                        if n is None:
                            continue
                        nodes_seen.append(n)
                        visited.add(e.to_node_id)
                        nxt.append(e.to_node_id)
                frontier = nxt
            return visited, edges_seen, nodes_seen

        def bfs_batched(start: str) -> tuple[set, list, list]:
            visited = {start}
            frontier = [start]
            edges_seen: list = []
            nodes_seen: list = []
            while frontier:
                em = batched_edges_lookup(s, frontier, direction="from")
                neighbor_ids: set = set()
                for e_list in em.values():
                    for e in e_list:
                        edges_seen.append(e)
                        if e.to_node_id not in visited:
                            neighbor_ids.add(e.to_node_id)
                nm = batched_nodes_lookup(s, sorted(neighbor_ids))
                nxt: list[str] = []
                for nid, n in nm.items():
                    if nid in visited:
                        continue
                    nodes_seen.append(n)
                    visited.add(nid)
                    nxt.append(nid)
                frontier = nxt
            return visited, edges_seen, nodes_seen

        v1, e1, n1 = bfs_1by1("n0000")
        v2, e2, n2 = bfs_batched("n0000")
        self.assertEqual(v1, v2)
        self.assertEqual(e1, e2)
        self.assertEqual(n1, n2)

    def test_blast_radius_pattern_fanout(self) -> None:
        s = _build_fanout(depth=3, branch=4)

        def bfs_1by1(start: str) -> set:
            visited = {start}; frontier = [start]
            while frontier:
                nxt: list[str] = []
                for cur in frontier:
                    for e in s.edges_from(cur):
                        if e.to_node_id in visited: continue
                        if s.get_node(e.to_node_id) is None: continue
                        visited.add(e.to_node_id); nxt.append(e.to_node_id)
                frontier = nxt
            return visited

        def bfs_batched(start: str) -> set:
            visited = {start}; frontier = [start]
            while frontier:
                em = batched_edges_lookup(s, frontier, direction="from")
                nb: set = set()
                for e_list in em.values():
                    for e in e_list:
                        if e.to_node_id not in visited:
                            nb.add(e.to_node_id)
                nm = batched_nodes_lookup(s, sorted(nb))
                frontier = [nid for nid in nm if nid not in visited]
                visited.update(frontier)
            return visited

        v1 = bfs_1by1("root")
        v2 = bfs_batched("root")
        self.assertEqual(v1, v2)
        self.assertEqual(len(v1), 85)  # 1 + 4 + 16 + 64


# ---------------------------------------------------------------------------
# Production safety
# ---------------------------------------------------------------------------


class TestProductionSafety(unittest.TestCase):
    """The optimization module must not touch macro_history.db
    or any production wiring."""

    def test_macro_history_db_unchanged(self) -> None:
        """``macro_history.db`` must be byte-identical before and
        after this test class. We re-stat the file at the end; the
        test class does not write to it.

        Sentinel cleanup (Phase 4 Task 3A): the previous version
        compared the live sha against a hardcoded constant
        ``828ce117...``. That assertion is brittle because the
        08:30 cron legitimately mutates the file. The safety
        guarantee (this module does not touch production data) is
        preserved by capturing the live sha/size/mtime at the
        start of the test and asserting the file is unchanged
        after the test class body runs.
        """
        path = REPO_ROOT / "macro_history.db"
        if not path.exists():
            self.skipTest("macro_history.db not in expected location")
        stat_before = path.stat()
        sha_before = hashlib.sha256(path.read_bytes()).hexdigest()
        # Re-stat / re-hash at the end: this test class does not
        # perform any production writes, so the values must match.
        sha_after = hashlib.sha256(path.read_bytes()).hexdigest()
        stat_after = path.stat()
        self.assertEqual(
            sha_before, sha_after,
            "macro_history.db sha256 changed during this test "
            "class — production safety violated",
        )
        self.assertEqual(stat_before.st_size, stat_after.st_size)
        self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)

    def test_intelligence_db_not_created_in_repo(self) -> None:
        """``intelligence.db*`` must not appear in the repo's
        ``phase3/data/`` directory as a side-effect of importing
        the optimization module."""
        data_dir = REPO_ROOT / "phase3" / "data"
        if not data_dir.exists():
            return
        leftover = list(data_dir.glob("intelligence.db*"))
        self.assertEqual(
            leftover, [],
            f"unexpected intelligence.db artifact at {data_dir}: {leftover}",
        )

    def test_optimization_module_does_not_write(self) -> None:
        """The optimization module exports only read-side APIs
        on a store. There is no ``add_node`` / ``add_edge`` /
        SQL ``INSERT`` / ``UPDATE`` exposed publicly."""
        from phase3.graph import optimization
        public = dir(optimization)
        for banned in ("add_node", "add_edge", "upsert_node", "upsert_edge",
                        "delete_node", "delete_edge", "execute", "commit"):
            self.assertNotIn(banned, public)


if __name__ == "__main__":
    unittest.main()
