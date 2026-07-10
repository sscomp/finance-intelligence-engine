"""Recovery / Resume Layer — Phase 3B Task 5 Run 2.

This module adds **persistence-aware resume and replay** semantics
around :class:`IntelligencePipeline` (Task 5 Run 1) **without**
duplicating any repository, snapshot, or graph logic.

The orchestrator from Run 1 is one-shot: it runs ``score →
graph_write → evidence_trace`` and either succeeds or fails as a
whole. In production, a run can be interrupted at any stage:

* Process crash between ``score`` and ``graph_write`` — snapshots
  are persisted, graph nodes/edges are not.
* Database lock / disk-full mid-``graph_write`` — partial graph.
* ``evidence_tracer`` OOM after both prior stages finished.

The recovery layer captures the *state* of a run into a small
typed envelope (:class:`RunState`) and exposes a single entry
point (:class:`RecoveryManager.resume`) that re-runs the missing
stages only. Two safety contracts are enforced:

* **Append-only snapshots.** Replays never call
  :class:`ScoreRepository` ``update``/``delete``. The score table
  is append-only by trigger; this layer reuses the contract by
  delegating to :class:`SnapshotWriter` (which itself only calls
  ``append``).
* **Idempotent graph writes.** Replays go through
  :class:`GraphWriter` + :class:`GraphRepository` whose
  ``upsert_node`` / ``upsert_edge`` are keyed on deterministic
  ids. Replaying the same inputs never duplicates a node or edge.

Design constraints
------------------
* **No duplicate repository logic.** This module never executes
  SQL itself. It delegates every read/write to existing
  repositories and the existing orchestrator.
* **Deterministic run_id / config_hash.** ``RunState.run_id`` and
  ``RunState.config_hash`` are the same values Run 1 emits in
  :class:`IntelligenceRunResult.metadata`. Re-running
  :class:`IntelligencePipeline.run` with the same
  :class:`IntelligencePipelineConfig` re-emits the same values.
* **Typed retryable vs terminal classification.** Every caught
  exception is classified into :class:`FailureCategory` —
  ``RETRYABLE`` (transient: lock, IOError, TimeoutError) or
  ``TERMINAL`` (config / contract / value errors). The caller
  decides what to do with the classification.
* **Default-safe.** :meth:`RecoveryManager.resume` *never* mutates
  production state. It only *reads* ``RunState`` and *writes* via
  the orchestrator's existing persist path.
* **No production wiring.** This is a library module. CLI surface,
  cron, and the dispatcher integration land in later runs.

Scope for Run 2
---------------
* :class:`Stage` — typed enum of pipeline stages.
* :class:`FailureCategory` — retryable / terminal classification.
* :class:`RunState` — typed envelope describing a run's progress.
* :class:`RecoveryResult` — typed envelope describing the outcome
  of a resume attempt.
* :class:`RecoveryConfig` — configuration DTO.
* :class:`RecoveryManager` — the orchestrator for resume + retry.

Non-goals for Run 2
-------------------
* Network / scheduler / CLI surface.
* Persisting :class:`RunState` to a side table — Run 2 builds the
  state from existing snapshot + graph reads; a side table can
  be added in Run 3 once the schema is stable.
* Replacing the orchestrator. Run 2 only *wraps* it.
"""
from __future__ import annotations

import hashlib
import json
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from phase3.persistence.score_repo import ScoreRepository
from phase3.pipeline.intelligence_pipeline import (
    IntelligencePipeline,
    IntelligencePipelineConfig,
    IntelligenceRunError,
    IntelligenceRunResult,
)


# ---------------------------------------------------------------------------
# Stage taxonomy
# ---------------------------------------------------------------------------


