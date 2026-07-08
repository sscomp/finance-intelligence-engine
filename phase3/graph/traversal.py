"""Graph traversal primitives: BFS, shortest_path.

Pure functions over a GraphStore. BFS and shortest_path both work for
unweighted graphs. For weighted shortest path the spec mentions Dijkstra
but Phase 3A only needs unweighted (BFS).
"""
from __future__ import annotations

from collections import deque
from typing import Literal

from phase3.datamodel.graph import EdgeType, GraphNode
from phase3.graph.in_memory_store import GraphStore


def bfs(
    store: GraphStore,
    start: str,
    max_depth: int = 3,
    edge_types: list[EdgeType] | None = None,
    direction: Literal["out", "in", "both"] = "out",
) -> list[GraphNode]:
    """Breadth-first traversal from `start`.

    Returns the list of nodes reachable within `max_depth` hops, EXCLUDING
    the start node itself. The first occurrence of each node in the BFS
    order is what we return (so a node found at depth 2 will not be
    re-yielded if there's a shorter path via depth 1).
    """
    if not store.has_node(start):
        return []
    visited: set[str] = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    result: list[GraphNode] = []
    while queue:
        cur, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for neighbor, _edge in store.get_neighbors(cur, edge_types, direction):
            if neighbor.node_id in visited:
                continue
            visited.add(neighbor.node_id)
            result.append(neighbor)
            queue.append((neighbor.node_id, depth + 1))
    return result


def shortest_path(
    store: GraphStore,
    from_id: str,
    to_id: str,
    edge_types: list[EdgeType] | None = None,
    direction: Literal["out", "in", "both"] = "out",
) -> list[str] | None:
    """Unweighted shortest path. Returns the list of node_ids from
    `from_id` to `to_id` (inclusive on both ends), or None if unreachable.
    """
    if not store.has_node(from_id) or not store.has_node(to_id):
        return None
    if from_id == to_id:
        return [from_id]
    visited: set[str] = {from_id}
    # BFS keeping parents for backtrack
    queue: deque[tuple[str, list[str]]] = deque([(from_id, [from_id])])
    while queue:
        cur, path = queue.popleft()
        for neighbor, _edge in store.get_neighbors(cur, edge_types, direction):
            if neighbor.node_id in visited:
                continue
            visited.add(neighbor.node_id)
            new_path = path + [neighbor.node_id]
            if neighbor.node_id == to_id:
                return new_path
            queue.append((neighbor.node_id, new_path))
    return None


__all__ = ["bfs", "shortest_path"]
