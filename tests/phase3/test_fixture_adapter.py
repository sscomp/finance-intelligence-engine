"""Tests for the FixtureAdapter.

Contract:
* Accepts a list of dicts, a dict with a 'signals' key, a single dict,
  or a path to a JSON file containing any of those shapes.
* Each well-formed entry produces exactly one Signal.
* Required fields (entity_type, entity_id, signal_type, value, unit,
  direction) must be present; missing → entry skipped with warning.
* signal_id is recomputed via make_signal_id unless the entry
  already provides a 16-char hex id.
* Direction values outside the legal set fall back to "neutral".
* Determinism: same input → same signals.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from phase3.datamodel.signals import Signal, make_signal_id
from phase3.signals.adapters.fixture import FixtureAdapter


def _tmp_path(suffix: str = ".json") -> Path:
    fd, p = tempfile.mkstemp(suffix=suffix)
    import os
    os.close(fd)
    return Path(p)


def _entry(entity_id: str = "2330", value: float = 1.5,
           signal_type: str = "pe_ratio", direction: str = "bullish",
           date_bucket: str = "2026-07-08") -> dict:
    return {
        "entity_type": "company",
        "entity_id": entity_id,
        "signal_type": signal_type,
        "value": value,
        "unit": "ratio",
        "direction": direction,
        "timestamp": "2026-07-08T00:00:00+00:00",
        "date_bucket": date_bucket,
        "source_id": f"fixture.{entity_id}.{date_bucket}",
        "source_type": "fixture",
    }


class FixtureAdapterBasicTests(unittest.TestCase):
    def test_accepts_list_of_entries(self) -> None:
        a = FixtureAdapter()
        sigs = a.adapt([_entry(), _entry(entity_id="2454")])
        self.assertEqual(len(sigs), 2)
        self.assertEqual(sigs[0].entity_id, "2330")
        self.assertEqual(sigs[1].entity_id, "2454")

    def test_accepts_dict_with_signals_key(self) -> None:
        a = FixtureAdapter()
        sigs = a.adapt({"signals": [_entry(), _entry(entity_id="2317")]})
        self.assertEqual(len(sigs), 2)

    def test_accepts_single_dict(self) -> None:
        a = FixtureAdapter()
        sigs = a.adapt(_entry())
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].entity_id, "2330")

    def test_returns_list_of_signal_instances(self) -> None:
        a = FixtureAdapter()
        sigs = a.adapt([_entry()])
        self.assertTrue(all(isinstance(s, Signal) for s in sigs))

    def test_deterministic_signal_id_from_inputs(self) -> None:
        """signal_id is recomputed from (source, entity, type, date_bucket)
        when not provided as a 16-char hex string."""
        a = FixtureAdapter()
        sigs = a.adapt([_entry(entity_id="2330", signal_type="pe_ratio",
                                date_bucket="2026-07-08")])
        expected = make_signal_id(
            "fixture.2330.2026-07-08", "2330", "pe_ratio", "2026-07-08"
        )
        self.assertEqual(sigs[0].signal_id, expected)

    def test_existing_16hex_signal_id_preserved(self) -> None:
        a = FixtureAdapter()
        entry = _entry()
        entry["signal_id"] = "0123456789abcdef"  # 16 hex chars
        sigs = a.adapt([entry])
        self.assertEqual(sigs[0].signal_id, "0123456789abcdef")

    def test_directional_default_for_invalid_direction(self) -> None:
        a = FixtureAdapter()
        entry = _entry()
        entry["direction"] = "sideways"  # not in legal set
        sigs = a.adapt([entry])
        self.assertEqual(sigs[0].direction, "neutral")

    def test_skips_missing_required_field(self) -> None:
        a = FixtureAdapter()
        result = a.adapt_with_stats([_entry()])
        good_count = len(result.signals)
        bad = _entry()
        del bad["signal_type"]
        result2 = a.adapt_with_stats([bad])
        self.assertEqual(len(result2.signals), 0)
        self.assertEqual(result2.skipped, 1)
        self.assertTrue(any("signal_type" in w for w in result2.warnings))

    def test_skips_non_numeric_value(self) -> None:
        a = FixtureAdapter()
        bad = _entry()
        bad["value"] = "not-a-number"
        result = a.adapt_with_stats([bad])
        self.assertEqual(result.skipped, 1)
        self.assertEqual(len(result.signals), 0)

    def test_determinism_same_input_same_output(self) -> None:
        a = FixtureAdapter()
        payload = [_entry(entity_id="2330"), _entry(entity_id="2454")]
        s1 = a.adapt(payload)
        s2 = a.adapt(payload)
        self.assertEqual(
            [s.signal_id for s in s1],
            [s.signal_id for s in s2],
        )


class FixtureAdapterPathTests(unittest.TestCase):
    def test_loads_json_file(self) -> None:
        p = _tmp_path()
        try:
            p.write_text(json.dumps([_entry(), _entry(entity_id="2454")]))
            a = FixtureAdapter()
            sigs = a.adapt(p)
            self.assertEqual(len(sigs), 2)
        finally:
            p.unlink()

    def test_loads_json_file_with_signals_key(self) -> None:
        p = _tmp_path()
        try:
            p.write_text(json.dumps({"signals": [_entry()]}))
            a = FixtureAdapter()
            sigs = a.adapt(p)
            self.assertEqual(len(sigs), 1)
        finally:
            p.unlink()

    def test_missing_path_warns_no_raise(self) -> None:
        a = FixtureAdapter()
        result = a.adapt_with_stats(Path("/no/such/path.json"))
        self.assertEqual(len(result.signals), 0)
        self.assertTrue(any("not found" in w for w in result.warnings))

    def test_invalid_json_warns_no_raise(self) -> None:
        p = _tmp_path()
        try:
            p.write_text("{this is not valid json")
            a = FixtureAdapter()
            result = a.adapt_with_stats(p)
            self.assertEqual(len(result.signals), 0)
            self.assertTrue(len(result.warnings) >= 1)
        finally:
            p.unlink()

    def test_skips_non_mapping_entries_in_list(self) -> None:
        """Non-dict entries inside a list are skipped, not raised."""
        a = FixtureAdapter()
        result = a.adapt_with_stats([_entry(), "not a dict", 42])
        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.skipped, 2)


if __name__ == "__main__":
    unittest.main()
