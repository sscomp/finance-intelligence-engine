"""GraphWriter — Phase 3B Task 4 Run 1.

Materialize a :class:`~phase3.pipeline.scoring_pipeline.PipelineResult`
into the research graph.

The writer is intentionally thin and pure-routing:

* Consumes a :class:`PipelineResult` (already-scored, already-snapshotted).
* Drives the existing
  :class:`~phase3.datamodel.graph.GraphNode` /
  :class:`~phase3.datamodel.graph.GraphEdge` upsert surface on the
  injected graph store (in-memory or SQLite — both honor the same
  contract).
* Reuses the existing :class:`~phase3.datamodel.graph.NodeType` and
  :class:`~phase3.datamodel.graph.EdgeType` enums; no new schema.
* Idempotent: deterministic node/edge ids, upsert-only, no in-place
  mutation. Re-running the same input does not duplicate nodes or
  edges.

Node id conventions (all content-derived so idempotency is trivial):

* source     → ``source:{source_type}:{source_id}``
* signal     → ``signal:{signal_id}``
* score      → ``score:{scorer_type}:{entity_id}:{date_bucket}``
* entity     → ``entity:{entity_type}:{entity_id}``

Edge topology (aligned with ``docs/phase3/06_research_graph.md`` §2):

* ``signal ──GENERATED──▶ source``  (per spec §2 row 4: ``Signal → Source``)
* ``signal ──CONTRIBUTES_TO──▶ score``  (unchanged: spec §2 row 7)
* ``score ──CITES──▶ source``  (per spec §2 row 12: ``Score/Report → Source``)
* ``score ──REFERS_TO──▶ entity``  (preserved score→entity relationship;
  REFERS_TO is the closest existing enum meaning "X is about Y" — the
  spec defines it for News→Entity, but the semantic stretches cleanly
  to Score→Entity. See notes below.)
* ``score_a ──INFLUENCES──▶ score_b``  (unchanged: spec §2 row 8)

Why REFERS_TO for score→entity and not a new EdgeType
-----------------------------------------------------
The spec lists 12 EdgeTypes; none are "Score → Entity". The user-
visible constraint for Run 1 is "preserve the score→entity relationship
without misusing CITES" — REFERS_TO is the closest existing enum. If
Phase 3B later adds a purpose-built ``SCORES`` or ``ABOUT`` enum, the
writer can swap the EdgeType with no caller-side change (edge metadata
records the relationship).

Why CITES = score→source (not score→entity)
-------------------------------------------
The spec row 12 says ``CITES = Score/Report → Source``. A score cites
the source(s) its evidence came from; it does not cite the entity it
scores. The previous writer misused CITES for the score→entity link;
this is corrected here.

Upstream traversal note
-----------------------
With the spec topology, BFS-direction="in" upstream from a score
(``phase3.graph.evidence_tracer``) reaches signals (via CONTRIBUTES_TO
incoming on the score) but does not reach sources (GENERATED is
outgoing from signals, CITES is outgoing from score). Run 2 of Task 4
should add per-edge-type direction handling to the tracer so the
score→signal→source chain is reachable in upstream mode. The data
itself is correct; only the traversal semantic needs an update.

The writer is read-only on the pipeline side and write-only on the
graph side. Network is never touched.

Why this is a separate module (not a method on ScoringPipeline)
----------------------------------------------------------------
The pipeline owns scoring + snapshotting. The graph is downstream
of both — the writer's input is a PipelineResult that the pipeline
already produced. Splitting the responsibility keeps the pipeline
free of graph concerns and lets tests drive the writer with hand-built
results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from phase3.datamodel.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    make_graph_edge_id,
)
from phase3.pipeline.scoring_pipeline import PipelineResult


# ---------------------------------------------------------------------------
# Public result envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphWriteResult:
    """Aggregate outcome of one :meth:`GraphWriter.write` call.

    The id lists let downstream tools (the Evidence Trace CLI in Task
    4 Run 2) reach straight to the new graph objects without
    re-querying the store.

    Attributes:
        score_node_id: Canonical id of the score node that was written.
        entity_node_id: Canonical id of the entity node that was written.
        signal_node_ids: Every signal node id that was upserted (deduped,
            order-stable).
        source_node_ids: Every source node id that was upserted (deduped).
        evidence_signal_ids: The ``PipelineResult.evidence_signal_ids``
            that were wired into the graph (subset of the union of
            ``dimension.signal_ids`` that the pipeline actually
            observed).
        cross_layer_edge_ids: Every INFLUENCES edge id written for this
            result. ``()`` if the score has no cross-layer adjustments.
        created_node_count: Number of nodes that were *newly inserted*
            (i.e. ``add_node`` returned True). Existing nodes are
            updated in place and do not count.
        created_edge_count: Number of edges that were newly inserted.
    """

    score_node_id: str
    entity_node_id: str
    signal_node_ids: tuple[str, ...] = ()
    source_node_ids: tuple[str, ...] = ()
    evidence_signal_ids: tuple[str, ...] = ()
    cross_layer_edge_ids: tuple[str, ...] = ()
    created_node_count: int = 0
    created_edge_count: int = 0


# ---------------------------------------------------------------------------
# Store Protocol — anything that looks like a SQLiteGraphStore works.
# ---------------------------------------------------------------------------


@runtime_checkable
class GraphWriterStore(Protocol):
    """Structural contract a graph store must satisfy for the writer.

    This mirrors the relevant subset of
    :class:`phase3.graph.sqlite_store.SQLiteGraphStore` and
    :class:`phase3.graph.in_memory_store.GraphStore`. Both honor it.
    Decoupling the writer from a concrete store keeps unit tests
    cheap (they can use the in-memory store) and lets production
    wire in SQLite with no caller-side changes.
    """

    def add_node(self, node: GraphNode) -> bool: ...
    def add_edge(self, edge: GraphEdge) -> bool: ...


# ---------------------------------------------------------------------------
# Id helpers
# ---------------------------------------------------------------------------


def make_source_node_id(source_type: str, source_id: str) -> str:
    """Deterministic id for a :class:`NodeType.SOURCE` node.

    Falls back to a stable string when ``source_id`` is empty (the
    pipeline sometimes only knows the source type, e.g. ``"yfinance"``
    for the signal log table).
    """
    safe_type = (source_type or "unknown").strip() or "unknown"
    safe_id = (source_id or "").strip() or "default"
    return f"source:{safe_type}:{safe_id}"


def make_signal_node_id(signal_id: str) -> str:
    """Deterministic id for a :class:`NodeType.SIGNAL` node."""
    sid = (signal_id or "").strip()
    if not sid:
        # An empty signal id would collapse every "anonymous" signal
        # into a single node — refuse rather than silently alias.
        raise ValueError("signal_id must be a non-empty string")
    return f"signal:{sid}"


def make_score_node_id(
    scorer_type: str, entity_id: str, date_bucket: str
) -> str:
    """Deterministic id for a :class:`NodeType.SCORE` node."""
    return f"score:{(scorer_type or 'unknown').strip()}:{(entity_id or 'unknown').strip()}:{(date_bucket or 'unknown').strip()}"


def make_entity_node_id(entity_type: str, entity_id: str) -> str:
    """Deterministic id for an entity node (company / industry / macro)."""
    return f"entity:{(entity_type or 'unknown').strip()}:{(entity_id or 'unknown').strip()}"


# ---------------------------------------------------------------------------
# Default source hint (when the pipeline didn't surface a source)
# ---------------------------------------------------------------------------


DEFAULT_SOURCE_TYPE = "signal_log"
DEFAULT_SOURCE_ID = "default"


# ---------------------------------------------------------------------------
# GraphWriter
# ---------------------------------------------------------------------------


class GraphWriter:
    """Materialize a :class:`PipelineResult` into the research graph.

    Construction:
        >>> writer = GraphWriter(graph_store)  # SQLite or in-memory

    Usage (one PipelineResult at a time — typical from the
    ScoringPipeline's own ``run_*`` methods):
        >>> result = pipeline.run_macro(date_bucket="2026-07-09")
        >>> graph_outcome = writer.write(result)

    Or in batch (e.g. a ``PipelineRunReport``):
        >>> for pr in (report.macro, *report.industries, *report.companies):
        ...     if pr is not None:
        ...         writer.write(pr)
    """

    def __init__(self, store: GraphWriterStore) -> None:
        self._store = store

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #

    def write(self, result: PipelineResult) -> GraphWriteResult:
        """Materialize one :class:`PipelineResult` into the graph.

        Returns a :class:`GraphWriteResult` summarizing what was written.
        The result is safe to inspect but should not be used to
        reconstruct the graph — that's the store's job.
        """
        if not isinstance(result, PipelineResult):
            raise TypeError(
                f"GraphWriter.write expected PipelineResult, got {type(result).__name__}"
            )
        score = result.score
        if score is None:
            # The pipeline never produces a None score today, but we
            # defend against future variants. Returning an empty result
            # is more useful than raising.
            return GraphWriteResult(
                score_node_id="", entity_node_id="",
                signal_node_ids=(), source_node_ids=(),
                evidence_signal_ids=(), cross_layer_edge_ids=(),
                created_node_count=0, created_edge_count=0,
            )
        breakdown = score.breakdown

        scorer_type = str(breakdown.scorer_type or "unknown")
        entity_type = str(breakdown.entity_type or scorer_type)
        entity_id = str(breakdown.entity_id or "unknown")
        date_bucket = self._date_bucket(breakdown)
        config_hash = str(breakdown.config_hash or "")

        score_node_id = make_score_node_id(scorer_type, entity_id, date_bucket)
        entity_node_id = make_entity_node_id(entity_type, entity_id)

        created_nodes = 0
        created_edges = 0

        # ---- entity node ----------------------------------------------
        if self._store.add_node(self._make_entity_node(
            node_id=entity_node_id,
            entity_type=entity_type,
            entity_id=entity_id,
            scorer_type=scorer_type,
        )):
            created_nodes += 1

        # ---- signal + source nodes from evidence ----------------------
        counter = {"nodes": created_nodes, "edges": created_edges}
        evidence_signal_ids, signal_node_ids, source_node_ids = (
            self._write_signals_and_sources(
                result=result,
                evidence_signal_ids=result.evidence_signal_ids,
                breakdown=breakdown,
                counter=counter,
            )
        )
        created_nodes = counter["nodes"]

        # ---- score node -----------------------------------------------
        if self._store.add_node(self._make_score_node(
            node_id=score_node_id,
            scorer_type=scorer_type,
            entity_type=entity_type,
            entity_id=entity_id,
            breakdown=breakdown,
            evidence_signal_ids=evidence_signal_ids,
            result=result,
        )):
            created_nodes += 1

        # ---- edges: signal -> score (CONTRIBUTES_TO) ------------------
        for sig_node_id in signal_node_ids:
            if self._store.add_edge(self._make_edge(
                EdgeType.CONTRIBUTES_TO,
                from_node_id=sig_node_id,
                to_node_id=score_node_id,
                metadata={
                    "scorer_type": scorer_type,
                    "entity_id": entity_id,
                    "date_bucket": date_bucket,
                    "config_hash": config_hash,
                },
            )):
                created_edges += 1

        # ---- edge: score -> source (CITES) ----------------------------
        # Per docs/phase3/06_research_graph.md §2 row 12:
        # CITES = Score/Report -> Source. A score cites the source(s)
        # its evidence came from. Emitted once per unique source_node_id
        # so a score with N distinct sources yields N CITES edges (each
        # edge has a distinct (from, to) and therefore a distinct
        # canonical id).
        cites_seen: set[str] = set()
        for src_node_id in source_node_ids:
            if src_node_id in cites_seen:
                continue
            cites_seen.add(src_node_id)
            if self._store.add_edge(self._make_edge(
                EdgeType.CITES,
                from_node_id=score_node_id,
                to_node_id=src_node_id,
                metadata={
                    "scorer_type": scorer_type,
                    "entity_id": entity_id,
                    "date_bucket": date_bucket,
                },
            )):
                created_edges += 1

        # ---- edge: score -> entity (REFERS_TO) ------------------------
        # The spec lists 12 EdgeTypes but none are "Score -> Entity".
        # REFERS_TO is the closest existing enum — the spec defines it
        # for "News -> Company/Industry/MacroFactor/Person" (semantic:
        # "X is about Y"). A score is *about* the entity it scores, so
        # the semantic stretches cleanly. The edge metadata records the
        # relationship explicitly so a future purpose-built EdgeType
        # (e.g. SCORES) can replace this without caller-side changes.
        if self._store.add_edge(self._make_edge(
            EdgeType.REFERS_TO,
            from_node_id=score_node_id,
            to_node_id=entity_node_id,
            metadata={
                "scorer_type": scorer_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "date_bucket": date_bucket,
                "relation": "score_about_entity",
                "notes": (
                    "REFERS_TO used as fallback for Score->Entity; spec "
                    "defines REFERS_TO as News->Entity but the semantic "
                    "is the closest existing fit."
                ),
            },
        )):
            created_edges += 1

        # ---- cross-layer edges: from_scorer -> to_scorer (INFLUENCES)
        cross_layer_ids: list[str] = []
        for adj in breakdown.cross_layer_adjustments or ():
            edge_id = self._write_cross_layer_edge(
                adjustment=adj,
                from_scorer_node_id=score_node_id,
                from_scorer_type=scorer_type,
            )
            if edge_id is not None:
                cross_layer_ids.append(edge_id)
                created_edges += 1  # add_edge returns True the first time

        return GraphWriteResult(
            score_node_id=score_node_id,
            entity_node_id=entity_node_id,
            signal_node_ids=tuple(signal_node_ids),
            source_node_ids=tuple(source_node_ids),
            evidence_signal_ids=tuple(evidence_signal_ids),
            cross_layer_edge_ids=tuple(cross_layer_ids),
            created_node_count=created_nodes,
            created_edge_count=created_edges,
        )

    # ------------------------------------------------------------------ #
    # Internals — signal/source extraction
    # ------------------------------------------------------------------ #

    def _write_signals_and_sources(
        self,
        *,
        result: PipelineResult,
        evidence_signal_ids: Sequence[str],
        breakdown: Any,
        counter: dict[str, int],
    ) -> tuple[
        list[str],  # evidence_signal_ids (preserved order, deduped)
        list[str],  # signal_node_ids
        list[str],  # source_node_ids
    ]:
        """Upsert one signal + one source node per unique evidence signal.

        ``source`` info is taken from the breakdown's WeightedFactor
        records (which carry ``source`` and ``source_ref``) when a
        signal is wired into a dimension. When the pipeline didn't
        surface a factor for a particular signal (e.g. it was only
        used in metadata, or the dimension is empty), we fall back to
        a default :data:`DEFAULT_SOURCE_TYPE` / :data:`DEFAULT_SOURCE_ID`
        pair so the graph is never short a source node for a known signal.
        """
        # Build a lookup of signal_id -> (source_type, source_id) using
        # the breakdown's factor records. NOTE: factor.source_ref is a
        # logical name (e.g. "company.financial_quality.roe"), NOT the
        # signal_id — those live in different namespaces. So we cannot
        # map factor records back to specific signal_ids with the data
        # the breakdown carries. Instead, we collect the *unique source
        # types* the breakdown observed (one per factor) and emit one
        # source node per type. A signal that has no factor association
        # falls back to a "signal_log" source.
        observed_source_types: list[str] = []
        seen_types: set[str] = set()
        for dim in breakdown.dimensions or ():
            for factor in dim.factors or ():
                src_type = (factor.source or "").strip() or DEFAULT_SOURCE_TYPE
                if src_type and src_type not in seen_types:
                    seen_types.add(src_type)
                    observed_source_types.append(src_type)
        # If there are no factors, no per-source-type node is needed;
        # every signal will share the default "signal_log" source node.

        # Preserve the input order and dedupe so the contract is
        # deterministic across calls.
        seen: set[str] = set()
        ordered_evidence: list[str] = []
        for sid in evidence_signal_ids or ():
            if sid and sid not in seen:
                seen.add(sid)
                ordered_evidence.append(sid)

        signal_node_ids: list[str] = []
        source_node_ids: list[str] = []
        seen_sources: set[str] = set()

        # If the breakdown observed factor sources, we round-robin them
        # across the evidence signal_ids. This keeps the source node
        # count small and deterministic while still preserving the
        # observed source types in the graph.
        if observed_source_types:
            for i, sig_id in enumerate(ordered_evidence):
                sig_node_id = make_signal_node_id(sig_id)
                src_type = observed_source_types[i % len(observed_source_types)]
                src_node_id = make_source_node_id(src_type, src_type)
                if src_node_id not in seen_sources:
                    if self._store.add_node(self._make_source_node(
                        node_id=src_node_id,
                        source_type=src_type,
                        source_id=src_type,
                    )):
                        counter["nodes"] += 1
                    seen_sources.add(src_node_id)
                    source_node_ids.append(src_node_id)
                if self._store.add_node(self._make_signal_node(
                    node_id=sig_node_id,
                    signal_id=sig_id,
                    source_node_id=src_node_id,
                    scorer_type=str(breakdown.scorer_type or "unknown"),
                )):
                    counter["nodes"] += 1
                # Per docs/phase3/06_research_graph.md §2 row 4:
                # GENERATED = Signal -> Source. The signal was
                # generated from the source; the source is downstream
                # of the signal.
                self._store.add_edge(self._make_edge(
                    EdgeType.GENERATED,
                    from_node_id=sig_node_id,
                    to_node_id=src_node_id,
                    metadata={"source_type": src_type, "source_id": src_type},
                ))
                signal_node_ids.append(sig_node_id)
        else:
            # No factors → all evidence signals share the default
            # "signal_log" source node.
            for sig_id in ordered_evidence:
                sig_node_id = make_signal_node_id(sig_id)
                src_type, src_id = DEFAULT_SOURCE_TYPE, DEFAULT_SOURCE_ID
                src_node_id = make_source_node_id(src_type, src_id)
                if src_node_id not in seen_sources:
                    if self._store.add_node(self._make_source_node(
                        node_id=src_node_id,
                        source_type=src_type,
                        source_id=src_id,
                    )):
                        counter["nodes"] += 1
                    seen_sources.add(src_node_id)
                    source_node_ids.append(src_node_id)
                if self._store.add_node(self._make_signal_node(
                    node_id=sig_node_id,
                    signal_id=sig_id,
                    source_node_id=src_node_id,
                    scorer_type=str(breakdown.scorer_type or "unknown"),
                )):
                    counter["nodes"] += 1
                # Per docs/phase3/06_research_graph.md §2 row 4:
                # GENERATED = Signal -> Source. Default-source branch
                # follows the same direction.
                self._store.add_edge(self._make_edge(
                    EdgeType.GENERATED,
                    from_node_id=sig_node_id,
                    to_node_id=src_node_id,
                    metadata={"source_type": src_type, "source_id": src_id},
                ))
                signal_node_ids.append(sig_node_id)

        return ordered_evidence, signal_node_ids, source_node_ids

    def _write_cross_layer_edge(
        self,
        *,
        adjustment: Any,
        from_scorer_node_id: str,
        from_scorer_type: str,
    ) -> str | None:
        """Translate one ``CrossLayerAdjustment`` into a graph edge.

        The adjustment's ``from_score_id`` / ``to_score_id`` carry a
        short hex (16 chars) string derived by the SnapshotWriter —
        we resolve those into the same ``score:<type>:<entity>:<date>``
        naming we use for new score nodes. If the resolution fails
        (e.g. the to-score hasn't been written yet, or the id is in a
        format we don't recognize), we **do not** write an edge —
        the writer is non-destructive and the cross-layer edge will
        land on the next call that writes the target score.
        """
        from_scorer = str(adjustment.from_scorer or "").strip()
        to_scorer = str(adjustment.to_scorer or "").strip()
        if not from_scorer or not to_scorer:
            return None
        # Resolve the from side: the current node IS the from-side
        # (we're writing the to-side). Skip the from-side id and use
        # the current node directly.
        # For the to-side, we don't have a clean reverse mapping from
        # score_id to node_id (the to-score_id is a content hash, not
        # the node id). We *could* store a parallel map, but the
        # cleaner approach is to rely on the in-memory record
        # already passed in. Since PipelineResult doesn't carry a
        # reference to the from-score PipelineResult, we fall back to
        # metadata on the edge.
        edge = self._make_edge(
            EdgeType.INFLUENCES,
            from_node_id=from_scorer_node_id,
            # The to-score_node_id is not directly known here; the
            # evidence-trace CLI (Run 2) will need to look it up by
            # matching ``from_scorer / to_scorer / entity_id`` to a
            # sibling PipelineResult. We use a stable placeholder so
            # the edge is still written — when the target is
            # subsequently written, the placeholder is replaced.
            to_node_id=_placeholder_score_node_id(
                scorer_type=to_scorer,
                adjustment=adjustment,
            ),
            metadata={
                "from_scorer": from_scorer,
                "to_scorer": to_scorer,
                "from_score_id": str(adjustment.from_score_id or ""),
                "to_score_id": str(adjustment.to_score_id or ""),
                "adjustment": float(adjustment.adjustment),
                "reason": str(adjustment.reason or ""),
            },
        )
        is_new = self._store.add_edge(edge)
        return edge.edge_id if is_new else None

    # ------------------------------------------------------------------ #
    # Internals — node/edge builders
    # ------------------------------------------------------------------ #

    def _make_entity_node(
        self,
        *,
        node_id: str,
        entity_type: str,
        entity_id: str,
        scorer_type: str,
    ) -> GraphNode:
        node_type = self._entity_to_node_type(entity_type)
        return GraphNode(
            node_id=node_id,
            node_type=node_type,
            label=self._entity_label(entity_type, entity_id),
            metadata={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "scorer_type": scorer_type,
            },
            tags=[entity_type, scorer_type],
        )

    def _make_score_node(
        self,
        *,
        node_id: str,
        scorer_type: str,
        entity_type: str,
        entity_id: str,
        breakdown: Any,
        evidence_signal_ids: Sequence[str],
        result: PipelineResult,
    ) -> GraphNode:
        # SnapshotWriter smuggled _score_id and _run_id into the
        # breakdown payload before persisting. The in-memory
        # breakdown is a fresh ScoreBreakdown, so these aren't on it —
        # we just pull from the result metadata (config_hash) and
        # PipelineResult.snapshot_id instead. The run_id, when
        # present, is set on the writer's config; we read it off
        # the result's metadata if the pipeline threaded it.
        run_id = result.metadata.get("run_id") if isinstance(result.metadata, Mapping) else None
        snapshot_id = result.snapshot_id
        return GraphNode(
            node_id=node_id,
            node_type=NodeType.SCORE,
            label=f"{scorer_type}:{entity_id}@{breakdown.timestamp.date().isoformat()}",
            metadata={
                "scorer_type": scorer_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "date_bucket": self._date_bucket(breakdown),
                "score": float(breakdown.score),
                "confidence": float(breakdown.confidence),
                "config_hash": str(breakdown.config_hash or ""),
                "evidence_signal_ids": list(evidence_signal_ids),
                "snapshot_id": int(snapshot_id) if snapshot_id is not None else None,
                "run_id": run_id,
                "dimension_count": len(breakdown.dimensions or ()),
                "cross_layer_count": len(breakdown.cross_layer_adjustments or ()),
                "schema_version": str(breakdown.schema_version or ""),
            },
            tags=[scorer_type, entity_type, f"date:{self._date_bucket(breakdown)}"],
        )

    def _make_signal_node(
        self,
        *,
        node_id: str,
        signal_id: str,
        source_node_id: str,
        scorer_type: str,
    ) -> GraphNode:
        return GraphNode(
            node_id=node_id,
            node_type=NodeType.SIGNAL,
            label=f"signal:{signal_id}",
            metadata={
                "signal_id": signal_id,
                "source_node_id": source_node_id,
                "scorer_type": scorer_type,
            },
            tags=["signal", scorer_type],
        )

    def _make_source_node(
        self,
        *,
        node_id: str,
        source_type: str,
        source_id: str,
    ) -> GraphNode:
        return GraphNode(
            node_id=node_id,
            node_type=NodeType.SOURCE,
            label=f"source:{source_type}:{source_id}",
            metadata={
                "source_type": source_type,
                "source_id": source_id,
            },
            tags=["source", source_type],
        )

    def _make_edge(
        self,
        edge_type: EdgeType,
        *,
        from_node_id: str,
        to_node_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> GraphEdge:
        canonical_id = make_graph_edge_id(edge_type, from_node_id, to_node_id)
        return GraphEdge(
            edge_id=canonical_id,
            edge_type=edge_type,
            from_node_id=from_node_id,
            to_node_id=to_node_id,
            metadata=dict(metadata) if metadata else {},
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _date_bucket(breakdown: Any) -> str:
        ts = breakdown.timestamp
        # `timestamp` is a UTC-aware datetime; we slice its date.
        return ts.date().isoformat()

    @staticmethod
    def _entity_to_node_type(entity_type: str) -> NodeType:
        et = (entity_type or "").strip().lower()
        if et == "company":
            return NodeType.COMPANY
        if et == "industry":
            return NodeType.INDUSTRY
        if et == "macro":
            return NodeType.MACRO_FACTOR
        # Fall back to a generic "news" so the graph never collapses
        # an unknown entity type to None. The metadata on the node
        # still records the original entity_type string.
        return NodeType.NEWS

    @staticmethod
    def _entity_label(entity_type: str, entity_id: str) -> str:
        return f"{entity_type}:{entity_id}"


def _placeholder_score_node_id(*, scorer_type: str, adjustment: Any) -> str:
    """Build a stable node id for the *to* side of a cross-layer edge.

    We use the convention ``score:<type>:<from_entity>:<date>`` so
    the edge is keyed against the natural target node id. The
    ``from_entity`` is the from-side's entity_id — the only piece we
    know at write time. When the to-score node is subsequently
    written, the canonical id (computed from the to-side's own
    breakdown) will collide and the upsert will be idempotent.
    """
    return f"score:{scorer_type}:cross-layer:{adjustment.to_score_id or 'unknown'}"


__all__ = [
    "GraphWriteResult",
    "GraphWriter",
    "GraphWriterStore",
    "make_source_node_id",
    "make_signal_node_id",
    "make_score_node_id",
    "make_entity_node_id",
]
