"""Tests for Phase 3B Task 5 Run 1 — IntelligencePipeline orchestrator.

Scope is intentionally tight: stdlib unittest only, and the test
matrix mirrors the brief's required scenarios:

1.  Macro-only dry-run
2.  Industry-only dry-run
3.  Company-only dry-run
4.  Persisted run with temp SQLite: score snapshot + graph nodes/edges
5.  Full chain macro -> industry -> company
6.  Evidence trace/query handle available after persisted run
7.  Repeated deterministic dry-run
8.  Repeated persist remains append-only/idempotent (re-uses existing
    contract on the snapshot and graph sides)
9.  Missing signal path returns warnings but does not crash
10. Failure propagation from a downstream component is explicit and
    typed (IntelligenceRunError carries partial + component)

Verification contract
---------------------
* No full Phase 3 regression — only targeted + compatibility tests.
* macro_history.db must be untouched.
* No intelligence.db* files in phase3/data/ after the run.
* Re-uses the existing pipeline/snapshot/graph fixtures (no duplicate
  logic).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any

from phase3.datamodel.graph import EdgeType, NodeType
from phase3.datamodel.scores import (
    DimensionResult,
    ScoreBreakdown,
    WeightedFactor,
)
from phase3.datamodel.scores_macro import (
    MACRO_ENTITY_ID,
    MACRO_ENTITY_TYPE,
    MACRO_SCORER_TYPE,
)
from phase3.graph.evidence_trace_export import EvidenceChainAdapter
from phase3.graph.in_memory_store import GraphStore
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.score_repo import ScoreRepository
from phase3.persistence.schema_v1 import build as build_v1
from phase3.persistence.signal_repo import SignalRecord, SignalRepository
from phase3.persistence.sqlite import SQLiteStore
from phase3.pipeline.graph_writer import GraphWriter
from phase3.pipeline.intelligence_pipeline import (
    EvidenceQueryHandle,
    IntelligencePipeline,
    IntelligencePipelineConfig,
    IntelligenceRunError,
    IntelligenceRunResult,
)
from phase3.pipeline.scoring_pipeline import (
    PipelineConfig,
    ScoringPipeline,
)
from phase3.pipeline.signal_loader import SignalLoader
from phase3.pipeline.snapshot_writer import SnapshotWriter, SnapshotWriterConfig
from phase3.scoring.company import CompanyScorer
from phase3.scoring.industry import IndustryScorer
from phase3.scoring.macro import MacroScorer


DATE = "2026-07-09"
TS = datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
CONFIG_HASH = "e2e-test-cfg"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_store() -> tuple[str, SQLiteStore]:
    fd, path = tempfile.mkstemp(prefix="intelligence_e2e_test", suffix=".db")
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


def _seed_industry_signals(
    repo: SignalRepository, industry_id: str = "AI",
) -> None:
    _seed_signal(repo, signal_id="i-relperf5", entity_type="industry", entity_id=industry_id,
                 signal_type="sector_relative_perf_5d", value=0.03)
    _seed_signal(repo, signal_id="i-rs", entity_type="industry", entity_id=industry_id,
                 signal_type="rs_rating", value=80.0)
    _seed_signal(repo, signal_id="i-bb", entity_type="industry", entity_id=industry_id,
                 signal_type="book_to_bill", value=1.1)
    _seed_signal(repo, signal_id="i-news", entity_type="industry", entity_id=industry_id,
                 signal_type="news_headline", value=1.0, source_type="rss")
    _seed_signal(repo, signal_id="i-foreign", entity_type="industry", entity_id=industry_id,
                 signal_type="foreign_net", value=2.0, source_type="t86")


def _seed_company_signals(
    repo: SignalRepository, code: str = "2330",
) -> None:
    _seed_signal(repo, signal_id="c-roe", entity_type="company", entity_id=code,
                 signal_type="roe", value=0.18)
    _seed_signal(repo, signal_id="c-peg", entity_type="company", entity_id=code,
                 signal_type="pe_ratio", value=22.0)
    _seed_signal(repo, signal_id="c-pm1m", entity_type="company", entity_id=code,
                 signal_type="price_momentum_1m", value=0.04)
    _seed_signal(repo, signal_id="c-beta", entity_type="company", entity_id=code,
                 signal_type="beta", value=1.0)
    _seed_signal(repo, signal_id="c-sent", entity_type="company", entity_id=code,
                 signal_type="sentiment", value=0.3, source_type="rss")
    _seed_signal(repo, signal_id="c-news", entity_type="company", entity_id=code,
                 signal_type="news_headline", value=1.0, source_type="rss")


def _build_scoring_pipeline(
    store: SQLiteStore,
    *args: Any,
    **kwargs: Any,
) -> ScoringPipeline:
    signal_repo = SignalRepository(store)
    score_repo = ScoreRepository(store)
    writer = SnapshotWriter(
        score_repo,
        SnapshotWriterConfig(
            run_mode=kwargs.pop("run_mode", "live"),
            run_id=kwargs.pop("run_id", None),
            dry_run=kwargs.pop("dry_run", False),
        ),
    )
    pipeline = ScoringPipeline(
        signal_loader=SignalLoader(signal_repo),
        macro_scorer=MacroScorer(config_hash=CONFIG_HASH),
        industry_scorer=IndustryScorer(config_hash=CONFIG_HASH),
        company_scorer=CompanyScorer(config_hash=CONFIG_HASH),
        sink=writer,
        config=PipelineConfig(
            dry_run=kwargs.pop("pipeline_dry_run", False),
            notes=kwargs.pop("notes", "e2e-test"),
            config_hash=CONFIG_HASH,
        ),
    )
    return pipeline


def _build_intelligence_pipeline(
    store: SQLiteStore,
    *,
    graph_store: GraphStore | None = None,
    scoring_kwargs: dict | None = None,
) -> tuple[IntelligencePipeline, ScoreRepository, GraphStore]:
    """Wire the orchestrator against a temp store + in-memory graph.

    Returns the pipeline + the score repo (for snapshot count assertions)
    + the in-memory graph store (for node/edge assertions).
    """
    scoring_kwargs = dict(scoring_kwargs or {})
    score_repo = ScoreRepository(store)
    signal_repo = SignalRepository(store)
    writer = SnapshotWriter(
        score_repo,
        SnapshotWriterConfig(
            run_mode=scoring_kwargs.get("run_mode", "live"),
            run_id=scoring_kwargs.get("run_id"),
            dry_run=scoring_kwargs.get("dry_run", False),
        ),
    )
    pipeline = ScoringPipeline(
        signal_loader=SignalLoader(signal_repo),
        macro_scorer=MacroScorer(config_hash=CONFIG_HASH),
        industry_scorer=IndustryScorer(config_hash=CONFIG_HASH),
        company_scorer=CompanyScorer(config_hash=CONFIG_HASH),
        sink=writer,
        config=PipelineConfig(
            dry_run=scoring_kwargs.get("dry_run", False),
            notes=scoring_kwargs.get("notes", "e2e-test"),
            config_hash=CONFIG_HASH,
        ),
    )
    if graph_store is None:
        graph_store = GraphStore()
    graph_writer = GraphWriter(graph_store)
    adapter = EvidenceChainAdapter(graph_store)
    orchestrator = IntelligencePipeline(
        scoring_pipeline=pipeline,
        graph_writer=graph_writer,
        evidence_adapter=adapter,
        graph_store=graph_store,
    )
    return orchestrator, score_repo, graph_store


# ---------------------------------------------------------------------------
# Test classes — one per scenario the brief asked for
# ---------------------------------------------------------------------------


class MacroOnlyDryRunTests(unittest.TestCase):
    """Scenario 1: Macro-only dry-run.

    No signals are required to flow through any other leg; the
    orchestrator must succeed, return a result with one PipelineResult
    for macro, and produce zero writes.
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        self.orchestrator, self.score_repo, self.graph_store = (
            _build_intelligence_pipeline(
                self.store, scoring_kwargs={"dry_run": True},
            )
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_macro_only_dry_run(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            industry_ids=(),
            company_specs=(),
            run_macro=True,
            persist=False,
            config_hash=CONFIG_HASH,
        )
        result = self.orchestrator.run(cfg)

        self.assertIsInstance(result, IntelligenceRunResult)
        self.assertTrue(result.dry_run)
        self.assertFalse(result.persist)
        # One macro result, no industries, no companies
        self.assertIsNotNone(result.pipeline_result.macro)
        self.assertEqual(result.pipeline_result.industries, ())
        self.assertEqual(result.pipeline_result.companies, ())
        # Dry-run ⇒ snapshot_id for the macro leg is None
        snap = result.snapshot_ids.get((MACRO_SCORER_TYPE, MACRO_ENTITY_ID))
        self.assertIsNone(snap)
        # No rows persisted on the score repo
        self.assertEqual(self.score_repo.count(), 0)
        # No errors
        self.assertEqual(result.errors, ())
        # Graph write for macro happened
        self.assertEqual(len(result.graph_writes), 1)
        self.assertTrue(result.graph_writes[0].score_node_id)
        # Evidence handle exists
        self.assertEqual(len(result.evidence_handles), 1)


class IndustryOnlyDryRunTests(unittest.TestCase):
    """Scenario 2: Industry-only dry-run.

    Macro leg is skipped (``run_macro=False``) and the orchestrator
    must produce a single industry result without a macro context.
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_industry_signals(self.signal_repo, industry_id="AI")
        self.orchestrator, self.score_repo, self.graph_store = (
            _build_intelligence_pipeline(
                self.store, scoring_kwargs={"dry_run": True},
            )
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_industry_only_dry_run(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            industry_ids=("AI",),
            company_specs=(),
            run_macro=False,
            persist=False,
            config_hash=CONFIG_HASH,
        )
        result = self.orchestrator.run(cfg)

        self.assertTrue(result.dry_run)
        self.assertIsNone(result.pipeline_result.macro)
        self.assertEqual(len(result.pipeline_result.industries), 1)
        self.assertEqual(result.pipeline_result.industries[0].input_bundle.entity_id, "AI")
        self.assertEqual(result.pipeline_result.companies, ())
        # Dry-run ⇒ no score repo writes
        self.assertEqual(self.score_repo.count(), 0)
        self.assertEqual(result.errors, ())


class CompanyOnlyDryRunTests(unittest.TestCase):
    """Scenario 3: Company-only dry-run."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_company_signals(self.signal_repo, code="2330")
        self.orchestrator, self.score_repo, self.graph_store = (
            _build_intelligence_pipeline(
                self.store, scoring_kwargs={"dry_run": True},
            )
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_company_only_dry_run(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            industry_ids=(),
            company_specs=({"code": "2330", "name": "TSMC", "sector": "Technology"},),
            run_macro=False,
            persist=False,
            config_hash=CONFIG_HASH,
        )
        result = self.orchestrator.run(cfg)
        self.assertTrue(result.dry_run)
        self.assertIsNone(result.pipeline_result.macro)
        self.assertEqual(result.pipeline_result.industries, ())
        self.assertEqual(len(result.pipeline_result.companies), 1)
        self.assertEqual(
            result.pipeline_result.companies[0].input_bundle.entity_id, "2330",
        )
        self.assertEqual(self.score_repo.count(), 0)
        self.assertEqual(result.errors, ())


class PersistedRunTests(unittest.TestCase):
    """Scenario 4: Persisted run with temp SQLite.

    The run must produce a real snapshot row in ``score_snapshot``
    AND a non-empty graph in the in-memory store (we use the in-memory
    store so the test is hermetic; the SQLiteGraphStore is exercised
    by its own parity tests).
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        self.orchestrator, self.score_repo, self.graph_store = (
            _build_intelligence_pipeline(self.store)  # default: persist
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_persisted_run_writes_snapshot_and_graph(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            industry_ids=(),
            company_specs=(),
            run_macro=True,
            persist=True,
            config_hash=CONFIG_HASH,
        )
        result = self.orchestrator.run(cfg)

        # Score snapshot was written
        self.assertEqual(self.score_repo.count(), 1)
        macro_snap = result.snapshot_ids.get(
            (MACRO_SCORER_TYPE, MACRO_ENTITY_ID)
        )
        self.assertIsNotNone(macro_snap)
        # Graph is populated
        self.assertGreaterEqual(result.node_count, 3)  # 1 entity + 1 score + ≥1 signal
        self.assertGreaterEqual(result.edge_count, 1)
        # Graph store has the score node we wrote
        score_node = self.graph_store.get_node(result.graph_writes[0].score_node_id)
        self.assertIsNotNone(score_node)
        assert score_node is not None  # for type-checker
        self.assertEqual(score_node.node_type, NodeType.SCORE)
        # Macro result's snapshot_id matches the persisted row
        rec = self.score_repo.latest(MACRO_SCORER_TYPE, MACRO_ENTITY_TYPE, MACRO_ENTITY_ID)
        self.assertIsNotNone(rec)
        assert rec is not None  # for type-checker
        self.assertEqual(rec.snapshot_id, macro_snap)
        # Errors empty
        self.assertEqual(result.errors, ())


class FullChainTests(unittest.TestCase):
    """Scenario 5: Full chain macro -> industry -> company.

    Three legs run; the result envelope covers all three.
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        _seed_industry_signals(self.signal_repo, industry_id="AI")
        _seed_company_signals(self.signal_repo, code="2330")
        self.orchestrator, self.score_repo, self.graph_store = (
            _build_intelligence_pipeline(self.store)
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_full_chain_persisted(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            industry_ids=("AI",),
            company_specs=({
                "code": "2330", "name": "TSMC",
                "sector": "Technology",
                "industry_id_for_adjustment": "AI",
            },),
            run_macro=True,
            persist=True,
            config_hash=CONFIG_HASH,
        )
        result = self.orchestrator.run(cfg)

        # All three legs present
        self.assertIsNotNone(result.pipeline_result.macro)
        self.assertEqual(len(result.pipeline_result.industries), 1)
        self.assertEqual(len(result.pipeline_result.companies), 1)
        # Three snapshots in the score repo
        self.assertEqual(self.score_repo.count(), 3)
        # Three graph writes
        self.assertEqual(len(result.graph_writes), 3)
        # Three evidence handles
        self.assertEqual(len(result.evidence_handles), 3)
        # No errors
        self.assertEqual(result.errors, ())
        # Metadata captures the run
        self.assertEqual(result.metadata["date_bucket"], DATE)
        self.assertIn("orchestrator_id", result.metadata)


class EvidenceHandleTests(unittest.TestCase):
    """Scenario 6: Evidence trace/query handle available after persisted run."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        self.orchestrator, self.score_repo, self.graph_store = (
            _build_intelligence_pipeline(self.store)
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_evidence_handle_contains_summary(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            run_macro=True,
            persist=True,
            config_hash=CONFIG_HASH,
        )
        result = self.orchestrator.run(cfg)

        # One handle for the macro leg
        self.assertEqual(len(result.evidence_handles), 1)
        handle = result.evidence_handles[0]
        self.assertIsInstance(handle, EvidenceQueryHandle)
        # Score node id matches the graph write
        self.assertEqual(handle.score_node_id, result.graph_writes[0].score_node_id)
        # Evidence summary is populated (even when zero-visited)
        self.assertIsNotNone(handle.evidence_summary)
        # The adapter was invoked, so upstream is present
        self.assertIsNotNone(handle.upstream)


class DeterministicDryRunTests(unittest.TestCase):
    """Scenario 7: Repeated deterministic dry-run.

    Two dry-run calls with identical config and identical input data
    must produce the same number of nodes, the same number of edges,
    and the same graph write summaries. The actual node_ids are
    content-derived so they're stable across runs.
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        self.orchestrator1, _, self.graph_store1 = _build_intelligence_pipeline(
            self.store, scoring_kwargs={"dry_run": True},
        )
        # Build a second orchestrator against the SAME store so the
        # signal rows persist between runs.
        self.orchestrator2, _, self.graph_store2 = _build_intelligence_pipeline(
            self.store, scoring_kwargs={"dry_run": True},
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_two_dry_runs_have_same_graph_writes(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            run_macro=True,
            persist=False,
            config_hash=CONFIG_HASH,
        )
        r1 = self.orchestrator1.run(cfg)
        r2 = self.orchestrator2.run(cfg)

        # Same number of writes
        self.assertEqual(len(r1.graph_writes), len(r2.graph_writes))
        # Same score_node_ids (deterministic)
        ids1 = [g.score_node_id for g in r1.graph_writes]
        ids2 = [g.score_node_id for g in r2.graph_writes]
        self.assertEqual(ids1, ids2)
        # Same evidence_signal_ids (deterministic)
        ev1 = sorted(r1.graph_writes[0].evidence_signal_ids)
        ev2 = sorted(r2.graph_writes[0].evidence_signal_ids)
        self.assertEqual(ev1, ev2)


class AppendOnlyPersistTests(unittest.TestCase):
    """Scenario 8: Repeated persist remains append-only/idempotent.

    The snapshot contract is append-only (enforced by the repo's
    triggers). The graph contract is upsert-only. The orchestrator
    must respect both on a second persist run over the same input.
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        self.orchestrator, self.score_repo, self.graph_store = (
            _build_intelligence_pipeline(self.store)
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_repeated_persist_appends_score_and_idempotent_graph(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            run_macro=True,
            persist=True,
            config_hash=CONFIG_HASH,
        )
        r1 = self.orchestrator.run(cfg)
        r2 = self.orchestrator.run(cfg)

        # Two snapshot rows (append-only, distinct ids)
        self.assertEqual(self.score_repo.count(), 2)
        ids1 = r1.snapshot_ids[(MACRO_SCORER_TYPE, MACRO_ENTITY_ID)]
        ids2 = r2.snapshot_ids[(MACRO_SCORER_TYPE, MACRO_ENTITY_ID)]
        self.assertIsNotNone(ids1)
        self.assertIsNotNone(ids2)
        self.assertNotEqual(ids1, ids2)
        # Graph node count did not double — upsert is idempotent.
        # We compare r1.node_count to r2.node_count (each returns
        # the live in-memory store total after its own run).
        self.assertEqual(r1.node_count, r2.node_count)
        # Edge count also idempotent.
        self.assertEqual(r1.edge_count, r2.edge_count)


class MissingSignalPathTests(unittest.TestCase):
    """Scenario 9: Missing signal path returns warnings but does not crash.

    The run is configured for an industry that has no signals seeded;
    the orchestrator must complete (with a missing-signal warning),
    not raise.
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        # Macro is seeded; industry is NOT
        _seed_macro_signals(self.signal_repo)
        self.orchestrator, self.score_repo, self.graph_store = (
            _build_intelligence_pipeline(
                self.store, scoring_kwargs={"dry_run": True},
            )
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_missing_signal_path_warns_not_crashes(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            industry_ids=("AI",),
            run_macro=True,
            persist=False,
            config_hash=CONFIG_HASH,
        )
        result = self.orchestrator.run(cfg)

        self.assertEqual(result.errors, ())
        # The orchestrator surfaces a missing-signal warning
        joined = "\n".join(result.warnings)
        self.assertIn("no signals observed for industry/AI", joined)
        # The macro leg still ran
        self.assertIsNotNone(result.pipeline_result.macro)


class FailurePropagationTests(unittest.TestCase):
    """Scenario 10: Failure propagation is explicit and typed.

    Inject a broken graph writer that always raises; the orchestrator
    must (a) raise :class:`IntelligenceRunError` with the component
    name set to ``graph_writer`` and the partial result attached, and
    (b) the partial result must still carry the scoring pipeline's
    macro PipelineResult (we don't lose upstream state).
    """

    class _RaisingGraphWriter:
        def write(self, result: Any) -> Any:  # type: ignore[override]
            raise RuntimeError("boom from graph writer")

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        score_repo = ScoreRepository(self.store)
        writer = SnapshotWriter(
            score_repo, SnapshotWriterConfig(dry_run=True),
        )
        scoring_pipeline = ScoringPipeline(
            signal_loader=SignalLoader(self.signal_repo),
            macro_scorer=MacroScorer(config_hash=CONFIG_HASH),
            industry_scorer=IndustryScorer(config_hash=CONFIG_HASH),
            company_scorer=CompanyScorer(config_hash=CONFIG_HASH),
            sink=writer,
            config=PipelineConfig(dry_run=True, config_hash=CONFIG_HASH),
        )
        graph_store = GraphStore()
        self.orchestrator = IntelligencePipeline(
            scoring_pipeline=scoring_pipeline,
            graph_writer=self._RaisingGraphWriter(),  # type: ignore[arg-type]
            evidence_adapter=EvidenceChainAdapter(graph_store),
            graph_store=graph_store,
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_graph_writer_failure_typed(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            run_macro=True,
            persist=False,
            config_hash=CONFIG_HASH,
        )
        with self.assertRaises(IntelligenceRunError) as ctx:
            self.orchestrator.run(cfg)
        err = ctx.exception
        self.assertEqual(err.component, "graph_writer")
        self.assertEqual(err.error_class, "RuntimeError")
        # Partial result is attached and well-typed
        self.assertIsNotNone(err.partial)
        assert err.partial is not None  # for type-checker
        self.assertIsInstance(err.partial, IntelligenceRunResult)
        self.assertIsNotNone(err.partial.pipeline_result.macro)
        # The error appears in the partial.errors tuple
        self.assertGreaterEqual(len(err.partial.errors), 1)
        self.assertEqual(err.partial.errors[0].component, "graph_writer")


# ---------------------------------------------------------------------------
# Quick smoke for the public DTO contract
# ---------------------------------------------------------------------------


class ResultDtoContractTests(unittest.TestCase):
    """Verify the result envelope is well-typed and JSON-serializable."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        self.orchestrator, _, _ = _build_intelligence_pipeline(
            self.store, scoring_kwargs={"dry_run": True},
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_result_to_dict_round_trips_json(self) -> None:
        cfg = IntelligencePipelineConfig(
            date_bucket=DATE,
            run_macro=True,
            persist=False,
            config_hash=CONFIG_HASH,
        )
        result = self.orchestrator.run(cfg)
        d = result.to_dict()
        # JSON-serializable (no datetime, no dataclass leaks)
        payload = json.dumps(d, default=str)
        self.assertIsInstance(payload, str)
        # Spot-check the canonical fields
        self.assertEqual(d["date_bucket"], DATE)
        self.assertEqual(d["config_hash"], CONFIG_HASH)
        self.assertTrue(d["dry_run"])
        self.assertFalse(d["persist"])
        self.assertIn("metadata", d)
        self.assertIn("run_id", d["metadata"])
        self.assertEqual(d["metadata"]["config_hash"], CONFIG_HASH)


class ConfigValidationTests(unittest.TestCase):
    """The config DTO must reject invalid inputs at construction."""

    def test_empty_date_bucket_rejected(self) -> None:
        with self.assertRaises(ValueError):
            IntelligencePipelineConfig(date_bucket="")

    def test_invalid_trace_direction_rejected(self) -> None:
        with self.assertRaises(ValueError):
            IntelligencePipelineConfig(
                date_bucket=DATE, trace_directions=("sideways",),
            )

    def test_resolved_run_id_fills_uuid(self) -> None:
        cfg = IntelligencePipelineConfig(date_bucket=DATE)
        rid = cfg.resolved_run_id()
        self.assertTrue(rid.startswith("ipr-"))
        self.assertGreater(len(rid), 4)

    def test_resolved_run_id_keeps_caller_value(self) -> None:
        cfg = IntelligencePipelineConfig(date_bucket=DATE, run_id="my-run-1")
        self.assertEqual(cfg.resolved_run_id(), "my-run-1")


if __name__ == "__main__":
    unittest.main()
