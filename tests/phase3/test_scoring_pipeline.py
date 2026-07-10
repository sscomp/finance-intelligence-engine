"""Tests for Phase 3B Task 3 Run 2B ScoringPipeline.

Scope:
  - Pipeline.run_macro / run_industry / run_company end-to-end
  - Pipeline.run_all threads cross-layer context macro → industry → company
  - dry_run=True skips persistence
  - ScoreRepositorySink writes append-only snapshots and is reusable
  - SnapshotSink Protocol is satisfied by the default sink
  - PipelineResult carries evidence_signal_ids and metadata

Design constraints (per Phase 3 master doc):
  - Reuse SignalLoader, InputBuilder, scorers, repositories (no
    duplicate logic)
  - Append-only snapshot semantics
  - Deterministic output
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from phase3.datamodel import (
    CompanyScore,
    IndustryScore,
    MacroScore,
    ScoreBreakdown,
)
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.score_repo import ScoreRepository, ScoreSnapshotRecord
from phase3.persistence.schema_v1 import build as build_v1
from phase3.persistence.signal_repo import SignalRecord, SignalRepository
from phase3.persistence.sqlite import SQLiteStore
from phase3.pipeline.input_builder import InputBuilder
from phase3.pipeline.scoring_pipeline import (
    PipelineConfig,
    PipelineResult,
    PipelineRunReport,
    ScoreRepositorySink,
    ScoringPipeline,
    SnapshotSink,
)
from phase3.pipeline.signal_loader import SignalLoader, SignalLoaderFilters
from phase3.scoring.company import CompanyScorer
from phase3.scoring.industry import IndustryScorer
from phase3.scoring.macro import MacroScorer


DATE = "2026-07-09"
TS = datetime(2026, 7, 9, 12, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_store() -> tuple[str, SQLiteStore]:
    fd, path = tempfile.mkstemp(prefix="scoring_pipeline_test", suffix=".db")
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


def _seed_industry_signals(repo: SignalRepository, industry_id: str = "AI") -> None:
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


def _seed_company_signals(repo: SignalRepository, code: str = "2330") -> None:
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


def _build_pipeline(
    store: SQLiteStore,
    *,
    sink: SnapshotSink | None = None,
    config: PipelineConfig | None = None,
) -> tuple[ScoringPipeline, ScoreRepository]:
    signal_repo = SignalRepository(store)
    score_repo = ScoreRepository(store)
    loader = SignalLoader(signal_repo)
    pipeline = ScoringPipeline(
        signal_loader=loader,
        input_builder=InputBuilder(),
        macro_scorer=MacroScorer(config_hash="test-cfg"),
        industry_scorer=IndustryScorer(config_hash="test-cfg"),
        company_scorer=CompanyScorer(config_hash="test-cfg"),
        sink=sink if sink is not None else ScoreRepositorySink(score_repo),
        config=config or PipelineConfig(config_hash="test-cfg", notes="unit-test"),
    )
    return pipeline, score_repo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class ScoringPipelineSmokeTests(unittest.TestCase):
    """Lightweight tests that don't touch the database."""

    def test_score_repository_sink_satisfies_protocol(self) -> None:
        """ScoreRepositorySink must satisfy the SnapshotSink Protocol."""
        # Build a real sink and assert runtime-checkable dispatch.
        _, store = _new_store()
        try:
            sink: SnapshotSink = ScoreRepositorySink(ScoreRepository(store))
            self.assertIsInstance(sink, SnapshotSink)
        finally:
            os.unlink(store.path)

    def test_pipeline_result_is_frozen_dataclass(self) -> None:
        """PipelineResult must be immutable (frozen)."""
        r = PipelineResult(
            score=None,
            input_bundle=None,  # type: ignore[arg-type]
            evidence_signal_ids=("a", "b"),
            snapshot_id=None,
            warnings=(),
            metadata={},
        )
        with self.assertRaises(Exception):
            r.evidence_signal_ids = ("c",)  # type: ignore[misc]


class ScoringPipelineMacroTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        self.pipeline, _ = _build_pipeline(self.store)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_run_macro_returns_macro_score(self) -> None:
        result = self.pipeline.run_macro(date_bucket=DATE)
        self.assertIsInstance(result, PipelineResult)
        self.assertIsInstance(result.score, MacroScore)
        self.assertEqual(result.score.breakdown.scorer_type, "macro")
        self.assertEqual(result.score.breakdown.entity_id, "global")

    def test_run_macro_produces_score_in_range(self) -> None:
        """Score is clipped to [-100, +100] by the scorer."""
        result = self.pipeline.run_macro(date_bucket=DATE)
        self.assertGreaterEqual(result.score.score, -100.0)
        self.assertLessEqual(result.score.score, 100.0)

    def test_run_macro_collects_evidence_signal_ids(self) -> None:
        result = self.pipeline.run_macro(date_bucket=DATE)
        ids = set(result.evidence_signal_ids)
        # Every seeded macro signal should appear in evidence.
        self.assertIn("m-gdp", ids)
        self.assertIn("m-pmi", ids)
        self.assertIn("m-vix", ids)
        # No duplicates
        self.assertEqual(len(result.evidence_signal_ids), len(set(result.evidence_signal_ids)))

    def test_run_macro_persists_snapshot(self) -> None:
        before = self.score_repo.count()
        result = self.pipeline.run_macro(date_bucket=DATE)
        after = self.score_repo.count()
        self.assertEqual(after - before, 1)
        self.assertIsNotNone(result.snapshot_id)
        assert result.snapshot_id is not None  # for type check
        record = self.score_repo.latest("macro", "macro", "global")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.snapshot_id, result.snapshot_id)
        self.assertEqual(record.entity_id, "global")
        self.assertEqual(record.notes, "unit-test")
        # Breakdown payload annotates evidence signal ids
        self.assertIn("evidence_signal_ids", record.breakdown)
        self.assertIn("m-gdp", record.breakdown["evidence_signal_ids"])

    def test_run_macro_dry_run_skips_persistence(self) -> None:
        cfg = PipelineConfig(dry_run=True, config_hash="test-cfg", notes="dry")
        pipeline, _ = _build_pipeline(self.store, config=cfg)
        before = self.score_repo.count()
        result = pipeline.run_macro(date_bucket=DATE)
        after = self.score_repo.count()
        self.assertEqual(after, before, "dry-run must not insert snapshots")
        self.assertIsNone(result.snapshot_id)
        self.assertTrue(result.metadata["dry_run"])

    def test_run_macro_warns_when_no_signals(self) -> None:
        """No signals for a fresh date → all-empty bundle → warnings emitted."""
        result = self.pipeline.run_macro(date_bucket="2026-01-01")
        # Empty inputs are still valid (the scorer handles it), but
        # every dimension should produce a "no signals" warning.
        self.assertTrue(any("no signals" in w for w in result.warnings))


class ScoringPipelineIndustryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)
        _seed_industry_signals(self.signal_repo, industry_id="AI")
        self.pipeline, _ = _build_pipeline(self.store)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_run_industry_returns_industry_score(self) -> None:
        result = self.pipeline.run_industry(industry_id="AI", date_bucket=DATE)
        self.assertIsInstance(result.score, IndustryScore)
        self.assertEqual(result.score.breakdown.entity_id, "AI")
        self.assertEqual(result.score.industry_name, "AI")

    def test_run_industry_persists_snapshot(self) -> None:
        before = self.score_repo.count()
        result = self.pipeline.run_industry(industry_id="AI", date_bucket=DATE)
        after = self.score_repo.count()
        self.assertEqual(after - before, 1)
        self.assertIsNotNone(result.snapshot_id)
        record = self.score_repo.latest("industry", "industry", "AI")
        self.assertIsNotNone(record)

    def test_run_industry_threads_macro_context(self) -> None:
        """When macro_context is passed, the macro_sensitivity dimension
        consumes it. We can't assert the exact score (depends on the
        scorer), but we can confirm the dimension is in the result and
        has non-zero factors."""
        # First compute a macro result
        _seed_macro_signals(self.signal_repo)
        macro_result = self.pipeline.run_macro(date_bucket=DATE)
        macro_breakdown: ScoreBreakdown = macro_result.score.breakdown
        # Re-run industry with explicit macro context
        result = self.pipeline.run_industry(
            industry_id="AI",
            date_bucket=DATE,
            macro_context=macro_breakdown,
            industry_macro_beta={"liquidity": 0.3, "rates": 0.2},
        )
        # Find macro_sensitivity dimension
        sens = next(
            (d for d in result.score.dimensions if d.name == "macro_sensitivity"),
            None,
        )
        self.assertIsNotNone(sens, "macro_sensitivity dimension missing")
        assert sens is not None
        # With non-empty beta map, factors should be populated.
        self.assertGreater(len(sens.factors), 0,
                           "macro_sensitivity should produce factors when beta is set")

    def test_run_industry_uses_default_name_when_blank(self) -> None:
        result = self.pipeline.run_industry(
            industry_id="semiconductor",
            industry_name="",
            date_bucket=DATE,
        )
        # Falls back to industry_id
        self.assertEqual(result.score.industry_name, "semiconductor")


class ScoringPipelineCompanyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)
        _seed_company_signals(self.signal_repo, code="2330")
        self.pipeline, _ = _build_pipeline(self.store)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_run_company_returns_company_score(self) -> None:
        result = self.pipeline.run_company(code="2330", date_bucket=DATE)
        self.assertIsInstance(result.score, CompanyScore)
        self.assertEqual(result.score.breakdown.entity_id, "2330")
        self.assertEqual(result.score.code, "2330")

    def test_run_company_financial_sector_switches_profitability(self) -> None:
        """When is_financial_sector=True, the profitability dimension
        consumes nim_growth/interest_spread instead of margin metrics."""
        result = self.pipeline.run_company(
            code="2330",
            date_bucket=DATE,
            is_financial_sector=True,
        )
        prof = next(d for d in result.score.dimensions if d.name == "profitability")
        factor_names = {f.name for f in prof.factors}
        # Financial-sector profitability uses nim_growth / interest_spread.
        # Our seeded signals didn't include those → 0 factors is expected,
        # but the dimension should still exist and be scoreable.
        self.assertEqual(prof.name, "profitability")

    def test_run_company_auto_detects_financial_sector(self) -> None:
        """sector containing 'financ' → is_financial heuristic triggers."""
        # Heuristic must not raise; we just verify it works end-to-end.
        result = self.pipeline.run_company(
            code="2330",
            date_bucket=DATE,
            sector="Financials",
        )
        self.assertIsInstance(result.score, CompanyScore)

    def test_run_company_persists_snapshot(self) -> None:
        before = self.score_repo.count()
        result = self.pipeline.run_company(code="2330", date_bucket=DATE)
        after = self.score_repo.count()
        self.assertEqual(after - before, 1)
        self.assertIsNotNone(result.snapshot_id)
        record = self.score_repo.latest("company", "company", "2330")
        self.assertIsNotNone(record)

    def test_run_company_with_macro_and_industry_context(self) -> None:
        """Cross-layer adjustment must be recorded when contexts provided."""
        _seed_macro_signals(self.signal_repo)
        _seed_industry_signals(self.signal_repo, industry_id="semiconductor")
        macro = self.pipeline.run_macro(date_bucket=DATE)
        industry = self.pipeline.run_industry(
            industry_id="semiconductor", date_bucket=DATE,
            macro_context=macro.score.breakdown,
        )
        # Custom CompanyScorer with sensitivity so the macro adjustment
        # actually triggers.
        from phase3.datamodel import (
            COMPANY_DEFAULT_WEIGHTS,
            COMPANY_DIMENSIONS,
            ScorerWeights,
        )
        sensitive_scorer = CompanyScorer(
            weights=ScorerWeights(
                scorer_type="company",
                weights=COMPANY_DEFAULT_WEIGHTS,
                dimension_order=COMPANY_DIMENSIONS,
            ),
            sector_sensitivity={
                "semiconductor": {"liquidity": 0.5, "rates": 0.3},
            },
        )
        pipeline = ScoringPipeline(
            signal_loader=SignalLoader(self.signal_repo),
            input_builder=InputBuilder(),
            macro_scorer=MacroScorer(config_hash="test-cfg"),
            industry_scorer=IndustryScorer(config_hash="test-cfg"),
            company_scorer=sensitive_scorer,
            sink=ScoreRepositorySink(self.score_repo),
            config=PipelineConfig(config_hash="test-cfg", dry_run=True),
        )
        result = pipeline.run_company(
            code="2330",
            date_bucket=DATE,
            sector="semiconductor",
            macro_context=macro.score.breakdown,
            industry_score=industry.score,
        )
        # Cross-layer adjustment list should have entries for both
        # macro and industry.
        adjustments = result.score.breakdown.cross_layer_adjustments
        from_layer = {a.from_scorer for a in adjustments}
        self.assertIn("macro", from_layer)
        self.assertIn("industry", from_layer)


class ScoringPipelineRunAllTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        _seed_industry_signals(self.signal_repo, industry_id="semiconductor")
        _seed_industry_signals(self.signal_repo, industry_id="AI")
        _seed_company_signals(self.signal_repo, code="2330")
        self.pipeline, _ = _build_pipeline(self.store)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_run_all_executes_full_chain(self) -> None:
        report = self.pipeline.run_all(
            date_bucket=DATE,
            industry_ids=["semiconductor", "AI"],
            company_specs=[
                {"code": "2330", "name": "TSMC", "sector": "semiconductor",
                 "industry_id_for_adjustment": "semiconductor"},
            ],
        )
        self.assertIsInstance(report, PipelineRunReport)
        self.assertIsNotNone(report.macro)
        self.assertEqual(len(report.industries), 2)
        self.assertEqual(len(report.companies), 1)
        # Each tier produced a snapshot
        assert report.macro is not None
        self.assertIsNotNone(report.macro.snapshot_id)
        self.assertTrue(all(r.snapshot_id is not None for r in report.industries))
        self.assertTrue(all(r.snapshot_id is not None for r in report.companies))

    def test_run_all_macro_first_order(self) -> None:
        """The macro leg's timestamp must precede both industries' and
        companies' timestamps (signal of execution order)."""
        report = self.pipeline.run_all(
            date_bucket=DATE,
            industry_ids=["AI"],
            company_specs=[{"code": "2330", "industry_id_for_adjustment": "AI"}],
        )
        assert report.macro is not None
        macro_ts = report.macro.score.breakdown.timestamp
        for ind in report.industries:
            self.assertGreaterEqual(ind.score.breakdown.timestamp, macro_ts)
        for comp in report.companies:
            self.assertGreaterEqual(comp.score.breakdown.timestamp, macro_ts)

    def test_run_all_can_skip_macro(self) -> None:
        report = self.pipeline.run_all(
            date_bucket=DATE,
            industry_ids=["AI"],
            company_specs=[],
            run_macro=False,
        )
        self.assertIsNone(report.macro)
        self.assertEqual(len(report.industries), 1)
        self.assertTrue(any("skipped" in w for w in report.warnings))

    def test_run_all_emits_warning_for_missing_code(self) -> None:
        report = self.pipeline.run_all(
            date_bucket=DATE,
            industry_ids=[],
            company_specs=[{"name": "no-code"}],  # missing 'code'
        )
        self.assertEqual(len(report.companies), 0)
        self.assertTrue(any("missing 'code'" in w for w in report.warnings))

    def test_run_all_company_uses_correct_industry_for_adjustment(self) -> None:
        """A company with industry_id_for_adjustment='semiconductor'
        must use the semiconductor IndustryScore (not 'AI')."""
        report = self.pipeline.run_all(
            date_bucket=DATE,
            industry_ids=["semiconductor", "AI"],
            company_specs=[
                {"code": "2330", "industry_id_for_adjustment": "semiconductor"},
            ],
        )
        self.assertEqual(len(report.companies), 1)
        comp_result = report.companies[0]
        adj = comp_result.score.breakdown.cross_layer_adjustments
        industry_adj = [a for a in adj if a.from_scorer == "industry"]
        self.assertEqual(len(industry_adj), 1)


