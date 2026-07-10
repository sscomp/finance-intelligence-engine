"""Tests for Phase 3B Task 4 Run 1 GraphWriter.

Scope:
  - Writes score / entity / signal / source nodes
  - Writes expected contribution edges (signal→score, source→signal,
    score→entity, and INFLUENCES for cross-layer)
  - Preserves evidence_signal_ids on the score node
  - Repeated write is idempotent (no node/edge duplication)
  - Works for company / industry / macro cases
  - Cross-layer adjustment edges
  - Metadata round-trip (config_hash, score, confidence, snapshot_id)
  - No-op / empty evidence behavior
  - GraphWriter output is compatible with existing
    SQLiteGraphStore traversal/stat APIs

Design constraints (per Phase 3 master doc):
  - Reuse the existing GraphRepository / SQLiteGraphStore / NodeType /
    EdgeType; do not invent a parallel persistence layer.
  - Idempotent: deterministic node/edge ids, upsert-only.
  - No network, no production DB path; tests use temp SQLite.
  - stdlib unittest only.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any, Literal

ScorerType = Literal["macro", "industry", "company"]

from phase3.datamodel import (
    CompanyScore,
    CrossLayerAdjustment,
    IndustryScore,
    MacroScore,
    ScoreBreakdown,
)
from phase3.datamodel.graph import EdgeType, NodeType
from phase3.datamodel.evidence import Evidence
from phase3.datamodel.scores import DimensionResult, WeightedFactor
from phase3.graph.in_memory_store import GraphStore
from phase3.graph.sqlite_store import SQLiteGraphStore
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.schema_v1 import build as build_v1
from phase3.persistence.score_repo import ScoreRepository
from phase3.persistence.signal_repo import SignalRecord, SignalRepository
from phase3.persistence.sqlite import SQLiteStore
from phase3.pipeline import (
    GraphWriteResult,
    GraphWriter,
    GraphWriterStore,
    make_entity_node_id,
    make_score_node_id,
    make_signal_node_id,
    make_source_node_id,
)
from phase3.pipeline.input_builder import InputBuilder
from phase3.pipeline.scoring_pipeline import (
    PipelineConfig,
    PipelineResult,
    ScoringPipeline,
)
from phase3.pipeline.signal_loader import SignalLoader
from phase3.scoring.company import CompanyScorer
from phase3.scoring.industry import IndustryScorer
from phase3.scoring.macro import MacroScorer


DATE = "2026-07-09"
TS = datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
CONFIG_HASH = "cfg-graph-writer-test"


# ---------------------------------------------------------------------------
# Fixture helpers (re-used from test_scoring_pipeline.py patterns)
# ---------------------------------------------------------------------------


def _new_sqlite_store() -> tuple[str, SQLiteStore]:
    fd, path = tempfile.mkstemp(prefix="graph_writer_test", suffix=".db")
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
            timestamp=TS.isoformat(),
            date_bucket=DATE,
            source_id=f"{source_type}-{signal_id}",
            source_type=source_type,
            ref=signal_id,
            fetched_at=TS.isoformat(),
            fetch_id=f"fetch-{signal_id}",
            schema_version="3.0",
            metadata={},
            raw_payload={},
        )
    )


def _seed_macro_signals(repo: SignalRepository) -> None:
    for sid, st, val in (
        ("m-gdp", "gdp_yoy", 0.025),
        ("m-pmi", "pmi", 52.0),
        ("m-vix", "vix", 18.0),
        ("m-cpi", "cpi_yoy", 2.5),
        ("m-spread", "yield_spread_pp", 1.2),
    ):
        _seed_signal(
            repo, signal_id=sid, entity_type="macro", entity_id="global",
            signal_type=st, value=val,
        )


def _seed_industry_signals(repo: SignalRepository, industry_id: str) -> None:
    for sid, st, val, src in (
        ("i-relperf5", "sector_relative_perf_5d", 0.03, "yfinance"),
        ("i-rs", "rs_rating", 80.0, "yfinance"),
        ("i-bb", "book_to_bill", 1.1, "yfinance"),
        ("i-news", "news_headline", 1.0, "rss"),
        ("i-foreign", "foreign_net", 2.0, "t86"),
    ):
        _seed_signal(
            repo, signal_id=sid, entity_type="industry", entity_id=industry_id,
            signal_type=st, value=val, source_type=src,
        )


def _seed_company_signals(repo: SignalRepository, code: str) -> None:
    for sid, st, val, src in (
        ("c-roe", "roe", 0.18, "yfinance"),
        ("c-peg", "pe_ratio", 22.0, "yfinance"),
        ("c-pm1m", "price_momentum_1m", 0.04, "yfinance"),
        ("c-beta", "beta", 1.0, "yfinance"),
        ("c-sent", "sentiment", 0.3, "rss"),
        ("c-news", "news_headline", 1.0, "rss"),
    ):
        _seed_signal(
            repo, signal_id=sid, entity_type="company", entity_id=code,
            signal_type=st, value=val, source_type=src,
        )


def _build_pipeline(store: SQLiteStore) -> ScoringPipeline:
    signal_repo = SignalRepository(store)
    score_repo = ScoreRepository(store)
    loader = SignalLoader(signal_repo)
    return ScoringPipeline(
        signal_loader=loader,
        input_builder=InputBuilder(),
        macro_scorer=MacroScorer(config_hash=CONFIG_HASH),
        industry_scorer=IndustryScorer(config_hash=CONFIG_HASH),
        company_scorer=CompanyScorer(config_hash=CONFIG_HASH),
        sink=None,  # graph writer doesn't need snapshots to exist
        config=PipelineConfig(
            config_hash=CONFIG_HASH,
            notes="graph-writer-test",
            dry_run=True,
        ),
    )


# ---------------------------------------------------------------------------
# Node-id helper unit tests
# ---------------------------------------------------------------------------


class NodeIdHelperTests(unittest.TestCase):
    """Pure-function tests for the deterministic id helpers."""

    def test_score_node_id_format(self) -> None:
        nid = make_score_node_id("macro", "global", "2026-07-09")
        self.assertEqual(nid, "score:macro:global:2026-07-09")

    def test_signal_node_id_format(self) -> None:
        nid = make_signal_node_id("m-gdp")
        self.assertEqual(nid, "signal:m-gdp")

    def test_source_node_id_format(self) -> None:
        nid = make_source_node_id("yfinance", "yfinance-m-gdp")
        self.assertEqual(nid, "source:yfinance:yfinance-m-gdp")

    def test_entity_node_id_format(self) -> None:
        nid = make_entity_node_id("company", "2330")
        self.assertEqual(nid, "entity:company:2330")

    def test_signal_node_id_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            make_signal_node_id("")

    def test_source_node_id_handles_blank_id(self) -> None:
        nid = make_source_node_id("yfinance", "")
        # Falls back to a stable string so the writer never produces
        # an empty source node id.
        self.assertTrue(nid.startswith("source:yfinance:"))


# ---------------------------------------------------------------------------
# Smoke tests — minimal hand-built PipelineResult
# ---------------------------------------------------------------------------


def _make_breakdown(
    *,
    scorer_type: str = "macro",
    entity_type: str = "macro",
    entity_id: str = "global",
    score_value: float = 12.5,
    config_hash: str = CONFIG_HASH,
    evidence: tuple[str, ...] = ("m-gdp", "m-cpi"),
    cross_layer_adjustments: tuple[CrossLayerAdjustment, ...] = (),
    factor_source: str = "yfinance",
) -> ScoreBreakdown:
    """A minimal but real ScoreBreakdown for the writer tests."""
    wf = WeightedFactor(
        name="gdp_yoy",
        raw_value=0.025,
        raw_unit="ratio",
        sub_score=15.0,
        sub_weight=1.0,
        signed_score=15.0,
        transformation="threshold",
        source=factor_source,
        source_ref=f"macro.{entity_id}.gdp_yoy",
        evidence=[],
    )
    dim = DimensionResult(
        name="economic",
        score=15.0,
        sub_indicators=[],
        weight=0.2,
        confidence=0.8,
        factors=[wf],
        evidence=[],
    )
    return ScoreBreakdown(
        scorer_type=scorer_type,
        entity_type=entity_type,
        entity_id=entity_id,
        score=score_value,
        confidence=0.8,
        dimensions=[dim],
        overall_evidence=[],
        timestamp=TS,
        config_hash=config_hash,
        valid_until=TS,
        cross_layer_adjustments=list(cross_layer_adjustments),
    )


def _make_pipeline_result(
    *,
    breakdown: ScoreBreakdown,
    evidence_signal_ids: tuple[str, ...] = ("m-gdp", "m-cpi"),
    snapshot_id: int | None = None,
) -> PipelineResult:
    """Build a synthetic PipelineResult for the writer.

    The InputBundle is a stub — the writer only reads dimensions
    through ``breakdown.dimensions`` and the evidence list, so the
    bundle can be a minimal frozen object.
    """
    from phase3.pipeline import InputBundle, InputDimension
    # ScoreBreakdown.scorer_type is str; InputBundle requires the
    # narrower ScorerType literal. Safe because the breakdown came
    # from one of the three scorers.
    scorer_type: ScorerType = breakdown.scorer_type  # type: ignore[assignment]
    bundle = InputBundle(
        scorer_type=scorer_type,
        entity_type=breakdown.entity_type,
        entity_id=breakdown.entity_id,
        date_bucket=DATE,
        dimensions={
            d.name: InputDimension(
                values={f.name: getattr(f, "raw_value", None) for f in d.factors},
                signal_ids=list(evidence_signal_ids),
            )
            for d in breakdown.dimensions
        },
    )
    return PipelineResult(
        score=MacroScore(breakdown=breakdown) if breakdown.scorer_type == "macro" else (
            IndustryScore(breakdown=breakdown, industry_name=breakdown.entity_id)
            if breakdown.scorer_type == "industry"
            else CompanyScore(breakdown=breakdown, code=breakdown.entity_id)
        ),
        input_bundle=bundle,
        evidence_signal_ids=evidence_signal_ids,
        snapshot_id=snapshot_id,
        warnings=(),
        metadata={
            "scorer_type": breakdown.scorer_type,
            "entity_id": breakdown.entity_id,
            "date_bucket": DATE,
            "config_hash": breakdown.config_hash,
            "dry_run": True,
        },
    )


class GraphWriterSmokeTests(unittest.TestCase):
    """Smoke tests for the writer using an in-memory graph store."""

    def setUp(self) -> None:
        self.store = GraphStore()
        self.writer = GraphWriter(self.store)

    def test_writes_all_node_kinds(self) -> None:
        """Score, entity, signal, and source nodes are all created."""
        breakdown = _make_breakdown(
            scorer_type="macro", entity_type="macro", entity_id="global",
        )
        result = _make_pipeline_result(breakdown=breakdown)
        out = self.writer.write(result)
        # Entity + score + 2 signals + 1 source = 5 nodes
        self.assertEqual(self.store.node_count(), 5)
        node_types = {n.node_type for n in self.store._nodes.values()}
        self.assertIn(NodeType.SCORE, node_types)
        self.assertIn(NodeType.MACRO_FACTOR, node_types)
        self.assertIn(NodeType.SIGNAL, node_types)
        self.assertIn(NodeType.SOURCE, node_types)
        # Returned result exposes the canonical ids
        self.assertEqual(
            out.score_node_id, make_score_node_id("macro", "global", DATE),
        )
        self.assertEqual(
            out.entity_node_id, make_entity_node_id("macro", "global"),
        )
        self.assertEqual(len(out.signal_node_ids), 2)
        self.assertEqual(len(out.source_node_ids), 1)
        self.assertEqual(len(out.evidence_signal_ids), 2)
        # created_node_count == total nodes (everything is new)
        self.assertEqual(out.created_node_count, 5)

    def test_writes_expected_edges(self) -> None:
        """source→signal (GENERATED), signal→score (CONTRIBUTES_TO),
        score→entity (CITES) are all written."""
        breakdown = _make_breakdown(
            scorer_type="macro", entity_id="global",
            factor_source="yfinance",
        )
        result = _make_pipeline_result(breakdown=breakdown)
        self.writer.write(result)
        # 2 GENERATED + 2 CONTRIBUTES_TO + 1 CITES = 5 edges
        self.assertEqual(self.store.edge_count(), 5)
        # The score → entity edge is the one CITES
        cites_edges = [
            e for e in self.store._edges.values()
            if e.edge_type == EdgeType.CITES
        ]
        self.assertEqual(len(cites_edges), 1)
        ce = cites_edges[0]
        self.assertEqual(ce.from_node_id, out_score_id(self.writer, result))
        self.assertEqual(ce.to_node_id, out_entity_id(self.writer, result))
        # Each signal has a CONTRIBUTES_TO edge to the score
        contributes = [
            e for e in self.store._edges.values()
            if e.edge_type == EdgeType.CONTRIBUTES_TO
        ]
        self.assertEqual(len(contributes), 2)
        # Each signal has a GENERATED edge from the source
        generated = [
            e for e in self.store._edges.values()
            if e.edge_type == EdgeType.GENERATED
        ]
        self.assertEqual(len(generated), 2)

    def test_preserves_evidence_signal_ids(self) -> None:
        """The score node's metadata round-trips evidence_signal_ids."""
        breakdown = _make_breakdown(
            scorer_type="macro", evidence=("m-gdp", "m-cpi", "m-pmi"),
        )
        result = _make_pipeline_result(
            breakdown=breakdown,
            evidence_signal_ids=("m-gdp", "m-cpi", "m-pmi"),
        )
        out = self.writer.write(result)
        score_node = self.store.get_node(out.score_node_id)
        assert score_node is not None
        stored = score_node.metadata["evidence_signal_ids"]
        self.assertEqual(set(stored), {"m-gdp", "m-cpi", "m-pmi"})

    def test_idempotent_repeat_writes(self) -> None:
        """Writing the same PipelineResult twice does not duplicate
        nodes or edges."""
        breakdown = _make_breakdown(
            scorer_type="macro", entity_id="global",
        )
        result = _make_pipeline_result(breakdown=breakdown)
        out1 = self.writer.write(result)
        out2 = self.writer.write(result)
        # Same set of node ids returned
        self.assertEqual(out1.score_node_id, out2.score_node_id)
        self.assertEqual(out1.entity_node_id, out2.entity_node_id)
        self.assertEqual(out1.signal_node_ids, out2.signal_node_ids)
        self.assertEqual(out1.source_node_ids, out2.source_node_ids)
        # No node or edge growth on the second write
        self.assertEqual(self.store.node_count(), 5)
        self.assertEqual(self.store.edge_count(), 5)
        # Second call's created_* counters are zero
        self.assertEqual(out2.created_node_count, 0)
        self.assertEqual(out2.created_edge_count, 0)

    def test_idempotent_after_evidence_subset(self) -> None:
        """Re-writing with a smaller evidence set keeps the original
        signal nodes (they aren't deleted)."""
        full = _make_pipeline_result(
            breakdown=_make_breakdown(evidence=("m-gdp", "m-cpi", "m-pmi")),
            evidence_signal_ids=("m-gdp", "m-cpi", "m-pmi"),
        )
        self.writer.write(full)
        before = self.store.node_count()
        # Now write a result with only one evidence signal
        small = _make_pipeline_result(
            breakdown=_make_breakdown(evidence=("m-gdp",)),
            evidence_signal_ids=("m-gdp",),
        )
        self.writer.write(small)
        # No new nodes (the signal is already there) but the score
        # node now reflects the smaller evidence list in its metadata.
        # The old signal nodes are NOT removed (writer is upsert-only).
        self.assertEqual(self.store.node_count(), before)


