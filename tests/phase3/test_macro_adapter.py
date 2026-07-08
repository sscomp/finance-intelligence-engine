"""Tests for the MacroAdapter.

Contract:
* Maps known macro fields to macro Signals (entity_type="macro",
  entity_id=MACRO_ENTITY_ID='global').
* Recognized fields: fed_rate, us10y, us2y, us13w, dxy, vix, cpi_yoy,
  core_cpi_yoy, m2_yoy, credit_spread_bp (→credit_spread),
  yield_spread, rate_hike_flag (→rate_hike).
* Direction: vix > 25 → bearish, vix < 15 → bullish; rate_hike > 0
  → bearish, < 0 → bullish; others → neutral.
* Missing/None/NaN/non-numeric/bool fields are skipped (with warning).
* Unknown fields are ignored (no error).
* All-missing rows produce a warning and increment rows_skipped.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from phase3.datamodel import MACRO_ENTITY_ID, MACRO_ENTITY_TYPE
from phase3.datamodel.signals import Signal, make_signal_id
from phase3.signals.adapters.macro import MacroAdapter


def _row(**fields) -> dict:
    return {
        "timestamp": "2026-07-08T00:00:00+00:00",
        "date_bucket": "2026-07-08",
        **fields,
    }


class MacroAdapterBasicTests(unittest.TestCase):
    def test_recognized_fields_produce_signals(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(
            fed_rate=0.0525, us10y=0.043, vix=18.5, dxy=102.0,
            cpi_yoy=0.028, core_cpi_yoy=0.022, m2_yoy=0.04,
            credit_spread_bp=130.0, yield_spread=-0.0015, rate_hike_flag=0,
        ))
        # 10 fields → 10 signals
        self.assertEqual(len(sigs), 10)

    def test_entity_is_macro_global(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(fed_rate=0.05, vix=18.0))
        for s in sigs:
            self.assertEqual(s.entity_type, MACRO_ENTITY_TYPE)
            self.assertEqual(s.entity_id, MACRO_ENTITY_ID)
            self.assertEqual(s.entity_id, "global")

    def test_credit_spread_bp_remaps_to_credit_spread(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(credit_spread_bp=150.0))
        types = {s.signal_type for s in sigs}
        self.assertIn("credit_spread", types)
        self.assertNotIn("credit_spread_bp", types)

    def test_rate_hike_flag_remaps_to_rate_hike(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(rate_hike_flag=1.0))
        types = {s.signal_type for s in sigs}
        self.assertIn("rate_hike", types)

    def test_signal_id_deterministic(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(fed_rate=0.05))
        for s in sigs:
            expected = make_signal_id(
                s.source.source_id, s.entity_id, s.signal_type, s.date_bucket
            )
            self.assertEqual(s.signal_id, expected)

    def test_source_type_is_macro(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(fed_rate=0.05))
        for s in sigs:
            self.assertEqual(s.source.source_type, "macro")
            self.assertTrue(s.source.source_id.startswith("macro_yfinance."))


class MacroAdapterDirectionTests(unittest.TestCase):
    def test_vix_high_is_bearish(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(vix=30.0))
        vix = next(s for s in sigs if s.signal_type == "vix")
        self.assertEqual(vix.direction, "bearish")

    def test_vix_low_is_bullish(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(vix=12.0))
        vix = next(s for s in sigs if s.signal_type == "vix")
        self.assertEqual(vix.direction, "bullish")

    def test_vix_mid_is_neutral(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(vix=20.0))
        vix = next(s for s in sigs if s.signal_type == "vix")
        self.assertEqual(vix.direction, "neutral")

    def test_rate_hike_positive_is_bearish(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(rate_hike_flag=1))
        rh = next(s for s in sigs if s.signal_type == "rate_hike")
        self.assertEqual(rh.direction, "bearish")

    def test_rate_hike_negative_is_bullish(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(rate_hike_flag=-1))
        rh = next(s for s in sigs if s.signal_type == "rate_hike")
        self.assertEqual(rh.direction, "bullish")

    def test_fed_rate_default_neutral(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(fed_rate=0.05))
        fr = next(s for s in sigs if s.signal_type == "fed_rate")
        self.assertEqual(fr.direction, "neutral")


class MacroAdapterEdgeCaseTests(unittest.TestCase):
    def test_unknown_field_ignored(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt(_row(fed_rate=0.05, random_unknown_field=99.0))
        # Only fed_rate should produce a signal
        self.assertEqual(len(sigs), 1)

    def test_none_value_skipped(self) -> None:
        a = MacroAdapter()
        result = a.adapt_with_stats(_row(fed_rate=None, vix=18.0))
        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].signal_type, "vix")

    def test_nan_value_skipped(self) -> None:
        a = MacroAdapter()
        result = a.adapt_with_stats(_row(fed_rate=float("nan")))
        self.assertEqual(len(result.signals), 0)
        self.assertTrue(any("nan" in w.lower() for w in result.warnings))

    def test_bool_value_skipped(self) -> None:
        a = MacroAdapter()
        result = a.adapt_with_stats(_row(rate_hike_flag=True))
        # bool is skipped (rate_hike_flag=True → skip)
        self.assertEqual(len(result.signals), 0)

    def test_non_numeric_value_skipped(self) -> None:
        a = MacroAdapter()
        result = a.adapt_with_stats(_row(fed_rate="not-a-number"))
        self.assertEqual(len(result.signals), 0)

    def test_all_unknown_fields_skips_row(self) -> None:
        a = MacroAdapter()
        result = a.adapt_with_stats(_row(unknown1=1.0, unknown2=2.0))
        self.assertEqual(len(result.signals), 0)
        self.assertEqual(result.rows_skipped, 1)
        self.assertTrue(any("no recognized" in w for w in result.warnings))


class MacroAdapterInputShapeTests(unittest.TestCase):
    def test_accepts_list_of_rows(self) -> None:
        a = MacroAdapter()
        sigs = a.adapt([
            _row(fed_rate=0.05),
            _row(vix=18.0),
        ])
        self.assertEqual(len(sigs), 2)

    def test_accepts_dict_with_rows_key(self) -> None:
        a = MacroAdapter()
        result = a.adapt_with_stats({"rows": [_row(fed_rate=0.05)]})
        self.assertEqual(result.rows_processed, 1)

    def test_loads_from_json_file(self) -> None:
        fd, p = tempfile.mkstemp(suffix=".json")
        import os
        os.close(fd)
        try:
            Path(p).write_text(json.dumps(_row(fed_rate=0.05, vix=18.0)))
            a = MacroAdapter()
            sigs = a.adapt(Path(p))
            self.assertEqual(len(sigs), 2)
        finally:
            Path(p).unlink()

    def test_missing_path_warns_no_raise(self) -> None:
        a = MacroAdapter()
        result = a.adapt_with_stats(Path("/no/such/macro.json"))
        self.assertEqual(len(result.signals), 0)
        self.assertTrue(any("not found" in w for w in result.warnings))

    def test_determinism(self) -> None:
        a = MacroAdapter()
        row = _row(fed_rate=0.05, vix=18.0, dxy=102.0)
        s1 = a.adapt(row)
        s2 = a.adapt(row)
        self.assertEqual(
            [s.signal_id for s in s1],
            [s.signal_id for s in s2],
        )


if __name__ == "__main__":
    unittest.main()
