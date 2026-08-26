#!/usr/bin/env python3
"""MVR 2026-08-10: Wrapper-level coverage verification.

Verifies that:
1. taiwan50_config.json constituents parse to valid --company args
2. industry_config.json top-level keys parse to valid --industry args
3. pipeline-export accepts the derived args without error
4. Output artifact contains company and industry score handles (coverage present)
5. Macro output remains present
6. Values may be defaults (0.0) due to empty SignalRepository — that is expected

Usage:
    PYTHONPATH=/home/ubuntu/macro-report python3 tests/test_mvr_wrapper_coverage.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path("/home/ubuntu/macro-report")
VENV_PYTHON = "/home/ubuntu/macro-venv/bin/python3"
TW50_CONFIG = REPO / "taiwan50_config.json"
INDUSTRY_CONFIG = REPO / "industry_config.json"


def _parse_company_args():
    """Parse taiwan50_config.json → list of --company <code> pairs."""
    with open(TW50_CONFIG) as f:
        data = json.load(f)
    args = []
    for c in data["constituents"]:
        args.extend(["--company", c["code"]])
    return args


def _parse_industry_args():
    """Parse industry_config.json → list of --industry <id> pairs."""
    with open(INDUSTRY_CONFIG) as f:
        data = json.load(f)
    args = []
    for k in data:
        args.extend(["--industry", k])
    return args


class TestConfigParsing(unittest.TestCase):
    """Verify config files parse correctly and produce valid args."""

    def test_taiwan50_config_has_constituents(self):
        with open(TW50_CONFIG) as f:
            data = json.load(f)
        self.assertIn("constituents", data)
        self.assertGreater(len(data["constituents"]), 0)
        for c in data["constituents"]:
            self.assertIn("code", c)
            self.assertIsInstance(c["code"], str)
            self.assertGreater(len(c["code"]), 0)

    def test_industry_config_has_sectors(self):
        with open(INDUSTRY_CONFIG) as f:
            data = json.load(f)
        self.assertGreater(len(data), 0)
        for k, v in data.items():
            self.assertIsInstance(k, str)
            self.assertGreater(len(k), 0)

    def test_company_args_nonempty(self):
        args = _parse_company_args()
        self.assertGreater(len(args), 0)
        # Should be pairs of --company <code>
        self.assertEqual(len(args) % 2, 0)
        for i in range(0, len(args), 2):
            self.assertEqual(args[i], "--company")

    def test_industry_args_nonempty(self):
        args = _parse_industry_args()
        self.assertGreater(len(args), 0)
        self.assertEqual(len(args) % 2, 0)
        for i in range(0, len(args), 2):
            self.assertEqual(args[i], "--industry")


class TestPipelineExportCoverage(unittest.TestCase):
    """Verify pipeline-export with --company and --industry produces coverage."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="mvr_test_")
        cls.date = "2099-12-31"  # Future date to avoid collisions with real artifacts
        cls.output_dir = cls.tmpdir
        cls.env = os.environ.copy()
        cls.env["PYTHONPATH"] = str(REPO)

    def _run_pipeline_export(self, extra_args=None):
        """Run pipeline-export with given extra args, return (rc, stdout, stderr)."""
        cmd = [
            VENV_PYTHON, "-m", "phase3.cli", "pipeline-export",
            "--date", self.date,
            "--run-label", f"mvr-test-{self.date}",
            "--output-dir", self.output_dir,
        ]
        if extra_args:
            cmd.extend(extra_args)
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=self.env, cwd=str(REPO)
        )
        return result.returncode, result.stdout, result.stderr

    def test_macro_only_baseline(self):
        """Without --company/--industry, only macro coverage present."""
        rc, stdout, stderr = self._run_pipeline_export()
        self.assertEqual(rc, 0, f"pipeline-export failed: {stderr}")
        # Find the JSON artifact
        json_path = Path(self.output_dir) / f"mvr-test-{self.date}.intelligence_report.json"
        self.assertTrue(json_path.exists(), f"Artifact not found at {json_path}")
        with open(json_path) as f:
            artifact = json.load(f)
        self.assertEqual(artifact["summary"]["macro_score_count"], 1)
        self.assertEqual(artifact["summary"]["company_score_count"], 0)
        self.assertEqual(artifact["summary"]["industry_score_count"], 0)
        # Clean up
        json_path.unlink(missing_ok=True)
        md_path = Path(self.output_dir) / f"mvr-test-{self.date}.intelligence_report.md"
        md_path.unlink(missing_ok=True)

    def test_with_company_and_industry_coverage(self):
        """With --company and --industry, company/industry coverage present."""
        company_args = _parse_company_args()
        industry_args = _parse_industry_args()
        rc, stdout, stderr = self._run_pipeline_export(
            company_args + industry_args
        )
        self.assertEqual(rc, 0, f"pipeline-export failed: {stderr}")
        json_path = Path(self.output_dir) / f"mvr-test-{self.date}.intelligence_report.json"
        self.assertTrue(json_path.exists(), f"Artifact not found at {json_path}")
        with open(json_path) as f:
            artifact = json.load(f)
        # Macro should still be present
        self.assertGreaterEqual(artifact["summary"]["macro_score_count"], 1)
        # Company coverage should now be present (>0 handles)
        company_count = artifact["summary"]["company_score_count"]
        industry_count = artifact["summary"]["industry_score_count"]
        self.assertGreater(company_count, 0,
            f"company_score_count still 0 — company coverage NOT present")
        self.assertGreater(industry_count, 0,
            f"industry_score_count still 0 — industry coverage NOT present")
        # Verify metadata has non-empty entity specs
        self.assertGreater(len(artifact["result"]["metadata"]["company_specs"]), 0)
        self.assertGreater(len(artifact["result"]["metadata"]["industry_ids"]), 0)
        # Verify evidence_handles contain company and industry types
        handle_types = set(h["scorer_type"] for h in artifact["result"]["evidence_handles"])
        self.assertIn("macro", handle_types)
        self.assertIn("company", handle_types)
        self.assertIn("industry", handle_types)
        # Clean up
        json_path.unlink(missing_ok=True)
        md_path = Path(self.output_dir) / f"mvr-test-{self.date}.intelligence_report.md"
        md_path.unlink(missing_ok=True)

    def test_company_args_pass_parse_success(self):
        """Verify company specs are passed and parse successfully via CLI."""
        company_args = _parse_company_args()
        rc, stdout, stderr = self._run_pipeline_export(company_args)
        self.assertEqual(rc, 0, f"pipeline-export with company args failed: {stderr}")
        json_path = Path(self.output_dir) / f"mvr-test-{self.date}.intelligence_report.json"
        self.assertTrue(json_path.exists())
        with open(json_path) as f:
            artifact = json.load(f)
        self.assertGreater(artifact["summary"]["company_score_count"], 0)
        # Clean up
        json_path.unlink(missing_ok=True)
        md_path = Path(self.output_dir) / f"mvr-test-{self.date}.intelligence_report.md"
        md_path.unlink(missing_ok=True)

    def test_industry_args_pass_parse_success(self):
        """Verify industry IDs are passed and parse successfully via CLI."""
        industry_args = _parse_industry_args()
        rc, stdout, stderr = self._run_pipeline_export(industry_args)
        self.assertEqual(rc, 0, f"pipeline-export with industry args failed: {stderr}")
        json_path = Path(self.output_dir) / f"mvr-test-{self.date}.intelligence_report.json"
        self.assertTrue(json_path.exists())
        with open(json_path) as f:
            artifact = json.load(f)
        self.assertGreater(artifact["summary"]["industry_score_count"], 0)
        # Clean up
        json_path.unlink(missing_ok=True)
        md_path = Path(self.output_dir) / f"mvr-test-{self.date}.intelligence_report.md"
        md_path.unlink(missing_ok=True)