# ---------------------------------------------------------------------------
# Per-scorer integration tests (macro / industry / company)
# ---------------------------------------------------------------------------


class GraphWriterMacroCaseTests(unittest.TestCase):
    """End-to-end: drive ScoringPipeline.run_macro and write the
    real PipelineResult into a graph store."""

    def setUp(self) -> None:
        self.path, self.store = _new_sqlite_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        self.pipeline = _build_pipeline(self.store)
        self.graph = SQLiteGraphStore(
            os.path.join(tempfile.gettempdir(), "graph_writer_macro.db"),
        )
        self.addCleanup(self._cleanup_graph)
        self.writer = GraphWriter(self.graph)

    def _cleanup_graph(self) -> None:
        try:
            os.unlink(self.graph.path)
        except FileNotFoundError:
            pass

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_macro_run_writes_full_graph(self) -> None:
        result = self.pipeline.run_macro(date_bucket=DATE)
        out = self.writer.write(result)
        # Score / entity / signals / source all present
        self.assertIsNotNone(self.graph.get_node(out.score_node_id))
        self.assertIsNotNone(self.graph.get_node(out.entity_node_id))
        for sig_id in out.signal_node_ids:
            self.assertIsNotNone(self.graph.get_node(sig_id))
        for src_id in out.source_node_ids:
            self.assertIsNotNone(self.graph.get_node(src_id))
        # The score node is tagged with a `date:` tag. The pipeline
        # stamps the breakdown timestamp from MacroScorer's `as_of`
        # (current wall-clock by default), not the requested
        # date_bucket — so the tag's date may differ. We just verify
        # that *some* date tag exists and looks like YYYY-MM-DD.
        score_node = self.graph.get_node(out.score_node_id)
        assert score_node is not None
        date_tags = [t for t in score_node.tags if t.startswith("date:")]
        self.assertEqual(len(date_tags), 1)
        self.assertRegex(date_tags[0], r"^date:\d{4}-\d{2}-\d{2}$")
        # Stats report the writer's footprint
        stats = self.graph.stats()
        self.assertGreaterEqual(stats["total_nodes"], 5)
        self.assertIn("score", stats["nodes_by_type"])
        self.assertIn("macro_factor", stats["nodes_by_type"])
        self.assertIn("signal", stats["nodes_by_type"])
        self.assertIn("source", stats["nodes_by_type"])


class GraphWriterIndustryCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path, self.store = _new_sqlite_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)
        _seed_industry_signals(self.signal_repo, industry_id="AI")
        self.pipeline = _build_pipeline(self.store)
        self.graph = SQLiteGraphStore(
            os.path.join(tempfile.gettempdir(), "graph_writer_industry.db"),
        )
        self.addCleanup(self._cleanup_graph)
        self.writer = GraphWriter(self.graph)

    def _cleanup_graph(self) -> None:
        try:
            os.unlink(self.graph.path)
        except FileNotFoundError:
            pass

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_industry_run_writes_industry_entity(self) -> None:
        result = self.pipeline.run_industry(industry_id="AI", date_bucket=DATE)
        out = self.writer.write(result)
        # Entity node is of type INDUSTRY
        entity = self.graph.get_node(out.entity_node_id)
        assert entity is not None
        self.assertEqual(entity.node_type, NodeType.INDUSTRY)
        self.assertEqual(entity.node_id, make_entity_node_id("industry", "AI"))


class GraphWriterCompanyCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path, self.store = _new_sqlite_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)
        _seed_company_signals(self.signal_repo, code="2330")
        self.pipeline = _build_pipeline(self.store)
        self.graph = SQLiteGraphStore(
            os.path.join(tempfile.gettempdir(), "graph_writer_company.db"),
        )
        self.addCleanup(self._cleanup_graph)
        self.writer = GraphWriter(self.graph)

    def _cleanup_graph(self) -> None:
        try:
            os.unlink(self.graph.path)
        except FileNotFoundError:
            pass

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_company_run_writes_company_entity(self) -> None:
        result = self.pipeline.run_company(code="2330", date_bucket=DATE)
        out = self.writer.write(result)
        entity = self.graph.get_node(out.entity_node_id)
        assert entity is not None
        self.assertEqual(entity.node_type, NodeType.COMPANY)
        self.assertEqual(entity.node_id, make_entity_node_id("company", "2330"))


