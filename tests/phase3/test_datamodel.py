"""Tests for Phase 3A data model dataclasses.

Covers: Signal, Evidence, GraphNode, ScoreBreakdown construction and
round-trip serialization, frozen/immutable dataclass behavior.

These are stdlib-only unit tests — no fixtures, no network.
"""
from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, timezone

from phase3.datamodel.evidence import Evidence, make_evidence_id
from phase3.datamodel.graph import (
    DEFAULT_GRAPH_CONFIG,
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    make_graph_edge_id,
)
from phase3.datamodel.scores import (
    CrossLayerAdjustment,
    DimensionResult,
    ScoreBreakdown,
    SubIndicatorResult,
    WeightedFactor,
)
from phase3.datamodel.scores_company import (
    COMPANY_DIMENSIONS,
    COMPANY_SCORER_TYPE,
    COMPANY_SCORER_VERSION,
    CompanyScore,
)
from phase3.datamodel.scores_industry import (
    INDUSTRY_DIMENSIONS,
    INDUSTRY_SCORER_TYPE,
    IndustryScore,
)
from phase3.datamodel.scores_macro import (
    MACRO_DIMENSIONS,
    MACRO_ENTITY_ID,
    MACRO_ENTITY_TYPE,
    MACRO_SCORER_TYPE,
    MacroScore,
)
from phase3.datamodel.signals import (
    DEFAULT_DECAY_CONFIG,
    Direction,
    Signal,
    SignalSource,
    make_signal_id,
)
from phase3.datamodel._version import (
    MACRO_SCORER_VERSION,
    SCHEMA_VERSION,
)


def _ts() -> datetime:
    return datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def _make_signal(
    signal_id: str = "sig-test-1",
    entity_type: str = "company",
    entity_id: str = "2330",
    signal_type: str = "pe_ratio",
    value: float = 22.5,
    unit: str = "ratio",
    direction: Direction = "bullish",
    timestamp: datetime | None = None,
    source: SignalSource | None = None,
    date_bucket: str = "2026-07-08",
    metadata: dict | None = None,
) -> Signal:
    return Signal(
        signal_id=signal_id,
        entity_type=entity_type,
        entity_id=entity_id,
        signal_type=signal_type,
        value=value,
        unit=unit,
        direction=direction,
        timestamp=timestamp or _ts(),
        source=source or SignalSource(source_id="yfinance.2330", source_type="yfinance"),
        date_bucket=date_bucket,
        metadata=metadata if metadata is not None else {},
    )


class TestSignalConstruction(unittest.TestCase):
    def test_required_fields(self):
        s = _make_signal()
        self.assertEqual(s.signal_id, "sig-test-1")
        self.assertEqual(s.entity_type, "company")
        self.assertEqual(s.entity_id, "2330")
        self.assertEqual(s.signal_type, "pe_ratio")
        self.assertEqual(s.value, 22.5)
        self.assertEqual(s.unit, "ratio")
        self.assertEqual(s.direction, "bullish")
        self.assertEqual(s.schema_version, SCHEMA_VERSION)

    def test_make_signal_id_is_deterministic(self):
        a = make_signal_id("yfinance", "2330", "pe_ratio", "2026-07-08")
        b = make_signal_id("yfinance", "2330", "pe_ratio", "2026-07-08")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_make_signal_id_differs_on_inputs(self):
        a = make_signal_id("yfinance", "2330", "pe_ratio", "2026-07-08")
        b = make_signal_id("yfinance", "2330", "pe_ratio", "2026-07-09")
        c = make_signal_id("yfinance", "2454", "pe_ratio", "2026-07-08")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_default_metadata_is_independent(self):
        # Default factory must produce separate dicts, not shared mutable.
        s1 = _make_signal()
        s2 = _make_signal()
        s1.metadata["k"] = "v"
        self.assertNotIn("k", s2.metadata)

    def test_signal_is_frozen(self):
        s = _make_signal()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s.value = 99.0  # type: ignore[misc]


