"""Evidence Trace Export — Phase 3B Task 4 Run 3.

JSON-serializable export of :class:`EvidenceChain` plus a
:class:`PipelineResult` → :class:`EvidenceChain` adapter that uses the
canonical :func:`~phase3.pipeline.graph_writer.make_score_node_id` for
the start node id.

Why a separate module
---------------------
The :class:`EvidenceChain` already has a human-readable ``to_text()``
method (Phase 3A), but no machine-readable form. Run 3 adds:

* :func:`chain_to_dict` — JSON-serializable payload of an
  :class:`EvidenceChain` (lists, plain dicts, ISO timestamps).
* :class:`EvidenceChainAdapter` — given a :class:`PipelineResult`
  (already produced by :class:`ScoringPipeline`) and a graph store,
  compute the score node id, run an upstream trace, and return the
  :class:`EvidenceChain`. This is the load-bearing integration: it
  closes the loop between the PipelineResult envelope and the
  EvidenceTracer's BFS machinery without modifying either.

Why the adapter uses ``GraphWriterStore`` (or any GraphStore)
------------------------------------------------------------
The adapter needs to call ``EvidenceTracer(store).trace(score_node_id,
...)``. The store must satisfy :class:`EvidenceTracer`'s constructor
(:class:`~phase3.graph.in_memory_store.GraphStore`-shaped). In
production that will be :class:`~phase3.graph.sqlite_store.SQLiteGraphStore`;
in tests we use the in-memory store. The structural type check is
duck-typed; we don't add a new Protocol.

No production deployment, no network, no production DB writes.
This module is pure read against the graph store; the writer side
is the existing :class:`GraphWriter` and is untouched here.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Mapping

from phase3.datamodel.graph import EdgeType, GraphEdge, GraphNode, NodeType
from phase3.graph.evidence_tracer import (
    EVIDENCE_UPSTREAM_EDGE_TYPES,
    EvidenceChain,
    EvidenceTracer,
)
from phase3.graph.in_memory_store import GraphStore
from phase3.pipeline.graph_writer import make_score_node_id
from phase3.pipeline.scoring_pipeline import PipelineResult


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    """Render a datetime as ISO 8601 (or None for the unset case)."""
    if dt is None:
        return None
    return dt.isoformat()


def _node_to_dict(node: GraphNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "node_type": node.node_type.value,
        "label": node.label,
        "created_at": _iso(node.created_at),
        "metadata": dict(node.metadata),
        "tags": list(node.tags),
        "schema_version": node.schema_version,
    }


def _edge_to_dict(edge: GraphEdge) -> dict[str, Any]:
    return {
        "edge_id": edge.edge_id,
        "edge_type": edge.edge_type.value,
        "from_node_id": edge.from_node_id,
        "to_node_id": edge.to_node_id,
        "weight": edge.weight,
        "metadata": dict(edge.metadata),
        "created_at": _iso(edge.created_at),
        "schema_version": edge.schema_version,
    }


def chain_to_dict(
    chain: EvidenceChain,
    *,
    include_nodes: bool = True,
    include_edges: bool = True,
) -> dict[str, Any]:
    """Render an :class:`EvidenceChain` as a JSON-serializable dict.

    Parameters
    ----------
    chain:
        The chain to render.
    include_nodes:
        If False, the ``nodes`` list is omitted (kept lightweight for
        the case where the caller only needs the typed buckets).
    include_edges:
        If False, the ``edges`` list is omitted.

    Returns
    -------
    A dict that round-trips through :func:`json.dumps` without
    further conversion. Keys mirror :class:`EvidenceChain` fields so
    the output is easy to diff against the dataclass.

    The "truncated" boolean and the "warnings" list are always
    included — they are the load-bearing evidence that the trace may
    be incomplete.
    """
    payload: dict[str, Any] = {
        "start": chain.start,
        "truncated": chain.truncated,
        "depth_reached": chain.depth_reached,
        "leaf_node_ids": list(chain.leaf_node_ids),
        "visited_node_ids": list(chain.visited_node_ids),
        "traversed_edge_ids": list(chain.traversed_edge_ids),
        "source_node_ids": list(chain.source_node_ids),
        "signal_node_ids": list(chain.signal_node_ids),
        "entity_node_ids": list(chain.entity_node_ids),
        "score_node_ids": list(chain.score_node_ids),
        "warnings": list(chain.warnings),
    }
    if include_nodes:
        payload["nodes"] = [_node_to_dict(n) for n in chain.nodes]
    if include_edges:
        payload["edges"] = [_edge_to_dict(e) for e in chain.edges]
    return payload


def chain_to_json(
    chain: EvidenceChain,
    *,
    indent: int | None = 2,
    include_nodes: bool = True,
    include_edges: bool = True,
) -> str:
    """Render an :class:`EvidenceChain` as a JSON string."""
    import json

    return json.dumps(
        chain_to_dict(chain, include_nodes=include_nodes,
                      include_edges=include_edges),
        ensure_ascii=False,
        indent=indent,
        sort_keys=False,
        default=str,  # belt-and-suspenders for any unrendered datetimes
    )


# ---------------------------------------------------------------------------
# PipelineResult → EvidenceChain adapter
# ---------------------------------------------------------------------------


def score_node_id_for_result(result: PipelineResult) -> str:
    """Compute the canonical score node id for a :class:`PipelineResult`.

    Pulls ``scorer_type`` and ``entity_id`` off the breakdown and uses
    the ``date_bucket`` from the result metadata (falls back to the
    breakdown ``timestamp`` date). This mirrors what
    :class:`~phase3.pipeline.graph_writer.GraphWriter` writes, so the
    trace picks up the same node id the writer produced.
    """
    breakdown = result.score.breakdown
    scorer_type = str(breakdown.scorer_type or "unknown")
    entity_id = str(breakdown.entity_id or "unknown")
    # Prefer the run's date_bucket (from metadata), then the result's
    # declared date_bucket, then the breakdown timestamp date.
    date_bucket = (
        result.metadata.get("date_bucket")
        if isinstance(result.metadata, Mapping)
        else None
    )
    if not date_bucket:
        ts = breakdown.timestamp
        date_bucket = ts.date().isoformat() if isinstance(ts, datetime) else "unknown"
    return make_score_node_id(scorer_type, entity_id, str(date_bucket))


class EvidenceChainAdapter:
    """Bridge from :class:`PipelineResult` to :class:`EvidenceChain`.

    The bridge is intentionally tiny: it computes the score node id,
    instantiates an :class:`EvidenceTracer` against the injected
    graph store, and returns the chain.

    Default edge_types are the spec-faithful upstream set
    (:data:`EVIDENCE_UPSTREAM_EDGE_TYPES`). Callers may override for
    ad-hoc queries.
    """

    def __init__(
        self,
        store: GraphStore,
        *,
        edge_types: list[EdgeType] | None = None,
    ) -> None:
        self._store = store
        self._edge_types = edge_types

    def trace(
        self,
        result: PipelineResult,
        *,
        max_depth: int = 5,
        direction: str = "upstream",
        max_nodes: int | None = None,
        include_start: bool = False,
    ) -> EvidenceChain:
        """Run a trace from the score node id computed for ``result``.

        Parameters mirror :meth:`EvidenceTracer.trace` for the ones
        that matter for evidence queries.
        """
        if direction not in ("upstream", "downstream"):
            raise ValueError(
                f"direction must be 'upstream' or 'downstream', got {direction!r}"
            )
        start_id = score_node_id_for_result(result)
        tracer = EvidenceTracer(self._store)
        return tracer.trace(
            start_id,
            max_depth=max_depth,
            direction=direction,  # type: ignore[arg-type]
            edge_types=self._edge_types,
            max_nodes=max_nodes,
            include_start=include_start,
        )


__all__ = [
    "EvidenceChainAdapter",
    "chain_to_dict",
    "chain_to_json",
    "score_node_id_for_result",
]
