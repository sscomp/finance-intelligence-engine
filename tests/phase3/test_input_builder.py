from __future__ import annotations

import unittest
from datetime import datetime, timezone

from phase3.datamodel.signals import Signal, SignalSource
from phase3.pipeline import InputDimension
from phase3.pipeline.input_builder import InputBuilder


class InputBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = InputBuilder()

    def _signal(self, signal_type: str, value: float, entity_type: str = "macro", entity_id: str = "global") -> Signal:
        return Signal(
            signal_id=f"sig-{signal_type}-{value}",
            entity_type=entity_type,
            entity_id=entity_id,
            signal_type=signal_type,
            value=value,
            unit="ratio",
            direction="bullish",
            timestamp=datetime(2026, 7, 8, tzinfo=timezone.utc),
            source=SignalSource(source_id="fixture", source_type="fixture"),
            date_bucket="2026-07-08",
        )

    def test_macro_builder_maps_signals(self) -> None:
        signals = [
            self._signal("gdp_yoy", 0.02),
            self._signal("pmi", 52.0),
            self._signal("vix", 18.0),
            self._signal("credit_spread", 150.0),
        ]
        bundle = self.builder.build_macro(signals, entity_id="global", date_bucket="2026-07-08")
        self.assertEqual(bundle.scorer_type, "macro")
        self.assertIn("economic", bundle.dimensions)
        econ = bundle.dimensions["economic"]
        self.assertEqual(econ.values["gdp_yoy"], 0.02)
        self.assertEqual(econ.values["pmi"], 52.0)
        self.assertIn("liquidity", bundle.dimensions)
        liquidity = bundle.dimensions["liquidity"]
        self.assertEqual(liquidity.values["vix"], 18.0)
        self.assertEqual(liquidity.values["credit_spread"], 150.0)

    def test_industry_builder_handles_multiple_dimensions(self) -> None:
        signals = [
            self._signal("sector_relative_perf_5d", 0.03, entity_type="industry", entity_id="AI"),
            self._signal("rs_rating", 80.0, entity_type="industry", entity_id="AI"),
            self._signal("book_to_bill", 1.1, entity_type="industry", entity_id="AI"),
            self._signal("news_headline", 1.0, entity_type="industry", entity_id="AI"),
            self._signal("foreign_net", 2.0, entity_type="industry", entity_id="AI"),
        ]
        bundle = self.builder.build_industry(signals, industry_id="AI", date_bucket="2026-07-08")
        self.assertEqual(bundle.entity_id, "AI")
        rotation = bundle.dimensions["rotation"]
        self.assertEqual(rotation.values["sector_relative_perf_5d"], 0.03)
        relative_strength = bundle.dimensions["relative_strength"]
        self.assertEqual(relative_strength.values["rs_rating"], 80.0)
        capital_flow = bundle.dimensions["capital_flow"]
        self.assertEqual(capital_flow.values["foreign_net"], 2.0)

    def test_company_builder_collapses_latest_value_per_indicator(self) -> None:
        signals = [
            self._signal("roe", 0.28, entity_type="company", entity_id="2330"),
            self._signal("roe", 0.30, entity_type="company", entity_id="2330"),
            self._signal("beta", 1.1, entity_type="company", entity_id="2330"),
            self._signal("news_headline", 1.0, entity_type="company", entity_id="2330"),
        ]
        bundle = self.builder.build_company(signals, company_id="2330", date_bucket="2026-07-08")
        self.assertEqual(bundle.entity_id, "2330")
        fin = bundle.dimensions["financial_quality"]
        self.assertEqual(fin.values["roe"], 0.30)
        self.assertGreaterEqual(len(fin.signal_ids), 2)
        self.assertIn("news_sentiment", bundle.dimensions)

    def test_missing_dimension_emits_warning(self) -> None:
        bundle = self.builder.build_macro([], entity_id="global", date_bucket="2026-07-08")
        self.assertTrue(bundle.warnings)
        self.assertIn("dimension economic", bundle.warnings[0])


if __name__ == "__main__":
    unittest.main()