class TestSignalRoundTrip(unittest.TestCase):
    def test_to_from_dict_round_trip(self):
        s = _make_signal(metadata={"extra": "data"})
        d = s.to_dict()
        self.assertIn("source", d)
        self.assertEqual(d["value"], 22.5)
        # Round-trip
        s2 = Signal.from_dict(d)
        self.assertEqual(s2.signal_id, s.signal_id)
        self.assertEqual(s2.entity_id, s.entity_id)
        self.assertEqual(s2.value, s.value)
        self.assertEqual(s2.unit, s.unit)
        self.assertEqual(s2.direction, s.direction)
        self.assertEqual(s2.timestamp, s.timestamp)
        self.assertEqual(s2.source.source_id, s.source.source_id)
        self.assertEqual(s2.source.source_type, s.source.source_type)
        self.assertEqual(s2.metadata, {"extra": "data"})


class TestEvidenceConstruction(unittest.TestCase):
    def test_required_fields(self):
        ev = Evidence(
            evidence_id="abc123",
            source_type="rss",
            source_ref="cnyes-2330-1",
            raw_value="0.6",
            description="positive news",
            timestamp=_ts(),
        )
        self.assertEqual(ev.evidence_id, "abc123")
        self.assertEqual(ev.weight, None)
        self.assertEqual(ev.metadata, {})

    def test_make_evidence_id_is_deterministic(self):
        a = make_evidence_id("rss", "cnyes-2330-1", "0.6")
        b = make_evidence_id("rss", "cnyes-2330-1", "0.6")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_evidence_is_frozen(self):
        ev = Evidence(
            evidence_id="abc", source_type="rss", source_ref="r",
            raw_value="0.5", description="d", timestamp=_ts(),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ev.evidence_id = "xyz"  # type: ignore[misc]

    def test_evidence_to_from_dict_round_trip(self):
        ev = Evidence(
            evidence_id="ev-1", source_type="rss", source_ref="r",
            raw_value="0.5", description="d", timestamp=_ts(),
            weight=0.07, metadata={"k": "v"},
        )
        d = ev.to_dict()
        ev2 = Evidence.from_dict(d)
        self.assertEqual(ev2.evidence_id, ev.evidence_id)
        self.assertEqual(ev2.weight, ev.weight)
        self.assertEqual(ev2.metadata, ev.metadata)


class TestGraphNodeConstruction(unittest.TestCase):
    def test_required_fields(self):
        n = GraphNode(node_id="company:2330", node_type=NodeType.COMPANY, label="台積電")
        self.assertEqual(n.node_id, "company:2330")
        self.assertEqual(n.node_type, NodeType.COMPANY)
        self.assertEqual(n.label, "台積電")
        self.assertEqual(n.metadata, {})
        self.assertEqual(n.tags, [])

    def test_node_is_frozen(self):
        n = GraphNode(node_id="x", node_type=NodeType.COMPANY, label="L")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            n.label = "new"  # type: ignore[misc]

    def test_node_to_from_dict_round_trip(self):
        n = GraphNode(
            node_id="company:2330", node_type=NodeType.COMPANY, label="台積電",
            metadata={"sector": "半導體"}, tags=["taiwan", "semiconductor"],
        )
        d = n.to_dict()
        self.assertEqual(d["node_type"], "company")
        n2 = GraphNode.from_dict(d)
        self.assertEqual(n2.node_id, n.node_id)
        self.assertEqual(n2.node_type, NodeType.COMPANY)
        self.assertEqual(n2.label, n.label)
        self.assertEqual(n2.metadata, n.metadata)
        self.assertEqual(n2.tags, n.tags)

    def test_make_graph_edge_id_is_deterministic(self):
        a = make_graph_edge_id(EdgeType.MEMBER_OF, "company:2330", "industry:半導體")
        b = make_graph_edge_id(EdgeType.MEMBER_OF, "company:2330", "industry:半導體")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_make_graph_edge_id_differs_by_type_or_endpoints(self):
        a = make_graph_edge_id(EdgeType.MEMBER_OF, "company:2330", "industry:半導體")
        b = make_graph_edge_id(EdgeType.EXPOSED_TO, "company:2330", "industry:半導體")
        c = make_graph_edge_id(EdgeType.MEMBER_OF, "company:2454", "industry:半導體")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_default_graph_config_has_expected_keys(self):
        for k in ("max_traversal_depth", "default_shortest_path_algorithm",
                  "evidence_tracer_max_hops"):
            self.assertIn(k, DEFAULT_GRAPH_CONFIG)


class TestGraphEdgeConstruction(unittest.TestCase):
    def test_edge_construction(self):
        e = GraphEdge(
            edge_id="abc123",
            edge_type=EdgeType.MEMBER_OF,
            from_node_id="company:2330",
            to_node_id="industry:半導體",
            weight=0.5,
        )
        self.assertEqual(e.edge_type, EdgeType.MEMBER_OF)
        self.assertEqual(e.weight, 0.5)

    def test_edge_is_frozen(self):
        e = GraphEdge(
            edge_id="e1", edge_type=EdgeType.MEMBER_OF,
            from_node_id="a", to_node_id="b",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            e.weight = 0.9  # type: ignore[misc]

    def test_edge_round_trip(self):
        e = GraphEdge(
            edge_id="e1", edge_type=EdgeType.CONTRIBUTES_TO,
            from_node_id="signal:x", to_node_id="score:y",
            weight=None, metadata={"k": "v"},
        )
        d = e.to_dict()
        e2 = GraphEdge.from_dict(d)
        self.assertEqual(e2.edge_id, e.edge_id)
        self.assertEqual(e2.edge_type, EdgeType.CONTRIBUTES_TO)
        self.assertEqual(e2.weight, None)
        self.assertEqual(e2.metadata, {"k": "v"})


class TestScoreBreakdownConstruction(unittest.TestCase):
    def _make_breakdown(self, score=10.0, confidence=0.8):
        return ScoreBreakdown(
            scorer_type=MACRO_SCORER_TYPE,
            entity_type=MACRO_ENTITY_TYPE,
            entity_id=MACRO_ENTITY_ID,
            score=score,
            confidence=confidence,
            dimensions=[],
            overall_evidence=[],
            timestamp=_ts(),
            config_hash="test-hash",
            valid_until=_ts(),
        )

    def test_required_fields(self):
        b = self._make_breakdown()
        self.assertEqual(b.scorer_type, "macro")
        self.assertEqual(b.entity_id, "global")
        self.assertEqual(b.schema_version, SCHEMA_VERSION)
        self.assertEqual(b.cross_layer_adjustments, [])

    def test_score_breakdown_is_frozen(self):
        b = self._make_breakdown()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            b.score = 99.0  # type: ignore[misc]

    def test_score_breakdown_round_trip(self):
        b = self._make_breakdown(score=12.5, confidence=0.65)
        d = b.to_dict()
        # required keys
        for k in ("scorer_type", "entity_id", "score", "confidence",
                  "dimensions", "overall_evidence", "timestamp",
                  "config_hash", "valid_until", "schema_version"):
            self.assertIn(k, d)
        b2 = ScoreBreakdown.from_dict(d)
        self.assertEqual(b2.scorer_type, b.scorer_type)
        self.assertEqual(b2.entity_id, b.entity_id)
        self.assertEqual(b2.score, b.score)
        self.assertEqual(b2.confidence, b.confidence)
        self.assertEqual(b2.timestamp, b.timestamp)


class TestScoreBreakdownWithDimensions(unittest.TestCase):
    def test_score_breakdown_with_dimensions(self):
        ev = Evidence(
            evidence_id="e1", source_type="rss", source_ref="r",
            raw_value="0.5", description="d", timestamp=_ts(),
        )
        wf = WeightedFactor(
            name="pmi", raw_value=51.0, raw_unit="index",
            sub_score=20.0, sub_weight=0.5, signed_score=10.0,
            transformation="threshold", source="rss_wealth", source_ref="macro.pmi",
            evidence=[ev],
        )
        si = SubIndicatorResult(
            name="pmi", raw_value=51.0, raw_unit="index", sub_score=20.0,
            transformation="threshold", source="rss_wealth", source_ref="macro.pmi",
            evidence=[ev],
        )
        dim = DimensionResult(
            name="economic", score=20.0, sub_indicators=[si], weight=0.2,
            confidence=0.8, factors=[wf], evidence=[ev],
        )
        b = ScoreBreakdown(
            scorer_type=MACRO_SCORER_TYPE,
            entity_type=MACRO_ENTITY_TYPE, entity_id=MACRO_ENTITY_ID,
            score=4.0, confidence=0.8,
            dimensions=[dim], overall_evidence=[ev],
            timestamp=_ts(), config_hash="h", valid_until=_ts(),
        )
        self.assertEqual(len(b.dimensions), 1)
        self.assertEqual(b.dimensions[0].name, "economic")
        self.assertEqual(b.dimensions[0].sub_indicators[0].name, "pmi")
        d = b.to_dict()
        self.assertEqual(len(d["dimensions"]), 1)
        self.assertEqual(d["dimensions"][0]["name"], "economic")
        b2 = ScoreBreakdown.from_dict(d)
        self.assertEqual(len(b2.dimensions), 1)
        self.assertEqual(b2.dimensions[0].sub_indicators[0].name, "pmi")


class TestCrossLayerAdjustment(unittest.TestCase):
    def test_construction(self):
        a = CrossLayerAdjustment(
            from_scorer="macro",
            from_score_id="hash-1",
            to_scorer="company",
            to_score_id="2330",
            adjustment=2.5,
            reason="liquidity +35 × sensitivity 0.05",
        )
        self.assertEqual(a.from_scorer, "macro")
        self.assertEqual(a.adjustment, 2.5)

    def test_is_frozen(self):
        a = CrossLayerAdjustment(
            from_scorer="macro", from_score_id="h",
            to_scorer="company", to_score_id="c",
            adjustment=1.0, reason="r",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.adjustment = 9.0  # type: ignore[misc]

    def test_round_trip(self):
        a = CrossLayerAdjustment(
            from_scorer="industry", from_score_id="h2",
            to_scorer="company", to_score_id="2330",
            adjustment=-1.5, reason="industry negative",
        )
        d = a.to_dict()
        a2 = CrossLayerAdjustment.from_dict(d)
        self.assertEqual(a2.from_scorer, "industry")
        self.assertEqual(a2.adjustment, -1.5)


class TestScoreRoundTrip(unittest.TestCase):
    def test_macro_score_round_trip(self):
        b = ScoreBreakdown(
            scorer_type=MACRO_SCORER_TYPE, entity_type=MACRO_ENTITY_TYPE,
            entity_id=MACRO_ENTITY_ID, score=12.0, confidence=0.7,
            dimensions=[], overall_evidence=[],
            timestamp=_ts(), config_hash="h1", valid_until=_ts(),
        )
        ms = MacroScore(breakdown=b)
        d = ms.to_dict()
        self.assertEqual(d["scorer_type"], "macro")
        self.assertEqual(d["scorer_version"], MACRO_SCORER_VERSION)
        ms2 = MacroScore.from_dict(d)
        self.assertEqual(ms2.score, 12.0)
        self.assertEqual(ms2.entity_id, MACRO_ENTITY_ID)
        self.assertEqual(ms2.scorer_version, MACRO_SCORER_VERSION)

    def test_industry_score_round_trip(self):
        b = ScoreBreakdown(
            scorer_type=INDUSTRY_SCORER_TYPE, entity_type="industry",
            entity_id="半導體", score=5.0, confidence=0.6,
            dimensions=[], overall_evidence=[],
            timestamp=_ts(), config_hash="h2", valid_until=_ts(),
        )
        isc = IndustryScore(
            breakdown=b, industry_name="半導體", constituent_count=8,
        )
        d = isc.to_dict()
        self.assertEqual(d["scorer_type"], "industry")
        self.assertEqual(d["industry_name"], "半導體")
        self.assertEqual(d["constituent_count"], 8)
        isc2 = IndustryScore.from_dict(d)
        self.assertEqual(isc2.industry_name, "半導體")
        self.assertEqual(isc2.constituent_count, 8)
        self.assertEqual(isc2.score, 5.0)

    def test_company_score_round_trip(self):
        b = ScoreBreakdown(
            scorer_type=COMPANY_SCORER_TYPE, entity_type="company",
            entity_id="2330", score=10.0, confidence=0.7,
            dimensions=[], overall_evidence=[],
            timestamp=_ts(), config_hash="h3", valid_until=_ts(),
        )
        cs = CompanyScore(
            breakdown=b, code="2330", name="台積電", sector="半導體",
            raw_score=10.0, macro_adjustment=0.5, industry_adjustment=0.3,
        )
        d = cs.to_dict()
        self.assertEqual(d["scorer_type"], "company")
        self.assertEqual(d["code"], "2330")
        self.assertEqual(d["sector"], "半導體")
        self.assertEqual(d["raw_score"], 10.0)
        self.assertEqual(d["macro_adjustment"], 0.5)
        cs2 = CompanyScore.from_dict(d)
        self.assertEqual(cs2.code, "2330")
        self.assertEqual(cs2.sector, "半導體")
        self.assertEqual(cs2.score, 10.0)
        self.assertEqual(cs2.raw_score, 10.0)


class TestDimensionTuples(unittest.TestCase):
    def test_macro_dimensions_count_and_order(self):
        self.assertEqual(len(MACRO_DIMENSIONS), 6)
        self.assertEqual(
            MACRO_DIMENSIONS,
            ("economic", "monetary", "inflation", "rates", "liquidity", "geopolitics"),
        )

    def test_industry_dimensions_count_and_order(self):
        self.assertEqual(len(INDUSTRY_DIMENSIONS), 6)
        self.assertEqual(
            INDUSTRY_DIMENSIONS,
            ("rotation", "relative_strength", "cyclicality",
             "macro_sensitivity", "industry_news", "capital_flow"),
        )

    def test_company_dimensions_count_and_order(self):
        self.assertEqual(len(COMPANY_DIMENSIONS), 7)
        self.assertEqual(
            COMPANY_DIMENSIONS,
            ("financial_quality", "growth", "profitability", "valuation",
             "momentum", "risk", "news_sentiment"),
        )


class TestDefaultDecayConfig(unittest.TestCase):
    def test_default_decay_has_required_keys(self):
        for k in ("fed_rate", "news_headline", "pe_ratio", "default"):
            self.assertIn(k, DEFAULT_DECAY_CONFIG)
        # each rule must have a function and a relevant time parameter
        for sig_type, rule in DEFAULT_DECAY_CONFIG.items():
            self.assertIn("function", rule, f"{sig_type} missing function")
            if rule["function"] == "step":
                self.assertIn(
                    "step_threshold_days", rule,
                    f"{sig_type} (step) missing step_threshold_days",
                )
            else:
                self.assertIn(
                    "half_life_days", rule,
                    f"{sig_type} missing half_life_days",
                )

    def test_all_decay_functions_are_known(self):
        from phase3.signals.decay import DECAY_FUNCTIONS
        for sig_type, rule in DEFAULT_DECAY_CONFIG.items():
            self.assertIn(
                rule["function"], DECAY_FUNCTIONS,
                f"{sig_type} uses unknown function {rule['function']!r}",
            )


if __name__ == "__main__":
    unittest.main()