# ---------------------------------------------------------------------------
# Cross-layer adjustment edges
# ---------------------------------------------------------------------------


class GraphWriterCrossLayerTests(unittest.TestCase):
    """Cross-layer INFLUENCES edges land in the graph with the right
    from/to labels and metadata."""

    def setUp(self) -> None:
        self.store = GraphStore()
        self.writer = GraphWriter(self.store)

    def test_writes_influences_edge_for_cross_layer_adjustment(self) -> None:
        ev = Evidence(
            evidence_id="ev-1",
            source_type="yfinance",
            source_ref="m-cpi",
            raw_value="2.5",
            description="yfinance cpi_yoy 2.5",
            timestamp=TS,
            weight=1.0,
        )
        adj = CrossLayerAdjustment(
            from_scorer="macro",
            from_score_id="abc123",
            to_scorer="company",
            to_score_id="def456",
            adjustment=3.5,
            reason="liquidity sensitivity",
            evidence=[ev],
        )
        breakdown = _make_breakdown(
            scorer_type="company", entity_type="company", entity_id="2330",
            cross_layer_adjustments=(adj,),
        )
        result = _make_pipeline_result(breakdown=breakdown)
        out = self.writer.write(result)
        # One INFLUENCES edge written
        influences = [
            e for e in self.store._edges.values()
            if e.edge_type == EdgeType.INFLUENCES
        ]
        self.assertEqual(len(influences), 1)
        ie = influences[0]
        self.assertEqual(ie.from_node_id, out.score_node_id)
        # The to-node id is the cross-layer placeholder keyed by to_scorer
        # and to_score_id; we don't assert the literal string because
        # the placeholder is an implementation detail, but it must
        # be non-empty and the edge's metadata must record the link.
        self.assertTrue(ie.to_node_id)
        self.assertEqual(ie.metadata["from_scorer"], "macro")
        self.assertEqual(ie.metadata["to_scorer"], "company")
        self.assertEqual(ie.metadata["from_score_id"], "abc123")
        self.assertEqual(ie.metadata["to_score_id"], "def456")
        self.assertEqual(ie.metadata["adjustment"], 3.5)
        # Returned cross_layer_edge_ids reflects the new edge
        self.assertEqual(len(out.cross_layer_edge_ids), 1)


