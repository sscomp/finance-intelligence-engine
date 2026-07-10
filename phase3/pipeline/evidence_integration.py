"""Evidence Integration — Phase 3B Task 4 Run 3.

Propagate evidence-trace information through
:class:`~phase3.pipeline.scoring_pipeline.PipelineResult` so callers
that consume ``PipelineResult.metadata`` (the cron snapshot path,
the GraphWriter, future Bridge delivery verification) can see the
trace without re-running the BFS.

Why a separate module
---------------------
:class:`PipelineResult` is a frozen dataclass. We can mutate its
state only by :func:`dataclasses.replace`. That keeps the existing
field set and ordering intact and lets callers thread evidence info
in without touching the pipeline. This module is the one place
that knows the replacement contract:

* ``metadata['evidence_trace']`` — JSON-serializable dict from
  :func:`~phase3.graph.evidence_trace_export.chain_to_dict`
  (a copy; subsequent mutations of the original chain do not
  leak into the result metadata).
* ``metadata['evidence_trace_summary']`` — compact summary that
  survives even if the consumer discards the full trace.
* ``warnings`` — extended with the chain's ``warnings`` list (de-duped,
  order-stable) so a single ``result.warnings`` tuple carries both
  pipeline-level and trace-level warnings.

Why propagate rather than recompute
-----------------------------------
Recomputing the trace on every read would mean re-traversing the
graph store at delivery time, which:

* couples the read path to the graph store (the snapshot is supposed
  to be self-contained),
* makes the trace non-deterministic with respect to graph mutations
  between run and delivery,
* blows the delivery-json budget for large chains.

The right contract is "snapshot the trace once, thread the snapshot
through metadata."

What this module does NOT do
----------------------------
* It does not run the trace itself — that's
  :class:`~phase3.graph.evidence_trace_export.EvidenceChainAdapter`.
* It does not modify the pipeline or the GraphWriter.
* It does not depend on persistence.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from phase3.graph.evidence_tracer import EvidenceChain
from phase3.graph.evidence_trace_export import chain_to_dict
from phase3.pipeline.scoring_pipeline import PipelineResult


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_summary(chain: EvidenceChain) -> dict[str, Any]:
    """Compact summary safe for any metadata size budget.

    Captures the cardinality of each typed bucket, the truncation
    flag, and the warning count. Enough for an operator to decide
    "do I need the full trace" without paying for the nodes/edges.
    """
    return {
        "start": chain.start,
        "truncated": chain.truncated,
        "depth_reached": chain.depth_reached,
        "n_visited": len(chain.visited_node_ids),
        "n_traversed_edges": len(chain.traversed_edge_ids),
        "n_leaves": len(chain.leaf_node_ids),
        "n_source_nodes": len(chain.source_node_ids),
        "n_signal_nodes": len(chain.signal_node_ids),
        "n_entity_nodes": len(chain.entity_node_ids),
        "n_score_nodes": len(chain.score_node_ids),
        "n_warnings": len(chain.warnings),
    }


def _merge_warnings(
    existing: tuple[str, ...], trace_warnings: tuple[str, ...],
) -> tuple[str, ...]:
    """Order-stable, de-duplicated union of two warning lists."""
    if not trace_warnings:
        return existing
    seen: set[str] = set(existing)
    out: list[str] = list(existing)
    for w in trace_warnings:
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return tuple(out)


def attach_evidence_metadata(
    result: PipelineResult,
    chain: EvidenceChain,
    *,
    include_full_trace: bool = True,
) -> PipelineResult:
    """Return a new :class:`PipelineResult` with evidence metadata merged.

    The original ``result`` is not mutated (it is a frozen dataclass);
    callers receive a fresh copy via :func:`dataclasses.replace`.

    Parameters
    ----------
    result:
        The original pipeline result.
    chain:
        The :class:`EvidenceChain` produced by tracing
        ``result`` against the graph store.
    include_full_trace:
        If True, the full JSON-serializable chain is stored under
        ``metadata['evidence_trace']`` AND the compact summary
        under ``metadata['evidence_trace_summary']``. If False,
        only the summary is stored. Default True.
    """
    base_meta: dict[str, Any] = dict(result.metadata or {})
    base_meta["evidence_trace_summary"] = _build_summary(chain)
    if include_full_trace:
        base_meta["evidence_trace"] = chain_to_dict(chain)
    new_warnings = _merge_warnings(result.warnings, tuple(chain.warnings))
    return replace(result, metadata=base_meta, warnings=new_warnings)


def trace_warnings_only(
    result: PipelineResult, chain: EvidenceChain,
) -> PipelineResult:
    """Variant of :func:`attach_evidence_metadata` that only carries
    the trace's warnings and the summary (no full trace).

    Useful when the consumer is human-facing (a Telegram message,
    a log line) and the full node/edge lists are noise.
    """
    return attach_evidence_metadata(result, chain, include_full_trace=False)


# ---------------------------------------------------------------------------
# Introspection helpers (used by the CLI to print compact trace blocks)
# ---------------------------------------------------------------------------


def metadata_has_evidence(result: PipelineResult) -> bool:
    """Return True if ``result.metadata`` carries a trace snapshot."""
    meta = result.metadata or {}
    return isinstance(meta, Mapping) and "evidence_trace_summary" in meta


def evidence_summary(result: PipelineResult) -> dict[str, Any] | None:
    """Return the stored summary, or None if no trace was attached."""
    meta = result.metadata or {}
    if not isinstance(meta, Mapping):
        return None
    summary = meta.get("evidence_trace_summary")
    return summary if isinstance(summary, dict) else None


__all__ = [
    "attach_evidence_metadata",
    "evidence_summary",
    "metadata_has_evidence",
    "trace_warnings_only",
]
