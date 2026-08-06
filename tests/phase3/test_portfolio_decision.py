"""Phase 5 M4-S2 — Portfolio Decision Engine Tests.

Tests for ``PortfolioDecisionEngine.run()`` score-weighted allocation
policy, ``AllocationPolicyConfig``, evidence chain extraction,
determinism, edge cases, and round-trip G-P5-4.

Test groups:
- SW1-SW10: Score-weighted policy correctness (10 tests)
- EC1-EC4: Evidence chain traceability (4 tests)
- DET1-DET3: Determinism (3 tests)
- CFG1-CFG6: AllocationPolicyConfig (6 tests)
- RT1-RT2: Round-trip G-P5-4 (2 tests)
- ANN1-ANN2: Annotation tightening (2 tests)
- EDGE1-EDGE5: Additional edge cases (5 tests)

Total: 32 tests (≥30 required).
"""
from __future__ import annotations

import inspect
import json
import math
import unittest
from pathlib import Path
from typing import Any

from phase3.portfolio.decision import (
    AllocationPolicyConfig,
    PortfolioDecision,
    PortfolioDecisionEngine,
)
from phase3.portfolio.domain import (
    EntityId,
    Portfolio,
    PortfolioId,
    Position,
    PositionId,
    Quantity,
    Weight,
)
from phase3.portfolio.allocation import Allocation
from phase3.pipeline.scoring_pipeline import PipelineResult, PipelineRunReport
from phase3.datamodel.scores import ScoreBreakdown

# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pipeline_export_sample.json"

_FP_REL_TOL = 1e-9
_FP_ABS_TOL = 1e-12


def _make_score_breakdown(
    scorer_type: str = "company",
    entity_id: str = "company:TW:2330",
    score: float = 80.0,
    confidence: float = 0.9,
) -> ScoreBreakdown:
    """Build a minimal ScoreBreakdown for testing."""
    from datetime import datetime
    return ScoreBreakdown(
        scorer_type=scorer_type,
        entity_type=scorer_type,
        entity_id=entity_id,
        score=score,
        confidence=confidence,
        dimensions=[],
        overall_evidence=[],
        timestamp=datetime(2026, 8, 5, 0, 0, 0),
        config_hash="test_config_hash",
        valid_until=datetime(2026, 8, 6, 0, 0, 0),
        cross_layer_adjustments=[],
        schema_version="2.0",
    )


class _MockScore:
    """Minimal mock score object with a breakdown attribute."""
    def __init__(self, breakdown: ScoreBreakdown) -> None:
        self.breakdown = breakdown


def _make_pipeline_result(
    entity_id: str,
    score: float,
    confidence: float,
    evidence_signal_ids: tuple[str, ...] = (),
    scorer_type: str = "company",
) -> PipelineResult:
    """Build a PipelineResult with a mock score wrapping ScoreBreakdown."""
    breakdown = _make_score_breakdown(
        scorer_type=scorer_type,
        entity_id=entity_id,
        score=score,
        confidence=confidence,
    )
    return PipelineResult(
        score=_MockScore(breakdown),
        input_bundle=None,
        evidence_signal_ids=evidence_signal_ids,
        snapshot_id=1,
        warnings=(),
        metadata={"scorer_type": scorer_type, "entity_id": entity_id},
    )


def _make_portfolio(
    entity_ids: list[str],
    name: str = "Test Portfolio",
    portfolio_id: str = "test-portfolio-001",
) -> Portfolio:
    """Build a Portfolio with positions for the given entity IDs."""
    positions = []
    for i, eid in enumerate(entity_ids):
        positions.append(
            Position(
                position_id=PositionId(f"pos-{i:04d}"),
                entity_id=EntityId(eid),
                weight=Weight(0.0),
                quantity=Quantity(0),
            )
        )
    return Portfolio(
        portfolio_id=PortfolioId(portfolio_id),
        name=name,
        positions=tuple(positions),
    )