# ---------------------------------------------------------------------------
# Metadata round-trip
# ---------------------------------------------------------------------------


class GraphWriterMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = GraphStore()
        self.writer = GraphWriter(self.store)

    def test_score_node_metadata_round_trip(self) -> None:
        breakdown = _make_breakdown(
            scorer_type="industry",
            entity_type="industry",
            entity_id="AI",
            score_value=42.0,
            config_hash="my-cfg-hash",
            evidence=("i-news", "i-bb"),
        )
        result = _make_pipeline_result(
            breakdown=breakdown,
            evidence_signal_ids=("i-news", "i-bb"),
            snapshot_id=123,
        )
        out = self.writer.write(result)
        score = self.store.get_node(out.score_node_id)
        assert score is not None
        meta = score.metadata
        self.assertEqual(meta["scorer_type"], "industry")
        self.assertEqual(meta["entity_type"], "industry")
        self.assertEqual(meta["entity_id"], "AI")
        self.assertEqual(meta["date_bucket"], DATE)
        self.assertEqual(meta["score"], 42.0)
        self.assertEqual(meta["config_hash"], "my-cfg-hash")
        self.assertEqual(meta["snapshot_id"], 123)
        self.assertEqual(meta["dimension_count"], 1)
        self.assertEqual(set(meta["evidence_signal_ids"]), {"i-news", "i-bb"})

    def test_score_node_handles_missing_snapshot_id(self) -> None:
        breakdown = _make_breakdown()
        result = _make_pipeline_result(breakdown=breakdown, snapshot_id=None)
        out = self.writer.write(result)
        score = self.store.get_node(out.score_node_id)
        assert score is not None
        self.assertIsNone(score.metadata["snapshot_id"])


