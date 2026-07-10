"""IntelligencePipeline — Phase 3B Task 5 Run 1.

The end-to-end orchestrator that composes the existing Phase 3B
components into one testable flow:

    Signal (loaded from SignalRepository via SignalLoader)
        │
        ▼
    InputBuilder (per scorer type)
        │
        ▼
    ScoringPipeline (MacroScorer | IndustryScorer | CompanyScorer)
        │   (snapshots persisted through the configured SnapshotSink)
        ▼
    GraphWriter (materialize each PipelineResult into the research graph)
        │
        ▼
    EvidenceChainAdapter (trace each score back to its sources)

This module ships only the *composition layer*. It does not implement
scoring, graph upsert, or evidence tracing on its own — every step
delegates to the existing Task 3 / Task 4 modules. The intent is to
land a runnable, deterministic, offline-only end-to-end skeleton that
future production wiring can swap in (CLI surface, scheduling,
networking) without re-implementing the orchestration.

Scope for Run 1
----------------
* :class:`IntelligencePipelineConfig` — typed configuration DTO.
* :class:`IntelligenceRunResult` — typed result DTO with all the
  fields the brief asked for (pipeline results, snapshot ids, graph
  write summaries, evidence/query handles, warnings/errors, run_id,
  config_hash, timing).
* :class:`IntelligenceRunError` — typed exception for downstream
  component failures.
* :class:`IntelligencePipeline` — the orchestrator. Composes the
  injected :class:`ScoringPipeline` + :class:`GraphWriter` +
  :class:`EvidenceChainAdapter`; supports dry-run and temp-SQLite
  persist modes; returns the typed result.

Non-goals for Run 1
-------------------
* Production CLI surface (deferred to Task 5 Run 2).
* Cron / scheduling / networking (deferred to a later run).
* Real adapter ingest (the pipeline consumes an already-seeded
  :class:`SignalRepository`; the production wiring is identical to
  what :mod:`phase3.cli.ingest_signals` uses today).
* Shadow run / explainability CLI (deferred to Run 3+).

Design constraints
------------------
* Offline & deterministic — no network, no real-time clock dependence
  (timing is measured, but every run with the same inputs produces the
  same graph and the same snapshot ids modulo an autoincrement counter
  that is itself deterministic for a fresh DB).
* Reuse-only — no new scoring, no new graph layout, no new persistence
  schema. Everything this module writes goes through existing
  protocols.
* Default-safe — dry-run is the default. Persist mode requires an
  explicit ``persist=True`` + a real :class:`ScoreRepository`. Missing
  signals produce warnings, never crashes.
* Typed failure propagation — any error from a downstream component
  is wrapped in :class:`IntelligenceRunError` and the partial
  results are still returned in the result envelope so a caller can
  surface the warning list alongside the error.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from phase3.graph.evidence_tracer import EvidenceChain
from phase3.graph.evidence_trace_export import (
    EvidenceChainAdapter,
    score_node_id_for_result,
)
from phase3.graph.in_memory_store import GraphStore as InMemoryGraphStore
from phase3.pipeline.graph_writer import (
    GraphWriteResult,
    GraphWriter,
    GraphWriterStore,
    make_score_node_id,
)
from phase3.pipeline.scoring_pipeline import (
    PipelineConfig,
    PipelineResult,
    PipelineRunReport,
    ScoringPipeline,
    SnapshotSink,
)
from phase3.pipeline.signal_loader import SignalLoader, SignalLoaderFilters


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IntelligenceRunError(RuntimeError):
    """Typed failure raised when a downstream component throws.

    The error carries the component name + a short error_class so the
    caller can branch on it without parsing the message. The original
    exception is preserved as ``__cause__`` (via ``raise ... from``)
    so the traceback is intact.
    """

    def __init__(
        self,
        component: str,
        message: str,
        *,
        error_class: str = "",
        partial: "IntelligenceRunResult | None" = None,
    ) -> None:
        super().__init__(f"{component}: {message}")
        self.component = component
        self.error_class = error_class or message.split(":", 1)[0].strip() or "Error"
        self.partial = partial


# ---------------------------------------------------------------------------
# Configuration DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntelligencePipelineConfig:
    """End-to-end orchestrator configuration.

    Attributes:
        date_bucket: Shared YYYY-MM-DD for every score in the run.
        industry_ids: Industries to score after macro.
        company_specs: Iterable of dicts with at least ``code``;
            optional ``name``, ``sector``, ``is_financial_sector``,
            ``industry_id_for_adjustment``.
        run_macro: Set False to skip the macro leg.
        persist: When True, snapshots and graph writes are persisted
            to the configured sink + graph store. When False, the
            pipeline is fully dry-run (no DB writes anywhere).
        run_id: Stable caller-supplied run handle. If empty, a fresh
            UUID4 is generated at run time.
        config_hash: Stable hash identifying the scorer config; the
            same value is threaded into the snapshot record and the
            result envelope.
        notes: Free-form note forwarded to the SnapshotSink and the
            run envelope.
        trace_directions: Edge-type direction(s) the
            :class:`EvidenceChainAdapter` should use when computing
            the evidence chain for each score. Default: ``("upstream",)``.
        trace_max_depth: ``max_depth`` forwarded to
            :class:`EvidenceTracer.trace`. Default: 5.
    """

    date_bucket: str
    industry_ids: tuple[str, ...] = ()
    company_specs: tuple[Mapping[str, Any], ...] = ()
    run_macro: bool = True
    persist: bool = False
    run_id: str = ""
    config_hash: str = "no-config"
    notes: str = ""
    trace_directions: tuple[str, ...] = ("upstream",)
    trace_max_depth: int = 5

    def __post_init__(self) -> None:
        if not self.date_bucket:
            raise ValueError("IntelligencePipelineConfig.date_bucket must be non-empty")
        for d in self.trace_directions:
            if d not in ("upstream", "downstream"):
                raise ValueError(
                    f"trace_directions entries must be 'upstream' or 'downstream', got {d!r}"
                )

    def resolved_run_id(self) -> str:
        return self.run_id or f"ipr-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Result DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceQueryHandle:
    """Lightweight handle returned to callers for follow-up queries.

    Captures the score node id + the upstream EvidenceChain (and any
    downstream trace requested by the config) so a caller can reach
    straight to the graph nodes/edges without re-running the BFS.

    Attributes:
        scorer_type: ``"macro"`` | ``"industry"`` | ``"company"``.
        entity_id: The scored entity.
        date_bucket: The YYYY-MM-DD the score belongs to.
        score_node_id: The canonical ``score:<type>:<entity>:<date>`` id.
        upstream: Upstream EvidenceChain (or None if not requested).
        downstream: Downstream EvidenceChain (or None if not requested).
        evidence_summary: Compact summary of the upstream chain (or None).
    """

    scorer_type: str
    entity_id: str
    date_bucket: str
    score_node_id: str
    upstream: EvidenceChain | None
    downstream: EvidenceChain | None
    evidence_summary: dict[str, Any] | None


@dataclass(frozen=True)
class IntelligenceRunResult:
    """Typed envelope returned by :meth:`IntelligencePipeline.run`.

    The envelope is immutable. Callers read it directly; they never
    mutate it.

    Attributes:
        run_id: Run handle (echoes config.run_id; auto-generated if the
            config had none).
        config_hash: Stable hash identifying the scorer config.
        date_bucket: YYYY-MM-DD shared by every score in the run.
        persist: True if the run wrote to any persistence layer.
        dry_run: Convenience flag — True iff ``persist`` is False.
        started_at / finished_at: UTC ISO-8601 timestamps.
        duration_seconds: Wall-clock duration of the run.
        pipeline_result: The :class:`PipelineRunReport` from
            :class:`ScoringPipeline.run_all`.
        snapshot_ids: Mapping ``(scorer_type, entity_id) → snapshot_id``
            (or ``None`` when persist=False). The macro snapshot is
            keyed as ``("macro", "global")`` by default.
        graph_writes: Tuple of :class:`GraphWriteResult`, one per
            scored entity (macro + each industry + each company).
        evidence_handles: Tuple of :class:`EvidenceQueryHandle`, one
            per scored entity. The order matches ``graph_writes``.
        node_count / edge_count: Convenience counts derived from the
            graph store (or the in-memory store when ``persist=False``).
        evidence_node_count: How many evidence signal nodes were
            observed across all graph writes (sum of
            ``GraphWriteResult.signal_node_ids`` lengths).
        warnings: Order-stable, de-duplicated run-level warnings
            (pipeline + graph + evidence + missing-signal).
        errors: Typed error list — one :class:`IntelligenceRunError`
            per failed component, or empty tuple.
        metadata: Free-form per-run metadata. Always includes
            ``run_id``, ``config_hash``, ``date_bucket``, ``persist``,
            ``started_at``, ``finished_at``, ``duration_seconds``.
    """

    run_id: str
    config_hash: str
    date_bucket: str
    persist: bool
    dry_run: bool
    started_at: str
    finished_at: str
    duration_seconds: float
    pipeline_result: PipelineRunReport
    snapshot_ids: Mapping[tuple[str, str], int | None]
    graph_writes: tuple[GraphWriteResult, ...]
    evidence_handles: tuple[EvidenceQueryHandle, ...]
    node_count: int
    edge_count: int
    evidence_node_count: int
    warnings: tuple[str, ...] = ()
    errors: tuple[IntelligenceRunError, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view of the result envelope.

        DTOs and frozen dataclasses render cleanly. EvidenceChain is
        not in the dict (callers who need it should read
        ``evidence_handles[*].upstream`` / ``.downstream`` directly).
        """
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "date_bucket": self.date_bucket,
            "persist": self.persist,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "snapshot_ids": [
                {"scorer_type": k[0], "entity_id": k[1], "snapshot_id": v}
                for k, v in self.snapshot_ids.items()
            ],
            "graph_writes": [
                {
                    "score_node_id": gw.score_node_id,
                    "entity_node_id": gw.entity_node_id,
                    "signal_node_ids": list(gw.signal_node_ids),
                    "source_node_ids": list(gw.source_node_ids),
                    "evidence_signal_ids": list(gw.evidence_signal_ids),
                    "cross_layer_edge_ids": list(gw.cross_layer_edge_ids),
                    "created_node_count": gw.created_node_count,
                    "created_edge_count": gw.created_edge_count,
                }
                for gw in self.graph_writes
            ],
            "evidence_handles": [
                {
                    "scorer_type": h.scorer_type,
                    "entity_id": h.entity_id,
                    "date_bucket": h.date_bucket,
                    "score_node_id": h.score_node_id,
                    "evidence_summary": h.evidence_summary,
                }
                for h in self.evidence_handles
            ],
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "evidence_node_count": self.evidence_node_count,
            "warnings": list(self.warnings),
            "errors": [
                {
                    "component": e.component,
                    "error_class": e.error_class,
                    "message": str(e),
                }
                for e in self.errors
            ],
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stable_orchestrator_id(config: IntelligencePipelineConfig) -> str:
    """Hash the configuration for the result metadata.

    The hash is content-derived and stable: same industry_ids,
    company_specs, date_bucket, config_hash ⇒ same orchestrator id.
    """
    payload = {
        "date_bucket": config.date_bucket,
        "industry_ids": sorted(config.industry_ids),
        "company_specs": sorted(
            (json.dumps(spec, sort_keys=True, default=str) for spec in config.company_specs),
        ),
        "run_macro": config.run_macro,
        "trace_directions": sorted(config.trace_directions),
        "trace_max_depth": int(config.trace_max_depth),
        "config_hash": config.config_hash,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _merge_warnings(*lists: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for w in lst or ():
            if w in seen:
                continue
            seen.add(w)
            out.append(w)
    return tuple(out)


def _iso(ts: datetime | None) -> str:
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _summary_dict(chain: EvidenceChain | None) -> dict[str, Any] | None:
    if chain is None:
        return None
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


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class IntelligencePipeline:
    """End-to-end intelligence orchestrator.

    Composes the injected scoring pipeline + graph writer + evidence
    adapter. The class is a thin facade — every load-bearing call is
    delegated to a Task 3 / Task 4 module.

    Construction
    ------------
    The minimal form is::

        pipeline = IntelligencePipeline(scoring_pipeline=p, graph_writer=g)
        result = pipeline.run(cfg)

    For the dry-run-only path (no graph store, no DB), pass a fresh
    :class:`~phase3.graph.in_memory_store.GraphStore` as the
    ``graph_writer`` store. The pipeline never reads from the graph
    store; it only writes.

    For the persist path, hand in a fully-migrated
    :class:`~phase3.graph.sqlite_store.SQLiteGraphStore` (pointed at
    a temp path, never ``macro_history.db``) and a real
    :class:`ScoreRepository`/sink.

    Why a Protocol-ish surface
    --------------------------
    The constructor takes the already-constructed components. The
    orchestrator does not own their lifecycle; tests can hand in
    any compatible implementation (e.g. a stub GraphWriter that
    records calls but never writes).
    """

    def __init__(
        self,
        *,
        scoring_pipeline: ScoringPipeline,
        graph_writer: GraphWriter,
        evidence_adapter: EvidenceChainAdapter | None = None,
        graph_store: InMemoryGraphStore | GraphWriterStore | None = None,
    ) -> None:
        self._pipeline = scoring_pipeline
        self._writer = graph_writer
        # The evidence adapter needs the graph store to compute traces.
        # If the caller didn't pass one, we ask the graph writer's
        # underlying store. (GraphWriter itself doesn't carry a
        # public store handle today; we accept the store as a
        # separate parameter for testability.)
        self._graph_store = graph_store
        self._adapter = evidence_adapter

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #

    def run(self, config: IntelligencePipelineConfig) -> IntelligenceRunResult:
        """Run the end-to-end pipeline.

        The function is total: a downstream component failure is
        captured in ``IntelligenceRunResult.errors`` and the partial
        state is returned alongside. The exception is **also**
        re-raised as :class:`IntelligenceRunError` so the caller can
        branch on the failure with a single ``try/except`` — the
        result envelope on the exception is the same partial result
        the caller would see in the success path.

        Why re-raise after returning partial
        -----------------------------------
        Some downstream consumers (cron, Telegram notifier) want a
        boolean success/failure; others want the partial state for a
        best-effort summary. Returning the partial in both shapes
        (envelope + raised exception) is the cleanest contract.
        """
        run_id = config.resolved_run_id()
        orchestrator_id = _stable_orchestrator_id(config)
        started_at_dt = datetime.now(timezone.utc)
        started_at = _iso(started_at_dt)
        t0 = time.perf_counter()
        warnings: list[str] = []
        errors: list[IntelligenceRunError] = []
        snapshot_ids: dict[tuple[str, str], int | None] = {}
        graph_writes: list[GraphWriteResult] = []
        evidence_handles: list[EvidenceQueryHandle] = []
        evidence_node_count = 0

        # --- 1. Score (SignalLoader → InputBuilder → Scorers) ----------
        try:
            pipeline_result = self._run_pipeline(config)
        except Exception as exc:  # noqa: BLE001 — typed wrap below
            err = IntelligenceRunError(
                component="scoring_pipeline",
                message=str(exc),
                error_class=type(exc).__name__,
            )
            errors.append(err)
            warnings.append(
                f"scoring_pipeline failed ({type(exc).__name__}): {exc}"
            )
            # Build an empty report so the result envelope is well-typed.
            pipeline_result = PipelineRunReport(macro=None)

        # Collect snapshot ids (per-result, off the PipelineResult).
        for pr in _iter_pipeline_results(pipeline_result):
            key = (pr.input_bundle.scorer_type, pr.input_bundle.entity_id)
            snapshot_ids[key] = pr.snapshot_id

        # --- 2. Graph writes (PipelineResult → graph) ------------------
        if errors:
            warnings.append("graph writes skipped: scoring_pipeline failed")
        else:
            for pr in _iter_pipeline_results(pipeline_result):
                try:
                    gw = self._writer.write(pr)
                except Exception as exc:  # noqa: BLE001
                    err = IntelligenceRunError(
                        component="graph_writer",
                        message=str(exc),
                        error_class=type(exc).__name__,
                    )
                    errors.append(err)
                    warnings.append(
                        f"graph_writer failed for {pr.input_bundle.scorer_type}/"
                        f"{pr.input_bundle.entity_id} ({type(exc).__name__}): {exc}"
                    )
                    # Synthesize an empty handle so the per-entity list
                    # stays index-aligned with what we'd otherwise return.
                    graph_writes.append(GraphWriteResult(
                        score_node_id="",
                        entity_node_id="",
                        signal_node_ids=(),
                        source_node_ids=(),
                        evidence_signal_ids=tuple(pr.evidence_signal_ids),
                        cross_layer_edge_ids=(),
                        created_node_count=0,
                        created_edge_count=0,
                    ))
                    continue
                graph_writes.append(gw)
                evidence_node_count += len(gw.signal_node_ids)

        # --- 3. Evidence traces (graph → EvidenceChain) -----------------
        if errors:
            warnings.append("evidence traces skipped: prior component failed")
        else:
            for pr, gw in zip(
                _iter_pipeline_results(pipeline_result), graph_writes
            ):
                if gw is None or not gw.score_node_id:
                    evidence_handles.append(EvidenceQueryHandle(
                        scorer_type=pr.input_bundle.scorer_type,
                        entity_id=pr.input_bundle.entity_id,
                        date_bucket=pr.input_bundle.date_bucket,
                        score_node_id="",
                        upstream=None,
                        downstream=None,
                        evidence_summary=None,
                    ))
                    continue
                try:
                    handle = self._trace_one(pr, gw, config)
                except Exception as exc:  # noqa: BLE001
                    err = IntelligenceRunError(
                        component="evidence_tracer",
                        message=str(exc),
                        error_class=type(exc).__name__,
                    )
                    errors.append(err)
                    warnings.append(
                        f"evidence_tracer failed for {gw.score_node_id} "
                        f"({type(exc).__name__}): {exc}"
                    )
                    handle = EvidenceQueryHandle(
                        scorer_type=pr.input_bundle.scorer_type,
                        entity_id=pr.input_bundle.entity_id,
                        date_bucket=pr.input_bundle.date_bucket,
                        score_node_id=gw.score_node_id,
                        upstream=None,
                        downstream=None,
                        evidence_summary=None,
                    )
                evidence_handles.append(handle)

        # --- 4. Aggregate ----------------------------------------------
        finished_at_dt = datetime.now(timezone.utc)
        duration = time.perf_counter() - t0
        node_count, edge_count = self._count_graph()

        # Missing-signal warning is appended at the very end so it
        # can be added even when the run completed successfully but
        # had no signals.
        missing = _missing_signal_warnings(pipeline_result, config)
        warnings = list(_merge_warnings(warnings, missing))

        metadata = {
            "run_id": run_id,
            "orchestrator_id": orchestrator_id,
            "config_hash": config.config_hash,
            "date_bucket": config.date_bucket,
            "persist": config.persist,
            "started_at": started_at,
            "finished_at": _iso(finished_at_dt),
            "duration_seconds": round(duration, 6),
            "industry_ids": list(config.industry_ids),
            "company_specs": [
                {"code": str(s.get("code", ""))}
                for s in config.company_specs
            ],
            "trace_directions": list(config.trace_directions),
            "trace_max_depth": int(config.trace_max_depth),
            "notes": config.notes,
        }

        result = IntelligenceRunResult(
            run_id=run_id,
            config_hash=config.config_hash,
            date_bucket=config.date_bucket,
            persist=config.persist,
            dry_run=not config.persist,
            started_at=started_at,
            finished_at=_iso(finished_at_dt),
            duration_seconds=duration,
            pipeline_result=pipeline_result,
            snapshot_ids=snapshot_ids,
            graph_writes=tuple(graph_writes),
            evidence_handles=tuple(evidence_handles),
            node_count=node_count,
            edge_count=edge_count,
            evidence_node_count=evidence_node_count,
            warnings=tuple(warnings),
            errors=tuple(errors),
            metadata=metadata,
        )

        # Re-raise if any component failed, attaching the partial.
        if errors:
            top = errors[0]
            raise IntelligenceRunError(
                component=top.component,
                message=str(top),
                error_class=top.error_class,
                partial=result,
            ) from None

        return result

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _run_pipeline(
        self, config: IntelligencePipelineConfig,
    ) -> PipelineRunReport:
        """Run the composed :class:`ScoringPipeline` and surface warnings.

        Translates the orchestrator config into the scoring pipeline's
        own config (PipelineConfig) and runs :meth:`run_all`. Any
        exception propagates to the outer run().
        """
        pipeline_cfg = PipelineConfig(
            dry_run=not config.persist,
            notes=config.notes or f"intelligence_pipeline run_id={config.resolved_run_id()}",
            config_hash=config.config_hash,
        )
        # Patch the live config so the per-leg write goes through the
        # same dry_run flag the orchestrator chose. ``ScoringPipeline``
        # captures the config at construction; we accept that and run
        # the pipeline in a mode that honours config.persist:
        # * persist=True  → the sink writes through (default PipelineConfig.dry_run=False)
        # * persist=False → we want the legs to skip writes even when the
        #   pipeline's sink is a real one. The simplest way: temporarily
        #   flip pipeline._config.dry_run. The pipeline is mutated
        #   in-memory; no test should rely on it surviving past a run.
        self._pipeline._config = pipeline_cfg  # noqa: SLF001 (intentional hook)

        return self._pipeline.run_all(
            date_bucket=config.date_bucket,
            industry_ids=list(config.industry_ids),
            company_specs=[dict(s) for s in config.company_specs],
            run_macro=config.run_macro,
        )

    def _trace_one(
        self,
        result: PipelineResult,
        graph_write: GraphWriteResult,
        config: IntelligencePipelineConfig,
    ) -> EvidenceQueryHandle:
        """Run upstream / downstream evidence traces for one score."""
        upstream_chain: EvidenceChain | None = None
        downstream_chain: EvidenceChain | None = None
        if self._adapter is not None:
            for direction in config.trace_directions:
                chain = self._adapter.trace(
                    result,
                    max_depth=config.trace_max_depth,
                    direction=direction,
                )
                if direction == "upstream":
                    upstream_chain = chain
                elif direction == "downstream":
                    downstream_chain = chain
        else:
            # Adapter is optional; if not provided, return an empty
            # handle so callers can still rely on the surface.
            warnings_extend: list[str] = []
            # No-op, but we still try to build the score node id so
            # the handle is well-typed.
        score_node_id = graph_write.score_node_id or score_node_id_for_result(result)
        return EvidenceQueryHandle(
            scorer_type=result.input_bundle.scorer_type,
            entity_id=result.input_bundle.entity_id,
            date_bucket=result.input_bundle.date_bucket,
            score_node_id=score_node_id,
            upstream=upstream_chain,
            downstream=downstream_chain,
            evidence_summary=_summary_dict(upstream_chain),
        )

    def _count_graph(self) -> tuple[int, int]:
        """Return ``(node_count, edge_count)`` from the graph store.

        For :class:`InMemoryGraphStore` we call its public ``stats()``;
        for stores that only implement the writer Protocol we return
        ``(0, 0)`` — the handles are still authoritative.
        """
        store = self._graph_store
        if store is None:
            return (0, 0)
        if hasattr(store, "stats"):
            stats = store.stats()  # type: ignore[attr-defined]
            return (int(stats.get("total_nodes", 0)), int(stats.get("total_edges", 0)))
        return (0, 0)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _iter_pipeline_results(report: PipelineRunReport):
    """Yield :class:`PipelineResult` objects in stable order.

    Order: macro (if present) → industries → companies. Matches
    :meth:`ScoringPipeline.run_all` so the per-entity list is
    index-aligned across ``graph_writes`` and ``evidence_handles``.
    """
    if report.macro is not None:
        yield report.macro
    yield from report.industries
    yield from report.companies


def _missing_signal_warnings(
    report: PipelineRunReport,
    config: IntelligencePipelineConfig,
) -> tuple[str, ...]:
    """Surface a warning when a configured entity had no signals.

    The scoring pipeline already emits its own per-leg warnings, but
    those are tied to the dimension data. A leg that had zero signals
    still produces a score (the scorers default to neutral), which
    can be surprising to a caller. We add a single, stable warning
    per affected leg so the issue is visible in the result envelope.
    """
    out: list[str] = []
    seen: set[tuple[str, str]] = set()

    def _warn(scorer: str, entity: str) -> None:
        key = (scorer, entity)
        if key in seen:
            return
        seen.add(key)
        out.append(
            f"no signals observed for {scorer}/{entity} at {config.date_bucket}"
        )

    for pr in _iter_pipeline_results(report):
        bundle = pr.input_bundle
        any_signal = any(d.signal_ids for d in bundle.dimensions.values())
        if not any_signal:
            _warn(bundle.scorer_type, bundle.entity_id)
    return tuple(out)


# Re-export the two Protocols callers will need most.
__all__ = [
    "IntelligencePipeline",
    "IntelligencePipelineConfig",
    "IntelligenceRunResult",
    "IntelligenceRunError",
    "EvidenceQueryHandle",
    "GraphWriterStore",
    "SnapshotSink",
    "SignalLoaderFilters",
    "make_score_node_id",
]
