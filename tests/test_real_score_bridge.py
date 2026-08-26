#!/usr/bin/env python3
"""FIE Real-Score Bridge Wiring — comprehensive test suite.

Tests:
1. Bridge seeding from macro_history.db (macro, company, institutional, industry)
2. Non-default real company score emission
3. Non-default real macro score emission
4. Industry score emission (capital_flow from aggregation)
5. As-of date alignment semantics
6. Deterministic output (same inputs -> same results)
7. Empty/missing-source fallback behavior
8. Wrapper/backward compatibility (--seed-from-history CLI path)
9. resolve_as_of_dates correctness
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path("/home/ubuntu/macro-report")
sys.path.insert(0, str(REPO))

from phase3.bridge.seed_signals import (
    seed_from_macro_history,
    resolve_as_of_dates,
    _seed_industry_signals,
    _load_industry_mapping,
)
from phase3.persistence.signal_repo import SignalRepository, SignalRecord
from phase3.persistence.sqlite import SQLiteStore

SOURCE_DB = str(REPO / "macro_history.db")
INDUSTRY_CONFIG = str(REPO / "industry_config.json")
VENV_PYTHON = "/home/ubuntu/macro-venv/bin/python3"


class TestResolveAsOfDates(unittest.TestCase):
    """Test the deterministic freshness/as-of date resolution."""

    def test_as_of_latest_when_no_date(self):
        """When requested_date is None, return latest available per table."""
        result = resolve_as_of_dates(SOURCE_DB, None)
        self.assertIsNotNone(result["macro_date"])
        self.assertIsNotNone(result["company_date"])
        self.assertIsNotNone(result["institutional_date"])

    def test_as_of_exact_date(self):
        """When requested_date matches, return that date."""
        result = resolve_as_of_dates(SOURCE_DB, "2026-07-29")
        self.assertEqual(result["macro_date"], "2026-07-29")
        self.assertEqual(result["company_date"], "2026-07-29")
        self.assertEqual(result["institutional_date"], "2026-07-29")

    def test_as_of_future_date_returns_latest(self):
        """When requested_date is in the future, return latest available."""
        result = resolve_as_of_dates(SOURCE_DB, "2099-01-01")
        self.assertIsNotNone(result["macro_date"])
        # macro should be the latest available, not the future date
        self.assertNotEqual(result["macro_date"], "2099-01-01")

    def test_as_of_past_date_returns_none_or_latest(self):
        """When requested_date is before all data, return None."""
        result = resolve_as_of_dates(SOURCE_DB, "2020-01-01")
        # All tables should have None (no data before 2020)
        self.assertIsNone(result["macro_date"])

    def test_as_of_macro_company_differ(self):
        """Macro (daily) and company (monthly) have different cadences."""
        result = resolve_as_of_dates(SOURCE_DB, "2026-08-07")
        self.assertEqual(result["macro_date"], "2026-08-07")
        # Company is monthly — 2026-07-29 is the latest <= 2026-08-07
        self.assertEqual(result["company_date"], "2026-07-29")

    def test_deterministic(self):
        """Same input always produces same output."""
        r1 = resolve_as_of_dates(SOURCE_DB, "2026-08-07")
        r2 = resolve_as_of_dates(SOURCE_DB, "2026-08-07")
        self.assertEqual(r1, r2)


class TestBridgeSeeding(unittest.TestCase):
    """Test bridge seeding from macro_history.db."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="bridge_full_")

    def _fresh_target(self):
        """Return a fresh target DB path."""
        path = os.path.join(self.tmpdir, f"test-{self.id()}.db")
        if os.path.exists(path):
            os.unlink(path)
        return path

    def test_seed_macro_signals_nonzero(self):
        """Macro signals are seeded with non-zero values."""
        target = self._fresh_target()
        result = seed_from_macro_history(
            source_db=SOURCE_DB,
            target_db=target,
            macro_date="2026-07-29",
            company_codes=None,
            institutional_codes=None,
        )
        self.assertGreater(result["macro"], 0)
        self.assertGreater(result["total"], 0)

    def test_seed_company_signals_nonzero(self):
        """Company signals are seeded with non-zero, non-default values."""
        target = self._fresh_target()
        result = seed_from_macro_history(
            source_db=SOURCE_DB,
            target_db=target,
            company_codes=["2330", "2317"],
            company_date="2026-07-29",
            institutional_codes=["2330", "2317"],
            institutional_date="2026-07-29",
        )
        self.assertGreater(result["company"], 0)
        self.assertGreater(result["institutional"], 0)

        # Verify non-default values
        store = SQLiteStore(target)
        repo = SignalRepository(store)
        company_signals = repo.query(entity_type="company", entity_id="2330")
        non_zero = [s for s in company_signals if s.value != 0.0]
        self.assertGreater(len(non_zero), 0, "All company values are 0.0 (default)")
        store.close()

    def test_seed_industry_signals(self):
        """Industry signals are seeded from institutional aggregation."""
        target = self._fresh_target()
        result = seed_from_macro_history(
            source_db=SOURCE_DB,
            target_db=target,
            industry_ids=["AI", "半導體"],
            institutional_date="2026-07-29",
            industry_config_path=INDUSTRY_CONFIG,
        )
        self.assertGreater(result["industry"], 0, "No industry signals seeded")

        # Verify industry signals exist
        store = SQLiteStore(target)
        repo = SignalRepository(store)
        industry_signals = repo.query(entity_type="industry")
        self.assertGreater(len(industry_signals), 0)

        # Check that industry signals have the right signal types
        signal_types = {s.signal_type for s in industry_signals}
        self.assertIn("foreign_net", signal_types)
        self.assertIn("prop_net", signal_types)

        # Check entity_ids are industry names
        entity_ids = {s.entity_id for s in industry_signals}
        self.assertIn("AI", entity_ids)

        store.close()

    def test_seed_with_auto_resolve_dates(self):
        """Auto-resolve dates picks latest-available per table."""
        target = self._fresh_target()
        result = seed_from_macro_history(
            source_db=SOURCE_DB,
            target_db=target,
            company_codes=["2330"],
            auto_resolve_dates=True,
            requested_date="2026-08-07",
            industry_config_path=INDUSTRY_CONFIG,
        )
        self.assertGreater(result["macro"], 0)
        self.assertGreater(result["company"], 0)
        resolved = result["resolved_dates"]
        self.assertEqual(resolved["macro_date"], "2026-08-07")
        self.assertEqual(resolved["company_date"], "2026-07-29")

    def test_seed_empty_source_returns_zeros(self):
        """Seeding from an empty/non-existent source returns zeros."""
        empty_db = os.path.join(self.tmpdir, "empty.db")
        # Create empty DB with correct schema matching macro_history.db
        import sqlite3
        conn = sqlite3.connect(empty_db)
        conn.execute("CREATE TABLE IF NOT EXISTS macro_daily (date TEXT PRIMARY KEY, us10y REAL, us2y REAL, us13w REAL, dxy REAL, vix REAL, usdtwd REAL, yield_spread REAL, score INTEGER, verdict TEXT, signals_json TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS stock_monthly (date TEXT, code TEXT, name TEXT, sector TEXT, price REAL, eps_ttm REAL, pe_trailing REAL, pe_forward REAL, roe REAL, roa REAL, gross_margin REAL, operating_margin REAL, profit_margin REAL, dividend_rate REAL, dividend_yield REAL, payout_ratio REAL, pb_ratio REAL, revenue_growth REAL, earnings_growth REAL, nim_growth REAL, interest_spread REAL, high_52 REAL, low_52 REAL, dist_from_high REAL, target_mean REAL, peg_ratio REAL, market_cap REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS institutional_daily (date TEXT, code TEXT, foreign_net INTEGER, prop_net INTEGER, total_net INTEGER, trading_days INTEGER)")
        conn.commit()
        conn.close()

        target = self._fresh_target()
        result = seed_from_macro_history(
            source_db=empty_db,
            target_db=target,
        )
        self.assertEqual(result["macro"], 0)
        self.assertEqual(result["company"], 0)
        self.assertEqual(result["institutional"], 0)
        self.assertEqual(result["industry"], 0)
        self.assertEqual(result["total"], 0)

    def test_seed_deterministic(self):
        """Same inputs produce same signal counts."""
        target1 = self._fresh_target()
        target2 = self._fresh_target()
        kwargs = dict(
            source_db=SOURCE_DB,
            company_codes=["2330", "2317"],
            company_date="2026-07-29",
            institutional_codes=["2330", "2317"],
            institutional_date="2026-07-29",
        )
        r1 = seed_from_macro_history(target_db=target1, **kwargs)
        r2 = seed_from_macro_history(target_db=target2, **kwargs)
        self.assertEqual(r1["macro"], r2["macro"])
        self.assertEqual(r1["company"], r2["company"])
        self.assertEqual(r1["institutional"], r2["institutional"])
        self.assertEqual(r1["total"], r2["total"])

    def test_industry_mapping_loads(self):
        """_load_industry_mapping returns non-empty mapping from config."""
        mapping = _load_industry_mapping(INDUSTRY_CONFIG)
        self.assertGreater(len(mapping), 0)
        self.assertIn("AI", mapping)
        self.assertIn("半導體", mapping)
        self.assertGreater(len(mapping["AI"]), 0)

    def test_industry_mapping_missing_file(self):
        """_load_industry_mapping returns empty dict when file missing."""
        mapping = _load_industry_mapping("/nonexistent/path.json")
        self.assertEqual(mapping, {})

    def test_industry_signals_have_correct_entity_type(self):
        """Industry signals have entity_type='industry', not 'company'."""
        target = self._fresh_target()
        result = seed_from_macro_history(
            source_db=SOURCE_DB,
            target_db=target,
            industry_ids=["AI"],
            institutional_date="2026-07-29",
            industry_config_path=INDUSTRY_CONFIG,
        )
        store = SQLiteStore(target)
        repo = SignalRepository(store)
        all_industry = repo.query(entity_type="industry")
        for sig in all_industry:
            self.assertEqual(sig.entity_type, "industry")
            self.assertEqual(sig.entity_id, "AI")
        store.close()


