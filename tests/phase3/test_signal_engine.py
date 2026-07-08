"""Tests for Phase 3A Signal Engine: weighting, confidence, aggregation.

Covers:
  - combine_weights: source × type × recency, with clipping
  - lookup_source_weight: precedence chain
  - resolve_decay_rule: per-signal-type + default fallback
  - compute_confidence: completeness × recency × consensus
  - SignalEngine.weight / weight_all: produces WeightedSignal
  - SignalAggregator: bucket consistency, direction consensus,
    aggregate_confidence, weighted_sum.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from phase3.datamodel.signals import Signal, SignalSource
from phase3.signals.aggregator import SignalAggregator, WeightedSignal
from phase3.signals.confidence import (
    completeness_factor,
    compute_confidence,
    consensus_factor,
    recency_factor,
)
from phase3.signals.engine import EngineConfig, SignalEngine
from phase3.signals.weighting import (
    combine_weights,
    lookup_source_weight,
    resolve_decay_rule,
)


# ---------- helpers ----------

def _ts(days_ago: float = 0.0) -> datetime:
    return datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc) - timedelta(days=days_ago)


def _make_signal(
    signal_type: str = "pe_ratio",
    value: float = 22.0,
    direction: str = "bullish",
    source_type: str = "yfinance",
    source_id: str = "yfinance.2330",
    entity_id: str = "2330",
    days_ago: float = 0.0,
    signal_id: str = "sig-test",
) -> Signal:
    return Signal(
        signal_id=signal_id,
        entity_type="company",
        entity_id=entity_id,
        signal_type=signal_type,
        value=value,
        unit="ratio",
        direction=direction,
        timestamp=_ts(days_ago),
        source=SignalSource(source_id=source_id, source_type=source_type),
        date_bucket="2026-07-08",
    )


# ---------- combine_weights ----------

class TestCombineWeights(unittest.TestCase):
    def test_perfect_inputs(self):
        wb = combine_weights(0.9, 0.8, 1.0)
        self.assertEqual(wb.source_weight, 0.9)
        self.assertEqual(wb.type_weight, 0.8)
        self.assertEqual(wb.recency_weight, 1.0)
        self.assertAlmostEqual(wb.final_weight, 0.72)

    def test_clipping_above_one(self):
        wb = combine_weights(1.2, 0.5, 0.5)
        # 1.2 clipped to 1.0, 0.5 × 0.5 = 0.25
        self.assertEqual(wb.source_weight, 1.0)
        self.assertAlmostEqual(wb.final_weight, 0.25)

    def test_clipping_below_zero(self):
        wb = combine_weights(-0.5, 0.5, 0.5)
        self.assertEqual(wb.source_weight, 0.0)
        self.assertEqual(wb.final_weight, 0.0)

    def test_zero_inputs(self):
        wb = combine_weights(0.0, 0.0, 0.0)
        self.assertEqual(wb.final_weight, 0.0)


# ---------- lookup_source_weight ----------

class TestLookupSourceWeight(unittest.TestCase):
    def test_empty_returns_default(self):
        self.assertEqual(lookup_source_weight({}, "yfinance", "pe_ratio"), 0.5)

    def test_source_weight_only(self):
        sw = {"yfinance": {"source_weight": 0.8}}
        self.assertAlmostEqual(lookup_source_weight(sw, "yfinance", "pe_ratio"), 0.8)

    def test_type_specific_overrides_source(self):
        sw = {
            "yfinance": {
                "source_weight": 0.5,
                "type_weights": {"pe_ratio": 0.9},
            }
        }
        self.assertAlmostEqual(lookup_source_weight(sw, "yfinance", "pe_ratio"), 0.9)

    def test_unknown_type_falls_back_to_source(self):
        sw = {
            "yfinance": {
                "source_weight": 0.7,
                "type_weights": {"pe_ratio": 0.9},
            }
        }
        # Asking for "roe" should fall back to source_weight=0.7
        self.assertAlmostEqual(lookup_source_weight(sw, "yfinance", "roe"), 0.7)

    def test_unknown_source_returns_default(self):
        sw = {"yfinance": {"source_weight": 0.8}}
        self.assertEqual(lookup_source_weight(sw, "t86", "foreign_net"), 0.5)

    def test_clipping_when_out_of_range(self):
        sw = {"yfinance": {"type_weights": {"pe_ratio": 1.5}}}
        # 1.5 clipped to 1.0
        self.assertEqual(lookup_source_weight(sw, "yfinance", "pe_ratio"), 1.0)


# ---------- resolve_decay_rule ----------

class TestResolveDecayRule(unittest.TestCase):
    def test_explicit_signal_type(self):
        cfg = {"custom": {"function": "linear", "half_life_days": 7.0}}
        rule = resolve_decay_rule("custom", cfg)
        self.assertEqual(rule["function"], "linear")
        self.assertEqual(rule["half_life_days"], 7.0)

    def test_fallback_to_default_config(self):
        # "fed_rate" lives in DEFAULT_DECAY_CONFIG
        rule = resolve_decay_rule("fed_rate", {})
        self.assertEqual(rule["function"], "linear")
        self.assertEqual(rule["half_life_days"], 14.0)

    def test_fallback_to_default_key(self):
        # "this_signal_does_not_exist" → "default" rule
        rule = resolve_decay_rule("this_signal_does_not_exist", {})
        self.assertEqual(rule["function"], "exponential")
        self.assertEqual(rule["half_life_days"], 7.0)

    def test_none_call_uses_default_config(self):
        # decay_config=None → DEFAULT_DECAY_CONFIG
        rule = resolve_decay_rule("news_headline", None)
        self.assertEqual(rule["function"], "exponential")
        self.assertEqual(rule["half_life_days"], 1.0)


# ---------- confidence ----------

class TestCompletenessFactor(unittest.TestCase):
    def test_full_completeness(self):
        self.assertEqual(completeness_factor(5, 5), 1.0)

    def test_zero_completeness(self):
        self.assertEqual(completeness_factor(0, 5), 0.0)

    def test_half_completeness(self):
        self.assertAlmostEqual(completeness_factor(5, 10), 0.5)

    def test_zero_expected_returns_one(self):
        # 0 expected → 1.0 (don't permanently zero a signal)
        self.assertEqual(completeness_factor(0, 0), 1.0)


class TestRecencyFactor(unittest.TestCase):
    def test_at_zero_age(self):
        self.assertAlmostEqual(recency_factor(0.0, 14.0), 1.0)

    def test_at_half_life(self):
        self.assertAlmostEqual(recency_factor(14.0, 14.0), 0.5)

    def test_negative_age_treated_as_zero(self):
        self.assertAlmostEqual(recency_factor(-1.0, 14.0), 1.0)

    def test_zero_half_life_returns_one(self):
        # half_life=0 → no time degradation
        self.assertAlmostEqual(recency_factor(100.0, 0.0), 1.0)


class TestConsensusFactor(unittest.TestCase):
    def test_empty_returns_half(self):
        self.assertEqual(consensus_factor([]), 0.5)

    def test_single_direction_is_one(self):
        self.assertEqual(consensus_factor(["bullish"]), 1.0)
        self.assertEqual(consensus_factor(["bearish"]), 1.0)

    def test_all_agree(self):
        self.assertEqual(consensus_factor(["bullish", "bullish", "bullish"]), 1.0)

    def test_split_is_zero(self):
        # 50/50 bull/bear with no neutrals → 0.0
        self.assertAlmostEqual(consensus_factor(["bullish", "bearish"]), 0.0)
        self.assertAlmostEqual(
            consensus_factor(["bullish", "bullish", "bearish", "bearish"]), 0.0
        )

    def test_with_neutral(self):
        # 1 bull, 1 bear, 1 neutral → not zero
        c = consensus_factor(["bullish", "bearish", "neutral"])
        # 0.5 bull_eff + 0.5 bull_eff = 1; 0.5 bear_eff + 0.5 = 1.0
        # diff / max = 0 / 1 = 0
        # Actually: half_neut = 0.5, bull_eff = 1.0, bear_eff = 1.0
        # diff = 0, max = 1, ratio = 0
        self.assertEqual(c, 0.0)

    def test_mixed_bullish_majority(self):
        # 2 bull, 1 bear → bull_eff=2, bear_eff=1, diff=1, max=2, ratio=0.5
        self.assertAlmostEqual(consensus_factor(["bullish", "bullish", "bearish"]), 0.5)


class TestComputeConfidence(unittest.TestCase):
    def test_default_inputs(self):
        cb = compute_confidence()
        # all defaults → all factors = 1.0
        self.assertAlmostEqual(cb.completeness, 1.0)
        self.assertAlmostEqual(cb.recency, 1.0)
        self.assertAlmostEqual(cb.consensus, 0.5)  # empty directions
        self.assertAlmostEqual(cb.final, 0.5)

    def test_full_inputs(self):
        cb = compute_confidence(
            n_present_keys=4, n_expected_keys=4,
            age_days=0.0, recency_half_life_days=14.0,
            directions=["bullish"],
        )
        # completeness=1.0, recency=1.0, consensus=1.0
        self.assertAlmostEqual(cb.final, 1.0)

    def test_partial_inputs(self):
        cb = compute_confidence(
            n_present_keys=2, n_expected_keys=4,
            age_days=14.0, recency_half_life_days=14.0,
            directions=["bullish", "bearish"],
        )
        # completeness=0.5, recency=0.5, consensus=0.0 → 0.0
        self.assertAlmostEqual(cb.final, 0.0)

    def test_within_zero_to_one(self):
        for args in [
            {}, {"n_present_keys": 0, "n_expected_keys": 10},
            {"age_days": 100, "recency_half_life_days": 1},
            {"directions": ["bullish", "bearish", "neutral"]},
        ]:
            cb = compute_confidence(**args)
            self.assertGreaterEqual(cb.final, 0.0)
            self.assertLessEqual(cb.final, 1.0)


# ---------- SignalEngine ----------

class TestSignalEngine(unittest.TestCase):
    def test_engine_default_construction(self):
        engine = SignalEngine()
        # With no source_weights and no decay_config, the engine should
        # use the DEFAULT_DECAY_CONFIG for decay lookups.
        s = _make_signal(signal_type="pe_ratio")
        ws = engine.weight(s)
        self.assertIsInstance(ws, WeightedSignal)
        self.assertEqual(ws.signal, s)

    def test_weight_with_source_weights(self):
        cfg = EngineConfig(
            source_weights={
                "yfinance": {
                    "source_weight": 0.9,
                    "type_weights": {"pe_ratio": 0.7},
                },
            },
            decay_config={"pe_ratio": {"function": "none", "half_life_days": 0.0}},
            default_decay={"function": "none", "half_life_days": 0.0},
        )
        engine = SignalEngine(cfg)
        s = _make_signal(signal_type="pe_ratio")
        ws = engine.weight(s)
        # source_weight = 0.9 (from yfinance entry)
        # type_weight = 0.7 (from yfinance.type_weights.pe_ratio)
        # recency_weight = 1.0 (none function)
        # final = 0.9 * 0.7 * 1.0 = 0.63
        self.assertAlmostEqual(ws.source_weight, 0.9, places=4)
        self.assertAlmostEqual(ws.type_weight, 0.7, places=4)
        self.assertAlmostEqual(ws.recency_weight, 1.0, places=4)
        self.assertAlmostEqual(ws.final_weight, 0.63, places=4)

    def test_weight_with_exponential_decay(self):
        cfg = EngineConfig(
            source_weights={},
            decay_config={"pe_ratio": {"function": "exponential", "half_life_days": 7.0}},
            default_decay={"function": "exponential", "half_life_days": 7.0},
        )
        engine = SignalEngine(cfg)
        # Use explicit as_of pinned to the test helper base so the age
        # is exactly 7.0 days, not (7 + delta) since faked "now".
        s = _make_signal(signal_type="pe_ratio", days_ago=7.0)
        ws = engine.weight(s, as_of=_ts(0.0))
        # age=7 days, half_life=7 → recency = 0.5
        self.assertAlmostEqual(ws.recency_weight, 0.5, places=4)

    def test_weight_with_step_decay(self):
        cfg = EngineConfig(
            source_weights={},
            decay_config={"institutional_flow": {"function": "step",
                                                 "step_threshold_days": 5.0}},
            default_decay={"function": "none", "half_life_days": 0.0},
        )
        engine = SignalEngine(cfg)
        # as_of pinned to base, days_ago controls age
        s = _make_signal(signal_type="institutional_flow", days_ago=3.0)
        ws = engine.weight(s, as_of=_ts(0.0))
        self.assertAlmostEqual(ws.recency_weight, 1.0)

        s2 = _make_signal(
            signal_type="institutional_flow", days_ago=10.0,
            signal_id="sig-test-2",
        )
        ws2 = engine.weight(s2, as_of=_ts(0.0))
        self.assertAlmostEqual(ws2.recency_weight, 0.0)

    def test_weight_returns_weighted_signal_fields(self):
        engine = SignalEngine()
        s = _make_signal()
        ws = engine.weight(s)
        # Required fields
        self.assertIsInstance(ws, WeightedSignal)
        self.assertEqual(ws.signal.signal_id, s.signal_id)
        self.assertIsInstance(ws.final_weight, float)
        self.assertIsInstance(ws.confidence, float)
        self.assertIsInstance(ws.decay_function, str)
        self.assertGreaterEqual(ws.final_weight, 0.0)
        self.assertLessEqual(ws.final_weight, 1.0)
        self.assertGreaterEqual(ws.confidence, 0.0)
        self.assertLessEqual(ws.confidence, 1.0)

    def test_weight_all_preserves_order_and_count(self):
        engine = SignalEngine()
        sigs = [
            _make_signal(signal_id=f"sig-{i}", days_ago=float(i))
            for i in range(5)
        ]
        ws_list = engine.weight_all(sigs)
        self.assertEqual(len(ws_list), 5)
        for orig, ws in zip(sigs, ws_list):
            self.assertEqual(ws.signal.signal_id, orig.signal_id)


# ---------- SignalAggregator ----------

class TestSignalAggregator(unittest.TestCase):
    def test_aggregator_weighted_sum(self):
        # Two bullish signals with the same final_weight, value sums simply.
        s1 = _make_signal(signal_id="s1", value=10.0, direction="bullish")
        s2 = _make_signal(signal_id="s2", value=20.0, direction="bullish")
        # Use a fake final_weight on each
        ws1 = WeightedSignal(
            signal=s1, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        ws2 = WeightedSignal(
            signal=s2, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        agg = SignalAggregator().aggregate([ws1, ws2])
        # weighted_sum = 0.5*10 + 0.5*20 = 15
        self.assertAlmostEqual(agg.weighted_sum, 15.0)
        # all bullish → consensus=bullish
        self.assertEqual(agg.direction_consensus, "bullish")

    def test_aggregator_with_score_sign(self):
        # PE ratio: lower is better. score_sign = negative identity.
        s1 = _make_signal(signal_id="s1", value=10.0, direction="bullish")
        ws1 = WeightedSignal(
            signal=s1, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        # score_sign = -1 → weighted_sum = 0.5 * (-1) * 10 = -5
        agg = SignalAggregator(score_sign=lambda sig: -1.0).aggregate([ws1])
        self.assertAlmostEqual(agg.weighted_sum, -5.0)

    def test_aggregator_empty_list_raises(self):
        with self.assertRaises(ValueError):
            SignalAggregator().aggregate([])

    def test_aggregator_rejects_mixed_buckets(self):
        s1 = _make_signal(signal_id="s1", signal_type="pe_ratio")
        s2 = _make_signal(signal_id="s2", signal_type="roe")
        ws1 = WeightedSignal(
            signal=s1, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        ws2 = WeightedSignal(
            signal=s2, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        with self.assertRaises(ValueError):
            SignalAggregator().aggregate([ws1, ws2])

    def test_aggregator_direction_consensus_mixed(self):
        s1 = _make_signal(signal_id="s1", direction="bullish")
        s2 = _make_signal(signal_id="s2", direction="bearish")
        ws1 = WeightedSignal(
            signal=s1, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        ws2 = WeightedSignal(
            signal=s2, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        agg = SignalAggregator().aggregate([ws1, ws2])
        self.assertEqual(agg.direction_consensus, "mixed")
        # mixed → consensus_factor=0.5, mean conf=0.8 → aggregate=0.4
        self.assertAlmostEqual(agg.aggregate_confidence, 0.4, places=4)

    def test_aggregator_direction_consensus_neutral(self):
        s1 = _make_signal(signal_id="s1", direction="neutral")
        ws1 = WeightedSignal(
            signal=s1, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        agg = SignalAggregator().aggregate([ws1])
        self.assertEqual(agg.direction_consensus, "neutral")
        # neutral → consensus_factor=0.7, mean conf=0.8 → 0.56
        self.assertAlmostEqual(agg.aggregate_confidence, 0.56, places=4)

    def test_aggregator_bucketing_separate_calls(self):
        # Two separate aggregations for two different buckets should
        # both succeed and produce different consensus strings.
        s1 = _make_signal(signal_id="s1", signal_type="pe_ratio",
                          direction="bullish", value=20.0)
        s2 = _make_signal(signal_id="s2", signal_type="roe",
                          direction="bullish", value=0.3)
        ws1 = WeightedSignal(
            signal=s1, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        ws2 = WeightedSignal(
            signal=s2, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        agg1 = SignalAggregator().aggregate([ws1])
        agg2 = SignalAggregator().aggregate([ws2])
        self.assertEqual(agg1.signal_type, "pe_ratio")
        self.assertEqual(agg2.signal_type, "roe")
        self.assertEqual(agg1.direction_consensus, "bullish")
        self.assertEqual(agg2.direction_consensus, "bullish")
        # weighted_sum differs by value
        self.assertAlmostEqual(agg1.weighted_sum, 10.0)
        self.assertAlmostEqual(agg2.weighted_sum, 0.15, places=4)

    def test_aggregated_signal_to_dict(self):
        s1 = _make_signal(signal_id="s1", direction="bullish")
        ws1 = WeightedSignal(
            signal=s1, source_weight=1.0, type_weight=1.0, recency_weight=1.0,
            final_weight=0.5, confidence=0.8, decay_function="none",
        )
        agg = SignalAggregator().aggregate([ws1])
        d = agg.to_dict()
        self.assertEqual(d["entity_type"], "company")
        self.assertEqual(d["entity_id"], "2330")
        self.assertEqual(d["signal_type"], "pe_ratio")
        self.assertEqual(d["direction_consensus"], "bullish")
        self.assertEqual(len(d["contributors"]), 1)


if __name__ == "__main__":
    unittest.main()
