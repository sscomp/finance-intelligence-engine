#!/usr/bin/env python3
"""Test the bridge module that seeds Phase 3 signal_log from macro_history.db.

Verifies:
1. seed_from_macro_history produces non-zero signal counts
2. Seeded signals are readable via SignalRepository
3. pipeline-export with the seeded DB produces non-default company scores
4. Company score evidence handles have non-zero signal_node counts
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from phase3.bridge.seed_signals import seed_from_macro_history
from phase3.persistence.signal_repo import SignalRepository
from phase3.persistence.sqlite import SQLiteStore


SOURCE_DB = str(REPO / "macro_history.db")


def _source_db_ready() -> bool:
    """True when the host has a real production macro_history.db.

    These bridge tests integrate with the operator's production history
    DB (git-ignored, per-host data — absent from a portable checkout).
    They skip explicitly when it is missing rather than failing, and
    open it read-only so the check does not create an empty DB file.
    """
    if not os.path.exists(SOURCE_DB):
        return False
    conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    return {"macro_daily", "stock_monthly", "institutional_daily"} <= names


_SKIP_PROD_DB = unittest.skipUnless(
    _source_db_ready(),
    "requires the host production macro_history.db (git-ignored data, "
    "absent from a portable checkout)",
)


@_SKIP_PROD_DB
class TestBridgeSeedSignals(unittest.TestCase):
    """Verify the bridge correctly seeds Phase 3 signals from production data."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="bridge_test_")
        cls.target_db = os.path.join(cls.tmpdir, "test-intelligence.db")

    def test_01_seed_produces_signals(self):
        """seed_from_macro_history returns non-zero counts for all three types."""
        result = seed_from_macro_history(
            source_db=SOURCE_DB,
            target_db=self.target_db,
            macro_date=None,
            company_codes=["2330", "2317"],
            company_date="2026-07-29",
            institutional_codes=["2330", "2317"],
            institutional_date="2026-07-29",
        )
        self.assertGreater(result["macro"], 0, "No macro signals seeded")
        self.assertGreater(result["company"], 0, "No company signals seeded")
        self.assertGreater(result["institutional"], 0, "No institutional signals seeded")
        self.assertGreater(result["total"], 0, "Total signals is 0")
        self.assertEqual(
            result["total"],
            result["macro"] + result["company"] + result["institutional"] + result.get("industry", 0),
        )

    def test_02_seeded_signals_are_readable(self):
        """SignalRepository can read back the seeded signals."""
        store = SQLiteStore(self.target_db)
        repo = SignalRepository(store)
        count = repo.count()
        self.assertGreater(count, 0, "signal_log is empty after seeding")

        # Check company signals for 2330
        company_signals = repo.query(entity_type="company", entity_id="2330")
        self.assertGreater(len(company_signals), 0, "No signals for company 2330")

        # Check that values are non-default (not all 0.0)
        non_zero_values = [s for s in company_signals if s.value != 0.0]
        self.assertGreater(len(non_zero_values), 0, "All company signal values are 0.0")

        # Check macro signals
        macro_signals = repo.query(entity_type="macro", entity_id="global")
        self.assertGreater(len(macro_signals), 0, "No macro signals")

        store.close()

    def test_03_pipeline_export_with_real_scores(self):
        """pipeline-export with seeded DB produces non-default company scores."""
        import subprocess

        output_dir = os.path.join(self.tmpdir, "pipeline_output")
        os.makedirs(output_dir, exist_ok=True)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        cmd = [
            sys.executable, "-m", "phase3.cli",
            "pipeline-export",
            "--date", "2026-07-29",
            "--run-label", "bridge-test",
            "--output-dir", output_dir,
            "--db-path", self.target_db,
            "--persist",
            "--company", "2330",
            "--company", "2317",
            "--json",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=str(REPO),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, f"pipeline-export failed: {result.stderr}")

        json_path = Path(output_dir) / "bridge-test.intelligence_report.json"
        self.assertTrue(json_path.exists(), f"Artifact not found at {json_path}")

        with open(json_path) as f:
            artifact = json.load(f)

        summary = artifact.get("summary", {})
        self.assertGreater(summary["company_score_count"], 0,
            "company_score_count is 0")
        self.assertGreater(summary["signal_count"], 0,
            "signal_count is 0 — no real signals were loaded")

        # Verify evidence handles have signal nodes (non-default)
        result_obj = artifact.get("result", {})
        for gw in result_obj.get("graph_writes", []):
            if "company" in gw.get("score_node_id", ""):
                self.assertGreater(len(gw["signal_node_ids"]), 0,
                    f"company score {gw['score_node_id']} has 0 signal nodes — "
                    f"score is default/synthetic, not real")


if __name__ == "__main__":
    unittest.main(verbosity=2)