class TestBackwardCompatibility(unittest.TestCase):
    """Verify existing wrapper invocations remain backward compatible."""

    def test_wrapper_scripts_exist_and_are_executable(self):
        for script in ["run.sh", "run_weekly.sh", "run_monthly.sh"]:
            path = REPO / script
            self.assertTrue(path.exists(), f"{script} not found")
            self.assertTrue(os.access(path, os.X_OK), f"{script} not executable")

    def test_wrapper_scripts_have_set_u(self):
        for script in ["run.sh", "run_weekly.sh", "run_monthly.sh"]:
            content = (REPO / script).read_text()
            self.assertIn("set -u", content, f"{script} missing set -u")

    def test_wrapper_scripts_have_basename_guard(self):
        for script in ["run.sh", "run_weekly.sh", "run_monthly.sh"]:
            content = (REPO / script).read_text()
            self.assertIn("macro_history.db", content,
                f"{script} missing basename guard")

    def test_wrapper_scripts_have_config_extraction(self):
        for script in ["run.sh", "run_weekly.sh", "run_monthly.sh"]:
            content = (REPO / script).read_text()
            self.assertIn("taiwan50_config.json", content,
                f"{script} missing taiwan50_config.json reference")
            self.assertIn("industry_config.json", content,
                f"{script} missing industry_config.json reference")
            self.assertIn("COMPANY_ARGS", content,
                f"{script} missing COMPANY_ARGS variable")
            self.assertIn("INDUSTRY_ARGS", content,
                f"{script} missing INDUSTRY_ARGS variable")

    def test_wrapper_scripts_preserve_existing_run_labels(self):
        """Each wrapper must still use its original run-label pattern."""
        run_sh = (REPO / "run.sh").read_text()
        self.assertIn('"${ARTIFACT_DATE}"', run_sh)
        run_weekly = (REPO / "run_weekly.sh").read_text()
        self.assertIn('"${ARTIFACT_DATE}-weekly"', run_weekly)
        run_monthly = (REPO / "run_monthly.sh").read_text()
        self.assertIn('"${ARTIFACT_DATE}-monthly"', run_monthly)


if __name__ == "__main__":
    unittest.main(verbosity=2)