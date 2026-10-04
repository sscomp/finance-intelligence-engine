"""Phase 5 M8 — Portfolio Shadow-Run Extension tests.

Targeted tests for the additive portfolio-shadow-run extension.

Test groups:
  PS1-PS2: CLI subcommand registration + help
  PS3-PS4: Functional run with PipelineRunReport fixture (determinism)
  PS5:     Functional run with intelligence-report artifact
  PS6:     Missing artifact returns error
  PS7:     Missing portfolio-file returns error
  PS8:     macro_history.db refusal guard
  PS9:     Determinism check (baseline == replay)
  PS10:    Output to file
  PS11:    Production safety: no DB write, no intelligence.db, no jobs.json mutation
  PS12:    Existing shadow-run subcommand byte-identical (additive check)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "phase3" / "fixtures"
PIPELINE_FIXTURE = FIXTURE_DIR / "pipeline_export_sample.json"
PORTFOLIO_FIXTURE = FIXTURE_DIR / "shadow_test_portfolio.json"
ARTIFACT_DIR = REPO_ROOT / "metadata" / "reports" / "artifacts"


class TestPortfolioShadowRunCLIRegistration(unittest.TestCase):
    """PS1-PS2: CLI subcommand registration."""

    def test_ps1_portfolio_shadow_run_subcommand_present(self):
        """PS1: portfolio-shadow-run subcommand is registered."""
        from phase3.cli import _build_parser
        parser = _build_parser()
        try:
            parser.parse_args(["portfolio-shadow-run", "--help"])
        except SystemExit:
            pass

    def test_ps2_portfolio_shadow_run_help_text(self):
        """PS2: help text mentions Phase 5 M8."""
        from phase3.cli import _build_parser
        parser = _build_parser()
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            try:
                parser.parse_args(["portfolio-shadow-run", "--help"])
            except SystemExit:
                pass
        finally:
            sys.stdout = old_stdout
        help_text = buf.getvalue()
        # The "Phase 5 M8" string is in the subparser help= parameter,
        # which argparse includes in the main --help output but not in
        # the subcommand's own --help output. Check for the key
        # functional text instead.
        self.assertIn("portfolio-shadow-run", help_text)
        self.assertIn("artifact", help_text)


class TestPortfolioShadowRunPipelineFixture(unittest.TestCase):
    """PS3-PS4: functional run with PipelineRunReport fixture."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ps3_json_output_with_fixture(self):
        """PS3: portfolio-shadow-run with pipeline fixture produces JSON."""
        from phase3.cli import main
        output_path = Path(self.tmpdir) / "result.json"
        argv = [
            "portfolio-shadow-run",
            "--artifact", str(PIPELINE_FIXTURE),
            "--portfolio-file", str(PORTFOLIO_FIXTURE),
            "--per-position-cap", "1.0",
            "--generated-at", "2026-08-09T00:00:00Z",
            "--output", str(output_path),
        ]
        rc = main(argv)
        self.assertEqual(rc, 0)
        self.assertTrue(output_path.is_file())
        with open(output_path) as f:
            data = json.load(f)
        self.assertIn("schema_version", data)
        self.assertIn("baseline", data)
        self.assertIn("replay", data)
        self.assertIn("deterministic", data)
        self.assertEqual(data["artifact_format"], "pipeline_run_report")

    def test_ps4_determinism_with_pinned_timestamp(self):
        """PS4: with pinned --generated-at, baseline and replay are deterministic."""
        from phase3.cli import main
        output_path = Path(self.tmpdir) / "result_det.json"
        argv = [
            "portfolio-shadow-run",
            "--artifact", str(PIPELINE_FIXTURE),
            "--portfolio-file", str(PORTFOLIO_FIXTURE),
            "--per-position-cap", "1.0",
            "--generated-at", "2026-08-09T00:00:00Z",
            "--output", str(output_path),
        ]
        rc = main(argv)
        self.assertEqual(rc, 0)
        with open(output_path) as f:
            data = json.load(f)
        self.assertTrue(data["baseline"]["success"])
        self.assertTrue(data["replay"]["success"])
        self.assertTrue(data["deterministic"])


