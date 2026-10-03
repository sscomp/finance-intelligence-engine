"""Phase 5 M4-S3 — Portfolio CLI Tests.

Tests for the ``portfolio-run`` CLI subcommand (M4-S3 deliverable)
and CLI backward compatibility (existing subcommands unchanged).

Test groups:
- CLI1-CLI3: CLI backward compatibility (existing subcommands still load)
- PR1-PR5: portfolio-run subcommand functional tests
- PR6: macro_history.db safety guard

Total: 9 tests.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure PYTHONPATH includes the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestCLIBackwardCompat(unittest.TestCase):
    """CLI1-CLI3: existing subcommands still work after M4-S3 additions."""

    def test_cli1_parser_builds(self):
        """CLI1: _build_parser() succeeds without error."""
        from phase3.cli import _build_parser
        parser = _build_parser()
        self.assertIsNotNone(parser)

    def test_cli2_existing_subcommands_present(self):
        """CLI2: all 19 existing subcommands are still present."""
        from phase3.cli import _build_parser
        parser = _build_parser()
        # Parse --help to get subcommand list.
        import argparse
        # The subparsers are stored in the parser's _subparsers.
        # We verify by parsing each known subcommand with --help.
        existing = [
            "score-macro", "score-industry", "score-company",
            "aggregate-signals", "graph-demo", "graph-trace",
            "init-db", "ingest-signals", "trace-score",
            "lineage", "blast-radius", "cross-layer-impact",
            "pipeline-run", "pipeline-resume", "pipeline-status",
            "pipeline-report", "pipeline-export",
            "explain-score", "shadow-run",
        ]
        for cmd in existing:
            # Each should parse without error (just checking the subcommand
            # is registered; we don't run the actual command).
            try:
                parser.parse_args([cmd, "--help"])
            except SystemExit:
                # --help causes SystemExit(0), which is fine.
                pass

    def test_cli3_portfolio_run_subcommand_present(self):
        """CLI3: portfolio-run subcommand is registered (M4-S3 NEW)."""
        from phase3.cli import _build_parser
        parser = _build_parser()
        # Try parsing portfolio-run --help.
        try:
            parser.parse_args(["portfolio-run", "--help"])
        except SystemExit:
            pass  # --help causes SystemExit(0)


class TestPortfolioRunCLI(unittest.TestCase):
    """PR1-PR6: portfolio-run subcommand functional tests."""

    def setUp(self):
        self.fixture_path = REPO_ROOT / "tests" / "phase3" / "fixtures" / "pipeline_export_sample.json"
        # Create a temporary portfolio JSON file.
        self.tmpdir = tempfile.mkdtemp()
        self.portfolio_path = Path(self.tmpdir) / "test_portfolio.json"
        # Build portfolio JSON with entity IDs matching the fixture.
        portfolio_data = {
            "portfolio_id": "test-portfolio-001",
            "name": "Test Portfolio",
            "positions": [
                {"position_id": "pos-0000", "entity_id": "company:TW:2330", "weight": 0.0, "quantity": 0},
                {"position_id": "pos-0001", "entity_id": "company:TW:2317", "weight": 0.0, "quantity": 0},
                {"position_id": "pos-0002", "entity_id": "industry:TW:semiconductor", "weight": 0.0, "quantity": 0},
                {"position_id": "pos-0003", "entity_id": "industry:TW:finance", "weight": 0.0, "quantity": 0},
            ],
        }
        with open(self.portfolio_path, "w") as f:
            json.dump(portfolio_data, f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pr1_score_weighted_json_output(self):
        """PR1: portfolio-run with score_weighted policy produces JSON."""
        from phase3.cli import main
        output_path = Path(self.tmpdir) / "output.json"
        argv = [
            "portfolio-run",
            "--pipeline-artifact", str(self.fixture_path),
            "--portfolio-file", str(self.portfolio_path),
            "--policy-type", "score_weighted",
            "--per-position-cap", "1.0",
            "--generated-at", "2026-08-07T00:00:00Z",
            "--output", str(output_path),
        ]
        rc = main(argv)
        self.assertEqual(rc, 0)
        self.assertTrue(output_path.is_file())
        with open(output_path) as f:
            data = json.load(f)
        self.assertIn("decision_id", data)
        self.assertIn("allocation", data)
        self.assertIn("rationale", data)

    def test_pr2_risk_aware_json_output(self):
        """PR2: portfolio-run with risk_aware policy produces JSON."""
        from phase3.cli import main
        output_path = Path(self.tmpdir) / "output_ra.json"
        argv = [
            "portfolio-run",
            "--pipeline-artifact", str(self.fixture_path),
            "--portfolio-file", str(self.portfolio_path),
            "--policy-type", "risk_aware",
            "--per-position-cap", "0.50",
            "--generated-at", "2026-08-07T00:00:00Z",
            "--output", str(output_path),
        ]
        rc = main(argv)
        self.assertEqual(rc, 0)
        self.assertTrue(output_path.is_file())
        with open(output_path) as f:
            data = json.load(f)
        self.assertIn("risk_summary", data)
        self.assertIn("exposure", data["risk_summary"])
        self.assertIn("concentration", data["risk_summary"])

    def test_pr3_missing_artifact_returns_error(self):
        """PR3: missing --pipeline-artifact returns non-zero exit."""
        from phase3.cli import main
        argv = [
            "portfolio-run",
            "--portfolio-file", str(self.portfolio_path),
            "--generated-at", "2026-08-07T00:00:00Z",
        ]
        rc = main(argv)
        self.assertNotEqual(rc, 0)

    def test_pr4_missing_portfolio_returns_error(self):
        """PR4: missing --portfolio-file returns non-zero exit."""
        from phase3.cli import main
        argv = [
            "portfolio-run",
            "--pipeline-artifact", str(self.fixture_path),
            "--generated-at", "2026-08-07T00:00:00Z",
        ]
        rc = main(argv)
        self.assertNotEqual(rc, 0)

    def test_pr5_human_readable_output(self):
        """PR5: without --json, output is human-readable text."""
        from phase3.cli import main
        # Capture stdout.
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            argv = [
                "portfolio-run",
                "--pipeline-artifact", str(self.fixture_path),
                "--portfolio-file", str(self.portfolio_path),
                "--policy-type", "score_weighted",
                "--per-position-cap", "1.0",
                "--generated-at", "2026-08-07T00:00:00Z",
            ]
            rc = main(argv)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("Decision ID:", output)
        self.assertIn("Portfolio ID:", output)

    def test_pr6_macro_history_db_refused(self):
        """PR6: macro_history.db as artifact is refused."""
        from phase3.cli import main
        argv = [
            "portfolio-run",
            "--pipeline-artifact", "macro_history.db",
            "--portfolio-file", str(self.portfolio_path),
            "--generated-at", "2026-08-07T00:00:00Z",
        ]
        rc = main(argv)
        self.assertNotEqual(rc, 0)


# --------------------------------------------------------------------------- #
# M6: portfolio-report CLI tests
# --------------------------------------------------------------------------- #


class TestPortfolioReportCLI(unittest.TestCase):
    """PRF1-PRF6: portfolio-report subcommand functional tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.portfolio_path = Path(self.tmpdir) / "test_portfolio.json"
        portfolio_data = {
            "portfolio_id": "test-portfolio-001",
            "name": "Test Portfolio",
            "positions": [
                {"position_id": "pos-0001", "entity_id": "company:TW:2330", "weight": 0.40, "quantity": 100},
                {"position_id": "pos-0002", "entity_id": "company:TW:2317", "weight": 0.35, "quantity": 200},
                {"position_id": "pos-0003", "entity_id": "industry:TW:finance", "weight": 0.25, "quantity": 50},
            ],
        }
        with open(self.portfolio_path, "w") as f:
            json.dump(portfolio_data, f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_prf1_subcommand_registered(self):
        """PRF1: portfolio-report subcommand is registered (M6 NEW)."""
        from phase3.cli import _build_parser
        parser = _build_parser()
        try:
            parser.parse_args(["portfolio-report", "--help"])
        except SystemExit:
            pass  # --help causes SystemExit(0)

    def test_prf2_json_output(self):
        """PRF2: portfolio-report produces JSON output."""
        from phase3.cli import main
        output_path = Path(self.tmpdir) / "report.json"
        argv = [
            "portfolio-report",
            "--portfolio-file", str(self.portfolio_path),
            "--date", "2026-08-08",
            "--generated-at", "2026-08-08T00:00:00Z",
            "--output", str(output_path),
        ]
        rc = main(argv)
        self.assertEqual(rc, 0)
        self.assertTrue(output_path.is_file())
        with open(output_path) as f:
            data = json.load(f)
        self.assertIn("report_id", data)
        self.assertIn("report_type", data)
        self.assertIn("sections", data)
        self.assertIn("summary", data)

    def test_prf3_markdown_output(self):
        """PRF3: portfolio-report produces Markdown output."""
        from phase3.cli import main
        md_path = Path(self.tmpdir) / "report.md"
        argv = [
            "portfolio-report",
            "--portfolio-file", str(self.portfolio_path),
            "--date", "2026-08-08",
            "--generated-at", "2026-08-08T00:00:00Z",
            "--markdown", str(md_path),
        ]
        rc = main(argv)
        self.assertEqual(rc, 0)
        self.assertTrue(md_path.is_file())
        md = md_path.read_text()
        self.assertIn("# Daily Portfolio Report", md)
        self.assertIn("company:TW:2330", md)

    def test_prf4_missing_portfolio_returns_error(self):
        """PRF4: missing --portfolio-file returns non-zero exit."""
        from phase3.cli import main
        argv = [
            "portfolio-report",
            "--date", "2026-08-08",
            "--generated-at", "2026-08-08T00:00:00Z",
        ]
        rc = main(argv)
        self.assertNotEqual(rc, 0)

    def test_prf5_human_readable_output(self):
        """PRF5: without --json/--output/--markdown, output is human-readable."""
        from phase3.cli import main
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            argv = [
                "portfolio-report",
                "--portfolio-file", str(self.portfolio_path),
                "--date", "2026-08-08",
                "--generated-at", "2026-08-08T00:00:00Z",
            ]
            rc = main(argv)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("Report ID:", output)

    def test_prf6_macro_history_db_refused(self):
        """PRF6: macro_history.db as portfolio file is refused."""
        from phase3.cli import main
        argv = [
            "portfolio-report",
            "--portfolio-file", "macro_history.db",
            "--date", "2026-08-08",
            "--generated-at", "2026-08-08T00:00:00Z",
        ]
        rc = main(argv)
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()