class Stage(str, Enum):
    """Ordered list of pipeline stages the recovery layer can resume.

    Order matters — it is the canonical progression of the
    orchestrator. ``Stage`` is a :class:`str` subclass so it
    serialises cleanly to JSON and is hashable in sets.
    """

    SCORED = "scored"
    SNAPSHOTTED = "snapshotted"
    GRAPH_WRITTEN = "graph_written"
    EVIDENCE_TRACED = "evidence_traced"

    @classmethod
    def ordered(cls) -> tuple["Stage", ...]:
        """Return the canonical order of all stages."""
        return (
            cls.SCORED,
            cls.SNAPSHOTTED,
            cls.GRAPH_WRITTEN,
            cls.EVIDENCE_TRACED,
        )

    def next(self) -> "Stage | None":
        """Return the next stage in canonical order, or ``None``."""
        seq = self.ordered()
        idx = seq.index(self)
        if idx + 1 >= len(seq):
            return None
        return seq[idx + 1]


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class FailureCategory(str, Enum):
    """Classification of a downstream-component failure.

    * ``RETRYABLE`` — transient. Re-running with the same config
      is expected to succeed (DB locked, IO error, OOM, timeout).
    * ``TERMINAL`` — config / contract error. Re-running with the
      same config will fail the same way (ValueError, KeyError,
      TypeError, schema mismatch).
    * ``UNKNOWN`` — we couldn't classify; treat as retryable but
      surface a warning.
    """

    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


# Substrings that map to RETRYABLE. The matching is case-insensitive.
_RETRYABLE_SUBSTRINGS: tuple[str, ...] = (
    "database is locked",
    "disk i/o error",
    "i/o error",
    "timeout",
    "connection",
    "temporarily unavailable",
    "resource temporarily",
    "out of memory",
    "memoryerror",
    "operationalerror",
    "busy",
)

# Substrings that map to TERMINAL. Anything that signals a logic
# or contract error.
_TERMINAL_SUBSTRINGS: tuple[str, ...] = (
    "valueerror",
    "keyerror",
    "typeerror",
    "attributeerror",
    "importerror",
    "modulenotfounderror",
    "syntaxerror",
    "indentationerror",
    "nameerror",
    "schema",
    "integrity",
    "constraint",
    "not implemented",
    "unsupported",
    "contract",
    "validation",
)

# Exception classes that are always RETRYABLE. We compare on the
# class name (lowercased) so the import surface stays small.
_RETRYABLE_EXC_NAMES: frozenset[str] = frozenset({
    "timeouterror",
    "interruptederror",
    "connectionerror",
    "ioerror",
    "oserror",
    "memoryerror",
    "operationalerror",
    "databaseerror",
    "busyerror",
})

# Exception classes that are always TERMINAL.
_TERMINAL_EXC_NAMES: frozenset[str] = frozenset({
    "valueerror",
    "keyerror",
    "typeerror",
    "attributeerror",
    "importerror",
    "modulenotfounderror",
    "syntaxerror",
    "indentationerror",
    "nameerror",
    "notimplementederror",
    "runtimeerror",  # ambiguous by name; we still treat as terminal
                     # by default — Run 1 wraps downstream errors
                     # as IntelligenceRunError, not RuntimeError.
    "scoresnapshotmutationerror",
})


def classify_failure(exc: BaseException) -> FailureCategory:
    """Classify an exception as :class:`FailureCategory`.

    The classification is deliberately conservative: a class
    match wins over a substring match; a substring match in the
    error message wins over ``UNKNOWN``; the default is
    ``UNKNOWN`` (treated as retryable by callers).

    Special case: :class:`IntelligenceRunError` carries the
    original error's class name in ``error_class`` (set by the
    orchestrator) — we classify by *that* field, which is more
    accurate than walking the cause chain (the orchestrator
    uses ``raise ... from None`` to suppress the chain).
    """
    # --- Special case: IntelligenceRunError carries the
    # original error's class in ``error_class``.
    err_class = getattr(exc, "error_class", None)
    if err_class:
        name = str(err_class).lower().strip()
        if name in _RETRYABLE_EXC_NAMES:
            return FailureCategory.RETRYABLE
        if name in _TERMINAL_EXC_NAMES:
            return FailureCategory.TERMINAL
    # --- Standard: walk the cause / context chain.
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__.lower()
        if name in _RETRYABLE_EXC_NAMES:
            return FailureCategory.RETRYABLE
        if name in _TERMINAL_EXC_NAMES:
            return FailureCategory.TERMINAL
        msg = str(cur).lower()
        for needle in _RETRYABLE_SUBSTRINGS:
            if needle in msg:
                return FailureCategory.RETRYABLE
        for needle in _TERMINAL_SUBSTRINGS:
            if needle in msg:
                return FailureCategory.TERMINAL
        # Walk the cause chain. ``__cause__`` is set when
        # ``raise X from Y`` is used; ``__context__`` is set
        # implicitly when an exception is raised inside an
        # ``except`` block. We follow both.
        cur = cur.__cause__ or cur.__context__
    return FailureCategory.UNKNOWN


