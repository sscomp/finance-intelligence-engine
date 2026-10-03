"""Tests for Phase 3A CLI: 6 subcommands via subprocess + parser coverage.

We invoke the CLI as a subprocess to verify the actual
`python3 -m phase3.cli <subcmd>` user-facing flow (which is the
form broken by the package-shadowing bug). Subprocess tests use
the current interpreter (sys.executable); no network, no live data.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

ENV_BASE = {
    "PYTHONPATH": str(REPO_ROOT),
    "PATH": os.environ.get("PATH", ""),
    "HOME": os.environ.get("HOME", str(REPO_ROOT)),
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
}

# All 6 subcommands, plus the top-level --help, that we want to
# confirm work end-to-end.
SUBCOMMANDS_NO_ARG: list[str] = [
    "score-macro",
    "score-industry",
    "score-company",
    "aggregate-signals",
    "graph-demo",
]


def _run_cli(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run python3 -m phase3.cli with given args. Return CompletedProcess."""
    return subprocess.run(
        [PYTHON, "-m", "phase3.cli", *args],
        cwd=str(REPO_ROOT),
        env=ENV_BASE,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestCLIBasics(unittest.TestCase):
    def test_python_m_phase3_cli_help_exits_zero(self):
        """The package-shadowing bug fix: 'python -m phase3.cli --help' must work."""
        result = _run_cli("--help")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # Help text mentions all subcommands
        for cmd in SUBCOMMANDS_NO_ARG + ["graph-trace"]:
            self.assertIn(cmd, result.stdout)

    def test_python_m_phase3_cli_no_args_fails(self):
        """No args should fail with a usage error (required subcommand)."""
        result = _run_cli()
        self.assertNotEqual(result.returncode, 0)

    def test_python_m_phase3_cli_unknown_subcommand_fails(self):
        result = _run_cli("nope-not-a-real-subcommand")
        self.assertNotEqual(result.returncode, 0)
        # argparse prints to stderr
        self.assertGreater(len(result.stderr), 0)

    def test_cli_subcommand_help_succeeds(self):
        for cmd in ["score-macro", "score-company", "graph-trace"]:
            result = _run_cli(cmd, "--help")
            self.assertEqual(result.returncode, 0, msg=result.stderr)


class TestCLISubcommands(unittest.TestCase):
    """Each subcommand runs the full pipeline (with sample data) and exits 0."""

    def test_score_macro_exits_zero_and_prints_score(self):
        result = _run_cli("score-macro")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("MacroScore", result.stdout)
        self.assertIn("score:", result.stdout)

    def test_score_industry_exits_zero_and_prints_score(self):
        result = _run_cli("score-industry")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("IndustryScore", result.stdout)
        self.assertIn("score:", result.stdout)

    def test_score_company_exits_zero_and_prints_score(self):
        result = _run_cli("score-company")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("CompanyScore", result.stdout)
        self.assertIn("raw_score:", result.stdout)
        # Cross-layer adjustments are present
        self.assertIn("macro_adj:", result.stdout)
        self.assertIn("industry_adj:", result.stdout)
        # Should have at least one cross-layer adjustment
        self.assertIn("Cross-layer adjustments:", result.stdout)

    def test_aggregate_signals_exits_zero_and_prints_buckets(self):
        result = _run_cli("aggregate-signals")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Signal Aggregation", result.stdout)
        self.assertIn("weighted_sum=", result.stdout)
        self.assertIn("buckets:", result.stdout)

    def test_graph_demo_exits_zero_and_prints_stats(self):
        result = _run_cli("graph-demo")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Graph Demo", result.stdout)
        # stats() JSON contains these fields
        self.assertIn("total_nodes", result.stdout)
        self.assertIn("total_edges", result.stdout)

    def test_graph_trace_exits_zero_with_default_args(self):
        result = _run_cli("graph-trace")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("EvidenceTrace", result.stdout)
        self.assertIn("score:company:2330:2026-07-08", result.stdout)

    def test_graph_trace_custom_node(self):
        result = _run_cli("graph-trace", "--node", "company:2330",
                          "--max-depth", "3", "--direction", "upstream")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("company:2330", result.stdout)


class TestCLIOutputSanity(unittest.TestCase):
    """Light checks that the printed output looks like a real score."""

    def test_score_macro_score_is_in_range(self):
        result = _run_cli("score-macro")
        self.assertEqual(result.returncode, 0)
        # The first non-header score line is "  score: +4.68  confidence: 0.97"
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("score:"):
                # Extract the number after "score:" until "confidence"
                rest = stripped.split("score:", 1)[1]
                # Stop at "confidence"
                head = rest.split("confidence")[0].strip()
                num = float(head)
                self.assertGreaterEqual(num, -100.0)
                self.assertLessEqual(num, 100.0)
                return
        self.fail(f"No top-level score line found in:\n{result.stdout}")

    def test_score_company_final_score_in_range(self):
        result = _run_cli("score-company")
        self.assertEqual(result.returncode, 0)
        for line in result.stdout.splitlines():
            if "final score:" in line:
                value = line.split("final score:")[-1].strip().split()[0]
                try:
                    num = float(value)
                    self.assertGreaterEqual(num, -100.0)
                    self.assertLessEqual(num, 100.0)
                    return
                except ValueError:
                    continue
        self.fail(f"No 'final score:' line in output:\n{result.stdout}")

    def test_graph_demo_total_nodes_positive(self):
        result = _run_cli("graph-demo")
        self.assertEqual(result.returncode, 0)
        # Find "total_nodes": <int> in the JSON
        import json
        # Extract JSON from the output
        lines = result.stdout.splitlines()
        # Find line with "total_nodes"
        for line in lines:
            if '"total_nodes"' in line:
                # "total_nodes": 8,
                colon_idx = line.find(":")
                num_str = line[colon_idx + 1:].strip().rstrip(",")
                count = int(num_str)
                self.assertGreater(count, 0)
                return
        self.fail(f"No total_nodes in output:\n{result.stdout}")


if __name__ == "__main__":
    unittest.main()
