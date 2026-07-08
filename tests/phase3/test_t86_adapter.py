"""Tests for the T86Adapter.

Contract:
* Each row must have ``code`` (or ``entity_id``); missing → skipped.
* ``investment_trust_net`` is aliased to ``prop_net``.
* Three flow types: foreign_net, prop_net (or investment_trust_net),
  dealer_net. Any subset may be present.
* Unit is "zhang" for all signals.
* Direction: positive → bullish, negative → bearish, zero → neutral.
* Industry rollup: when row has ``industry_id`` AND adapter
  ``emit_industry_rollup=True`` (default), industry-level signals
  are emitted in addition to company-level ones.
* All missing flow fields → row skipped (warning).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from phase3.datamodel.signals import Signal, make_signal_id
from phase3.signals.adapters.t86 import T86Adapter


def _row(code: str = "2330", foreign_net: float | None = 15000.0,
         prop_net: float | None = 2500.0, dealer_net: float | None = -800.0,
         industry_id: str = "", date_bucket: str = "2026-07-08",
         industry_rollup: bool = True) -> dict:
    r: dict = {"code": code, "date_bucket": date_bucket,
               "industry_rollup": industry_rollup}
    if foreign_net is not None:
        r["foreign_net"] = foreign_net
    if prop_net is not None:
        r["prop_net"] = prop_net
    if dealer_net is not None:
        r["dealer_net"] = dealer_net
    if industry_id:
        r["industry_id"] = industry_id
    return r


class T86AdapterBasicTests(unittest.TestCase):
    def test_full_row_produces_three_signals(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row())
        self.assertEqual(len(sigs), 3)
        types = sorted(s.signal_type for s in sigs)
        self.assertEqual(types, ["dealer_net", "foreign_net", "prop_net"])

    def test_unit_is_zhang(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row())
        for s in sigs:
            self.assertEqual(s.unit, "zhang")

    def test_direction_positive_is_bullish(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row(foreign_net=15000.0))
        foreign = next(s for s in sigs if s.signal_type == "foreign_net")
        self.assertEqual(foreign.direction, "bullish")

    def test_direction_negative_is_bearish(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row(foreign_net=-5000.0))
        foreign = next(s for s in sigs if s.signal_type == "foreign_net")
        self.assertEqual(foreign.direction, "bearish")

    def test_direction_zero_is_neutral(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row(foreign_net=0.0))
        foreign = next(s for s in sigs if s.signal_type == "foreign_net")
        self.assertEqual(foreign.direction, "neutral")

    def test_entity_type_is_company(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row())
        for s in sigs:
            self.assertEqual(s.entity_type, "company")
            self.assertEqual(s.entity_id, "2330")

    def test_signal_id_deterministic(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row())
        for s in sigs:
            expected = make_signal_id(
                s.source.source_id, s.entity_id, s.signal_type, s.date_bucket
            )
            self.assertEqual(s.signal_id, expected)

    def test_source_type_is_t86(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row())
        for s in sigs:
            self.assertEqual(s.source.source_type, "t86")


class T86AdapterAliasingTests(unittest.TestCase):
    def test_investment_trust_net_aliased_to_prop_net(self) -> None:
        a = T86Adapter()
        row = {"code": "2881", "date_bucket": "2026-07-08",
               "investment_trust_net": 1200.0, "foreign_net": 4500.0}
        sigs = a.adapt(row)
        types = {s.signal_type for s in sigs}
        self.assertIn("prop_net", types)
        self.assertIn("foreign_net", types)
        prop = next(s for s in sigs if s.signal_type == "prop_net")
        self.assertEqual(prop.value, 1200.0)

    def test_prop_net_overrides_alias(self) -> None:
        """If both prop_net and investment_trust_net are present,
        prop_net wins (the alias is only used when prop_net is missing)."""
        a = T86Adapter()
        row = {"code": "2330", "date_bucket": "2026-07-08",
               "prop_net": 100.0, "investment_trust_net": 999.0}
        sigs = a.adapt(row)
        prop = next(s for s in sigs if s.signal_type == "prop_net")
        self.assertEqual(prop.value, 100.0)


class T86AdapterEdgeCaseTests(unittest.TestCase):
    def test_missing_code_skipped(self) -> None:
        a = T86Adapter()
        result = a.adapt_with_stats({"date_bucket": "2026-07-08",
                                       "foreign_net": 100.0})
        self.assertEqual(len(result.signals), 0)
        self.assertEqual(result.rows_skipped, 1)

    def test_all_flow_fields_missing_skips_row(self) -> None:
        a = T86Adapter()
        result = a.adapt_with_stats([{"code": "2330", "date_bucket": "2026-07-08"}])
        self.assertEqual(len(result.signals), 0)
        self.assertEqual(result.rows_skipped, 1)
        self.assertTrue(any("no flow fields" in w for w in result.warnings))

    def test_partial_row_emits_only_present(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row(prop_net=None, dealer_net=None))
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].signal_type, "foreign_net")

    def test_non_numeric_value_warns(self) -> None:
        a = T86Adapter()
        result = a.adapt_with_stats(
            {"code": "2330", "date_bucket": "2026-07-08", "foreign_net": "abc"}
        )
        self.assertEqual(len(result.signals), 0)
        self.assertTrue(any("not numeric" in w for w in result.warnings))

    def test_bool_value_skipped(self) -> None:
        a = T86Adapter()
        result = a.adapt_with_stats(
            {"code": "2330", "date_bucket": "2026-07-08", "foreign_net": True}
        )
        self.assertEqual(len(result.signals), 0)


class T86AdapterIndustryRollupTests(unittest.TestCase):
    def test_industry_rollup_default_emits_industry_signals(self) -> None:
        a = T86Adapter()  # default emit_industry_rollup=True
        sigs = a.adapt(_row(industry_id="半導體"))
        # 3 company signals + 3 industry signals = 6
        self.assertEqual(len(sigs), 6)
        company_sigs = [s for s in sigs if s.entity_type == "company"]
        industry_sigs = [s for s in sigs if s.entity_type == "industry"]
        self.assertEqual(len(company_sigs), 3)
        self.assertEqual(len(industry_sigs), 3)
        for s in industry_sigs:
            self.assertEqual(s.entity_id, "半導體")

    def test_industry_rollup_disabled_skips_industry_signals(self) -> None:
        a = T86Adapter(emit_industry_rollup=False)
        sigs = a.adapt(_row(industry_id="半導體"))
        self.assertEqual(len(sigs), 3)
        self.assertTrue(all(s.entity_type == "company" for s in sigs))

    def test_industry_rollup_per_row_flag_off(self) -> None:
        """A row-level ``industry_rollup: false`` suppresses the rollup even
        if the adapter-level flag is on."""
        a = T86Adapter()
        sigs = a.adapt(_row(industry_id="半導體", industry_rollup=False))
        self.assertEqual(len(sigs), 3)
        self.assertTrue(all(s.entity_type == "company" for s in sigs))

    def test_no_industry_id_skips_rollup(self) -> None:
        a = T86Adapter()
        sigs = a.adapt(_row(industry_id=""))
        self.assertEqual(len(sigs), 3)
        self.assertTrue(all(s.entity_type == "company" for s in sigs))


class T86AdapterInputShapeTests(unittest.TestCase):
    def test_accepts_list_of_rows(self) -> None:
        a = T86Adapter()
        sigs = a.adapt([_row(code="2330"), _row(code="2454")])
        # 3 signals per row × 2 rows = 6 (no industry_id)
        self.assertEqual(len(sigs), 6)

    def test_accepts_dict_with_rows_key(self) -> None:
        a = T86Adapter()
        result = a.adapt_with_stats({"rows": [_row(code="2330")]})
        self.assertEqual(result.rows_processed, 1)

    def test_loads_from_json_file(self) -> None:
        fd, p = tempfile.mkstemp(suffix=".json")
        import os
        os.close(fd)
        try:
            Path(p).write_text(json.dumps([_row(code="2330")]))
            a = T86Adapter()
            sigs = a.adapt(Path(p))
            self.assertEqual(len(sigs), 3)
        finally:
            Path(p).unlink()

    def test_determinism(self) -> None:
        a = T86Adapter()
        row = _row()
        s1 = a.adapt(row)
        s2 = a.adapt(row)
        self.assertEqual(
            [s.signal_id for s in s1],
            [s.signal_id for s in s2],
        )


if __name__ == "__main__":
    unittest.main()