# ---------------------------------------------------------------------------
# Configuration DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryConfig:
    """Configuration for :class:`RecoveryManager`.

    Attributes:
        max_attempts: Maximum number of retry attempts for a
            retryable stage. ``0`` means "do not retry, fail
            fast". Default: 2 (i.e. one initial attempt plus
            one retry).
        retry_backoff_seconds: Sleep between attempts. Set to
            ``0`` to disable. Default: 0.
        start_from: Force the resume to start at ``start_from``
            even if the persisted state says an earlier stage
            already completed. Use with care — re-running
            ``SCORED`` from scratch always works; re-running
            ``GRAPH_WRITTEN`` after ``SNAPSHOTTED`` is safe
            (graph is upsert) but unnecessary. Default: ``None``
            (derive from state).
        strict_resume: If True, refuse to resume a run whose
            state is empty or whose ``config_hash`` does not
            match the new run's ``config_hash``. Default: True.
    """

    max_attempts: int = 2
    retry_backoff_seconds: float = 0.0
    start_from: Stage | None = None
    strict_resume: bool = True


# ---------------------------------------------------------------------------
# State / result DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunState:
    """Persisted state of a single intelligence pipeline run.

    A :class:`RunState` is the *minimum* the recovery layer
    needs to decide what to do next. It is built from existing
    snapshot + graph reads; nothing in this class is persisted
    directly.

    Attributes:
        run_id: The deterministic run handle.
        config_hash: The stable config hash.
        date_bucket: The YYYY-MM-DD bucket the run is for.
        last_completed_stage: The deepest stage the run has
            finished. ``Stage.SCORED`` means signals were loaded
            and scorers ran, regardless of whether the snapshot
            row was persisted (snapshot is a sub-stage of
            ``SCORED``).
        completed_stages: Tuple of stages that have been observed
            as complete. A stage is "complete" when the side
            effects of that stage are visible (snapshot row
            exists for ``SNAPSHOTTED``, graph nodes/edges exist
            for ``GRAPH_WRITTEN``, evidence handles non-empty
            for ``EVIDENCE_TRACED``).
        snapshot_ids: Mapping ``(scorer_type, entity_id) →
            snapshot_id`` observed in the score_snapshot table.
            Empty when ``persist=False`` was used.
        graph_node_ids: Tuple of graph node ids observed for
            this run. Sourced from the run_id tag (when nodes
            carry one) or from the ``config_hash``/``date_bucket``
            filter.
        graph_edge_ids: Tuple of graph edge ids observed.
        warnings: Order-stable state-level warnings.
        observed_at: UTC ISO-8601 timestamp the state was
            captured.
    """

    run_id: str
    config_hash: str
    date_bucket: str
    last_completed_stage: Stage
    completed_stages: tuple[Stage, ...] = ()
    snapshot_ids: Mapping[tuple[str, str], int] = field(default_factory=dict)
    graph_node_ids: tuple[str, ...] = ()
    graph_edge_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view (used by the result envelope)."""
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "date_bucket": self.date_bucket,
            "last_completed_stage": self.last_completed_stage.value,
            "completed_stages": [s.value for s in self.completed_stages],
            "snapshot_ids": [
                {"scorer_type": k[0], "entity_id": k[1], "snapshot_id": v}
                for k, v in self.snapshot_ids.items()
            ],
            "graph_node_ids": list(self.graph_node_ids),
            "graph_edge_ids": list(self.graph_edge_ids),
            "warnings": list(self.warnings),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class StageAttempt:
    """One execution attempt of one stage.

    Attributes:
        stage: The stage that was attempted.
        attempt_number: 1-based attempt count.
        succeeded: True if the stage finished without raising.
        category: :class:`FailureCategory` for the most recent
            failure, or ``None`` on success.
        error_class: The exception class name for the most
            recent failure, or ``""`` on success.
        error_message: The stringified exception, or ``""``.
        duration_seconds: Wall-clock duration of the attempt.
    """

    stage: Stage
    attempt_number: int
    succeeded: bool
    category: FailureCategory | None = None
    error_class: str = ""
    error_message: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "attempt_number": self.attempt_number,
            "succeeded": self.succeeded,
            "category": self.category.value if self.category else None,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "duration_seconds": round(self.duration_seconds, 6),
        }


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome envelope returned by :meth:`RecoveryManager.resume`.

    Attributes:
        run_id: The run_id the resume was for.
        state: The :class:`RunState` observed at the start of
            the resume.
        attempts: Tuple of :class:`StageAttempt`, one per
            stage-execution, in order.
        completed_through: The deepest stage that finished
            successfully (across this resume attempt + the
            observed state). ``Stage.SCORED`` means at least
            the score stage is done; ``Stage.EVIDENCE_TRACED``
            means the run is fully complete.
        resume_succeeded: True iff ``completed_through ==
            Stage.EVIDENCE_TRACED``.
        final_category: :class:`FailureCategory` of the last
            failed attempt, or ``None`` on success.
        result: The :class:`IntelligenceRunResult` returned by
            the underlying orchestrator on the final attempt,
            or ``None`` if the resume failed before reaching
            the orchestrator.
        warnings: Order-stable, de-duplicated warnings
            accumulated during the resume.
        started_at: UTC ISO-8601.
        finished_at: UTC ISO-8601.
        duration_seconds: Wall-clock duration of the resume.
    """

    run_id: str
    state: RunState
    attempts: tuple[StageAttempt, ...]
    completed_through: Stage
    resume_succeeded: bool
    final_category: FailureCategory | None
    result: IntelligenceRunResult | None
    warnings: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state.to_dict(),
            "attempts": [a.to_dict() for a in self.attempts],
            "completed_through": self.completed_through.value,
            "resume_succeeded": self.resume_succeeded,
            "final_category": self.final_category.value if self.final_category else None,
            "result": self.result.to_dict() if self.result is not None else None,
            "warnings": list(self.warnings),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 6),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(ts: datetime | None) -> str:
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _stable_state_hash(state: RunState) -> str:
    """Stable digest of the state envelope.

    Used to detect "the state I just observed matches the state
    I'm about to commit" (a sanity check for replay tests).
    """
    payload = state.to_dict()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# RecoveryManager