def _load_fixture_report() -> PipelineRunReport:
    """Load the pipeline_export_sample.json fixture into a PipelineRunReport."""
    with open(FIXTURE_PATH, "r") as f:
        data = json.load(f)
    return _deserialize_report(data)


def _deserialize_report(data: dict[str, Any]) -> PipelineRunReport:
    """Deserialize a JSON dict into a PipelineRunReport."""
    macro = None
    if data.get("macro") is not None:
        macro = _deserialize_pipeline_result(data["macro"])
    industries = tuple(
        _deserialize_pipeline_result(r) for r in data.get("industries", [])
    )
    companies = tuple(
        _deserialize_pipeline_result(r) for r in data.get("companies", [])
    )
    return PipelineRunReport(
        macro=macro,
        industries=industries,
        companies=companies,
        warnings=tuple(data.get("warnings", [])),
    )


def _deserialize_pipeline_result(data: dict[str, Any]) -> PipelineResult:
    """Deserialize a JSON dict into a PipelineResult."""
    score_data = data.get("score", {})
    breakdown_data = score_data.get("breakdown") if score_data else None
    if breakdown_data:
        from datetime import datetime
        breakdown = ScoreBreakdown(
            scorer_type=breakdown_data["scorer_type"],
            entity_type=breakdown_data["entity_type"],
            entity_id=breakdown_data["entity_id"],
            score=float(breakdown_data["score"]),
            confidence=float(breakdown_data["confidence"]),
            dimensions=[],
            overall_evidence=[],
            timestamp=datetime.fromisoformat(breakdown_data["timestamp"]),
            config_hash=breakdown_data["config_hash"],
            valid_until=datetime.fromisoformat(breakdown_data["valid_until"]),
            cross_layer_adjustments=[],
            schema_version=breakdown_data.get("schema_version", "2.0"),
        )
        score = _MockScore(breakdown)
    else:
        score = None
    return PipelineResult(
        score=score,
        input_bundle=None,
        evidence_signal_ids=tuple(data.get("evidence_signal_ids", [])),
        snapshot_id=data.get("snapshot_id"),
        warnings=tuple(data.get("warnings", [])),
        metadata=dict(data.get("metadata", {})),
    )


# --------------------------------------------------------------------------- #
# SW1-SW10: Score-weighted policy correctness
# --------------------------------------------------------------------------- #


