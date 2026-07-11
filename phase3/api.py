"""Phase 3B Task 5 Run 4 — Thin API / service entrypoints.

The API layer is a *thin* wrapper over the existing Run 1 / Run 2 /
Run 3 pipeline / recovery / reporting modules. It exists for callers
that do not want to construct the orchestrator by hand (cron jobs,
dispatcher integrations, programmatic tests) but still need a
stable, documented contract.

Design constraints
------------------

* **No new business logic.** Every function delegates to a Run 1-3
  module. The only logic here is *argument assembly* (translating
  primitive kwargs into the typed DTOs the existing modules expect)
  and *result unwrapping* (turning the typed envelopes into
  JSON-serialisable dicts that are easy for callers to consume).
* **No scheduler, daemon, HTTP server, or production wiring.** The
  functions are synchronous and in-process. The CLI subcommands in
  :mod:`phase3.cli` are the preferred user-facing surface; the
  API is for code.
* **Default-safe.** Persist requires an explicit ``persist=True``
  + a real ``db_path``; the dry-run path is the default. The
  database path is guarded against the reserved
  ``macro_history.db`` name (same path-guard used by ``init-db`` /
  ``ingest-signals``).
* **Deterministic.** Two calls with the same kwargs return
  ``to_dict()`` views whose equality depends only on the inputs,
  not on wall-clock state.

Public API
----------

* :func:`run_pipeline` — execute an end-to-end intelligence run.
* :func:`resume_pipeline` — resume a previously-started run.
* :func:`observe_pipeline_status` — build a :class:`RunState`
  snapshot for a given config without running anything.
* :func:`export_pipeline_report` — render JSON + Markdown export
  to a destination directory.

Error contract
--------------

All four functions raise :class:`PipelineAPIError` on any
configuration / persistence problem so callers have a single
typed exception to catch. The original exception (if any) is
preserved as ``__cause__``.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from phase3.graph.evidence_trace_export import EvidenceChainAdapter
from phase3.graph.in_memory_store import GraphStore as InMemoryGraphStore
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.score_repo import ScoreRepository
from phase3.persistence.schema_v1 import build as build_v1_schema
from phase3.persistence.signal_repo import SignalRepository
from phase3.persistence.sqlite import SQLiteStore, _check_path
from phase3.persistence.sqlite import PathGuardError as _PathGuardError

# Phase 4 Task 1B (F1 close): SQLiteGraphStore is now used on the
# persist path. Imported lazily inside the factory so the dry-run path
# does not pay the import cost; the import is also guarded so a
# broken SQLite stack does not take the whole module down on
# import-time.
try:  # noqa: SIM105
    from phase3.graph.sqlite_store import SQLiteGraphStore as _SQLiteGraphStore
except Exception:  # pragma: no cover - defensive
    _SQLiteGraphStore = None  # type: ignore[assignment]
from phase3.pipeline.graph_writer import GraphWriter
from phase3.pipeline.intelligence_pipeline import (
    IntelligencePipeline,
    IntelligencePipelineConfig,
    IntelligenceRunResult,
)
from phase3.pipeline.recovery import (
    RecoveryConfig,
    RecoveryManager,
    RecoveryResult,
    RunState,
)
from phase3.pipeline.reporting import (
    ReportArtifact,
    ReportConfig,
    build_json_export,
    export_report,
    render_markdown_report,
)
from phase3.pipeline.scoring_pipeline import (
    PipelineConfig,
    ScoringPipeline,
)
from phase3.pipeline.snapshot_writer import (
    SnapshotWriter,
    SnapshotWriterConfig,
)
from phase3.scoring.company import CompanyScorer
from phase3.scoring.industry import IndustryScorer
from phase3.scoring.macro import MacroScorer


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class PipelineAPIError(RuntimeError):
    """Typed failure raised by the API layer.

    Carries ``error_class`` (short string for programmatic
    branching) and ``component`` (which sub-layer failed). The
    original exception is preserved as ``__cause__``.
    """

    def __init__(
        self,
        component: str,
        message: str,
        *,
        error_class: str = "",
    ) -> None:
        super().__init__(message)
        self.component = component
        self.error_class = error_class or type(self).__name__


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


#: Reserved DB path that callers must NEVER point the API at.
#: Same convention as the persistence CLI uses.
_RESERVED_DB_NAMES: frozenset[str] = frozenset({"macro_history.db"})


def _validate_db_path(db_path: str | None) -> str | None:
    """Return the resolved db_path or raise :class:`PipelineAPIError`.

    ``None`` (or empty string) is returned unchanged — the caller
    may want the dry-run path which never touches a DB. For non-None
    inputs we delegate to the existing
    :func:`phase3.persistence.sqlite._check_path` so the same
    guards the CLI uses (basename match against
    ``macro_history.db``) apply here.
    """
    if not db_path:
        return db_path
    try:
        return _check_path(db_path)
    except _PathGuardError as exc:
        raise PipelineAPIError(
            component="db_path_guard",
            message=str(exc),
            error_class="PathGuardError",
        ) from exc


def _open_store(db_path: str) -> SQLiteStore:
    """Open a :class:`SQLiteStore` at ``db_path`` and apply the v1
    schema if the file is empty.

    Schema application is idempotent — re-running it on a
    pre-existing DB is a no-op. We use the existing
    :class:`MigrationManager` instead of the SQLite store's
    auto-apply so we can report applied migrations in a uniform
    shape.
    """
    store = SQLiteStore(db_path)
    try:
        mgr = MigrationManager(store, [build_v1_schema()])
        mgr.apply()
    except Exception:
        store.close()
        raise
    return store


# Phase 4 Task 1B (F1 close): small graph-store factory. The
# persist path used to instantiate :class:`InMemoryGraphStore`
# unconditionally; this helper centralises the choice so all
# three call sites (default-build, dry-run, resume) share the
# same selection logic. Default behaviour:
#
# * ``db_path is None`` or ``force_in_memory=True`` -> in-memory
#   store (the dry-run / ephemeral path; no filesystem touch).
# * Otherwise -> :class:`SQLiteGraphStore` at the (path-guard
#   validated) ``db_path``. Schema is auto-applied.
#
# The path-guard failure path mirrors ``_validate_db_path`` so
# callers get a typed :class:`PipelineAPIError` (component
# ``db_path_guard``) for the ``macro_history.db`` case instead
# of a raw :class:`PathGuardError`.
_GRAPH_STORE_IMPORT_ERROR_HINT = (
    "phase3.graph.sqlite_store could not be imported; the "
    "in-memory graph store will be used as a fallback."
)


def _build_graph_store(
    db_path: str | None,
    *,
    force_in_memory: bool = False,
    auto_migrate: bool = True,
) -> InMemoryGraphStore | Any:  # returns SQLiteGraphStore when persist
    """Return the right graph store for the API call site.

    Parameters
    ----------
    db_path:
        Resolved / validated DB path. ``None`` means the dry-run
        path which never touches a DB.
    force_in_memory:
        Explicit override — always return an in-memory store even
        when ``db_path`` is set. Used by tests and by the
        ``force-in-memory-graph`` knob the API surface may expose
        in future.
    auto_migrate:
        Forwarded to :class:`SQLiteGraphStore`. ``False`` is the
        test-friendly shape (the caller drives the migration).
    """
    if not db_path or force_in_memory:
        # Empty string / None -> dry-run path. The pre-F1 API's
        # ``_validate_db_path`` treats empty string the same as
        # None; we preserve that contract.
        return InMemoryGraphStore()
    if _SQLiteGraphStore is None:
        # SQLite stack is unavailable. Fall back to in-memory so
        # the API does not raise a hard ImportError. This is a
        # defensive branch; in normal operation ``_SQLiteGraphStore``
        # is the real class.
        return InMemoryGraphStore()
    # db_path is non-None here; we re-validate through the same
    # path guard the rest of the API uses so the call-site code
    # path is uniform. If the guard fails we let the typed
    # PipelineAPIError bubble up.
    try:
        resolved = _check_path(db_path)
    except _PathGuardError as exc:
        raise PipelineAPIError(
            component="db_path_guard",
            message=str(exc),
            error_class="PathGuardError",
        ) from exc
    return _SQLiteGraphStore(resolved, auto_migrate=auto_migrate)


def _build_default_components(
    *,
    db_path: str,
    config_hash: str,
    run_id: str | None = None,
    run_mode: str = "live",
    dry_run: bool = False,
) -> tuple[SQLiteStore, ScoringPipeline, InMemoryGraphStore, ScoreRepository, SignalRepository]:
    """Wire up the standard components for a real run.

    Returns ``(store, scoring_pipeline, in_memory_graph, score_repo,
    signal_repo)``. The graph store is in-memory even on a persist
    run — that matches the rest of the project (Task 3 ships
    :class:`GraphWriter` against in-memory storage; SQLite graph
    storage is a separate, optional concern).
    """
    store = _open_store(db_path)
    try:
        signal_repo = SignalRepository(store)
        score_repo = ScoreRepository(store)
        from phase3.pipeline.signal_loader import SignalLoader
        writer = SnapshotWriter(
            score_repo,
            SnapshotWriterConfig(
                run_mode=run_mode,
                run_id=run_id,
                dry_run=dry_run,
            ),
        )
        pipeline = ScoringPipeline(
            signal_loader=SignalLoader(signal_repo),
            macro_scorer=MacroScorer(config_hash=config_hash),
            industry_scorer=IndustryScorer(config_hash=config_hash),
            company_scorer=CompanyScorer(config_hash=config_hash),
            sink=writer,
            config=PipelineConfig(
                dry_run=dry_run,
                notes="phase3-api",
                config_hash=config_hash,
            ),
        )
        # Phase 4 Task 1B (F1 close): the persist path now writes the
        # graph to the same SQLite file the rest of the project uses
        # for signals / scores. The factory picks SQLiteGraphStore
        # here because ``db_path`` is non-None.
        graph_store = _build_graph_store(db_path)
        graph_writer = GraphWriter(graph_store)
        adapter = EvidenceChainAdapter(graph_store)
        return store, pipeline, graph_store, score_repo, signal_repo
    except Exception:
        store.close()
        raise


def _build_orchestrator(
    *,
    db_path: str | None,
    config_hash: str,
    run_id: str | None,
    run_mode: str,
    dry_run: bool,
) -> tuple[IntelligencePipeline, SQLiteStore | None, ScoreRepository | None]:
    """Build an :class:`IntelligencePipeline` and (optionally) a
    real :class:`ScoreRepository` for the persist path.

    The store handle is returned (alongside the pipeline) so the
    caller can close it. When ``db_path`` is None we return
    ``(pipeline, None, None)`` — the dry-run path.
    """
    if db_path is None:
        # Dry-run: an in-memory graph + a never-persisted pipeline
        # is enough. We use the same in-memory default to keep the
        # shape symmetric with the persist path. ``db_path=None``
        # signals the factory to skip SQLite entirely.
        graph_store = _build_graph_store(None)
        graph_writer = GraphWriter(graph_store)
        adapter = EvidenceChainAdapter(graph_store)
        # Build a *throwaway* store that we close immediately so
        # the components that need a repository (ScoringPipeline)
        # have *something* to call .history() on. The signal
        # repository returns no signals, so the pipeline produces
        # an empty (warmed) report.
        fd, path = tempfile.mkstemp(prefix="phase3_api_dry", suffix=".db")
        os.close(fd)
        try:
            store = _open_store(path)
        finally:
            # We do NOT close the store here because the
            # ScoringPipeline keeps a reference. The caller
            # receives it via the returned tuple and is
            # responsible for closing.
            pass
        try:
            signal_repo = SignalRepository(store)
            score_repo = ScoreRepository(store)
            writer = SnapshotWriter(
                score_repo,
                SnapshotWriterConfig(
                    run_mode="dry",
                    run_id=run_id,
                    dry_run=True,
                ),
            )
            from phase3.pipeline.signal_loader import SignalLoader
            pipeline = ScoringPipeline(
                signal_loader=SignalLoader(signal_repo),
                macro_scorer=MacroScorer(config_hash=config_hash),
                industry_scorer=IndustryScorer(config_hash=config_hash),
                company_scorer=CompanyScorer(config_hash=config_hash),
                sink=writer,
                config=PipelineConfig(
                    dry_run=True,
                    notes="phase3-api-dry",
                    config_hash=config_hash,
                ),
            )
            orch = IntelligencePipeline(
                scoring_pipeline=pipeline,
                graph_writer=graph_writer,
                evidence_adapter=adapter,
                graph_store=graph_store,
            )
            return orch, store, score_repo
        except Exception:
            store.close()
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
    resolved = _validate_db_path(db_path)
    assert resolved is not None  # type: ignore[unreachable]
    store, pipeline, graph_store, score_repo, _signal_repo = _build_default_components(
        db_path=resolved,
        config_hash=config_hash,
        run_id=run_id,
        run_mode=run_mode,
        dry_run=dry_run,
    )
    try:
        graph_writer = GraphWriter(graph_store)
        adapter = EvidenceChainAdapter(graph_store)
        orch = IntelligencePipeline(
            scoring_pipeline=pipeline,
            graph_writer=graph_writer,
            evidence_adapter=adapter,
            graph_store=graph_store,
        )
        return orch, store, score_repo
    except Exception:
        store.close()
        raise


def _result_to_dict(result: Any) -> dict[str, Any]:
    """Unwrap a typed result into a JSON-serialisable dict.

    Both :class:`IntelligenceRunResult` and :class:`RecoveryResult`
    expose a ``.to_dict()`` view; the dict is JSON-serialisable and
    is what the API returns.
    """
    if result is None:
        return {}
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if not isinstance(data, dict):
                raise PipelineAPIError(
                    component="serialization",
                    message=f"to_dict() returned non-dict: {type(data).__name__}",
                    error_class="TypeError",
                )
            return data
        except PipelineAPIError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PipelineAPIError(
                component="serialization",
                message=f"to_dict() raised: {exc!r}",
                error_class=type(exc).__name__,
            ) from exc
    raise PipelineAPIError(
        component="serialization",
        message=f"result of type {type(result).__name__!r} has no to_dict()",
        error_class="TypeError",
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class APIResult:
    """Container returned by the API functions.

    Carries the typed result envelope (so callers who want
    structured access can use it directly) plus a JSON-serialisable
    ``payload`` dict for the simpler "give me a dict" case.

    Attributes:
        kind: ``"run"`` | ``"resume"`` | ``"status"`` | ``"export"``.
        result: The typed result object (``IntelligenceRunResult`` /
            ``RecoveryResult`` / ``RunState`` / ``tuple[ReportArtifact, ReportArtifact]``).
        payload: JSON-serialisable dict. For ``"export"`` this is
            the two-artifact ``{"json": {...}, "markdown": {...}}`` shape.
    """

    kind: str
    result: Any
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": self.payload,
        }


def run_pipeline(
    *,
    date_bucket: str,
    industry_ids: Sequence[str] = (),
    company_specs: Sequence[Mapping[str, Any]] = (),
    run_macro: bool = True,
    persist: bool = False,
    db_path: str | None = None,
    run_id: str = "",
    config_hash: str = "phase3-api",
    notes: str = "",
    trace_directions: Sequence[str] = ("upstream",),
    trace_max_depth: int = 5,
) -> APIResult:
    """Run the end-to-end intelligence pipeline and return a serialisable result.

    Default is dry-run (``persist=False``). To exercise the persist
    path, set ``persist=True`` and supply a writable ``db_path``
    (the path is guarded against ``macro_history.db``).
    """
    if persist and not db_path:
        raise PipelineAPIError(
            component="run",
            message="persist=True requires a db_path",
            error_class="ValueError",
        )
    if not persist and not db_path:
        # dry-run path — we still need *some* score_repo to satisfy
        # the snapshot sink contract; build a temp store.
        db_path = None  # explicit: downstream uses the dry branch
    try:
        cfg = IntelligencePipelineConfig(
            date_bucket=date_bucket,
            industry_ids=tuple(industry_ids),
            company_specs=tuple(company_specs),
            run_macro=run_macro,
            persist=persist,
            run_id=run_id,
            config_hash=config_hash,
            notes=notes,
            trace_directions=tuple(trace_directions),
            trace_max_depth=trace_max_depth,
        )
    except Exception as exc:  # noqa: BLE001
        raise PipelineAPIError(
            component="run",
            message=f"invalid IntelligencePipelineConfig: {exc}",
            error_class=type(exc).__name__,
        ) from exc
    try:
        orch, store, _score_repo = _build_orchestrator(
            db_path=db_path,
            config_hash=config_hash,
            run_id=cfg.resolved_run_id(),
            run_mode="live" if persist else "dry",
            dry_run=not persist,
        )
    except PipelineAPIError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PipelineAPIError(
            component="run.setup",
            message=f"failed to build orchestrator: {exc}",
            error_class=type(exc).__name__,
        ) from exc
    try:
        result = orch.run(cfg)
    except Exception as exc:  # noqa: BLE001
        raise PipelineAPIError(
            component="run.execute",
            message=f"orchestrator.run raised: {exc}",
            error_class=type(exc).__name__,
        ) from exc
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
    return APIResult(
        kind="run",
        result=result,
        payload=_result_to_dict(result),
    )


def observe_pipeline_status(
    *,
    date_bucket: str,
    config_hash: str = "phase3-api",
    run_id: str = "",
    db_path: str | None,
) -> APIResult:
    """Read persisted state for a run *without* executing it.

    Returns a :class:`RunState` envelope. Requires ``db_path``
    (the recovery layer reads from existing score / graph stores).
    """
    if not db_path:
        raise PipelineAPIError(
            component="status",
            message="observe_pipeline_status requires db_path",
            error_class="ValueError",
        )
    resolved = _validate_db_path(db_path)
    if resolved is None:
        raise PipelineAPIError(
            component="status",
            message="db_path validation returned None",
            error_class="ValueError",
        )
    cfg = IntelligencePipelineConfig(
        date_bucket=date_bucket,
        config_hash=config_hash,
        run_id=run_id,
    )
    store = _open_store(resolved)
    try:
        score_repo = ScoreRepository(store)
        mgr = RecoveryManager(score_repo=score_repo)
        state = mgr.observe_state(cfg)
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass
    return APIResult(
        kind="status",
        result=state,
        payload=_result_to_dict(state),
    )


def resume_pipeline(
    *,
    date_bucket: str,
    config_hash: str = "phase3-api",
    run_id: str = "",
    db_path: str,
    max_attempts: int = 2,
    retry_backoff_seconds: float = 0.0,
    strict_resume: bool = True,
) -> APIResult:
    """Resume a previously-started run.

    Requires ``db_path`` (the recovery layer reads from existing
    score / graph stores). Returns a :class:`RecoveryResult`
    envelope.
    """
    if not db_path:
        raise PipelineAPIError(
            component="resume",
            message="resume_pipeline requires db_path",
            error_class="ValueError",
        )
    resolved = _validate_db_path(db_path)
    if resolved is None:
        raise PipelineAPIError(
            component="resume",
            message="db_path validation returned None",
            error_class="ValueError",
        )
    cfg = IntelligencePipelineConfig(
        date_bucket=date_bucket,
        config_hash=config_hash,
        run_id=run_id,
    )
    store = _open_store(resolved)
    try:
        score_repo = ScoreRepository(store)
        signal_repo = SignalRepository(store)
        # Build a fresh orchestrator pointing at the same store so
        # resume can re-execute the pipeline if needed.
        from phase3.pipeline.signal_loader import SignalLoader
        writer = SnapshotWriter(
            score_repo,
            SnapshotWriterConfig(run_mode="live"),
        )
        pipeline = ScoringPipeline(
            signal_loader=SignalLoader(signal_repo),
            macro_scorer=MacroScorer(config_hash=config_hash),
            industry_scorer=IndustryScorer(config_hash=config_hash),
            company_scorer=CompanyScorer(config_hash=config_hash),
            sink=writer,
            config=PipelineConfig(
                dry_run=False,
                notes="phase3-api-resume",
                config_hash=config_hash,
            ),
        )
        # Phase 4 Task 1B (F1 close): resume now also reads the
        # graph from SQLite (same path the original run wrote to).
        # The factory picks SQLiteGraphStore because ``resolved``
        # is non-None after the path-guard.
        graph_store = _build_graph_store(resolved)
        graph_writer = GraphWriter(graph_store)
        adapter = EvidenceChainAdapter(graph_store)
        orch = IntelligencePipeline(
            scoring_pipeline=pipeline,
            graph_writer=graph_writer,
            evidence_adapter=adapter,
            graph_store=graph_store,
        )
        recovery = RecoveryManager(
            score_repo=score_repo,
            pipeline=orch,
            config=RecoveryConfig(
                max_attempts=max_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                strict_resume=strict_resume,
            ),
        )
        state = recovery.observe_state(cfg)
        result = recovery.resume(cfg, state=state)
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass
    return APIResult(
        kind="resume",
        result=result,
        payload=_result_to_dict(result),
    )


def export_pipeline_report(
    *,
    result: Any,
    output_dir: str,
    run_label: str = "",
    schema_version: int = 1,
    include_evidence_payload: bool = True,
    recovery: bool = False,
) -> APIResult:
    """Render JSON + Markdown exports to ``output_dir``.

    Delegates to :func:`phase3.pipeline.reporting.export_report`
    unchanged. The result is an :class:`APIResult` whose
    ``payload`` carries the two :class:`ReportArtifact` records
    (path + sha256 + size + kind).
    """
    if not output_dir:
        raise PipelineAPIError(
            component="export",
            message="output_dir is required",
            error_class="ValueError",
        )
    try:
        cfg = ReportConfig(
            output_dir=output_dir,
            run_label=run_label,
            schema_version=schema_version,
            include_evidence_payload=include_evidence_payload,
            recovery=recovery,
        )
    except Exception as exc:  # noqa: BLE001
        raise PipelineAPIError(
            component="export",
            message=f"invalid ReportConfig: {exc}",
            error_class=type(exc).__name__,
        ) from exc
    try:
        json_artifact, md_artifact = export_report(result, config=cfg)
    except Exception as exc:  # noqa: BLE001
        raise PipelineAPIError(
            component="export",
            message=f"export_report raised: {exc}",
            error_class=type(exc).__name__,
        ) from exc
    payload = {
        "json": {
            "kind": json_artifact.kind,
            "path": json_artifact.path,
            "sha256": json_artifact.sha256,
            "size_bytes": json_artifact.size_bytes,
        },
        "markdown": {
            "kind": md_artifact.kind,
            "path": md_artifact.path,
            "sha256": md_artifact.sha256,
            "size_bytes": md_artifact.size_bytes,
        },
    }
    return APIResult(
        kind="export",
        result=(json_artifact, md_artifact),
        payload=payload,
    )


__all__ = [
    "PipelineAPIError",
    "APIResult",
    "run_pipeline",
    "resume_pipeline",
    "observe_pipeline_status",
    "export_pipeline_report",
    # re-exports for callers that want to build the config DTOs
    # without depending on the implementation modules directly.
    "IntelligencePipelineConfig",
    "RecoveryConfig",
    "ReportConfig",
]