class TestPipelineExportWithSeedFromHistory(unittest.TestCase):
    """Test the --seed-from-history CLI integration."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="cli_seed_")
        cls.target_db = os.path.join(cls.tmpdir, "seed-intelligence.db")

    def test_01_seed_from_history_pipeline_export(self):
        """pipeline-export --seed-from-history produces non-default scores."""
        output_dir = os.path.join(self.tmpdir, "pipeline_output")
        os.makedirs(output_dir, exist_ok=True)

        if os.path.exists(self.target_db):
            os.unlink(self.target_db)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        cmd = [
            VENV_PYTHON, "-m", "phase3.cli",
            "pipeline-export",
            "--date", "2026-08-07",
            "--run-label", "seed-test",
            "--output-dir", output_dir,
            "--db-path", self.target_db,
            "--persist",
            "--company", "2330",
            "--company", "2317",
            "--industry", "AI",
            "--industry", "半導體",
            "--seed-from-history",
            "--source-db", SOURCE_DB,
            "--industry-config", INDUSTRY_CONFIG,
            "--json",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=str(REPO),
            timeout=120,
        )
        self.assertEqual(result.returncode, 0,
            f"pipeline-export failed: {result.stderr}")
        # Verify seed output in stderr
        self.assertIn("seed-from-history", result.stderr)
        self.assertIn("resolved_dates", result.stderr)

        # Parse the export envelope — {json: {path: ...}, markdown: {path: ...}}
        export_payload = json.loads(result.stdout)
        json_path = export_payload["json"]["path"]
        self.assertTrue(os.path.exists(json_path))
        with open(json_path) as f:
            artifact = json.load(f)
        summary = artifact.get("summary", {})
        self.assertGreater(summary.get("signal_count", 0), 0,
            "signal_count is 0 — no real signals were seeded")

    def test_02_macro_score_non_default(self):
        """Macro score in the export artifact has non-default values."""
        output_dir = os.path.join(self.tmpdir, "macro_output")
        os.makedirs(output_dir, exist_ok=True)
        target = os.path.join(self.tmpdir, "macro-intel.db")
        if os.path.exists(target):
            os.unlink(target)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        cmd = [
            VENV_PYTHON, "-m", "phase3.cli",
            "pipeline-export",
            "--date", "2026-08-07",
            "--run-label", "macro-real",
            "--output-dir", output_dir,
            "--db-path", target,
            "--persist",
            "--seed-from-history",
            "--source-db", SOURCE_DB,
            "--industry-config", INDUSTRY_CONFIG,
            "--json",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=str(REPO),
            timeout=120,
        )
        self.assertEqual(result.returncode, 0,
            f"pipeline-export failed: {result.stderr}")

        # Parse the JSON export envelope (stdout)
        export_payload = json.loads(result.stdout)
        # The export returns {json: {...}, markdown: {...}} — the
        # intelligence report is the json artifact.
        json_path = export_payload["json"]["path"]
        with open(json_path) as f:
            artifact = json.load(f)
        summary = artifact.get("summary", {})
        self.assertGreater(summary.get("signal_count", 0), 0,
            "signal_count is 0 — no real signals were seeded")
        self.assertGreater(summary.get("macro_score_count", 0), 0,
            "macro_score_count is 0 — macro score was not produced")

    def test_03_company_score_has_signal_nodes(self):
        """Company scores in the export have evidence signal nodes."""
        output_dir = os.path.join(self.tmpdir, "company_output")
        os.makedirs(output_dir, exist_ok=True)
        target = os.path.join(self.tmpdir, "company-intel.db")
        if os.path.exists(target):
            os.unlink(target)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        cmd = [
            VENV_PYTHON, "-m", "phase3.cli",
            "pipeline-export",
            "--date", "2026-07-29",
            "--run-label", "company-real",
            "--output-dir", output_dir,
            "--db-path", target,
            "--persist",
            "--company", "2330",
            "--seed-from-history",
            "--source-db", SOURCE_DB,
            "--industry-config", INDUSTRY_CONFIG,
            "--json",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=str(REPO),
            timeout=120,
        )
        self.assertEqual(result.returncode, 0,
            f"pipeline-export failed: {result.stderr}")

        # Parse the export envelope
        export_payload = json.loads(result.stdout)
        json_path = export_payload["json"]["path"]
        with open(json_path) as f:
            artifact = json.load(f)
        summary = artifact.get("summary", {})
        self.assertGreater(summary.get("company_score_count", 0), 0,
            "company_score_count is 0")

        # Check graph_writes for signal nodes
        result_obj = artifact.get("result", {})
        for gw in result_obj.get("graph_writes", []):
            if "company" in gw.get("score_node_id", ""):
                self.assertGreater(len(gw["signal_node_ids"]), 0,
                    f"company score {gw['score_node_id']} has 0 signal nodes — "
                    f"score is default/synthetic, not real")

    def test_04_backward_compat_without_seed_flag(self):
        """pipeline-export without --seed-from-history still works (dry-run)."""
        output_dir = os.path.join(self.tmpdir, "compat_output")
        os.makedirs(output_dir, exist_ok=True)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        cmd = [
            VENV_PYTHON, "-m", "phase3.cli",
            "pipeline-export",
            "--date", "2026-07-29",
            "--run-label", "compat-test",
            "--output-dir", output_dir,
            "--json",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=str(REPO),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0,
            f"pipeline-export without seed failed: {result.stderr}")

    def test_05_seed_refuses_macro_history_target(self):
        """--seed-from-history refuses to seed into macro_history.db."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        cmd = [
            VENV_PYTHON, "-m", "phase3.cli",
            "pipeline-export",
            "--date", "2026-07-29",
            "--output-dir", "/tmp/should_not_exist",
            "--db-path", "macro_history.db",
            "--seed-from-history",
            "--json",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=str(REPO),
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0,
            "Should refuse to seed into macro_history.db")
        self.assertIn("reserved", result.stderr.lower())


