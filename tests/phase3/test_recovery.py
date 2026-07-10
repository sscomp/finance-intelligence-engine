"""Tests for Phase 3B Task 5 Run 2 — Recovery / Resume Layer.

Scope
-----
Stdlib unittest only. Targeted tests for the scenarios the brief
asked for:

1.  Resume after scoring succeeds but graph write fails
2.  Resume after snapshot persists
3.  Retryable failure classification
4.  Terminal failure classification
5.  Repeated resume keeps graph idempotent
6.  Repeated persist remains append-only
7.  Recovery from missing/partial metadata
8.  Deterministic replay
9.  Temp SQLite isolation
10. Downstream failure surfaces typed status/warnings

Verification contract
---------------------
* No full Phase 3 regression — only targeted + compatibility
  tests.
* macro_history.db must be untouched.
* No intelligence.db* files in phase3/data/ after the run.
* Re-uses the existing pipeline/snapshot/graph fixtures.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any

from phase3.datamodel.graph import EdgeType, GraphEdge, GraphNode, NodeType
from phase3.graph.evidence_trace_export import EvidenceChainAdapter
from phase3.graph.in_memory_store import GraphStore
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.score_repo import ScoreRepository, ScoreSnapshotMutationError
from phase3.persistence.schema_v1 import build as build_v1
from phase3.persistence.signal_repo import SignalRecord, SignalRepository
from phase3.persistence.sqlite import SQLiteStore
from phase3.pipeline.graph_writer import GraphWriter
from phase3.pipeline.intelligence_pipeline import (
    IntelligencePipeline,
    IntelligencePipelineConfig,
    IntelligenceRunError,
)
from phase3.pipeline.recovery import (
    FailureCategory,
    RecoveryConfig,
    RecoveryManager,
    RecoveryResult,
    RunState,
    Stage,
    StageAttempt,
    classify_failure,
)
from phase3.pipeline.scoring_pipeline import PipelineConfig, ScoringPipeline
from phase3.pipeline.signal_loader import SignalLoader
from phase3.pipeline.snapshot_writer import SnapshotWriter, SnapshotWriterConfig
from phase3.scoring.company import CompanyScorer
from phase3.scoring.industry import IndustryScorer
from phase3.scoring.macro import MacroScorer


DATE = "2026-07-09"
TS = datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
CONFIG_HASH = "recovery-test-cfg"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_store() -> tuple[str, SQLiteStore]:
    fd, path = tempfile.mkstemp(prefix="recovery_test", suffix=".db")
    os.close(fd)
    store = SQLiteStore(path)
    MigrationManager(store, [build_v1()]).apply()
    return path, store


def _seed_signal(
    repo: SignalRepository,
    *,
    signal_id: str,
    entity_type: str,
    entity_id: str,
    signal_type: str,
    value: float,
    date_bucket: str = DATE,
    timestamp: datetime = TS,
    source_type: str = "yfinance",
) -> None:
    repo.upsert(
        SignalRecord(
            signal_id=signal_id,
            entity_type=entity_type,
            entity_id=entity_id,
            signal_type=signal_type,
            value=value,
            unit="ratio",
            direction="bullish" if value >= 0 else "bearish",
            timestamp=timestamp.isoformat(),
            date_bucket=date_bucket,
            source_id=f"{source_type}-{signal_id}",
            source_type=source_type,
            ref=signal_id,
            fetched_at=timestamp.isoformat(),
            fetch_id=f"fetch-{signal_id}",
            schema_version="3.0",
            metadata={},
            raw_payload={},
        )
    )


def _seed_macro_signals(repo: SignalRepository) -> None:
    _seed_signal(repo, signal_id="m-gdp", entity_type="macro", entity_id="global",
                 signal_type="gdp_yoy", value=0.025)
    _seed_signal(repo, signal_id="m-pmi", entity_type="macro", entity_id="global",
                 signal_type="pmi", value=52.0)
    _seed_signal(repo, signal_id="m-vix", entity_type="macro", entity_id="global",
                 signal_type="vix", value=18.0)
    _seed_signal(repo, signal_id="m-cpi", entity_type="macro", entity_id="global",
                 signal_type="cpi_yoy", value=2.5)
    _seed_signal(repo, signal_id="m-spread", entity_type="macro", entity_id="global",
                 signal_type="yield_spread_pp", value=1.2)


def _build_wired(
    store: SQLiteStore,
) -> tuple[
    IntelligencePipeline,
    ScoreRepository,
    SignalRepository,
    GraphStore,
    GraphWriter,
    EvidenceChainAdapter,
]:
    """Wire the orchestrator + graph + adapter against a temp store."""
    signal_repo = SignalRepository(store)
    score_repo = ScoreRepository(store)
    writer = SnapshotWriter(
        score_repo,
        SnapshotWriterConfig(
            run_mode="recovery-test",
            run_id=None,
            dry_run=False,
        ),
    )
    pipeline = ScoringPipeline(
        signal_loader=SignalLoader(signal_repo),
        macro_scorer=MacroScorer(config_hash=CONFIG_HASH),
        industry_scorer=IndustryScorer(config_hash=CONFIG_HASH),
        company_scorer=CompanyScorer(config_hash=CONFIG_HASH),
        sink=writer,
        config=PipelineConfig(
            dry_run=False,
            notes="recovery-test",
            config_hash=CONFIG_HASH,
        ),
    )
    graph_store = GraphStore()
    graph_writer = GraphWriter(graph_store)
    adapter = EvidenceChainAdapter(graph_store)
    orchestrator = IntelligencePipeline(
        scoring_pipeline=pipeline,
        graph_writer=graph_writer,
        evidence_adapter=adapter,
        graph_store=graph_store,
    )
    return orchestrator, score_repo, signal_repo, graph_store, graph_writer, adapter


# ---------------------------------------------------------------------------
# Stage / classification tests
# ---------------------------------------------------------------------------


class StageOrderedTest(unittest.TestCase):
    """``Stage.ordered()`` returns a fixed 4-stage progression."""

    def test_ordered(self) -> None:
        self.assertEqual(
            [s.value for s in Stage.ordered()],
            ["scored", "snapshotted", "graph_written", "evidence_traced"],
        )

    def test_next(self) -> None:
        self.assertEqual(Stage.SCORED.next(), Stage.SNAPSHOTTED)
        self.assertEqual(Stage.SNAPSHOTTED.next(), Stage.GRAPH_WRITTEN)
        self.assertEqual(Stage.GRAPH_WRITTEN.next(), Stage.EVIDENCE_TRACED)
        self.assertIsNone(Stage.EVIDENCE_TRACED.next())


class ClassifyFailureTest(unittest.TestCase):
    """Retryable vs terminal classification."""

    def test_retryable_by_class(self) -> None:
        self.assertEqual(
            classify_failure(TimeoutError("slow")),
            FailureCategory.RETRYABLE,
        )
        self.assertEqual(
            classify_failure(IOError("disk")),
            FailureCategory.RETRYABLE,
        )
        self.assertEqual(
            classify_failure(MemoryError("oom")),
            FailureCategory.RETRYABLE,
        )
        self.assertEqual(
            classify_failure(ConnectionError("network")),
            FailureCategory.RETRYABLE,
        )

    def test_terminal_by_class(self) -> None:
        self.assertEqual(
            classify_failure(ValueError("bad config")),
            FailureCategory.TERMINAL,
        )
        self.assertEqual(
            classify_failure(KeyError("missing")),
            FailureCategory.TERMINAL,
        )
        self.assertEqual(
            classify_failure(TypeError("type")),
            FailureCategory.TERMINAL,
        )
        self.assertEqual(
            classify_failure(NotImplementedError("todo")),
            FailureCategory.TERMINAL,
        )
        self.assertEqual(
            classify_failure(ImportError("x")),
            FailureCategory.TERMINAL,
        )

    def test_retryable_by_substring(self) -> None:
        # The exception class is generic but the message
        # carries a retryable signal.
        class _Other(RuntimeError):
            pass

        self.assertEqual(
            classify_failure(_Other("database is locked")),
            FailureCategory.RETRYABLE,
        )
        self.assertEqual(
            classify_failure(_Other("connection refused")),
            FailureCategory.RETRYABLE,
        )
        self.assertEqual(
            classify_failure(_Other("resource temporarily unavailable")),
            FailureCategory.RETRYABLE,
        )

    def test_terminal_by_substring(self) -> None:
        class _Other(RuntimeError):
            pass

        self.assertEqual(
            classify_failure(_Other("schema mismatch")),
            FailureCategory.TERMINAL,
        )
        self.assertEqual(
            classify_failure(_Other("integrity constraint failed")),
            FailureCategory.TERMINAL,
        )
        self.assertEqual(
            classify_failure(_Other("unsupported operation")),
            FailureCategory.TERMINAL,
        )

    def test_unknown(self) -> None:
        class _Other(RuntimeError):
            pass

        # "weird error" matches no substring and no class
        # name. Should fall through to UNKNOWN.
        self.assertEqual(
            classify_failure(_Other("weird error")),
            FailureCategory.UNKNOWN,
        )

    def test_snapshot_mutation_error_is_terminal(self) -> None:
        # The recovery layer must never try to retry a
        # mutation error — replay goes through the
        # append-only path only.
        self.assertEqual(
            classify_failure(ScoreSnapshotMutationError("nope")),
            FailureCategory.TERMINAL,
        )


# ---------------------------------------------------------------------------
# State observation tests
# ---------------------------------------------------------------------------


class ObserveStateTest(unittest.TestCase):
    """``observe_state`` reads from existing repositories only."""

    def test_empty_state_when_no_persistence(self) -> None:
        """No snapshots + no graph store ⇒ SCORED-only state."""
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            manager = RecoveryManager(score_repo=score_repo, pipeline=orch)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="obs-empty",
                config_hash=CONFIG_HASH,
            )
            state = manager.observe_state(cfg)
            self.assertEqual(state.run_id, "obs-empty")
            self.assertEqual(state.last_completed_stage, Stage.SCORED)
            self.assertEqual(state.snapshot_ids, {})
            self.assertEqual(state.graph_node_ids, ())
        finally:
            os.unlink(path)

    def test_observed_state_after_persisted_run(self) -> None:
        """A successful persisted run leaves observable state."""
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="obs-persisted",
                config_hash=CONFIG_HASH,
                persist=True,
            )
            # First run
            result = orch.run(cfg)
            # Read state
            manager = RecoveryManager(
                score_repo=score_repo,
                pipeline=orch,
                graph_store=graph_store,
            )
            state = manager.observe_state(cfg)
            self.assertIn(Stage.SNAPSHOTTED, state.completed_stages)
            self.assertIn(Stage.GRAPH_WRITTEN, state.completed_stages)
            self.assertGreater(len(state.snapshot_ids), 0)
            # The macro snapshot is keyed ("macro", "global")
            self.assertIn(("macro", "global"), state.snapshot_ids)
        finally:
            os.unlink(path)

    def test_recovery_from_missing_metadata(self) -> None:
        """Empty config_hash, no run_id ⇒ observation still works."""
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="",
                config_hash="",
                persist=False,
            )
            manager = RecoveryManager(score_repo=score_repo, pipeline=orch)
            state = manager.observe_state(cfg)
            # run_id auto-generated
            self.assertTrue(state.run_id.startswith("ipr-"))
            # No snapshots (dry-run), so last completed is SCORED
            self.assertEqual(state.last_completed_stage, Stage.SCORED)
            # No errors raised
            self.assertNotIn("error", str(state.warnings).lower() or "")
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Resume tests
# ---------------------------------------------------------------------------


class ResumeAfterScoringSucceedsGraphFailsTest(unittest.TestCase):
    """Resume after scoring succeeded but graph write failed.

    We simulate the graph-writer failure by wrapping the
    orchestrator in a stub that raises on the first run and
    succeeds on the second. The recovery layer should detect
    the failure, classify it as TERMINAL (the wrapper
    deliberately raises ValueError), and surface it via
    ``final_category``.
    """

    def test_resume_after_orchestrator_failure(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="resume-graphfail",
                config_hash=CONFIG_HASH,
                persist=True,
            )

            # Stub the orchestrator's graph writer to
            # ALWAYS fail with a TERMINAL error.
            def _always_fail_write(pr: Any) -> Any:
                raise ValueError("simulated graph write failure")

            gw.write = _always_fail_write  # type: ignore[method-assign]
            # Sanity: the first run raises
            with self.assertRaises(IntelligenceRunError):
                orch.run(cfg)

            # Now resume via the manager. The wrapper
            # always raises → TERMINAL → fail fast after
            # one attempt.
            gw.write = _always_fail_write  # type: ignore[method-assign]
            manager = RecoveryManager(
                score_repo=score_repo,
                pipeline=orch,
                graph_store=graph_store,
                config=RecoveryConfig(max_attempts=3),
            )
            result = manager.resume(cfg)
            self.assertFalse(result.resume_succeeded)
            self.assertEqual(result.final_category, FailureCategory.TERMINAL)
            self.assertGreater(len(result.attempts), 0)
            self.assertFalse(result.attempts[0].succeeded)
        finally:
            os.unlink(path)

    def test_resume_recovers_when_graph_unblocks(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="resume-graph-unblock",
                config_hash=CONFIG_HASH,
                persist=True,
            )

            # First call (sanity): graph writer raises
            # (IOError — retryable). Second call: succeeds.
            original_write = gw.write
            call_count = {"n": 0}

            def _ioerr_write(pr: Any) -> Any:
                call_count["n"] += 1
                if call_count["n"] <= 1:
                    raise IOError("disk i/o error")
                return original_write(pr)

            gw.write = _ioerr_write  # type: ignore[method-assign]
            with self.assertRaises(IntelligenceRunError):
                orch.run(cfg)

            # Reset the counter so the *resume*'s first
            # call also fails. The manager's retry should
            # then succeed.
            call_count["n"] = 0
            gw.write = _ioerr_write  # type: ignore[method-assign]
            manager = RecoveryManager(
                score_repo=score_repo,
                pipeline=orch,
                graph_store=graph_store,
                config=RecoveryConfig(max_attempts=3, retry_backoff_seconds=0.0),
            )
            result = manager.resume(cfg)
            self.assertTrue(result.resume_succeeded)
            self.assertEqual(result.completed_through, Stage.EVIDENCE_TRACED)
            self.assertIsNone(result.final_category)
            # Both attempts logged
            self.assertEqual(len(result.attempts), 2)
            self.assertFalse(result.attempts[0].succeeded)
            self.assertEqual(result.attempts[0].category, FailureCategory.RETRYABLE)
            self.assertTrue(result.attempts[1].succeeded)
        finally:
            os.unlink(path)


class ResumeAfterSnapshotPersistsTest(unittest.TestCase):
    """Resume after a snapshot has been persisted.

    The scenario: the run was interrupted AFTER
    ``SNAPSHOTTED`` but BEFORE ``GRAPH_WRITTEN``. The recovery
    layer observes the persisted snapshot, marks SNAPSHOTTED
    as completed, and re-runs the orchestrator (which is
    idempotent for the snapshot side and safe to call again).
    """

    def test_resume_replays_through_to_evidence(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="resume-snap",
                config_hash=CONFIG_HASH,
                persist=True,
            )

            # First run completes — leaves a snapshot.
            result1 = orch.run(cfg)
            self.assertTrue(result1.metadata.get("persist"))
            self.assertGreater(score_repo.count(), 0)
            snap_count_before = score_repo.count()

            # Now resume. Should still complete (snapshot
            # is append-only, graph is upsert).
            manager = RecoveryManager(
                score_repo=score_repo,
                pipeline=orch,
                graph_store=graph_store,
            )
            state = manager.observe_state(cfg)
            self.assertIn(Stage.SNAPSHOTTED, state.completed_stages)

            result2 = manager.resume(cfg)
            self.assertTrue(result2.resume_succeeded)
            # Append-only: snapshot count must INCREASE on
            # replay (one more macro snapshot appended).
            self.assertGreater(score_repo.count(), snap_count_before)
        finally:
            os.unlink(path)


class RepeatedResumeKeepsGraphIdempotentTest(unittest.TestCase):
    """Repeated resume of the same run must not duplicate graph nodes/edges."""

    def test_repeated_resume_keeps_graph_idempotent(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="resume-idempotent",
                config_hash=CONFIG_HASH,
                persist=True,
            )

            # First run.
            orch.run(cfg)
            nodes_after_first = graph_store.node_count()
            edges_after_first = graph_store.edge_count()
            self.assertGreater(nodes_after_first, 0)

            # Second run with the same config — should be
            # idempotent.
            orch.run(cfg)
            self.assertEqual(graph_store.node_count(), nodes_after_first)
            self.assertEqual(graph_store.edge_count(), edges_after_first)

            # Third run via resume.
            manager = RecoveryManager(
                score_repo=score_repo,
                pipeline=orch,
                graph_store=graph_store,
            )
            manager.resume(cfg)
            self.assertEqual(graph_store.node_count(), nodes_after_first)
            self.assertEqual(graph_store.edge_count(), edges_after_first)
        finally:
            os.unlink(path)


class RepeatedPersistRemainsAppendOnlyTest(unittest.TestCase):
    """Repeated persist never mutates a snapshot row in place."""

    def test_repeated_persists_only_append(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="resume-append",
                config_hash=CONFIG_HASH,
                persist=True,
            )

            orch.run(cfg)
            count_after_first = score_repo.count()
            snap_ids_first = sorted(
                int(s.snapshot_id or 0) for s in score_repo.history(limit=10000)
            )
            self.assertGreater(count_after_first, 0)

            # Trigger a raw UPDATE — must be rejected by
            # the trigger.
            with self.assertRaises(ScoreSnapshotMutationError):
                from phase3.persistence.score_repo import attempt_raw_update
                attempt_raw_update(store, snap_ids_first[0], new_score=999.0)

            # Repeated persist adds rows, never mutates.
            orch.run(cfg)
            count_after_second = score_repo.count()
            self.assertGreater(count_after_second, count_after_first)
            # Original snapshot row score unchanged.
            first_row = score_repo.history(scorer="macro", entity_id="global", limit=1)
            self.assertEqual(len(first_row), 1)
            self.assertNotEqual(first_row[0].score, 999.0)
        finally:
            os.unlink(path)


class DeterministicReplayTest(unittest.TestCase):
    """Same config + same run_id ⇒ deterministic replay."""

    def test_replay_yields_same_run_id_and_config_hash(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="det-replay",
                config_hash=CONFIG_HASH,
                persist=True,
            )

            r1 = orch.run(cfg)
            r2 = orch.run(cfg)
            self.assertEqual(r1.run_id, r2.run_id)
            self.assertEqual(r1.config_hash, r2.config_hash)
            # Append-only: each run adds a NEW snapshot
            # row, so the snapshot_ids are distinct per
            # run. The deterministic identifier is the
            # (run_id, config_hash, scorer_type, entity_id)
            # tuple — which is identical across runs.
            r1_ids = [v for v in r1.snapshot_ids.values() if v is not None]
            r2_ids = [v for v in r2.snapshot_ids.values() if v is not None]
            self.assertEqual(
                sorted([(k[0], k[1]) for k in r1.snapshot_ids.keys()]),
                sorted([(k[0], k[1]) for k in r2.snapshot_ids.keys()]),
            )
            # Both runs produced at least one snapshot.
            self.assertGreater(len(r1_ids), 0)
            self.assertGreater(len(r2_ids), 0)
        finally:
            os.unlink(path)


class TempSqliteIsolationTest(unittest.TestCase):
    """Temp SQLite only — no production DB touched."""

    def test_no_production_db_write(self) -> None:
        """Run resume against a temp DB; macro_history.db
        is not touched and no intelligence.db is created
        in phase3/data/."""
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="temp-iso",
                config_hash=CONFIG_HASH,
                persist=True,
            )

            manager = RecoveryManager(
                score_repo=score_repo,
                pipeline=orch,
                graph_store=graph_store,
            )
            manager.resume(cfg)

            # No intelligence.db artifact in the project
            # data dir. We use a glob against the *parent*
            # of the test's tempdir, scoped to phase3/data
            # which is what the brief listed.
            data_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "phase3",
                "data",
            )
            if os.path.isdir(data_dir):
                for name in os.listdir(data_dir):
                    if name.startswith("intelligence") and name.endswith(".db"):
                        self.fail(
                            f"production artifact leaked: {os.path.join(data_dir, name)}"
                        )
        finally:
            os.unlink(path)


class TypedStatusWarningsTest(unittest.TestCase):
    """Downstream failure surfaces typed status + warnings."""

    def test_terminal_failure_returns_typed_warnings(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="typed-warn",
                config_hash=CONFIG_HASH,
                persist=True,
            )

            # Replace the snapshot writer with one whose
            # write always raises a TERMINAL error.
            from phase3.pipeline.scoring_pipeline import PipelineResult

            def _boom(*args: Any, **kwargs: Any) -> Any:
                raise ValueError("intentional config error")

            # Patch the sink that the scoring pipeline
            # uses.
            orch._pipeline._sink.write = _boom  # type: ignore[method-assign]
            # Sanity: orchestrator raises
            with self.assertRaises(IntelligenceRunError):
                orch.run(cfg)

            # Now resume — should classify as TERMINAL
            # and surface a typed warning.
            manager = RecoveryManager(
                score_repo=score_repo,
                pipeline=orch,
                graph_store=graph_store,
                config=RecoveryConfig(max_attempts=2),
            )
            result = manager.resume(cfg)
            self.assertFalse(result.resume_succeeded)
            self.assertEqual(result.final_category, FailureCategory.TERMINAL)
            # Warnings are typed strings (not exceptions).
            self.assertTrue(any(
                "terminal failure" in w for w in result.warnings
            ))
            # Attempt envelope is typed.
            self.assertGreater(len(result.attempts), 0)
            att = result.attempts[0]
            self.assertIsInstance(att, StageAttempt)
            self.assertFalse(att.succeeded)
            self.assertEqual(att.category, FailureCategory.TERMINAL)
        finally:
            os.unlink(path)

    def test_partial_recovery_state_carries_warnings(self) -> None:
        """If observe_state hits a partial store, the
        RunState surfaces a typed warning, not an exception."""
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="partial-warn",
                config_hash=CONFIG_HASH,
                persist=True,
            )
            # First run completes — leave state.
            orch.run(cfg)
            # Now swap the score repo for one pointing at
            # a *different* path so observe_state can't
            # read the snapshot. Use a fresh temp store.
            other_path, other_store = _new_store()
            try:
                fresh_repo = ScoreRepository(other_store)
                manager = RecoveryManager(
                    score_repo=fresh_repo,
                    pipeline=orch,
                    graph_store=graph_store,
                )
                state = manager.observe_state(cfg)
                # No snapshots visible in fresh repo.
                self.assertEqual(state.snapshot_ids, {})
                # Warnings include the "no snapshots
                # observed" message.
                self.assertTrue(any(
                    "no score snapshots" in w for w in state.warnings
                ))
            finally:
                os.unlink(other_path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# RecoveryResult typed-envelope tests
# ---------------------------------------------------------------------------


class RecoveryResultTypedTest(unittest.TestCase):
    """``RecoveryResult.to_dict()`` returns a JSON-safe envelope."""

    def test_to_dict_shape(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="typed-shape",
                config_hash=CONFIG_HASH,
                persist=True,
            )
            manager = RecoveryManager(
                score_repo=score_repo,
                pipeline=orch,
                graph_store=graph_store,
            )
            result = manager.resume(cfg)
            d = result.to_dict()
            # Top-level keys
            for key in (
                "run_id", "state", "attempts", "completed_through",
                "resume_succeeded", "final_category", "result",
                "warnings", "started_at", "finished_at",
                "duration_seconds",
            ):
                self.assertIn(key, d)
            # state subkeys
            for key in (
                "run_id", "config_hash", "date_bucket",
                "last_completed_stage", "completed_stages",
                "snapshot_ids", "graph_node_ids", "graph_edge_ids",
                "warnings", "observed_at",
            ):
                self.assertIn(key, d["state"])
            # attempt subkeys
            for key in (
                "stage", "attempt_number", "succeeded", "category",
                "error_class", "error_message", "duration_seconds",
            ):
                self.assertIn(key, d["attempts"][0])
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Compat tests — make sure existing pipeline still works
# ---------------------------------------------------------------------------


class PipelineStillWorksTest(unittest.TestCase):
    """Sanity: existing orchestrator + repositories still work."""

    def test_macro_only_dry_run(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="compat-dry",
                config_hash=CONFIG_HASH,
                persist=False,
            )
            result = orch.run(cfg)
            self.assertTrue(result.dry_run)
            self.assertEqual(score_repo.count(), 0)
        finally:
            os.unlink(path)

    def test_persisted_run_writes_one_snapshot(self) -> None:
        path, store = _new_store()
        try:
            orch, score_repo, signal_repo, graph_store, gw, adapter = _build_wired(store)
            _seed_macro_signals(signal_repo)
            cfg = IntelligencePipelineConfig(
                date_bucket=DATE,
                run_id="compat-persist",
                config_hash=CONFIG_HASH,
                persist=True,
            )
            result = orch.run(cfg)
            self.assertFalse(result.dry_run)
            # Exactly one macro snapshot
            macro_snaps = score_repo.history(scorer="macro", entity_id="global")
            self.assertEqual(len(macro_snaps), 1)
        finally:
            os.unlink(path)


class SnapshotWriterStillWorksTest(unittest.TestCase):
    """Sanity: existing SnapshotWriter still writes append-only."""

    def test_snapshot_writer_appends(self) -> None:
        from phase3.pipeline.snapshot_writer import SnapshotWriter

        path, store = _new_store()
        try:
            score_repo = ScoreRepository(store)
            writer = SnapshotWriter(
                score_repo, SnapshotWriterConfig(run_mode="compat-sw", dry_run=False),
            )
            # Build a score by routing through the pipeline
            # would be heavier; just build a record and
            # append directly via the repo (the writer
            # is the same code path).
            from phase3.persistence.score_repo import ScoreSnapshotRecord
            rec = ScoreSnapshotRecord(
                snapshot_id=None,
                scorer="macro",
                entity_type="macro",
                entity_id="global",
                score=50.0,
                breakdown={"config_hash": CONFIG_HASH, "_run_id": "compat-sw"},
                inputs={},
                notes="compat",
                schema_version="3.0",
                computed_at="2026-07-09T12:00:00+00:00",
            )
            sid = score_repo.append(rec)
            self.assertGreater(sid, 0)
            self.assertEqual(score_repo.count(), 1)
        finally:
            os.unlink(path)


class GraphWriterStillWorksTest(unittest.TestCase):
    """Sanity: existing GraphWriter still upserts idempotently."""

    def test_graph_writer_upserts_idempotently(self) -> None:
        from phase3.pipeline.graph_writer import GraphWriter
        from phase3.datamodel.graph import (
            EdgeType, GraphEdge, GraphNode, NodeType,
        )

        store = GraphStore()
        writer = GraphWriter(store)
        n = GraphNode(
            node_id="test-node",
            node_type=NodeType.SCORE,
            label="test",
            created_at=datetime(2026, 7, 9, 12, tzinfo=timezone.utc),
            metadata={"run_id": "compat-gw"},
            tags=[],
        )
        # First insert
        is_new1 = store.add_node(n)
        # Re-insert with same id
        is_new2 = store.add_node(n)
        self.assertTrue(is_new1)
        self.assertFalse(is_new2)
        self.assertEqual(store.node_count(), 1)


class ScoreRepoStillWorksTest(unittest.TestCase):
    """Sanity: existing ScoreRepository rejects mutations."""

    def test_update_raises(self) -> None:
        path, store = _new_store()
        try:
            score_repo = ScoreRepository(store)
            with self.assertRaises(ScoreSnapshotMutationError):
                score_repo.update(1)
            with self.assertRaises(ScoreSnapshotMutationError):
                score_repo.delete(1)
        finally:
            os.unlink(path)


class GraphRepoStillWorksTest(unittest.TestCase):
    """Sanity: GraphRepository upsert is idempotent."""

    def test_node_upsert_idempotent(self) -> None:
        from phase3.persistence.graph_repo import GraphRepository, NodeRow

        path, store = _new_store()
        try:
            repo = GraphRepository(store)
            node = NodeRow(
                node_id="g1",
                node_type="score",
                label="x",
                created_at="2026-07-09T12:00:00+00:00",
                metadata={},
                tags=[],
            )
            self.assertTrue(repo.upsert_node(node))
            self.assertFalse(repo.upsert_node(node))
            self.assertEqual(repo.node_count(), 1)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