class TestScoreWeightedPolicy(unittest.TestCase):
    """SW1-SW10: score-weighted allocation policy correctness."""

    def test_sw1_basic_score_weighted(self):
        """SW1: 3 companies +80, +60, +10 → weights 80/150, 60/150, 10/150."""
        eids = ["company:TW:2330", "company:TW:2317", "company:TW:2454"]
        results = tuple(
            _make_pipeline_result(eid, s, 1.0)
            for eid, s in zip(eids, [80.0, 60.0, 10.0])
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            per_position_cap=1.0,  # no cap for this test
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        weights = [p.weight.value for p in decision.allocation.positions]
        self.assertAlmostEqual(weights[0], 80.0 / 150.0, places=6)
        self.assertAlmostEqual(weights[1], 60.0 / 150.0, places=6)
        self.assertAlmostEqual(weights[2], 10.0 / 150.0, places=6)

    def test_sw2_confidence_weighted(self):
        """SW2: confidence affects weight. +80(c=0.9), +60(c=0.5), +10(c=1.0)."""
        eids = ["c1", "c2", "c3"]
        results = tuple(
            _make_pipeline_result(eid, s, c)
            for eid, s, c in zip(eids, [80.0, 60.0, 10.0], [0.9, 0.5, 1.0])
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            per_position_cap=1.0,
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        # 72, 30, 10 → total 112
        weights = [p.weight.value for p in decision.allocation.positions]
        self.assertAlmostEqual(weights[0], 72.0 / 112.0, places=6)
        self.assertAlmostEqual(weights[1], 30.0 / 112.0, places=6)
        self.assertAlmostEqual(weights[2], 10.0 / 112.0, places=6)

    def test_sw3_per_position_cap(self):
        """SW3: single company +90 → weight capped at per_position_cap=0.25."""
        eids = ["c1"]
        results = (_make_pipeline_result("c1", 90.0, 1.0),)
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            per_position_cap=0.25,
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 1)
        self.assertAlmostEqual(decision.allocation.positions[0].weight.value, 0.25, places=6)

    def test_sw4_all_equal_scores(self):
        """SW4: 3 companies +50, +50, +50 → equal weights 1/3 each."""
        eids = ["c1", "c2", "c3"]
        results = tuple(
            _make_pipeline_result(eid, 50.0, 1.0) for eid in eids
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            per_position_cap=1.0,
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        weights = [p.weight.value for p in decision.allocation.positions]
        for w in weights:
            self.assertAlmostEqual(w, 1.0 / 3.0, places=6)

    def test_sw5_macro_only_no_companies(self):
        """SW5: macro=+60, companies=() → empty allocation (cash fallback)."""
        macro = _make_pipeline_result("macro:global", 60.0, 0.8, scorer_type="macro")
        report = PipelineRunReport(macro=macro, companies=(), industries=())
        portfolio = _make_portfolio([])
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 0)

    def test_sw6_macro_none(self):
        """SW6: macro=None, 3 companies → normal allocation (macro ignored)."""
        eids = ["c1", "c2", "c3"]
        results = tuple(
            _make_pipeline_result(eid, s, 1.0)
            for eid, s in zip(eids, [80.0, 60.0, 10.0])
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            per_position_cap=1.0,
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 3)

    def test_sw7_no_matching_scores(self):
        """SW7: 3 companies scored, 0 matched portfolio positions → empty allocation."""
        eids = ["c1", "c2", "c3"]
        results = tuple(
            _make_pipeline_result(eid, 50.0, 1.0) for eid in eids
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(["unmatched1", "unmatched2"])
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 0)

    def test_sw8_all_negative_scores_cash(self):
        """SW8: all-negative scores with cash handling → empty allocation."""
        eids = ["c1", "c2", "c3"]
        results = tuple(
            _make_pipeline_result(eid, s, 1.0)
            for eid, s in zip(eids, [-50.0, -30.0, -20.0])
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            negative_score_handling="cash",
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 0)

    def test_sw9_out_of_range_clamp(self):
        """SW9: score +200 → clamped to +100 → weight 1.0 capped at 0.25."""
        eids = ["c1"]
        results = (_make_pipeline_result("c1", 200.0, 1.0),)
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            per_position_cap=0.25,
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 1)
        self.assertAlmostEqual(decision.allocation.positions[0].weight.value, 0.25, places=6)

    def test_sw10_empty_portfolio(self):
        """SW10: 3 companies, 0 positions → empty allocation, no exception."""
        eids = ["c1", "c2", "c3"]
        results = tuple(
            _make_pipeline_result(eid, 50.0, 1.0) for eid in eids
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio([])
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 0)


# --------------------------------------------------------------------------- #
# EC1-EC4: Evidence chain traceability
# --------------------------------------------------------------------------- #