class TestEndToEndVerification(unittest.TestCase):
    """End-to-end verification showing real emitted score handles."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="e2e_")
        cls.target_db = os.path.join(cls.tmpdir, "e2e-intelligence.db")
        cls.output_dir = os.path.join(cls.tmpdir, "e2e_output")
        os.makedirs(cls.output_dir, exist_ok=True)

        # Run the full pipeline with --seed-from-history
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        cmd = [
            VENV_PYTHON, "-m", "phase3.cli",
            "pipeline-export",
            "--date", "2026-08-07",
            "--run-label", "e2e-real",
            "--output-dir", cls.output_dir,
            "--db-path", cls.target_db,
            "--persist",
            "--company", "2330",
            "--company", "2317",
            "--industry", "AI",
            "--seed-from-history",
            "--source-db", SOURCE_DB,
            "--industry-config", INDUSTRY_CONFIG,
            "--json",
        ]
        cls.result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=str(REPO),
            timeout=120,
        )
        if cls.result.returncode == 0:
            # Parse the export envelope — {json: {path: ...}, markdown: {path: ...}}
            export_payload = json.loads(cls.result.stdout)
            json_path = export_payload["json"]["path"]
            with open(json_path) as f:
                cls.artifact = json.load(f)
        else:
            cls.artifact = None

    def test_e2e_success(self):
        """The end-to-end pipeline with seeding succeeds."""
        self.assertEqual(self.result.returncode, 0,
            f"E2E failed: {self.result.stderr}")
        self.assertIsNotNone(self.artifact)

    def test_e2e_signal_count_nonzero(self):
        """Signal count > 0 (real signals were loaded)."""
        summary = self.artifact.get("summary", {})
        self.assertGreater(summary.get("signal_count", 0), 0)

    def test_e2e_company_scores_present(self):
        """Company scores are present and have evidence."""
        summary = self.artifact.get("summary", {})
        self.assertGreater(summary.get("company_score_count", 0), 0)

    def test_e2e_macro_score_present(self):
        """Macro score is present."""
        summary = self.artifact.get("summary", {})
        self.assertGreater(summary.get("macro_score_count", 0), 0,
            "macro_score_count is 0")

    def test_e2e_industry_score_present(self):
        """Industry score is present (at least one industry)."""
        summary = self.artifact.get("summary", {})
        self.assertGreater(summary.get("industry_score_count", 0), 0,
            "industry_score_count is 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)