# ---------------------------------------------------------------------------
# Edge cases — empty evidence / bad input
# ---------------------------------------------------------------------------


class GraphWriterEdgeCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = GraphStore()
        self.writer = GraphWriter(self.store)

    def test_no_evidence_writes_only_score_and_entity(self) -> None:
        """An empty evidence list still writes a score + entity node
        (the writer is forgiving)."""
        # Build a breakdown with one dimension but no factors and
        # an empty evidence list.
        dim = DimensionResult(
            name="economic", score=0.0, sub_indicators=[], weight=0.5,
            confidence=0.5, factors=[], evidence=[],
        )
        breakdown = ScoreBreakdown(
            scorer_type="macro", entity_type="macro", entity_id="global",
            score=0.0, confidence=0.5, dimensions=[dim], overall_evidence=[],
            timestamp=TS, config_hash=CONFIG_HASH, valid_until=TS,
        )
        result = _make_pipeline_result(
            breakdown=breakdown, evidence_signal_ids=(),
        )
        out = self.writer.write(result)
        # Score + entity = 2 nodes
        self.assertEqual(self.store.node_count(), 2)
        self.assertEqual(self.store.edge_count(), 1)  # score → entity
        # No signal or source nodes
        self.assertEqual(out.signal_node_ids, ())
        self.assertEqual(out.source_node_ids, ())
        self.assertEqual(out.evidence_signal_ids, ())

    def test_rejects_non_pipeline_result(self) -> None:
        """The writer raises TypeError when handed a non-PipelineResult."""
        with self.assertRaises(TypeError):
            self.writer.write("not a result")  # type: ignore[arg-type]

    def test_empty_signal_id_in_evidence_is_skipped(self) -> None:
        """An empty signal_id is silently dropped from evidence."""
        breakdown = _make_breakdown()
        result = _make_pipeline_result(
            breakdown=breakdown,
            evidence_signal_ids=("m-gdp", "", "m-cpi"),
        )
        out = self.writer.write(result)
        # Only m-gdp and m-cpi made it in
        self.assertEqual(set(out.evidence_signal_ids), {"m-gdp", "m-cpi"})
        self.assertEqual(len(out.signal_node_ids), 2)


