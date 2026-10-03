"""Tests for ``python -m phase3.cli init-db`` — the gated Phase 3B
persistence entry point.

The contract:
* Default: refuse with exit code 2 (feature flag not set).
* With ``--force``: succeed and create the database.
* With ``PHASE3B_ENABLED=1``: succeed.
* The resulting database passes quick_check.

We invoke the CLI as a subprocess so we exercise the actual
``python3 -m phase3.cli`` user-facing path.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def _run_cli(*args: str, env_extra: dict | None = None,
             timeout: int = 60) -> subprocess.CompletedProcess:
    env = {
        "PYTHONPATH": str(REPO_ROOT),
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(REPO_ROOT)),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PYTHON, "-m", "phase3.cli", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class InitDbGateTests(unittest.TestCase):
    def test_refuses_when_feature_flag_missing(self) -> None:
        # Make sure the env var is NOT set for this call.
        env = {"PHASE3B_ENABLED": ""}
        proc = _run_cli("init-db", env_extra=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("PHASE3B_ENABLED", proc.stderr)

    def test_force_bypasses_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "force.db")
            proc = _run_cli("init-db", "--force", "--db-path", db)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertTrue(os.path.exists(db))

    def test_env_var_enables(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "env.db")
            proc = _run_cli(
                "init-db", "--db-path", db,
                env_extra={"PHASE3B_ENABLED": "1"},
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertTrue(os.path.exists(db))

    def test_env_var_truthy_values(self) -> None:
        """1, true, yes, on should all enable."""
        for val in ("1", "true", "yes", "on"):
            with self.subTest(val=val):
                with tempfile.TemporaryDirectory() as td:
                    db = os.path.join(td, f"env_{val}.db")
                    proc = _run_cli(
                        "init-db", "--db-path", db,
                        env_extra={"PHASE3B_ENABLED": val},
                    )
                    self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                    self.assertTrue(os.path.exists(db))

    def test_env_var_falsy_values_still_refuse(self) -> None:
        """Empty / 0 / false / off should still refuse."""
        for val in ("0", "false", "no", "off"):
            with self.subTest(val=val):
                proc = _run_cli(
                    "init-db",
                    env_extra={"PHASE3B_ENABLED": val},
                )
                self.assertEqual(proc.returncode, 2)


class InitDbResultTests(unittest.TestCase):
    def test_first_run_creates_schema_with_one_migration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "first.db")
            proc = _run_cli("init-db", "--force", "--db-path", db)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("applied 1 migration(s): [1]", proc.stdout)
            self.assertIn("quick_check = ok", proc.stdout)

    def test_second_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "second.db")
            first = _run_cli("init-db", "--force", "--db-path", db)
            self.assertEqual(first.returncode, 0)
            second = _run_cli("init-db", "--force", "--db-path", db)
            self.assertEqual(second.returncode, 0, msg=second.stderr)
            self.assertIn("no new migrations applied", second.stdout)
            self.assertIn("quick_check = ok", second.stdout)

    def test_db_path_containing_macro_history_is_refused(self) -> None:
        proc = _run_cli(
            "init-db", "--force",
            "--db-path", "/tmp/macro_history.db",
        )
        # The path guard fires inside SQLiteStore; the cli returns 1
        # (qc != "ok") or higher. We accept any non-zero exit and a
        # clear mention of the guard.
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertTrue(
            "macro_history" in combined or "refusing" in combined.lower(),
            msg=f"unexpected output: {combined!r}",
        )

    def test_help_lists_init_db(self) -> None:
        proc = _run_cli("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("init-db", proc.stdout)


if __name__ == "__main__":
    unittest.main()