# ---------------------------------------------------------------------------


class RecoveryManager:
    """Persistence-aware resume layer for :class:`IntelligencePipeline`.

    The manager is a thin wrapper around the orchestrator. It:

    1. Reads persisted state from the existing
       :class:`ScoreRepository` (and optionally the graph
       store) to determine which stages have already finished.
    2. Re-runs the orchestrator with the same
       :class:`IntelligencePipelineConfig`. The orchestrator is
       idempotent for ``SNAPSHOTTED`` (append-only snapshots)
       and ``GRAPH_WRITTEN`` (deterministic upserts), so
       replaying from a previous step is safe.
    3. Classifies any downstream failure as RETRYABLE or
       TERMINAL and either retries (RETRYABLE + attempts left)
       or surfaces a typed :class:`RecoveryResult`.

    Construction
    ------------
    The minimal form is::

        manager = RecoveryManager(score_repo=score_repo)
        state = manager.observe_state(cfg)
        result = manager.resume(cfg, state=state)

    The orchestrator is optional. If ``pipeline`` is not
    provided, :meth:`resume` will re-create the run by calling
    the orchestrator's :meth:`run` once it is supplied; the
    manager does not own the orchestrator's lifecycle.

    The graph store is optional. When provided, state
    observation also reads graph node/edge counts; when
    missing, the manager assumes the graph is in-memory and
    treats the graph stage as already-complete whenever the
    score snapshots exist.
    """

    def __init__(
        self,
        *,
        score_repo: ScoreRepository,
        pipeline: IntelligencePipeline | None = None,
        graph_store: Any = None,
        config: RecoveryConfig | None = None,
    ) -> None:
        self._score_repo = score_repo
        self._pipeline = pipeline
        self._graph_store = graph_store
        self._config = config or RecoveryConfig()

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #

    def observe_state(
        self, config: IntelligencePipelineConfig,
    ) -> RunState:
        """Build a :class:`RunState` from the existing repositories.

        Reads the score_snapshot table for any rows whose
        ``breakdown_json`` carries the same ``_run_id`` as
        ``config.resolved_run_id()`` (or, when no run_id is
        present, the same ``config_hash`` + ``date_bucket``),
        and the graph store for any nodes whose metadata
        ``run_id`` matches.

        The function is total: missing tables / missing graph
        store produce a state with the matching stage marked
        un-completed and a warning appended.
        """
        run_id = config.resolved_run_id()
        warnings: list[str] = []
        completed: list[Stage] = []
        snapshot_ids: dict[tuple[str, str], int] = {}

        # --- 1. SCORED is always considered complete once we
        # successfully observe the run config. (We have no
        # way to know the scorers ran unless we trust the
        # caller.) We mark it as "scored" iff the run
        # config is well-formed; this is a conservative
        # best-effort, not a hard contract.
        completed.append(Stage.SCORED)

        # --- 2. SNAPSHOTTED — read the score_snapshot table.
        try:
            history = self._score_repo.history(
                since=None,  # type: ignore[arg-type]
                limit=10000,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"score_repo.history failed ({type(exc).__name__}): {exc}"
            )
            history = []

        for row in history:
            row_run_id = ""
            row_cfg_hash = ""
            if isinstance(row.breakdown, dict):
                row_run_id = str(row.breakdown.get("_run_id", "") or "")
                row_cfg_hash = str(row.breakdown.get("config_hash", "") or "")
            # The score_snapshot table stores ``computed_at``
            # (ISO timestamp). Slice off the YYYY-MM-DD
            # prefix for the date-bucket match.
            row_date_bucket = ""
            if row.computed_at:
                row_date_bucket = str(row.computed_at)[:10]
            # Match logic (in priority order):
            # 1. ``_run_id`` matches the requested run_id
            #    AND the requested run_id is non-empty.
            # 2. ``config_hash`` matches AND the row's
            #    ``_run_id`` is missing (so a caller that
            #    didn't thread run_id through the snapshot
            #    can still find its rows by config_hash).
            #    Date_bucket is informational only — Run 2
            #    does not enforce it for row matching
            #    because the orchestrator writes
            #    ``computed_at`` at "now", which may
            #    diverge from the requested date_bucket
            #    (e.g. replay from a different day).
            if (
                run_id
                and row_run_id
                and row_run_id == run_id
            ):
                key = (str(row.scorer), str(row.entity_id))
                snapshot_ids[key] = int(row.snapshot_id or 0)
                continue
            if (
                row_cfg_hash
                and row_cfg_hash == config.config_hash
                and not row_run_id
            ):
                key = (str(row.scorer), str(row.entity_id))
                snapshot_ids[key] = int(row.snapshot_id or 0)

        if snapshot_ids:
            completed.append(Stage.SNAPSHOTTED)
        else:
            warnings.append(
                f"no score snapshots observed for run_id={run_id} "
                f"config_hash={config.config_hash} date_bucket={config.date_bucket}"
            )

        # --- 3. GRAPH_WRITTEN — best-effort read of the
        # graph store. We tag graph nodes/edges with the
        # run_id when present; the GraphWriter is required
        # to do that (Run 1 already does it for score
        # nodes).
        graph_nodes: tuple[str, ...] = ()
        graph_edges: tuple[str, ...] = ()
        if self._graph_store is not None:
            try:
                # Both store implementations support
                # ``query_nodes`` (with optional ``tags``)
                # and ``edges_from``/``edges_to``. The
                # cheapest reliable filter is: ask for all
                # nodes and inspect metadata.
                if hasattr(self._graph_store, "query_nodes"):
                    nodes = self._graph_store.query_nodes()  # type: ignore[attr-defined]
                elif hasattr(self._graph_store, "list_nodes"):
                    nodes = self._graph_store.list_nodes(limit=10000)  # type: ignore[attr-defined]
                else:
                    nodes = []
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"graph_store read failed ({type(exc).__name__}): {exc}"
                )
                nodes = []
            try:
                if hasattr(self._graph_store, "_edges"):
                    # InMemoryGraphStore exposes _edges
                    edges_iter = list(self._graph_store._edges.values())  # type: ignore[attr-defined]
                elif hasattr(self._graph_store, "list_edges"):
                    edges_iter = self._graph_store.list_edges(limit=10000)  # type: ignore[attr-defined]
                else:
                    edges_iter = []
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"graph_store edge read failed ({type(exc).__name__}): {exc}"
                )
                edges_iter = []
            for node in nodes:
                meta = getattr(node, "metadata", {}) or {}
                rid = str(meta.get("run_id", "") or "")
                if rid == run_id:
                    graph_nodes = graph_nodes + (str(node.node_id),)
                elif rid == "" and str(meta.get("config_hash", "") or "") == config.config_hash:
                    # Fallback: if the writer didn't thread
                    # run_id, match by config_hash (the next
                    # strongest identifier).
                    graph_nodes = graph_nodes + (str(node.node_id),)
            for edge in edges_iter:
                meta = getattr(edge, "metadata", {}) or {}
                rid = str(meta.get("run_id", "") or "")
                if rid == run_id:
                    graph_edges = graph_edges + (str(edge.edge_id),)
                elif rid == "" and str(meta.get("config_hash", "") or "") == config.config_hash:
                    graph_edges = graph_edges + (str(edge.edge_id),)
            if graph_nodes:
                completed.append(Stage.GRAPH_WRITTEN)
        else:
            # No graph store: we cannot read graph state
            # from disk, so we leave GRAPH_WRITTEN
            # *unobserved* — the resume re-runs the
            # orchestrator, which is idempotent (graph
            # upserts).
            warnings.append(
                "no graph_store provided; GRAPH_WRITTEN left unobserved"
            )

        # --- 4. EVIDENCE_TRACED is *not* persisted
        # anywhere in Run 1. We can only mark it complete
        # if the run config has a non-default trace hook
        # and the result envelope was previously stored.
        # For Run 2 we conservatively leave it out of
        # completed — the resume will re-run the
        # orchestrator, which is safe.

        # Determine the deepest stage.
        last = Stage.SCORED
        for s in Stage.ordered():
            if s in completed:
                last = s

        return RunState(
            run_id=run_id,
            config_hash=config.config_hash,
            date_bucket=config.date_bucket,
            last_completed_stage=last,
            completed_stages=tuple(completed),
            snapshot_ids=snapshot_ids,
            graph_node_ids=graph_nodes,
            graph_edge_ids=graph_edges,
            warnings=tuple(warnings),
            observed_at=_iso(datetime.now(timezone.utc)),
        )

    def resume(
        self,
        config: IntelligencePipelineConfig,
        *,
        state: RunState | None = None,
    ) -> RecoveryResult:
        """Resume a (possibly interrupted) run.

        Args:
            config: The same :class:`IntelligencePipelineConfig`
                that the original (interrupted) run was using.
                ``run_id`` and ``config_hash`` are the join
                keys for the persisted state.
            state: Optional pre-computed :class:`RunState`. If
                ``None``, :meth:`observe_state` is called.

        Returns:
            A :class:`RecoveryResult` envelope. The caller
            reads ``resume_succeeded`` to decide what to do
            next (publish, alert, escalate). On failure,
            ``final_category`` distinguishes retryable
            :class:`RecoveryResult`.
        """
        if self._pipeline is None:
            raise ValueError(
                "RecoveryManager.resume requires an IntelligencePipeline; "
                "construct the manager with pipeline=..."
            )

        started_at_dt = datetime.now(timezone.utc)
        t0 = _time.perf_counter()
        warnings: list[str] = []

        if state is None:
            state = self.observe_state(config)

        # --- strict-resume guard -------------------------------
        if self._config.strict_resume:
            if state.config_hash != config.config_hash:
                warnings.append(
                    f"strict_resume: state.config_hash={state.config_hash!r} "
                    f"does not match config.config_hash={config.config_hash!r}"
                )

        attempts: list[StageAttempt] = []
        last_completed: Stage = state.last_completed_stage
        result: IntelligenceRunResult | None = None
        final_category: FailureCategory | None = None
        last_exc: BaseException | None = None

        # Determine the first stage to attempt. If
        # start_from is set, override. Otherwise start at
        # the stage *after* the last completed stage.
        start = self._config.start_from or (state.last_completed_stage.next() or Stage.SCORED)
        # Snap "after the last stage" to EVIDENCE_TRACED
        # when everything is already done — the loop
        # short-circuits below.
        if state.last_completed_stage == Stage.EVIDENCE_TRACED:
            start = Stage.EVIDENCE_TRACED  # nothing to do

        for stage in Stage.ordered():
            if stage < start:
                continue
            # If everything up to and including this stage
            # is already complete AND no start_from
            # override was given, skip the run.
            if (
                self._config.start_from is None
                and stage <= state.last_completed_stage
            ):
                continue
            # If the stage is EVIDENCE_TRACED, re-running
            # the orchestrator from SCORED is the only
            # safe way to recompute the traces (they are
            # not persisted). We delegate that to a single
            # orchestrator run.
            break

        # Always re-run the orchestrator for at least one
        # attempt. The orchestrator is idempotent for
        # SNAPSHOTTED and GRAPH_WRITTEN, so the re-run is
        # safe.
        max_attempts = max(1, int(self._config.max_attempts))
        for attempt_no in range(1, max_attempts + 1):
            t_a0 = _time.perf_counter()
            try:
                result = self._pipeline.run(config)
            except IntelligenceRunError as exc:
                last_exc = exc
                cat = classify_failure(exc)
                final_category = cat
                attempts.append(StageAttempt(
                    stage=Stage.EVIDENCE_TRACED,  # run-level
                    attempt_number=attempt_no,
                    succeeded=False,
                    category=cat,
                    error_class=type(exc).__name__,
                    error_message=str(exc),
                    duration_seconds=_time.perf_counter() - t_a0,
                ))
                if cat == FailureCategory.TERMINAL:
                    # No point retrying. Bail out.
                    warnings.append(
                        f"terminal failure on attempt {attempt_no}: {exc}"
                    )
                    break
                if attempt_no < max_attempts:
                    warnings.append(
                        f"retryable failure on attempt {attempt_no} "
                        f"({type(exc).__name__}): {exc}"
                    )
                    if self._config.retry_backoff_seconds > 0:
                        _time.sleep(self._config.retry_backoff_seconds)
                continue
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                cat = classify_failure(exc)
                final_category = cat
                attempts.append(StageAttempt(
                    stage=Stage.SCORED,  # unknown — we assume
                    attempt_number=attempt_no,
                    succeeded=False,
                    category=cat,
                    error_class=type(exc).__name__,
                    error_message=str(exc),
                    duration_seconds=_time.perf_counter() - t_a0,
                ))
                if cat == FailureCategory.TERMINAL:
                    warnings.append(
                        f"terminal failure on attempt {attempt_no}: {exc}"
                    )
                    break
                if attempt_no < max_attempts:
                    warnings.append(
                        f"retryable failure on attempt {attempt_no} "
                        f"({type(exc).__name__}): {exc}"
                    )
                    if self._config.retry_backoff_seconds > 0:
                        _time.sleep(self._config.retry_backoff_seconds)
                continue
            # Success
            attempts.append(StageAttempt(
                stage=Stage.EVIDENCE_TRACED,
                attempt_number=attempt_no,
                succeeded=True,
                duration_seconds=_time.perf_counter() - t_a0,
            ))
            final_category = None
            last_exc = None
            break

        # Determine completed_through.
        if result is not None and final_category is None:
            last_completed = Stage.EVIDENCE_TRACED
        elif state.last_completed_stage != Stage.EVIDENCE_TRACED:
            # We started from state.last_completed_stage.next()
            # — that's the deepest stage we definitely had
            # done.
            last_completed = state.last_completed_stage

        finished_at_dt = datetime.now(timezone.utc)
        duration = _time.perf_counter() - t0
        return RecoveryResult(
            run_id=config.resolved_run_id(),
            state=state,
            attempts=tuple(attempts),
            completed_through=last_completed,
            resume_succeeded=(
                final_category is None and last_completed == Stage.EVIDENCE_TRACED
            ),
            final_category=final_category,
            result=result,
            warnings=tuple(warnings),
            started_at=_iso(started_at_dt),
            finished_at=_iso(finished_at_dt),
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------ #
    # Snapshot / lookup helpers
    # ------------------------------------------------------------------ #

    def find_snapshot(
        self,
        config: IntelligencePipelineConfig,
        *,
        scorer_type: str,
        entity_id: str,
    ) -> int | None:
        """Return the most recent snapshot_id for the given entity.

        Filters by ``config.config_hash`` first, then by
        ``config.date_bucket``. Returns ``None`` if nothing
        matches.
        """
        history = self._score_repo.history(
            scorer=scorer_type,
            entity_id=entity_id,
            limit=1000,
        )
        for row in history:
            if not isinstance(row.breakdown, dict):
                continue
            row_cfg = str(row.breakdown.get("config_hash", "") or "")
            if row_cfg and row_cfg != config.config_hash:
                continue
            if config.date_bucket and row.computed_at:
                row_date_bucket = str(row.computed_at)[:10]
                if row_date_bucket != config.date_bucket:
                    continue
            return int(row.snapshot_id or 0)
        return None

    def has_graph_writes(self, config: IntelligencePipelineConfig) -> bool:
        """Return True iff the graph store carries nodes for this run.

        Returns ``False`` when no graph store was supplied.
        """
        if self._graph_store is None:
            return False
        run_id = config.resolved_run_id()
        nodes = getattr(self._graph_store, "list_nodes", None)
        if nodes is None:
            return False
        try:
            for node in nodes(limit=10000):  # type: ignore[call-arg]
                meta = getattr(node, "metadata", {}) or {}
                if str(meta.get("run_id", "") or "") == run_id:
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def next_stage(
        self, state: RunState,
    ) -> Stage | None:
        """Return the next stage to run for the given state.

        ``None`` means "the run is already complete".
        """
        return state.last_completed_stage.next()


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "Stage",
    "FailureCategory",
    "RecoveryConfig",
    "RunState",
    "RecoveryResult",
    "StageAttempt",
    "RecoveryManager",
    "classify_failure",
]