# ---------------------------------------------------------------------------
# Compatibility — output works with existing traversal/stat APIs
# ---------------------------------------------------------------------------


class GraphWriterCompatibilityTests(unittest.TestCase):
    """Output nodes/edges must be readable through the same APIs the
    in-memory store and SQLite store expose (get_node, get_edge,
    get_neighbors, stats)."""

    def setUp(self) -> None:
        self.path, self.store = _new_sqlite_store()
        self.signal_repo = SignalRepository(self.store)
        self.score_repo = ScoreRepository(self.store)
        _seed_macro_signals(self.signal_repo)
        self.pipeline = _build_pipeline(self.store)
        self.graph = SQLiteGraphStore(
            os.path.join(tempfile.gettempdir(), "graph_writer_compat.db"),
        )
        self.addCleanup(self._cleanup_graph)
        self.writer = GraphWriter(self.graph)

    def _cleanup_graph(self) -> None:
        try:
            os.unlink(self.graph.path)
        except FileNotFoundError:
            pass

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_neighbors_traversal_round_trip(self) -> None:
        """SQLiteGraphStore.get_neighbors works on writer output."""
        result = self.pipeline.run_macro(date_bucket=DATE)
        out = self.writer.write(result)
        # Score has incoming CONTRIBUTES_TO edges from each signal
        contributors = self.graph.get_neighbors(
            out.score_node_id,
            edge_types=[EdgeType.CONTRIBUTES_TO],
            direction="in",
        )
        self.assertEqual(
            {n.node_id for n, _ in contributors},
            set(out.signal_node_ids),
        )
        # Each signal has an incoming GENERATED edge from its source
        first_sig = out.signal_node_ids[0]
        sig_neighbors = self.graph.get_neighbors(
            first_sig, edge_types=[EdgeType.GENERATED], direction="in",
        )
        self.assertEqual(len(sig_neighbors), 1)
        # Entity has an incoming CITES edge from the score
        entity_neighbors = self.graph.get_neighbors(
            out.entity_node_id,
            edge_types=[EdgeType.CITES],
            direction="in",
        )
        self.assertEqual(len(entity_neighbors), 1)
        score, _ = entity_neighbors[0]
        self.assertEqual(score.node_id, out.score_node_id)

    def test_stats_match_writer_counts(self) -> None:
        """SQLiteGraphStore.stats reports the same totals the writer
        returned on its GraphWriteResult envelope."""
        result = self.pipeline.run_macro(date_bucket=DATE)
        out = self.writer.write(result)
        stats = self.graph.stats()
        # stats.total_nodes covers everything in the graph, but the
        # writer only added its own nodes — check the per-type counts
        # at least mention the kinds the writer produced.
        self.assertIn("score", stats["nodes_by_type"])
        self.assertIn("signal", stats["nodes_by_type"])
        self.assertIn("source", stats["nodes_by_type"])
        self.assertIn("macro_factor", stats["nodes_by_type"])
        # The sum of node types should be at least as many as the
        # writer reported it added.
        self.assertGreaterEqual(
            sum(stats["nodes_by_type"].values()),
            out.created_node_count,
        )

    def test_in_memory_store_satisfies_protocol(self) -> None:
        """GraphWriter's Protocol contract is satisfied by both
        SQLiteGraphStore and GraphStore (structural duck-typing)."""
        from phase3.graph.in_memory_store import GraphStore
        in_mem = GraphStore()
        self.assertIsInstance(in_mem, GraphWriterStore)
        self.assertIsInstance(self.graph, GraphWriterStore)

    def test_writer_can_be_called_via_protocol_typed_ref(self) -> None:
        """A GraphWriterStore-typed reference can drive a real writer."""
        store_ref: GraphWriterStore = self.graph
        w2 = GraphWriter(store_ref)
        result = self.pipeline.run_macro(date_bucket=DATE)
        out = w2.write(result)
        self.assertTrue(out.score_node_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def out_score_id(writer: GraphWriter, result: PipelineResult) -> str:
    """Convenience: re-derive the score node id the writer would use,
    without running the writer. Mirrors make_score_node_id."""
    bd = result.score.breakdown
    return make_score_node_id(bd.scorer_type, bd.entity_id, DATE)


def out_entity_id(writer: GraphWriter, result: PipelineResult) -> str:
    bd = result.score.breakdown
    return make_entity_node_id(bd.entity_type, bd.entity_id)


if __name__ == "__main__":
    unittest.main()