class TestEvidenceChain(unittest.TestCase):
    """EC1-EC4: evidence chain extraction."""

    def test_ec1_evidence_refs_subset_of_signal_ids(self):
        """EC1: evidence_refs ⊆ source evidence_signal_ids."""
        eids = ["c1", "c2"]
        results = (
            _make_pipeline_result("c1", 80.0, 1.0, ("sig_001", "sig_002")),
            _make_pipeline_result("c2", 60.0, 1.0, ("sig_003",)),
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        source_ids = {"sig_001", "sig_002", "sig_003"}
        for ref in decision.evidence_refs:
            self.assertIn(ref, source_ids)

    def test_ec2_score_node_id_from_handles(self):
        """EC2: score_node_id from EvidenceQueryHandle appears in evidence_refs."""
        from phase3.pipeline.intelligence_pipeline import EvidenceQueryHandle
        eids = ["c1"]
        results = (_make_pipeline_result("c1", 80.0, 1.0, ("sig_001",)),)
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        handles = {"c1": EvidenceQueryHandle(
            scorer_type="company", entity_id="c1", date_bucket="2026-08-05",
            score_node_id="score:company:c1:2026-08-05",
            upstream=None, downstream=None, evidence_summary=None,
            inputs_payload=None,
        )}
        engine = PortfolioDecisionEngine()
        decision = engine.run_with_handles(report, portfolio, config, handles)
        self.assertIn("score:company:c1:2026-08-05", decision.evidence_refs)

    def test_ec3_no_handles_fallback_to_signal_ids(self):
        """EC3: without handles, evidence_refs == deduplicated signal_ids."""
        eids = ["c1", "c2"]
        results = (
            _make_pipeline_result("c1", 80.0, 1.0, ("sig_001", "sig_002")),
            _make_pipeline_result("c2", 60.0, 1.0, ("sig_003",)),
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        expected = {"sig_001", "sig_002", "sig_003"}
        self.assertEqual(set(decision.evidence_refs), expected)

    def test_ec4_deduplication(self):
        """EC4: duplicate signal_ids across results → deduplicated evidence_refs."""
        eids = ["c1", "c2"]
        results = (
            _make_pipeline_result("c1", 80.0, 1.0, ("sig_001", "sig_002")),
            _make_pipeline_result("c2", 60.0, 1.0, ("sig_002", "sig_003")),
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.evidence_refs), len(set(decision.evidence_refs)))


# --------------------------------------------------------------------------- #
# DET1-DET3: Determinism
# --------------------------------------------------------------------------- #


class TestDeterminism(unittest.TestCase):
    """DET1-DET3: deterministic computation."""

    def test_det1_replay_byte_identical(self):
        """DET1: same inputs → byte-identical to_dict() output."""
        eids = ["c1", "c2", "c3"]
        results = tuple(
            _make_pipeline_result(eid, s, c)
            for eid, s, c in zip(eids, [80.0, 60.0, 10.0], [0.9, 0.5, 1.0])
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        engine = PortfolioDecisionEngine()
        d1 = engine.run(report, portfolio, config)
        d2 = engine.run(report, portfolio, config)
        self.assertEqual(d1.to_dict(), d2.to_dict())

    def test_det2_different_generated_at_different_decision(self):
        """DET2: different generated_at → different to_dict()."""
        eids = ["c1"]
        results = (_make_pipeline_result("c1", 80.0, 1.0),)
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config1 = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        config2 = AllocationPolicyConfig(generated_at="2026-08-05T01:00:00Z")
        engine = PortfolioDecisionEngine()
        d1 = engine.run(report, portfolio, config1)
        d2 = engine.run(report, portfolio, config2)
        self.assertNotEqual(d1.to_dict(), d2.to_dict())

    def test_det3_config_round_trip(self):
        """DET3: AllocationPolicyConfig from_dict(to_dict(x)) == x byte-identical."""
        config = AllocationPolicyConfig(
            policy_type="score_weighted",
            target_total=0.8,
            per_position_cap=0.20,
            generated_at="2026-08-05T00:00:00Z",
            negative_score_handling="equal_weight",
            out_of_range_score_handling="raise",
            decision_id="test-decision-001",
            rationale_template="test template",
        )
        d1 = config.to_dict()
        config2 = AllocationPolicyConfig.from_dict(d1)
        d2 = config2.to_dict()
        self.assertEqual(d1, d2)


# --------------------------------------------------------------------------- #
# CFG1-CFG6: AllocationPolicyConfig
# --------------------------------------------------------------------------- #


class TestAllocationPolicyConfig(unittest.TestCase):
    """CFG1-CFG6: AllocationPolicyConfig invariants and serialization."""

    def test_cfg1_defaults(self):
        """CFG1: default values are correct."""
        config = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        self.assertEqual(config.policy_type, "score_weighted")
        self.assertAlmostEqual(config.target_total, 1.0)
        self.assertAlmostEqual(config.per_position_cap, 0.25)
        self.assertEqual(config.negative_score_handling, "cash")
        self.assertEqual(config.out_of_range_score_handling, "clamp")
        self.assertEqual(config.decision_id, "")

    def test_cfg2_invalid_policy_type(self):
        """CFG2: invalid policy_type raises ValueError."""
        with self.assertRaises(ValueError):
            AllocationPolicyConfig(policy_type="invalid", generated_at="t")

    def test_cfg3_invalid_target_total(self):
        """CFG3: target_total outside [0,1] raises ValueError."""
        with self.assertRaises(ValueError):
            AllocationPolicyConfig(target_total=1.5, generated_at="t")
        with self.assertRaises(ValueError):
            AllocationPolicyConfig(target_total=-0.1, generated_at="t")

    def test_cfg4_invalid_negative_handling(self):
        """CFG4: invalid negative_score_handling raises ValueError."""
        with self.assertRaises(ValueError):
            AllocationPolicyConfig(negative_score_handling="invalid", generated_at="t")

    def test_cfg5_invalid_out_of_range_handling(self):
        """CFG5: invalid out_of_range_score_handling raises ValueError."""
        with self.assertRaises(ValueError):
            AllocationPolicyConfig(out_of_range_score_handling="invalid", generated_at="t")

    def test_cfg6_config_hash_deterministic(self):
        """CFG6: same config → same config_hash (excluding decision_id and generated_at)."""
        c1 = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z", decision_id="d1")
        c2 = AllocationPolicyConfig(generated_at="2026-08-06T00:00:00Z", decision_id="d2")
        self.assertEqual(c1.config_hash, c2.config_hash)


# --------------------------------------------------------------------------- #
# RT1-RT2: Round-trip G-P5-4
# --------------------------------------------------------------------------- #


class TestRoundTrip(unittest.TestCase):
    """RT1-RT2: round-trip G-P5-4 gate tests."""

    def test_rt1_fixture_round_trip(self):
        """RT1: fixture → engine → PortfolioDecision with evidence refs back to source."""
        report = _load_fixture_report()
        eids = ["company:TW:2330", "company:TW:2317", "company:TW:2454",
                "industry:TW:semiconductor", "industry:TW:finance"]
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            per_position_cap=1.0,
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)

        # Assert evidence_refs ⊆ source evidence_signal_ids.
        source_ids: set[str] = set()
        for pr in report.companies + report.industries:
            source_ids.update(pr.evidence_signal_ids)
        if report.macro is not None:
            source_ids.update(report.macro.evidence_signal_ids)
        for ref in decision.evidence_refs:
            self.assertIn(ref, source_ids)

        # Assert allocation sums to target_total (within tolerance, allowing cap residual).
        total_w = sum(p.weight.value for p in decision.allocation.positions)
        self.assertLessEqual(total_w, config.target_total + _FP_ABS_TOL)

    def test_rt2_portfolio_decision_serialization_round_trip(self):
        """RT2: PortfolioDecision.to_dict() → from_dict() → to_dict() byte-identical."""
        eids = ["c1", "c2"]
        results = (
            _make_pipeline_result("c1", 80.0, 1.0, ("sig_001",)),
            _make_pipeline_result("c2", 60.0, 1.0, ("sig_002",)),
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        d1 = decision.to_dict()
        decision2 = PortfolioDecision.from_dict(d1)
        d2 = decision2.to_dict()
        self.assertEqual(d1, d2)


# --------------------------------------------------------------------------- #
# ANN1-ANN2: Annotation tightening
# --------------------------------------------------------------------------- #


class TestAnnotationTightening(unittest.TestCase):
    """ANN1-ANN2: run() signature uses concrete types."""

    def test_ann1_run_signature_concrete_types(self):
        """ANN1: inspect.signature(run) shows PipelineRunReport, Portfolio, AllocationPolicyConfig."""
        sig = inspect.signature(PortfolioDecisionEngine.run)
        params = sig.parameters
        self.assertIn("pipeline_report", params)
        self.assertIn("portfolio", params)
        self.assertIn("policy_config", params)
        # Check annotations (string form since annotations are forward refs).
        ann_report = str(params["pipeline_report"].annotation)
        ann_portfolio = str(params["portfolio"].annotation)
        ann_config = str(params["policy_config"].annotation)
        self.assertIn("PipelineRunReport", ann_report)
        self.assertIn("Portfolio", ann_portfolio)
        self.assertIn("AllocationPolicyConfig", ann_config)

    def test_ann2_return_type_portfolio_decision(self):
        """ANN2: return annotation is PortfolioDecision."""
        sig = inspect.signature(PortfolioDecisionEngine.run)
        ret_ann = str(sig.return_annotation)
        self.assertIn("PortfolioDecision", ret_ann)


# --------------------------------------------------------------------------- #
# EDGE1-EDGE5: Additional edge cases
# --------------------------------------------------------------------------- #


class TestEdgeCases(unittest.TestCase):
    """EDGE1-EDGE5: additional edge cases."""

    def test_edge1_all_negative_equal_weight_fallback(self):
        """EDGE1: all-negative with equal_weight → equal distribution among matched."""
        eids = ["c1", "c2", "c3"]
        results = tuple(
            _make_pipeline_result(eid, s, 1.0)
            for eid, s in zip(eids, [-50.0, -30.0, -20.0])
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            negative_score_handling="equal_weight",
            per_position_cap=1.0,
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 3)
        for p in decision.allocation.positions:
            self.assertAlmostEqual(p.weight.value, 1.0 / 3.0, places=6)

    def test_edge2_out_of_range_raise(self):
        """EDGE2: out-of-range score with 'raise' mode → ValueError."""
        eids = ["c1"]
        results = (_make_pipeline_result("c1", 200.0, 1.0),)
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            out_of_range_score_handling="raise",
        )
        engine = PortfolioDecisionEngine()
        with self.assertRaises(ValueError):
            engine.run(report, portfolio, config)

    def test_edge3_score_none_skipped(self):
        """EDGE3: PipelineResult with score=None → skipped, no crash."""
        result = PipelineResult(
            score=None,
            input_bundle=None,
            evidence_signal_ids=("sig_001",),
            snapshot_id=1,
            warnings=(),
            metadata={},
        )
        report = PipelineRunReport(macro=None, companies=(result,), industries=())
        portfolio = _make_portfolio(["c1"])
        config = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 0)

    def test_edge4_no_breakdown_skipped(self):
        """EDGE4: PipelineResult with score having no breakdown → skipped."""
        class _NoBreakdownScore:
            pass
        result = PipelineResult(
            score=_NoBreakdownScore(),
            input_bundle=None,
            evidence_signal_ids=("sig_001",),
            snapshot_id=1,
            warnings=(),
            metadata={},
        )
        report = PipelineRunReport(macro=None, companies=(result,), industries=())
        portfolio = _make_portfolio(["c1"])
        config = AllocationPolicyConfig(generated_at="2026-08-05T00:00:00Z")
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 0)

    def test_edge5_partial_match(self):
        """EDGE5: some positions match, some don't → matched allocated, unmatched dropped."""
        eids = ["c1", "c2", "unmatched"]
        results = (
            _make_pipeline_result("c1", 80.0, 1.0),
            _make_pipeline_result("c2", 60.0, 1.0),
        )
        report = PipelineRunReport(macro=None, companies=results, industries=())
        portfolio = _make_portfolio(eids)
        config = AllocationPolicyConfig(
            generated_at="2026-08-05T00:00:00Z",
            per_position_cap=1.0,
        )
        engine = PortfolioDecisionEngine()
        decision = engine.run(report, portfolio, config)
        self.assertEqual(len(decision.allocation.positions), 2)
        allocated_eids = {p.entity_id.value for p in decision.allocation.positions}
        self.assertEqual(allocated_eids, {"c1", "c2"})
        self.assertIn("Unmatched", decision.rationale)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    unittest.main()