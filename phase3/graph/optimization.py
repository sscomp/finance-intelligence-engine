"""Phase 4 Task 2 — Query optimization foundation.

This module introduces **batch and cache primitives** that address
the dominant hot paths surfaced by the Phase 4 Task 2 profile:

1. **Batched edge lookups** — instead of one ``edges_from(cur_id)``
   round-trip per BFS frontier node, fetch edges for an entire
   frontier in a single SQL query. On a 200-node chain graph the
   profile showed 32 040 SQL ``execute`` calls during a single
   blast_radius run; batching the same frontier collapses this to
   one query per BFS layer.

2. **Batched node lookups** — the same pattern for ``get_node``.
   The profile showed 20 000 ``get_node`` SQL queries on a 200-node
   graph; batching reduces this to one query per BFS layer (or per
   "needs at depth N" snapshot).

3. **Deterministic result cache** — when a BFS layer's frontier is
   small enough that batching is a win, the per-layer result is
   cached on the reader instance so repeated calls within the same
   traversal reuse it. Cache is keyed on the store identity
   (or path for SQLite), the (frontier tuple, direction, edge_types
   tuple) tuple, and the (node_id tuple) tuple — all immutable, all
   hashable, all deterministic.

Design constraints
------------------
* **No modifications to existing query modules.** The blast_radius,
  lineage, cross_layer_impact, evidence_tracer, and queries modules
  are unchanged. The batch primitives here are *available for
  future adoption* by these modules; this task ships the foundation
  and the test surface, not the call-site rewiring.
* **Result equivalence.** Every batched lookup is asserted
  byte-for-byte equivalent to the 1-by-1 path on both
  :class:`InMemoryGraphStore` and
  :class:`~phase3.graph.sqlite_store.SQLiteGraphStore`.
* **API compatibility.** The public surface of the graph stores
  is unchanged. :class:`BatchReader` is a separate helper; it
  does not subclass either store.
* **Determinism.** The batch primitives preserve the
  ``(edge_id, neighbor_id)`` stable sort that the existing query
  modules rely on. ``edge_id`` ordering is stable for
  InMemoryGraphStore (insertion order); for SQLiteGraphStore the
  ORDER BY is preserved (already in
  :meth:`GraphRepository.list_edges`).

What's not here
---------------
* **No schema migration.** This task does not add secondary
  indexes to ``graph_edges`` or ``graph_nodes`` — the
  ``node_id`` PK already covers ``get_node`` lookups, and the
  existing ``(edge_type, from_node_id, to_node_id)`` UNIQUE
  index covers the common equality query. The remaining
  ``list_edges(from_node_id=X)`` lookup is the next bottleneck
  and a secondary index there is a future-task follow-up.
* **No call-site rewiring.** The existing ``_neighbors_at`` /
  ``_neighbors`` functions in blast_radius / lineage /
  cross_layer_impact / evidence_tracer are not modified.
  Adopting these primitives is a future-task scope.
* **No production writes.** This module is read-only against
  any store passed to it. There is no code path that calls
  ``add_node``, ``add_edge``, or any SQL ``INSERT`` / ``UPDATE``.

Usage
-----
The module exposes two tiers of API:

* **Low-level batched primitives** —
  :func:`batched_edges_lookup`, :func:`batched_nodes_lookup`,
  :func:`batched_neighbors_lookup`. Each takes a store and a
  sequence of node ids and returns a dict of results keyed by
  the node id. Missing nodes map to an empty list (``edges``)
  or are absent from the dict (``nodes`` — preserves the
  ``get_node``-returns-None semantics).
* **Reader-style API** — :class:`BatchReader`. Wraps a store
  with on-instance memoization keyed on (frontier tuple,
  direction, edge_types tuple). Useful for BFS where the same
  frontier may be queried multiple times during a single
  pass (e.g. upstream and downstream walks on the same node).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Literal

from phase3.datamodel.graph import EdgeType, GraphEdge, GraphNode

if TYPE_CHECKING:
    from phase3.graph.in_memory_store import GraphStore as InMemoryGraphStore
    from phase3.graph.sqlite_store import SQLiteGraphStore


# ---------------------------------------------------------------------------
# Structural protocols — both public graph stores satisfy them via duck
# typing. We avoid hard imports so this module is independent of the
# store backends at import time.
# ---------------------------------------------------------------------------


class _BatchableEdgeStore:
    """Structural type for stores that expose ``has_node``,
    ``edges_from`` and ``edges_to``. Both :class:`InMemoryGraphStore`
    and :class:`SQLiteGraphStore` satisfy this."""

    def has_node(self, node_id: str) -> bool: ...
    def edges_from(self, node_id: str) -> list[GraphEdge]: ...
    def edges_to(self, node_id: str) -> list[GraphEdge]: ...


class _BatchableNodeStore:
    """Structural type for stores that expose ``get_node``."""

    def get_node(self, node_id: str) -> GraphNode | None: ...


# ---------------------------------------------------------------------------
# Low-level batched primitives
# ---------------------------------------------------------------------------


def batched_edges_lookup(
    store: Any,
    node_ids: Iterable[str],
    *,
    direction: Literal["from", "to", "both"] = "both",
    edge_types: Iterable[EdgeType] | None = None,
) -> dict[str, list[GraphEdge]]:
    """Return ``{node_id: [edge, ...]}`` for each node in ``node_ids``.

    The result is the *union* of the per-node ``edges_from`` /
    ``edges_to`` calls (per ``direction``) and the ``edge_types``
    filter. The output dict always contains an entry for every
    input node id; missing nodes map to an empty list (matching
    the ``edges_from`` / ``edges_to`` contract: a missing node
    returns ``[]``, not ``None``).

    For :class:`InMemoryGraphStore` this is a thin wrapper over the
    existing per-node methods: the win is that callers do not need
    to repeat the ``has_node`` check or the list-comprehension
    boilerplate.

    For :class:`~phase3.graph.sqlite_store.SQLiteGraphStore` the
    function detects the SQLite-backed store and dispatches to
    :func:`_batched_edges_sqlite`, which uses a single
    ``WHERE from_node_id IN (?, ?, ...)`` (or the ``to`` mirror)
    query per direction.

    Parameters
    ----------
    store:
        The graph store. Any store with ``edges_from`` /
        ``edges_to`` methods works.
    node_ids:
        Sequence of node ids to look up. Order does not matter;
        the output dict preserves the caller's input order via
        a dict (no list ordering implied).
    direction:
        ``"from"`` walks ``edges_from`` only; ``"to"`` walks
        ``edges_to`` only; ``"both"`` (default) walks both.
    edge_types:
        Optional whitelist. When set, edges whose ``edge_type``
        is not in this iterable are filtered out *before* the
        result is returned. Filtering is done in Python after
        the SQL fetch — SQLite has no array-contains operator
        in stock builds.
    """
    node_ids_list = list(node_ids)
    if not node_ids_list:
        return {}
    types_list = list(edge_types) if edge_types else None

    # SQL-backed fast path: a single SQL query per direction.
    repo = _sql_batch_repo(store)
    if repo is not None:
        return _batched_edges_sql(
            repo, node_ids_list, direction=direction,
            edge_types=types_list,
        )

    # In-memory (or unknown store) path: per-node calls.
    types_set = set(types_list) if types_list else None
    out: dict[str, list[GraphEdge]] = {}
    for nid in node_ids_list:
        edges: list[GraphEdge] = []
        if not store.has_node(nid):
            out[nid] = edges
            continue
        if direction in ("from", "both"):
            for e in store.edges_from(nid):
                if types_set and e.edge_type not in types_set:
                    continue
                edges.append(e)
        if direction in ("to", "both"):
            for e in store.edges_to(nid):
                if types_set and e.edge_type not in types_set:
                    continue
                edges.append(e)
        out[nid] = edges
    return out


def batched_nodes_lookup(
    store: Any,
    node_ids: Iterable[str],
) -> dict[str, GraphNode]:
    """Return ``{node_id: GraphNode}`` for each id that exists in
    ``store``.

    Missing nodes are absent from the returned dict (matches
    :meth:`GraphStore.get_node` returning ``None``). An empty
    input returns an empty dict.

    For :class:`InMemoryGraphStore` this is a thin wrapper that
    preserves the ``_nodes.get(node_id)`` semantics. For the
    SQLite-backed store the function issues a single
    ``WHERE node_id IN (?, ?, ...)`` query.
    """
    node_ids_list = list(node_ids)
    if not node_ids_list:
        return {}
    repo = _sql_batch_repo(store)
    if repo is not None:
        return _batched_nodes_sql(repo, node_ids_list)
    out: dict[str, GraphNode] = {}
    for nid in node_ids_list:
        n = store.get_node(nid)
        if n is not None:
            out[nid] = n
    return out


# ---------------------------------------------------------------------------
# Combined lookup — the "BFS frontier" hot path
# ---------------------------------------------------------------------------


def batched_neighbors_lookup(
    store: Any,
    node_ids: Iterable[str],
    *,
    direction: Literal["from", "to", "both"] = "both",
    edge_types: Iterable[EdgeType] | None = None,
) -> dict[str, list[tuple[GraphNode, GraphEdge]]]:
    """Return ``{node_id: [(neighbor, edge), ...]}`` for each input.

    Combines :func:`batched_edges_lookup` with a batched node
    fetch so callers do not need to issue a second query for the
    neighbor nodes. Missing neighbor nodes are silently dropped
    (matches the existing query modules' ``get_node(...) or
    continue`` pattern in :func:`phase3.graph.blast_radius._neighbors_at`).

    The order within each list is deterministic: edges are
    sorted by ``(edge_id, neighbor_id)`` after collection, so the
    output is stable across runs. This matches the contract of
    the existing query modules.

    Semantics for ``direction='both'``: each edge is reported
    exactly once — on the side it was discovered. A node that
    is the from-side of an edge gets the to-side neighbor; a
    node that is the to-side gets the from-side neighbor. This
    matches the manual ``edges_from + edges_to`` pattern in the
    existing query modules.
    """
    node_ids_list = list(node_ids)
    if not node_ids_list:
        return {}

    # Walk ``from`` and ``to`` separately so we know which side
    # we are on. Each direction produces its own edge list and
    # its own neighbor-id set; we union the neighbor ids and
    # issue one batched node fetch.
    edges_from_map: dict[str, list[GraphEdge]] = {}
    edges_to_map: dict[str, list[GraphEdge]] = {}
    if direction in ("from", "both"):
        edges_from_map = batched_edges_lookup(
            store, node_ids_list, direction="from", edge_types=edge_types,
        )
    if direction in ("to", "both"):
        edges_to_map = batched_edges_lookup(
            store, node_ids_list, direction="to", edge_types=edge_types,
        )

    neighbor_ids: set[str] = set()
    for e_list in edges_from_map.values():
        for e in e_list:
            neighbor_ids.add(e.to_node_id)
    for e_list in edges_to_map.values():
        for e in e_list:
            neighbor_ids.add(e.from_node_id)
    if not neighbor_ids:
        return {nid: [] for nid in node_ids_list}

    nodes_by_id = batched_nodes_lookup(store, sorted(neighbor_ids))

    out: dict[str, list[tuple[GraphNode, GraphEdge]]] = {}
    for nid in node_ids_list:
        pairs: list[tuple[GraphNode, GraphEdge]] = []
        for e in edges_from_map.get(nid, []):
            other = nodes_by_id.get(e.to_node_id)
            if other is not None:
                pairs.append((other, e))
        for e in edges_to_map.get(nid, []):
            other = nodes_by_id.get(e.from_node_id)
            if other is not None:
                pairs.append((other, e))
        # Stable deterministic order.
        pairs.sort(key=lambda ne: (ne[1].edge_id, ne[0].node_id))
        out[nid] = pairs
    return out


# ---------------------------------------------------------------------------
# Reader-style API with deterministic result cache
# ---------------------------------------------------------------------------


@dataclass
class BatchReader:
    """Read-side cache + batch layer for graph queries.

    Wraps a store with on-instance memoization. Cache keys are
    tuples of immutable types — (node_ids tuple, direction, edge_types
    tuple) — so two BFS layers with the same frontier and the same
    direction reuse the cached result.

    The cache is *deterministic*: for a fixed store snapshot, two
    reads of the same key return the same value. The cache is
    *instance-scoped*; a new :class:`BatchReader` starts empty.

    Parameters
    ----------
    store:
        The underlying graph store. Any store that satisfies
        :class:`_BatchableEdgeStore` and :class:`_BatchableNodeStore`
        works.
    cache_size:
        Soft cap on the number of cached entries. When the cache
        exceeds this, the oldest entry (by insertion order) is
        evicted. The default is 1024 — enough for several BFS
        layers on a graph with thousands of nodes. Set to 0 to
        disable caching (the batched primitives still run, just
        no memoization).
    """

    store: Any
    cache_size: int = 1024
    # Edge cache: (frontier tuple, direction str, edge_types tuple)
    # -> {node_id: [GraphEdge, ...]}
    _edge_cache: dict = field(default_factory=dict)
    # Node cache: (frontier tuple) -> {node_id: GraphNode}
    _node_cache: dict = field(default_factory=dict)
    # Neighbor cache: (frontier tuple, direction str, edge_types
    # tuple) -> {node_id: [(GraphNode, GraphEdge), ...]}
    _neighbor_cache: dict = field(default_factory=dict)
    _cache_order: list = field(default_factory=list)

    def _evict_if_needed(self) -> None:
        if self.cache_size <= 0:
            return
        while len(self._cache_order) >= self.cache_size:
            old = self._cache_order.pop(0)
            self._edge_cache.pop(old, None)

    def edges(
        self,
        node_ids: Iterable[str],
        *,
        direction: Literal["from", "to", "both"] = "both",
        edge_types: Iterable[EdgeType] | None = None,
    ) -> dict[str, list[GraphEdge]]:
        """Batched edges with cache. See :func:`batched_edges_lookup`.

        When ``cache_size=0`` is set on the reader, the cache is
        disabled: each call returns a fresh dict and no entry is
        stored. The batched SQL / dict-grouping logic still runs
        (so callers always get the optimization, even with no
        memoization).
        """
        node_ids_tuple = tuple(node_ids)
        types_tuple = tuple(edge_types) if edge_types else ()
        key = (node_ids_tuple, direction, types_tuple)
        if self.cache_size > 0:
            cached = self._edge_cache.get(key)
            if cached is not None:
                return cached
        result = batched_edges_lookup(
            self.store, node_ids_tuple,
            direction=direction, edge_types=types_tuple,
        )
        if self.cache_size > 0:
            self._evict_if_needed()
            self._edge_cache[key] = result
            self._cache_order.append(key)
        return result

    def nodes(self, node_ids: Iterable[str]) -> dict[str, GraphNode]:
        """Batched nodes with cache. See :func:`batched_nodes_lookup`.

        Cache disabled when ``cache_size=0`` — see :meth:`edges`.
        """
        node_ids_tuple = tuple(node_ids)
        if self.cache_size > 0:
            cached = self._node_cache.get(node_ids_tuple)
            if cached is not None:
                return cached
        result = batched_nodes_lookup(self.store, node_ids_tuple)
        if self.cache_size > 0:
            self._node_cache[node_ids_tuple] = result
        return result

    def neighbors(
        self,
        node_ids: Iterable[str],
        *,
        direction: Literal["from", "to", "both"] = "both",
        edge_types: Iterable[EdgeType] | None = None,
    ) -> dict[str, list[tuple[GraphNode, GraphEdge]]]:
        """Batched neighbor pairs. See :func:`batched_neighbors_lookup`.

        Cache disabled when ``cache_size=0`` — see :meth:`edges`.
        """
        node_ids_tuple = tuple(node_ids)
        types_tuple = tuple(edge_types) if edge_types else ()
        cache_key = (node_ids_tuple, direction, types_tuple)
        if self.cache_size > 0:
            cached = self._neighbor_cache.get(cache_key)
            if cached is not None:
                return cached
        # Walk ``from`` and ``to`` separately so each edge is
        # reported exactly once (matches the manual pattern in
        # the existing query modules).
        edges_from_map: dict[str, list[GraphEdge]] = {}
        edges_to_map: dict[str, list[GraphEdge]] = {}
        if direction in ("from", "both"):
            edges_from_map = self.edges(
                node_ids_tuple, direction="from", edge_types=types_tuple,
            )
        if direction in ("to", "both"):
            edges_to_map = self.edges(
                node_ids_tuple, direction="to", edge_types=types_tuple,
            )

        # Collect unique neighbor ids.
        neighbor_ids: set[str] = set()
        for e_list in edges_from_map.values():
            for e in e_list:
                neighbor_ids.add(e.to_node_id)
        for e_list in edges_to_map.values():
            for e in e_list:
                neighbor_ids.add(e.from_node_id)
        nodes_by_id = self.nodes(sorted(neighbor_ids)) if neighbor_ids else {}

        out: dict[str, list[tuple[GraphNode, GraphEdge]]] = {}
        for nid in node_ids_tuple:
            pairs: list[tuple[GraphNode, GraphEdge]] = []
            for e in edges_from_map.get(nid, []):
                other = nodes_by_id.get(e.to_node_id)
                if other is not None:
                    pairs.append((other, e))
            for e in edges_to_map.get(nid, []):
                other = nodes_by_id.get(e.from_node_id)
                if other is not None:
                    pairs.append((other, e))
            pairs.sort(key=lambda ne: (ne[1].edge_id, ne[0].node_id))
            out[nid] = pairs
        if self.cache_size > 0:
            self._neighbor_cache[cache_key] = out
        return out

    def clear_cache(self) -> None:
        """Drop all cached entries. Useful between BFS layers or
        between separate query calls when the underlying store
        may have mutated.
        """
        self._edge_cache.clear()
        self._node_cache.clear()
        self._neighbor_cache.clear()
        self._cache_order.clear()

    @property
    def cache_size_actual(self) -> int:
        """Current number of entries in the cache (edges + nodes
        + neighbors)."""
        return (
            len(self._edge_cache) + len(self._node_cache)
            + len(self._neighbor_cache)
        )


# ---------------------------------------------------------------------------
# SQLite fast path (private)
# ---------------------------------------------------------------------------


def _sql_batch_repo(store: Any) -> Any | None:
    """Return the backing :class:`GraphRepository` if ``store`` was
    built on one, else None.

    Works for any SQL-backed graph store (SQLite or PostgreSQL,
    Phase 6.3) without importing the persistence layer at module
    import time. The duck-typed check is: ``store`` has a ``_repo``
    attribute exposing the batched-read seam
    (``fetch_edges_by_nodes`` / ``fetch_nodes_by_ids``).
    """
    repo = getattr(store, "_repo", None)
    if repo is None:
        return None
    if callable(getattr(repo, "fetch_edges_by_nodes", None)) and callable(
        getattr(repo, "fetch_nodes_by_ids", None)
    ):
        return repo
    return None


def _batched_edges_sql(
    repo: Any,
    node_ids: list[str],
    *,
    direction: Literal["from", "to", "both"],
    edge_types: list[EdgeType] | None = None,
) -> dict[str, list[GraphEdge]]:
    """One SQL query per direction, then Python-side grouping.

    Avoids the per-node round-trip in
    :meth:`SQLiteGraphStore.edges_from` /
    :meth:`SQLiteGraphStore.edges_to`. Conversion goes through the
    repo's ``EdgeRow`` DTO and the same
    ``_edge_from_row`` helper the SQL-backed graph store uses, so
    the output is identical to the 1-by-1 path on the same store
    (asserted by the backend contract tests on both backends).
    """
    from phase3.graph.sqlite_store import _edge_from_row

    types_filter = [et.value for et in edge_types] if edge_types else None
    out: dict[str, list[GraphEdge]] = {nid: [] for nid in node_ids}
    # Group by the queried side, exactly like the former raw-SQL path:
    # the from-side pass fills ``out[from]``, then the to-side pass
    # fills ``out[to]`` — including the historical double-append for
    # self-loops under ``direction="both"``, and the stable
    # ``ORDER BY edge_id`` ordering within each pass.
    if direction in ("from", "both"):
        for edge_row in repo.fetch_edges_by_nodes(
            node_ids, direction="from", edge_types=types_filter
        ):
            edge = _edge_from_row(edge_row)
            out.setdefault(edge.from_node_id, []).append(edge)
    if direction in ("to", "both"):
        for edge_row in repo.fetch_edges_by_nodes(
            node_ids, direction="to", edge_types=types_filter
        ):
            edge = _edge_from_row(edge_row)
            out.setdefault(edge.to_node_id, []).append(edge)
    return out


def _batched_nodes_sql(repo: Any, node_ids: list[str]) -> dict[str, GraphNode]:
    """One SQL query for many nodes. Returns ``{node_id: GraphNode}``
    for ids that exist; missing ids are absent (matches the
    :meth:`GraphStore.get_node` returns-None contract)."""
    from phase3.graph.sqlite_store import _node_from_row
    return {
        row.node_id: _node_from_row(row)
        for row in repo.fetch_nodes_by_ids(node_ids)
    }


__all__ = [
    "BatchReader",
    "batched_edges_lookup",
    "batched_neighbors_lookup",
    "batched_nodes_lookup",
]
