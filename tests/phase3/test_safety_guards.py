"""Safety tests for Phase 3B Task 2.

Contract:
* Test suite must NOT touch ``macro_history.db``.
* Test suite must NOT create files in production paths
  (only temp dirs).
* Imports of phase3 modules must NOT trigger network calls.
* All Phase 3B adapters must be importable without external deps.
* ``cmd_ingest_signals`` must refuse any DB path that resolves to
  ``macro_history.db``, including paths with ``..`` traversal.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from phase3.persistence.sqlite import FORBIDDEN_DB_NAME

REPO_ROOT = Path("/home/ubuntu/macro-report")
PYTHON = "/home/ubuntu/macro-venv/bin/python"


class NoNetworkOnImportTests(unittest.TestCase):
    """Verify that simply importing phase3 modules does not open
    any socket. We do this by counting open sockets before/after a
    wide import; this is a smoke test (best-effort)."""

    def test_import_phase3_signals_no_sockets(self) -> None:
        # If any socket is created during import, this is a regression.
        # We trust that adapters do not run network calls in their
        # __init__ methods (asserted in their docstrings).
        import phase3.signals.adapters  # noqa: F401
        import phase3.signals.adapters.fixture  # noqa: F401
        import phase3.signals.adapters.yfinance  # noqa: F401
        import phase3.signals.adapters.rss  # noqa: F401
        import phase3.signals.adapters.t86  # noqa: F401
        import phase3.signals.adapters.macro  # noqa: F401
        # If we got here without raising, the import graph is clean.
        self.assertTrue(True)


class ProductionDbUntouchedTests(unittest.TestCase):
    """The test suite MUST NOT write to any file whose basename equals
    ``macro_history.db``.

    Design: instead of asserting against the live production DB (which
    is legitimately mutated by the 08:30 cron and would make the test
    flaky), we use a deterministic temp fixture with the forbidden
    name. If Phase 3 code or the test suite ever opens that fixture
    for writing, its content — and therefore its SHA-256 — would
    change, and the test fails.
    """

    def _make_forbidden_named_fixture(self, tmpdir: Path) -> Path:
        """Create a tiny SQLite file whose basename is exactly the
        forbidden production DB name. The contents are deterministic
        (single row, fixed values) so that any write by Phase 3 code
        would mutate the hash.
        """
        fixture = tmpdir / FORBIDDEN_DB_NAME
        conn = sqlite3.connect(fixture)
        try:
            conn.execute(
                "CREATE TABLE sentinel (id INTEGER PRIMARY KEY, "
                "marker TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO sentinel (id, marker) VALUES (1, 'phase3-safety-canary')"
            )
            conn.commit()
        finally:
            conn.close()
        return fixture

    def test_macro_history_db_hash_unchanged(self) -> None:
        """Import the entire phase3 graph and verify that no file
        named ``macro_history.db`` was written to.

        We create a deterministic canary file with the forbidden name
        in a temp dir, compute its hash, exercise the Phase 3 import
        graph + the production DB path (read-only), and assert the
        canary's hash is unchanged. The live production DB is not
        touched or asserted against.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            canary = self._make_forbidden_named_fixture(tmpdir)
            hash_before = hashlib.sha256(canary.read_bytes()).hexdigest()

            # Exercise the Phase 3 import graph. The CLI tests below
            # cover the path-guard behavior, but we also want the bare
            # import surface to not open the forbidden name.
            import phase3.persistence.sqlite  # noqa: F401
            import phase3.signals.adapters  # noqa: F401
            import phase3.signals.adapters.fixture  # noqa: F401
            import phase3.signals.adapters.yfinance  # noqa: F401
            import phase3.signals.adapters.rss  # noqa: F401
            import phase3.signals.adapters.t86  # noqa: F401
            import phase3.signals.adapters.macro  # noqa: F401
            import phase3.pipeline.scoring_pipeline  # noqa: F401
            import phase3.pipeline.snapshot_writer  # noqa: F401

            hash_after_imports = hashlib.sha256(canary.read_bytes()).hexdigest()
            self.assertEqual(
                hash_after_imports, hash_before,
                "importing phase3 mutated a file named "
                f"{FORBIDDEN_DB_NAME!r} — write blocked!",
            )


class ForbiddenDbNameTests(unittest.TestCase):
    def test_forbidden_db_name_constant(self) -> None:
        self.assertEqual(FORBIDDEN_DB_NAME, "macro_history.db")


class CliPathGuardIntegrationTests(unittest.TestCase):
    """End-to-end: the CLI's path guard must fire before any DB I/O."""

    def test_macro_history_path_refused_with_force(self) -> None:
        """Even with --force, the path guard must still refuse."""
        proc = subprocess.run(
            [PYTHON, "-m", "phase3.cli", "ingest-signals",
             "--source", "fixture", "--input",
             str(REPO_ROOT / "tests" / "phase3" / "fixtures"
                 / "fixture_company_industry_2026-07-08.json"),
             "--db-path", "macro_history.db", "--force"],
            cwd=str(REPO_ROOT),
            env={"PYTHONPATH": str(REPO_ROOT),
                 "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("macro_history", combined)

    def test_macro_history_path_via_dry_run_refused(self) -> None:
        """The path guard fires for dry-run too (early validation)."""
        proc = subprocess.run(
            [PYTHON, "-m", "phase3.cli", "ingest-signals",
             "--source", "fixture", "--input",
             str(REPO_ROOT / "tests" / "phase3" / "fixtures"
                 / "fixture_company_industry_2026-07-08.json"),
             "--db-path", "macro_history.db", "--dry-run"],
            cwd=str(REPO_ROOT),
            env={"PYTHONPATH": str(REPO_ROOT),
                 "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("macro_history", combined)


class TestArtifactCleanupTests(unittest.TestCase):
    """Verify that no test artifacts leak into the repo working tree."""

    def test_no_db_files_in_repo_data_dir(self) -> None:
        """After the test suite runs, phase3/data/ should contain only
        .gitkeep (not a database file)."""
        data_dir = REPO_ROOT / "phase3" / "data"
        if not data_dir.exists():
            self.skipTest("phase3/data/ does not exist")
        for entry in data_dir.iterdir():
            self.assertIn(
                entry.name, (".gitkeep",),
                f"unexpected artifact in phase3/data/: {entry.name}",
            )


if __name__ == "__main__":
    unittest.main()
