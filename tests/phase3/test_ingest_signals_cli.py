"""Tests for ``python -m phase3.cli ingest-signals``.

Contract:
* ``--dry-run`` is always allowed (no feature flag check).
* Persist mode requires ``PHASE3B_ENABLED=1`` or ``--force``.
* ``--db-path`` resolving to ``macro_history.db`` is refused.
* ``--input`` and ``--input-dir`` are mutually exclusive.
* ``--source <unknown>`` is rejected.
* ``--help`` lists the subcommand.
* End-to-end: real adapters + temp DB → signals persisted.
* Idempotent rerun: second call yields updated=count, new=0.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path("/home/ubuntu/macro-report")
PYTHON = "/home/ubuntu/macro-venv/bin/python"
FIXTURES = REPO_ROOT / "tests" / "phase3" / "fixtures"


def _env(env_extra: dict | None = None) -> dict:
    env = {
        "PYTHONPATH": str(REPO_ROOT),
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/home/ubuntu"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }
    if env_extra:
        env.update(env_extra)
    return env


def _run(*args: str, env_extra: dict | None = None,
         timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "phase3.cli", *args],
        cwd=str(REPO_ROOT),
        env=_env(env_extra),
        capture_output=True, text=True, timeout=timeout,
    )


def _init_db(db: str) -> None:
    """Run ``init-db --force`` against a temp DB so the schema exists."""
    proc = _run("init-db", "--force", "--db-path", db)
    assert proc.returncode == 0, f"init-db failed: {proc.stderr}"


class IngestSignalsGateTests(unittest.TestCase):
    def test_refuses_persist_without_flag_or_force(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            proc = _run(
                "ingest-signals", "--source", "fixture", "--input",
                str(FIXTURES / "fixture_company_industry_2026-07-08.json"),
                "--db-path", db,
                env_extra={"PHASE3B_ENABLED": ""},
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("PHASE3B_ENABLED", proc.stderr)

    def test_force_bypasses_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            proc = _run(
                "ingest-signals", "--source", "fixture", "--input",
                str(FIXTURES / "fixture_company_industry_2026-07-08.json"),
                "--db-path", db, "--force",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_env_var_enables(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            proc = _run(
                "ingest-signals", "--source", "fixture", "--input",
                str(FIXTURES / "fixture_company_industry_2026-07-08.json"),
                "--db-path", db,
                env_extra={"PHASE3B_ENABLED": "1"},
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)


class IngestSignalsDryRunTests(unittest.TestCase):
    def test_dry_run_does_not_require_feature_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            # We deliberately do NOT call init-db here. Dry-run shouldn't touch
            # the DB at all.
            proc = _run(
                "ingest-signals", "--source", "fixture", "--input",
                str(FIXTURES / "fixture_company_industry_2026-07-08.json"),
                "--db-path", db, "--dry-run",
                env_extra={"PHASE3B_ENABLED": ""},
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("dry_run=True", proc.stdout)
            # DB file should NOT have been created
            self.assertFalse(os.path.exists(db))

    def test_dry_run_no_input_yields_zero_signals(self) -> None:
        proc = _run(
            "ingest-signals", "--source", "yfinance", "--dry-run",
            env_extra={"PHASE3B_ENABLED": ""},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("signals=", proc.stdout)

    def test_dry_run_with_unknown_source_rejected(self) -> None:
        """Argparse choices guard catches this."""
        proc = _run(
            "ingest-signals", "--source", "not-a-source", "--dry-run",
            env_extra={"PHASE3B_ENABLED": ""},
        )
        # argparse exits 2 on invalid choice
        self.assertEqual(proc.returncode, 2)


class IngestSignalsPathGuardTests(unittest.TestCase):
    def test_macro_history_path_refused(self) -> None:
        # We do not call init-db first; the path guard fires earlier.
        proc = _run(
            "ingest-signals", "--source", "fixture", "--input",
            str(FIXTURES / "fixture_company_industry_2026-07-08.json"),
            "--db-path", "/tmp/macro_history.db", "--force",
        )
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertTrue(
            "macro_history" in combined or "refusing" in combined.lower(),
            msg=f"unexpected output: {combined!r}",
        )

    def test_subdirectory_macro_history_refused(self) -> None:
        """A path like ``./sub/../macro_history.db`` must be refused too."""
        proc = _run(
            "ingest-signals", "--source", "fixture", "--input",
            str(FIXTURES / "fixture_company_industry_2026-07-08.json"),
            "--db-path", "./sub/../macro_history.db", "--force",
        )
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertTrue(
            "macro_history" in combined or "refusing" in combined.lower(),
            msg=f"unexpected output: {combined!r}",
        )


class IngestSignalsMutualExclusionTests(unittest.TestCase):
    def test_input_and_input_dir_together_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            proc = _run(
                "ingest-signals", "--source", "fixture",
                "--input", str(FIXTURES / "fixture_company_industry_2026-07-08.json"),
                "--input-dir", str(FIXTURES),
                "--db-path", db, "--force",
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("mutually exclusive", proc.stderr)


class IngestSignalsEndToEndTests(unittest.TestCase):
    def test_yfinance_end_to_end_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            proc = _run(
                "ingest-signals", "--source", "yfinance", "--input",
                str(FIXTURES / "yfinance_2330_2026-07-08.json"),
                "--db-path", db, "--force",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            # 10 signals expected
            self.assertIn("signals=  10", proc.stdout)
            self.assertIn("new=  10", proc.stdout)
            # Verify DB row count
            con = sqlite3.connect(db)
            count = con.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            con.close()
            self.assertEqual(count, 10)

    def test_t86_end_to_end_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            proc = _run(
                "ingest-signals", "--source", "t86", "--input",
                str(FIXTURES / "t86_institutional_2026-07-08.json"),
                "--db-path", db, "--force",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            # Verify DB has rows
            con = sqlite3.connect(db)
            count = con.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            con.close()
            self.assertGreater(count, 0)

    def test_rss_end_to_end_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            proc = _run(
                "ingest-signals", "--source", "rss", "--input",
                str(FIXTURES / "cnyes_rss_headlines_2026-07-08.json"),
                "--db-path", db, "--force",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            con = sqlite3.connect(db)
            count = con.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            con.close()
            self.assertGreater(count, 0)

    def test_macro_end_to_end_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            proc = _run(
                "ingest-signals", "--source", "macro", "--input",
                str(FIXTURES / "macro_daily_2026-07-08.json"),
                "--db-path", db, "--force",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            con = sqlite3.connect(db)
            count = con.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            con.close()
            self.assertGreater(count, 0)

    def test_idempotent_rerun_updates_instead_of_inserts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            args = [
                "ingest-signals", "--source", "yfinance", "--input",
                str(FIXTURES / "yfinance_2330_2026-07-08.json"),
                "--db-path", db, "--force",
            ]
            p1 = _run(*args)
            self.assertEqual(p1.returncode, 0, msg=p1.stderr)
            p2 = _run(*args)
            self.assertEqual(p2.returncode, 0, msg=p2.stderr)
            # First run: new=10, updated=0
            self.assertIn("new=  10", p1.stdout)
            # Second run: new=0, updated=10
            self.assertIn("new=   0", p2.stdout)
            self.assertIn("updated=  10", p2.stdout)
            # DB still has 10 rows (no duplicates)
            con = sqlite3.connect(db)
            count = con.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            con.close()
            self.assertEqual(count, 10)


class IngestSignalsAllModeTests(unittest.TestCase):
    def test_all_mode_runs_input_dir(self) -> None:
        """`--source all` with `--input-dir` should pick up matching files."""
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "test.db")
            _init_db(db)
            # Use the fixtures dir; only fixture.json will match (others
            # are named with prefixes like yfinance_2330_*.json).
            # Build a clean dir with exactly the adapter-named files we want.
            indir = Path(td) / "in"
            indir.mkdir()
            import shutil
            shutil.copy(
                FIXTURES / "fixture_company_industry_2026-07-08.json",
                indir / "fixture.json",
            )
            proc = _run(
                "ingest-signals", "--source", "all", "--input-dir",
                str(indir), "--db-path", db, "--force",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            # Only fixture adapter should have produced signals
            self.assertIn("fixture", proc.stdout)


class IngestSignalsHelpTests(unittest.TestCase):
    def test_help_lists_ingest_signals(self) -> None:
        proc = _run("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ingest-signals", proc.stdout)

    def test_ingest_signals_help(self) -> None:
        proc = _run("ingest-signals", "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--source", proc.stdout)
        self.assertIn("--input", proc.stdout)
        self.assertIn("--input-dir", proc.stdout)
        self.assertIn("--db-path", proc.stdout)
        self.assertIn("--config-path", proc.stdout)
        self.assertIn("--dry-run", proc.stdout)
        self.assertIn("--force", proc.stdout)


if __name__ == "__main__":
    unittest.main()