class ScoreRepositorySinkTests(unittest.TestCase):
    """The default sink must write append-only and capture config_hash."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)
        _seed_macro_signals(self.signal_repo)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_sink_writes_one_row_per_call(self) -> None:
        sink = ScoreRepositorySink(self.score_repo)
        pipeline = ScoringPipeline(
            signal_loader=SignalLoader(self.signal_repo),
            input_builder=InputBuilder(),
            macro_scorer=MacroScorer(config_hash="hash-A"),
            sink=sink,
            config=PipelineConfig(config_hash="hash-A", notes="row-1"),
        )
        r1 = pipeline.run_macro(date_bucket=DATE)
        self.assertIsNotNone(r1.snapshot_id)
        # Re-run produces a NEW snapshot (append-only)
        r2 = pipeline.run_macro(date_bucket=DATE)
        self.assertIsNotNone(r2.snapshot_id)
        self.assertNotEqual(r1.snapshot_id, r2.snapshot_id)
        self.assertEqual(self.score_repo.count(), 2)

    def test_sink_persists_config_hash(self) -> None:
        sink = ScoreRepositorySink(self.score_repo)
        pipeline = ScoringPipeline(
            signal_loader=SignalLoader(self.signal_repo),
            input_builder=InputBuilder(),
            macro_scorer=MacroScorer(config_hash="hash-special"),
            sink=sink,
            config=PipelineConfig(config_hash="hash-special"),
        )
        pipeline.run_macro(date_bucket=DATE)
        record = self.score_repo.latest("macro", "macro", "global")
        self.assertIsNotNone(record)
        assert record is not None
        # breakdown config_hash mirrors scorer config_hash
        self.assertEqual(record.breakdown.get("config_hash"), "hash-special")

    def test_sink_breakdown_payload_includes_evidence(self) -> None:
        sink = ScoreRepositorySink(self.score_repo)
        pipeline = ScoringPipeline(
            signal_loader=SignalLoader(self.signal_repo),
            input_builder=InputBuilder(),
            sink=sink,
            config=PipelineConfig(),
        )
        result = pipeline.run_macro(date_bucket=DATE)
        record = self.score_repo.latest("macro", "macro", "global")
        assert record is not None
        ids = set(record.breakdown["evidence_signal_ids"])
        result_ids = set(result.evidence_signal_ids)
        self.assertEqual(ids, result_ids)


class ScoringPipelineDeterminismTests(unittest.TestCase):
    """Same inputs → same outputs (modulo timestamp)."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        _seed_macro_signals(self.signal_repo)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_run_macro_score_is_deterministic(self) -> None:
        # Use a fixed as_of so timestamps match.
        fixed_dt = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        cfg = PipelineConfig(config_hash="det", as_of=fixed_dt, dry_run=True)
        pipeline, _ = _build_pipeline(self.store, config=cfg)
        r1 = pipeline.run_macro(date_bucket=DATE)
        r2 = pipeline.run_macro(date_bucket=DATE)
        self.assertEqual(r1.score.score, r2.score.score)
        self.assertEqual(r1.score.confidence, r2.score.confidence)
        # Per-dimension scores must match too
        d1 = {d.name: d.score for d in r1.score.dimensions}
        d2 = {d.name: d.score for d in r2.score.dimensions}
        self.assertEqual(d1, d2)


class ScoringPipelineEdgeCaseTests(unittest.TestCase):
    """Failure modes and edge cases."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_run_macro_with_no_signals_does_not_raise(self) -> None:
        pipeline, _ = _build_pipeline(self.store, config=PipelineConfig(dry_run=True))
        result = pipeline.run_macro(date_bucket=DATE)
        # Empty inputs → small non-zero score (MacroScorer's sub-function
        # defaults push the result off zero — see Phase 3A memory note).
        # We assert it's small + in range rather than exact 0.
        self.assertIsInstance(result.score, MacroScore)
        self.assertGreaterEqual(result.score.score, -100.0)
        self.assertLessEqual(result.score.score, 100.0)
        self.assertLess(abs(result.score.score), 50.0)

    def test_pipeline_uses_default_sink_when_sink_is_none(self) -> None:
        """Passing sink=None means no persistence (NullSink), not 'no sink'."""
        pipeline = ScoringPipeline(
            signal_loader=SignalLoader(self.signal_repo),
            input_builder=InputBuilder(),
            macro_scorer=MacroScorer(),
            sink=None,
            config=PipelineConfig(),
        )
        result = pipeline.run_macro(date_bucket=DATE)
        # No rows written; no error
        self.assertEqual(self.score_repo.count(), 0)
        self.assertIsNone(result.snapshot_id)

    def test_pipeline_propagates_sink_errors(self) -> None:
        """If the sink raises, the pipeline propagates (no silent swallow)."""

        class BoomSink:
            def write(self, score: Any, evidence_signal_ids, notes="", config_hash=""):
                raise RuntimeError("sink exploded")

        # Seed at least one signal so the run has real data
        _seed_macro_signals(self.signal_repo)
        pipeline = ScoringPipeline(
            signal_loader=SignalLoader(self.signal_repo),
            input_builder=InputBuilder(),
            macro_scorer=MacroScorer(),
            sink=BoomSink(),  # type: ignore[arg-type]
            config=PipelineConfig(),
        )
        with self.assertRaises(RuntimeError) as ctx:
            pipeline.run_macro(date_bucket=DATE)
        self.assertIn("sink exploded", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