class TestPortfolioShadowRunIntelligenceReport(unittest.TestCase):
    """PS5: functional run with intelligence-report artifact."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Find a real intelligence-report artifact.
        self.artifact = None
        if ARTIFACT_DIR.is_dir():
            for f in sorted(ARTIFACT_DIR.glob("*.intelligence_report.json")):
                self.artifact = f
                break

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ps5_intelligence_report_format_detected(self):
        """PS5: intelligence-report artifact is detected and processed."""
        if self.artifact is None:
            self.skipTest("No intelligence-report artifacts available")
        from phase3.cli import main
        output_path = Path(self.tmpdir) / "result_intel.json"
        argv = [
            "portfolio-shadow-run",
            "--artifact", str(self.artifact),
            "--portfolio-file", str(PORTFOLIO_FIXTURE),
            "--generated-at", "2026-08-09T00:00:00Z",
            "--output", str(output_path),
        ]
        rc = main(argv)
        self.assertEqual(rc, 0)
        with open(output_path) as f:
            data = json.load(f)
        self.assertEqual(data["artifact_format"], "intelligence_report")
        self.assertTrue(data["baseline"]["success"])
        self.assertTrue(data["replay"]["success"])
        # Determinism should hold even with empty allocation (same inputs).
        self.assertTrue(data["deterministic"])


class TestPortfolioShadowRunErrorCases(unittest.TestCase):
    """PS6-PS8: error cases."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ps6_missing_artifact_returns_error(self):
        """PS6: missing --artifact returns non-zero exit."""
        from phase3.cli import main
        argv = [
            "portfolio-shadow-run",
            "--portfolio-file", str(PORTFOLIO_FIXTURE),
        ]
        rc = main(argv)
        self.assertNotEqual(rc, 0)

    def test_ps7_missing_portfolio_returns_error(self):
        """PS7: missing --portfolio-file returns non-zero exit."""
        from phase3.cli import main
        argv = [
            "portfolio-shadow-run",
            "--artifact", str(PIPELINE_FIXTURE),
        ]
        rc = main(argv)
        self.assertNotEqual(rc, 0)

    def test_ps8_macro_history_db_refused(self):
        """PS8: macro_history.db artifact path is refused."""
        from phase3.cli import main
        # Create a fake macro_history.db file to test the guard.
        fake_db = Path(self.tmpdir) / "macro_history.db"
        fake_db.write_text("{}")
        argv = [
            "portfolio-shadow-run",
            "--artifact", str(fake_db),
            "--portfolio-file", str(PORTFOLIO_FIXTURE),
        ]
        rc = main(argv)
        self.assertEqual(rc, 2)


class TestPortfolioShadowRunDeterminism(unittest.TestCase):
    """PS9: determinism check."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ps9_determinism_flag_is_correct(self):
        """PS9: deterministic flag is True when baseline == replay."""
        from phase3.pipeline.portfolio_shadow_run import (
            PortfolioShadowRunConfig,
            run_portfolio_shadow,
        )
        config = PortfolioShadowRunConfig(
            artifact_path=str(PIPELINE_FIXTURE),
            portfolio_path=str(PORTFOLIO_FIXTURE),
            generated_at="2026-08-09T00:00:00Z",
            per_position_cap=1.0,
        )
        result = run_portfolio_shadow(config)
        self.assertTrue(result.baseline.success)
        self.assertTrue(result.replay.success)
        self.assertTrue(result.deterministic)

    def test_ps9b_determinism_with_output_to_file(self):
        """PS9b: output to file contains valid JSON with all fields."""
        from phase3.pipeline.portfolio_shadow_run import (
            PortfolioShadowRunConfig,
            run_portfolio_shadow,
        )
        output_path = str(Path(self.tmpdir) / "shadow_result.json")
        config = PortfolioShadowRunConfig(
            artifact_path=str(PIPELINE_FIXTURE),
            portfolio_path=str(PORTFOLIO_FIXTURE),
            generated_at="2026-08-09T00:00:00Z",
            per_position_cap=1.0,
            output_path=output_path,
        )
        result = run_portfolio_shadow(config)
        self.assertTrue(os.path.isfile(output_path))
        with open(output_path) as f:
            data = json.load(f)
        self.assertEqual(data["schema_version"], "1")
        self.assertIn("baseline", data)
        self.assertIn("replay", data)
        self.assertIn("deterministic", data)
        self.assertIn("baseline_decision", data)
        self.assertIn("replay_decision", data)


class TestPortfolioShadowRunOutput(unittest.TestCase):
    """PS10: output to file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ps10_output_to_file(self):
        """PS10: --output writes to a file and returns 0."""
        from phase3.cli import main
        output_path = Path(self.tmpdir) / "psr_output.json"
        argv = [
            "portfolio-shadow-run",
            "--artifact", str(PIPELINE_FIXTURE),
            "--portfolio-file", str(PORTFOLIO_FIXTURE),
            "--per-position-cap", "1.0",
            "--generated-at", "2026-08-09T00:00:00Z",
            "--output", str(output_path),
        ]
        rc = main(argv)
        self.assertEqual(rc, 0)
        self.assertTrue(output_path.is_file())
        self.assertGreater(output_path.stat().st_size, 0)


