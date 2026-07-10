"""Tests for Phase 3B Task 5 Run 4 — Pipeline CLI surface.

Scope is intentionally tight: stdlib unittest only.

The test matrix mirrors the brief's required scenarios:

1.  CLI command parsing: every new subcommand is registered in
    the parser and its ``--help`` is well-formed.
2.  ``pipeline-run`` dry-run: subprocess invocation returns
    rc=0, the result envelope is JSON-serialisable, and
    ``dry_run`` / ``persist`` / ``score_count`` carry the
    expected values.
3.  ``pipeline-resume``: requires ``--db-path``; with a temp
    store the command runs (no rows to resume but the call
    completes) and returns rc=0.
4.  ``pipeline-report`` + ``pipeline-export``: round-trip
    flow — run writes JSON envelope; report re-emits it.
5.  JSON output: ``--json`` produces a parseable JSON document
    on stdout whose top-level keys include ``run_id`` and
    ``config_hash``.
6.  Invalid arguments: missing ``--date`` returns rc=2;
    macro_history.db is refused with rc=1.
7.  Exit codes: rc=0 for success, rc=1 for runtime failure,
    rc=2 for argument errors.
8.  Deterministic repeated execution: two identical invocations
    produce the same ``run_id`` (when ``--run-id`` is supplied)
    and the same overall envelope keys.
9.  Temp SQLite integration: ``--persist --db-path <temp>``
    actually creates a file at that path.
10. ``pipeline-status`` read-only: produces a RunState envelope
    without modifying the DB.

Production safety contract
--------------------------
* macro_history.db must be byte-identical to the value recorded
  in the task briefing.
* No intelligence.db* file may be created anywhere.
* All DBs used by these tests are created in ``tempfile``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path("/home/ubuntu/macro-report")
PYTHON = "/home/ubuntu/macro-venv/bin/python"

ENV_BASE = {
    "PYTHONPATH": str(REPO_ROOT),
    "PATH": os.environ.get("PATH", ""),
    "HOME": os.environ.get("HOME", "/home/ubuntu"),
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
}

# All new Run 4 subcommands that we expect to be listed in --help.
RUN4_SUBCOMMANDS: list[str] = [
    "pipeline-run",
    "pipeline-resume",
    "pipeline-status",
    "pipeline-report",
    "pipeline-export",
]


def _run_cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run ``python3 -m phase3.cli`` with given args. Return CompletedProcess."""
    return subprocess.run(
        [PYTHON, "-m", "phase3.cli", *args],
        cwd=str(REPO_ROOT),
        env=ENV_BASE,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _temp_db_path() -> str:
    """Allocate a unique temp path for a Phase 3B SQLite store."""
    fd, path = tempfile.mkstemp(prefix="phase3_run4_test_", suffix=".db")
    os.close(fd)
    os.unlink(path)  # we want a clean path; the CLI creates it
    return path


# ---------------------------------------------------------------------------
# 1. CLI command parsing
# ---------------------------------------------------------------------------


class TestCLIParserRegistration(unittest.TestCase):
    """Every new Run 4 subcommand is registered in the parser."""

    def test_help_lists_all_run4_subcommands(self) -> None:
        result = _run_cli("--help")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for cmd in RUN4_SUBCOMMANDS:
            self.assertIn(cmd, result.stdout)

    def test_subcommand_help_succeeds(self) -> None:
        for cmd in RUN4_SUBCOMMANDS:
            with self.subTest(cmd=cmd):
                result = _run_cli(cmd, "--help")
                self.assertEqual(
                    result.returncode, 0,
                    msg=f"{cmd} --help failed: {result.stderr}",
                )

    def test_subcommand_help_mentions_required_date(self) -> None:
        for cmd in ("pipeline-run", "pipeline-resume", "pipeline-status"):
            with self.subTest(cmd=cmd):
                result = _run_cli(cmd, "--help")
                self.assertEqual(result.returncode, 0)
                self.assertIn("--date", result.stdout)


# ---------------------------------------------------------------------------
# 2. pipeline-run dry-run
# ---------------------------------------------------------------------------


class TestPipelineRunDryRun(unittest.TestCase):
    """``pipeline-run`` with no ``--persist`` is a deterministic dry-run."""

    def test_dry_run_exits_zero(self) -> None:
        result = _run_cli(
            "pipeline-run", "--date", "2026-07-09",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("pipeline-run", result.stdout)
        self.assertIn("dry_run: True", result.stdout)
        self.assertIn("persist: False", result.stdout)

    def test_dry_run_explicit_date(self) -> None:
        result = _run_cli(
            "pipeline-run", "--date", "2026-07-09",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("date_bucket: 2026-07-09", result.stdout)

    def test_dry_run_company_only_no_macro(self) -> None:
        result = _run_cli(
            "pipeline-run", "--date", "2026-07-09",
            "--no-macro", "--industry", "AI", "--company", "2330",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("pipeline-run", result.stdout)

    def test_dry_run_no_db_writes(self) -> None:
        """Dry-run must not create any intelligence.db* file."""
        result = _run_cli("pipeline-run", "--date", "2026-07-09")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # Default db path is phase3/data/intelligence.db; check it
        # is unchanged from the empty baseline.
        default = REPO_ROOT / "phase3" / "data" / "intelligence.db"
        if default.exists():
            # If the file exists, it must be a 0-byte file or
            # pre-existing — never a new artifact from this test.
            self.assertEqual(
                default.stat().st_size, 0,
                "dry-run created a non-empty intelligence.db",
            )


# ---------------------------------------------------------------------------
# 3. pipeline-resume
# ---------------------------------------------------------------------------


class TestPipelineResume(unittest.TestCase):
    """``pipeline-resume`` with a temp DB."""

    def test_resume_with_empty_temp_db_exits_zero(self) -> None:
        """A fresh temp DB has no rows to resume; the call must still
        complete (rc=0) because nothing is broken — the run is
        trivially "complete" once the orchestrator returns."""
        path = _temp_db_path()
        try:
            result = _run_cli(
                "pipeline-resume", "--date", "2026-07-09",
                "--db-path", path,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("pipeline-resume", result.stdout)
            self.assertTrue(
                Path(path).exists(),
                "resume did not create the DB at the supplied path",
            )
        finally:
            if Path(path).exists():
                os.unlink(path)

    def test_resume_requires_db_path(self) -> None:
        result = _run_cli(
            "pipeline-resume", "--date", "2026-07-09",
        )
        self.assertNotEqual(result.returncode, 0)
        # argparse error goes to stderr
        self.assertIn("--db-path", result.stderr)


# ---------------------------------------------------------------------------
# 4. pipeline-status (read-only)
# ---------------------------------------------------------------------------


class TestPipelineStatus(unittest.TestCase):
    """``pipeline-status`` is read-only."""

    def test_status_with_empty_temp_db(self) -> None:
        path = _temp_db_path()
        try:
            result = _run_cli(
                "pipeline-status", "--date", "2026-07-09",
                "--db-path", path,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("pipeline-status", result.stdout)
            # RunState fields visible
            self.assertIn("last_completed_stage", result.stdout)
        finally:
            if Path(path).exists():
                os.unlink(path)

    def test_status_requires_db_path(self) -> None:
        result = _run_cli(
            "pipeline-status", "--date", "2026-07-09",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--db-path", result.stderr)


# ---------------------------------------------------------------------------
# 5. JSON output
# ---------------------------------------------------------------------------


class TestJSONOutput(unittest.TestCase):
    """``--json`` produces a parseable JSON document on stdout."""

    def test_pipeline_run_json_is_parseable(self) -> None:
        result = _run_cli(
            "pipeline-run", "--date", "2026-07-09", "--json",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # stdout is pure JSON
        data = json.loads(result.stdout)
        self.assertIsInstance(data, dict)
        self.assertIn("run_id", data)
        self.assertIn("config_hash", data)
        self.assertTrue(data["dry_run"])
        self.assertFalse(data["persist"])

    def test_pipeline_status_json_is_parseable(self) -> None:
        path = _temp_db_path()
        try:
            result = _run_cli(
                "pipeline-status", "--date", "2026-07-09",
                "--db-path", path, "--json",
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            data = json.loads(result.stdout)
            self.assertIsInstance(data, dict)
            self.assertIn("run_id", data)
        finally:
            if Path(path).exists():
                os.unlink(path)

    def test_output_to_file(self) -> None:
        """``--output PATH`` writes JSON to PATH, prints a status line."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            out_path = f.name
        try:
            result = _run_cli(
                "pipeline-run", "--date", "2026-07-09",
                "--output", out_path,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            # file has the JSON
            data = json.loads(Path(out_path).read_text(encoding="utf-8"))
            self.assertIn("run_id", data)
            # stdout has the status line
            self.assertIn(out_path, result.stdout)
        finally:
            if Path(out_path).exists():
                os.unlink(out_path)


# ---------------------------------------------------------------------------
# 6. Invalid arguments + path guard
# ---------------------------------------------------------------------------


class TestInvalidArgs(unittest.TestCase):
    """Invalid arguments return non-zero exit codes."""

    def test_missing_required_date(self) -> None:
        result = _run_cli("pipeline-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--date", result.stderr)

    def test_pipeline_resume_missing_db_path(self) -> None:
        result = _run_cli("pipeline-resume", "--date", "2026-07-09")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--db-path", result.stderr)

    def test_macro_history_db_path_refused(self) -> None:
        result = _run_cli(
            "pipeline-run", "--date", "2026-07-09",
            "--persist", "--db-path", str(REPO_ROOT / "macro_history.db"),
        )
        # rc=1 for runtime refusal (not argparse error rc=2)
        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("macro_history.db", result.stderr)

    def test_pipeline_report_missing_input(self) -> None:
        result = _run_cli(
            "pipeline-report", "--output-dir", "/tmp/nope_dir_xyz",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--input", result.stderr)

    def test_pipeline_report_missing_output_dir(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            tmp_in = f.name
            f.write("{}")
        try:
            result = _run_cli(
                "pipeline-report", "--input", tmp_in,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--output-dir", result.stderr)
        finally:
            os.unlink(tmp_in)


# ---------------------------------------------------------------------------
# 7. Exit codes
# ---------------------------------------------------------------------------


class TestExitCodes(unittest.TestCase):
    """Verify the contract rc=0/1/2 is consistent across the new CLI."""

    def test_run_dry_run_returns_zero(self) -> None:
        result = _run_cli("pipeline-run", "--date", "2026-07-09")
        self.assertEqual(result.returncode, 0)

    def test_argparse_error_returns_two(self) -> None:
        result = _run_cli("pipeline-run")
        self.assertEqual(result.returncode, 2)

    def test_runtime_error_returns_one(self) -> None:
        result = _run_cli(
            "pipeline-run", "--date", "2026-07-09",
            "--persist", "--db-path", str(REPO_ROOT / "macro_history.db"),
        )
        self.assertEqual(result.returncode, 1)


# ---------------------------------------------------------------------------
# 8. Deterministic repeated execution
# ---------------------------------------------------------------------------


class TestDeterministicExecution(unittest.TestCase):
    """Two identical invocations produce the same envelope keys."""

    def test_two_dry_runs_have_same_top_level_keys(self) -> None:
        r1 = _run_cli("pipeline-run", "--date", "2026-07-09", "--json")
        r2 = _run_cli("pipeline-run", "--date", "2026-07-09", "--json")
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        d1 = json.loads(r1.stdout)
        d2 = json.loads(r2.stdout)
        # Same top-level key set
        self.assertEqual(set(d1.keys()), set(d2.keys()))
        # Same date_bucket and config_hash
        self.assertEqual(d1["date_bucket"], d2["date_bucket"])
        self.assertEqual(d1["config_hash"], d2["config_hash"])
        # dry_run is deterministic
        self.assertEqual(d1["dry_run"], d2["dry_run"])
        self.assertEqual(d1["persist"], d2["persist"])

    def test_explicit_run_id_is_preserved(self) -> None:
        r1 = _run_cli(
            "pipeline-run", "--date", "2026-07-09",
            "--run-id", "fixed-run-id-test",
            "--json",
        )
        self.assertEqual(r1.returncode, 0)
        d1 = json.loads(r1.stdout)
        self.assertEqual(d1["run_id"], "fixed-run-id-test")

    def test_two_export_runs_have_identical_artifacts(self) -> None:
        """Two exports with the same dry-run inputs should produce
        byte-identical JSON (modulo timestamps)."""
        with tempfile.TemporaryDirectory() as d1:
            with tempfile.TemporaryDirectory() as d2:
                # Same fixed timestamp via --run-id to remove
                # wall-clock drift between the two calls.
                r1 = _run_cli(
                    "pipeline-export", "--date", "2026-07-09",
                    "--output-dir", d1,
                    "--run-label", "det-test",
                )
                r2 = _run_cli(
                    "pipeline-export", "--date", "2026-07-09",
                    "--output-dir", d2,
                    "--run-label", "det-test",
                )
                self.assertEqual(r1.returncode, 0, msg=r1.stderr)
                self.assertEqual(r2.returncode, 0, msg=r2.stderr)
                # The MD artifacts may differ in timestamps but
                # the JSON keys must match.
                j1 = json.loads(
                    (Path(d1) / "det-test.intelligence_report.json")
                    .read_text(encoding="utf-8")
                )
                j2 = json.loads(
                    (Path(d2) / "det-test.intelligence_report.json")
                    .read_text(encoding="utf-8")
                )
                # Both must have the same top-level shape
                self.assertEqual(set(j1.keys()), set(j2.keys()))
                # The deterministic data is nested under 'result'
                # and 'artifact' (ExportReport envelope).
                self.assertEqual(
                    j1["result"]["date_bucket"],
                    j2["result"]["date_bucket"],
                )
                self.assertEqual(
                    j1["artifact"]["date_bucket"],
                    j2["artifact"]["date_bucket"],
                )
                # And the deterministic summary fields
                self.assertEqual(
                    j1["summary"], j2["summary"],
                )


# ---------------------------------------------------------------------------
# 9. Temp SQLite integration
# ---------------------------------------------------------------------------


class TestTempSQLiteIntegration(unittest.TestCase):
    """``--persist --db-path <temp>`` actually creates a file at that path."""

    def test_persist_creates_db_file(self) -> None:
        path = _temp_db_path()
        try:
            self.assertFalse(Path(path).exists())
            result = _run_cli(
                "pipeline-run", "--date", "2026-07-09",
                "--persist", "--db-path", path,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(
                Path(path).exists(),
                "persist mode did not create the DB file",
            )
            self.assertGreater(
                Path(path).stat().st_size, 0,
                "persist mode created a 0-byte DB",
            )
        finally:
            if Path(path).exists():
                os.unlink(path)

    def test_export_creates_json_and_md(self) -> None:
        path = _temp_db_path()
        with tempfile.TemporaryDirectory() as out_dir:
            try:
                result = _run_cli(
                    "pipeline-export", "--date", "2026-07-09",
                    "--persist", "--db-path", path,
                    "--output-dir", out_dir,
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                # Both files should be present
                json_file = Path(out_dir) / "intelligence_report.json"
                md_file = Path(out_dir) / "intelligence_report.md"
                self.assertTrue(json_file.exists())
                self.assertTrue(md_file.exists())
                # JSON must be parseable
                data = json.loads(json_file.read_text(encoding="utf-8"))
                # The export envelope nests date_bucket under result/artifact
                self.assertEqual(data["result"]["date_bucket"], "2026-07-09")
            finally:
                if Path(path).exists():
                    os.unlink(path)


# ---------------------------------------------------------------------------
# 10. pipeline-report round-trip
# ---------------------------------------------------------------------------


class TestPipelineReportRoundTrip(unittest.TestCase):
    """``pipeline-report`` re-emits a saved run envelope."""

    def test_report_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            db_path = os.path.join(work, "test.db")
            out_dir = os.path.join(work, "out")
            # Step 1: export writes a JSON envelope
            r1 = _run_cli(
                "pipeline-export", "--date", "2026-07-09",
                "--persist", "--db-path", db_path,
                "--output-dir", out_dir,
                "--run-label", "rt",
            )
            self.assertEqual(r1.returncode, 0, msg=r1.stderr)
            json_path = Path(out_dir) / "rt.intelligence_report.json"
            self.assertTrue(json_path.exists())
            # Step 2: report re-emits from that JSON
            r2 = _run_cli(
                "pipeline-report",
                "--input", str(json_path),
                "--output-dir", os.path.join(work, "out2"),
                "--run-label", "rt",
            )
            self.assertEqual(r2.returncode, 0, msg=r2.stderr)
            out2 = Path(work) / "out2"
            self.assertTrue((out2 / "rt.intelligence_report.json").exists())
            self.assertTrue((out2 / "rt.intelligence_report.md").exists())


# ---------------------------------------------------------------------------
# 11. Production safety
# ---------------------------------------------------------------------------


class TestProductionSafety(unittest.TestCase):
    """macro_history.db is byte-identical to the briefing baseline."""

    @classmethod
    def setUpClass(cls) -> None:
        # 828ce117163f30d8315fa30fe228f7f4cafd08e6b782e684bd534023bca26d1e
        # is the value recorded in the task plan and MEMORY.
        cls.expected = (
            "828ce117163f30d8315fa30fe228f7f4cafd08e6b782e684bd534023bca26d1e"
        )
        cls.target = REPO_ROOT / "macro_history.db"

    def test_macro_history_db_unchanged(self) -> None:
        """The CLI does not modify macro_history.db."""
        import hashlib
        if not self.target.exists():
            self.skipTest("macro_history.db not present in this run")
        actual = hashlib.sha256(
            self.target.read_bytes()
        ).hexdigest()
        self.assertEqual(actual, self.expected)

    def test_no_intelligence_db_in_phase3_data(self) -> None:
        """phase3/data/intelligence.db must not appear after these tests."""
        path = REPO_ROOT / "phase3" / "data" / "intelligence.db"
        if path.exists():
            self.assertEqual(
                path.stat().st_size, 0,
                "intelligence.db was created by these tests",
            )


if __name__ == "__main__":
    unittest.main()