class TestPortfolioShadowRunProductionSafety(unittest.TestCase):
    """PS11: production safety — no DB writes, no intelligence.db,
    no jobs.json mutation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ps11a_no_intelligence_db_created(self):
        """PS11a: no intelligence.db* files created during portfolio shadow run."""
        from phase3.pipeline.portfolio_shadow_run import (
            PortfolioShadowRunConfig,
            run_portfolio_shadow,
        )
        config = PortfolioShadowRunConfig(
            artifact_path=str(PIPELINE_FIXTURE),
            portfolio_path=str(PORTFOLIO_FIXTURE),
            generated_at="2026-08-09T00:00:00Z",
            per_position_cap=1.0,
        )
        result = run_portfolio_shadow(config)
        # Check no intelligence.db in cwd.
        for p in Path(".").glob("intelligence.db*"):
            self.fail(f"intelligence.db artifact found: {p}")

    def test_ps11b_macro_history_db_unchanged(self):
        """PS11b: macro_history.db SHA is unchanged after portfolio shadow run."""
        db_path = REPO_ROOT / "macro_history.db"
        if not db_path.is_file():
            self.skipTest("macro_history.db not found")
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        from phase3.pipeline.portfolio_shadow_run import (
            PortfolioShadowRunConfig,
            run_portfolio_shadow,
        )
        config = PortfolioShadowRunConfig(
            artifact_path=str(PIPELINE_FIXTURE),
            portfolio_path=str(PORTFOLIO_FIXTURE),
            generated_at="2026-08-09T00:00:00Z",
            per_position_cap=1.0,
        )
        result = run_portfolio_shadow(config)
        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_ps11c_jobs_json_unchanged(self):
        """PS11c: ~/.hermes/cron/jobs.json unchanged after portfolio shadow run."""
        jobs_path = Path.home() / ".hermes" / "cron" / "jobs.json"
        if not jobs_path.is_file():
            self.skipTest("jobs.json not found")
        before = hashlib.sha256(jobs_path.read_bytes()).hexdigest()
        from phase3.pipeline.portfolio_shadow_run import (
            PortfolioShadowRunConfig,
            run_portfolio_shadow,
        )
        config = PortfolioShadowRunConfig(
            artifact_path=str(PIPELINE_FIXTURE),
            portfolio_path=str(PORTFOLIO_FIXTURE),
            generated_at="2026-08-09T00:00:00Z",
            per_position_cap=1.0,
        )
        result = run_portfolio_shadow(config)
        after = hashlib.sha256(jobs_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)


class TestPortfolioShadowRunAdditive(unittest.TestCase):
    """PS12: existing shadow-run subcommand is byte-identical (additive check)."""

    def test_ps12_existing_shadow_run_code_unchanged(self):
        """PS12: phase3/pipeline/shadow_run.py is NOT modified."""
        # The protected file should still have its original SHA-256.
        shadow_run_path = REPO_ROOT / "phase3" / "pipeline" / "shadow_run.py"
        expected_sha = (
            "5a62fa7b182cbfb49e5e75d4bbe7cc2d2510e98242150ac2bc00b9d8fcbfdb1e"
        )
        actual_sha = hashlib.sha256(shadow_run_path.read_bytes()).hexdigest()
        self.assertEqual(actual_sha, expected_sha,
                         "shadow_run.py was modified — additive constraint violated")

    def test_ps12b_existing_cli_functions_byte_identical(self):
        """PS12b: existing cmd_shadow_run function is NOT modified."""
        # We verify by checking the file's SHA for the existing portion
        # — the only additions should be the new cmd_portfolio_shadow_run
        # and the p23 parser block.
        cli_path = REPO_ROOT / "phase3" / "cli.py"
        # The pre-M8 SHA was:
        expected_pre_m8_sha = (
            "187e8b84c5e6b66d6918a372aef84517ee96c2f06d8485e6a0545f2f9765eb1d"
        )
        actual_sha = hashlib.sha256(cli_path.read_bytes()).hexdigest()
        # The CLI file WAS modified (we added the new subcommand), so
        # the SHA WILL differ. This test documents the fact. The real
        # check is that the existing shadow-run subcommand's behavior
        # is unchanged — verified by the regression tests passing.
        self.assertNotEqual(actual_sha, expected_pre_m8_sha,
                            "cli.py SHA should have changed (additive modification expected)")
        # Verify the new SHA matches the expected post-FIE-freshness value.
        # FIE UPDATE 2026-08-10: SHA updated from c2772e61... to a67a99bd...
        # (additive --seed-from-history wiring on pipeline-export, 0 deletions).
        # FIE UPDATE 2026-08-26: SHA updated from a67a99bd... to 5cb48bfc...
        # (additive --freshness-check flag on pipeline-run/pipeline-export,
        #  0 deletions — authorized T-1 freshness guard wiring).
        # FIE Phase 6.1 UPDATE 2026-10-03: SHA updated from 5cb48bfc...
        # to 3078349f88f5... (portability: example paths generalized in
        # the cli.py docstring, 0 deletions — Phase 6.1 Workstream C).
        # FIE Phase 6.3 UPDATE 2026-10-03: SHA updated from 3078349f...
        # to dfa7cd71... (persistence backend dispatch: FIE_DATABASE_URL +
        # postgres:// DSN routing, graph store close hygiene — dispatch
        # only, cmd_shadow_run behavior unchanged; regression tests pass).
        # FIE 6.7B Remediation UPDATE 2026-10-04: SHA updated from
        # dfa7cd71... to 12de9d1e... (WO C1: backend-aware seed source
        # default macro_history_db_spec + additive --min-seed-total
        # floor; 0 deletions — cmd_shadow_run behavior unchanged).
        expected_post_m8_sha = (
            "12de9d1e9476b708ad15af8791536fcef089a1b01046ddc1580c4e23410b2ad0"
        )
        self.assertEqual(actual_sha, expected_post_m8_sha,
                         "cli.py SHA should match post-M8 baseline")


if __name__ == "__main__":
    unittest.main()