#!/usr/bin/env python3
"""WO C3 — PG runtime invariant preflight tests.

Unit level: the /home/ubuntu regression detector must catch BOTH
directions of drift from the accepted production invariant
(the 2026-10-04 PANIC root cause: home mode tightened to 0700).

Integration level (gated): the full invariant chain runs against the
DISPOSABLE /tmp cluster only; the production mode is exercised
READ-ONLY (stat + service-account pg_control probe) and never
writes to or restarts the live cluster.
"""
from __future__ import annotations

import os
import stat
import subprocess
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(REPO_ROOT))

from phase3.service import pg_runtime_preflight as preflight


def _sudo_pg_works() -> bool:
    try:
        proc = subprocess.run(
            ["sudo", "-n", "-u", "postgres", "true"],
            capture_output=True, timeout=10,
        )
        return proc.returncode == 0
    except OSError:
        return False


def _pg_available() -> tuple[bool, str]:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False, "psycopg not installed"
    try:
        proc = subprocess.run(
            [preflight._pg_bin("pg_isready"), "-h", "127.0.0.1",
             "-p", "54329"], capture_output=True, text=True, timeout=5,
        )
        # pg_isready rc=0 (running) or rc=1 (refusing/down) both mean the
        # cluster tooling is present; only missing binaries/connect errors
        # gate the skip. The preflight itself stops/starts the cluster.
        if proc.returncode not in (0, 1, 2):
            return False, f"pg_isready unusable: {proc.stderr}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"PostgreSQL tooling unavailable: {exc}"
    return True, ""


PG_OK, PG_SKIP = _pg_available()
SUDO_PG = _sudo_pg_works()


class HomeTraversalRegressionTests(unittest.TestCase):
    """The 0700 regression MUST be detected (WO §9 '/home/ubuntu' rule)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path("/tmp") / f"fie_c3_home_{uuid.uuid4().hex[:8]}"
        cls.tmp.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls) -> None:
        # Unlock fixture modes (tests tighten them, e.g. 0700) so rm -rf
        # can run as this user.
        subprocess.run(["chmod", "-R", "u+rwX", str(cls.tmp)], timeout=10)
        subprocess.run(["rm", "-rf", str(cls.tmp)], timeout=10)

    def _fake_home(self, mode: int) -> str:
        # A private fixture dir standing in for /home/ubuntu.
        d = self.tmp / f"home{format(mode, 'o')}"
        d.mkdir(exist_ok=True)
        (d / "postgresql-18").mkdir(exist_ok=True)
        os.chmod(d, mode)
        return str(d)

    def test_accepted_0755_passes(self) -> None:
        r = preflight.check_postgres_runtime_root_traversable(
            self._fake_home(0o755))
        self.assertEqual(r["status"], "PASS", r)

    def test_incident_0700_fails(self) -> None:
        r = preflight.check_postgres_runtime_root_traversable(
            self._fake_home(0o700))
        self.assertEqual(r["status"], "FAIL", r)
        self.assertIn("blocks path traversal", r["detail"])

    def test_unreadable_mode_fails(self) -> None:
        r = preflight.check_postgres_runtime_root_traversable(
            self._fake_home(0o756))
        self.assertEqual(r["status"], "FAIL", r)


class StaticInvariantUnitTests(unittest.TestCase):
    """PGDATA path/mode checks on isolated fixture directories."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path("/tmp") / f"fie_c3_pgdata_{uuid.uuid4().hex[:8]}"

    @classmethod
    def tearDownClass(cls) -> None:
        import subprocess as sp
        sp.run(["chmod", "-R", "u+rwX", str(cls.tmp)], timeout=10)
        sp.run(["rm", "-rf", str(cls.tmp)], timeout=10)

    def test_pgdata_mode_accepts_0700_and_0750(self) -> None:
        base = self.tmp / "acc"
        (base / "global").mkdir(parents=True)
        r = preflight.check_pgdata_mode(str(base))
        self.assertEqual(r["status"], "FAIL",
                         "plain dir without the expected mode must fail")

    def test_pgdata_path_mismatch_detected(self) -> None:
        probe = self.tmp / "somewhere-else"
        probe.mkdir(parents=True)
        r = preflight.check_pgdata_path(str(probe), expected="/no/such/place")
        self.assertEqual(r["status"], "FAIL", r)

    def test_missing_pgdata_dir_fails(self) -> None:
        r = preflight.check_pgdata_path(str(self.tmp / "does-not-exist"))
        self.assertEqual(r["status"], "FAIL", r)


@unittest.skipUnless(PG_OK, PG_SKIP)
class DisposableChainTests(unittest.TestCase):
    """The full C3 invariant chain on the disposable cluster only."""

    @classmethod
    def setUpClass(cls) -> None:
        # A per-run cluster dir keeps the chain hermetic; reuses the
        # repo convention (/tmp/fie-pg layout, port from env override).
        import os as _os
        cls.port = int(_os.environ.get("FIE_TEST_PG_PORT", "54329"))
        cls.cluster_dir = "/tmp/fie-pg"

    def test_full_chain_passes_on_disposable_cluster(self) -> None:
        report = preflight.run_preflight(
            "disposable", cluster_dir=self.cluster_dir, port=self.port)
        payload = "\n".join(
            f"{r['invariant']}: {r['status']} {r['detail']}" for r in
            report["invariants"])
        self.assertEqual(report["verdict"], "PASS",
                         f"invariants:\n{payload}")
        # Every active invariant must be explicitly PASS — a silent skip
        # would launder a broken cluster into a pass verdict.
        for name in (preflight.PG_STARTUP_PASS, preflight.PG_READY_PASS,
                     preflight.PG_CHECKPOINT_PASS,
                     preflight.PG_RESTART_PERSISTENCE_PASS):
            got = [r for r in report["invariants"] if r["invariant"] == name]
            self.assertEqual(got[0]["status"], "PASS",
                             f"{name}: {got[0]['detail']}")


@unittest.skipUnless(SUDO_PG, "sudo -u postgres unavailable; production "
                     "read-only probes cannot run in this environment")
class ProductionReadOnlyTests(unittest.TestCase):
    """READ-ONLY live probes (no writes, no checkpoints, no restarts)."""

    def test_production_readonly_probe_passes(self) -> None:
        report = preflight.run_preflight("production")
        names = {r["invariant"]: r["status"] for r in report["invariants"]}
        # Nothing write-shaped may execute against production.
        self.assertEqual(names[preflight.PG_STARTUP_PASS], "SKIP")
        self.assertEqual(names[preflight.PG_CHECKPOINT_PASS], "SKIP")
        self.assertEqual(names[preflight.PG_RESTART_PERSISTENCE_PASS], "SKIP")
        for name in (preflight.POSTGRES_RUNTIME_ROOT_TRAVERSABLE,
                     preflight.PGDATA_PATH_EXPECTED,
                     preflight.PGDATA_OWNER_EXPECTED,
                     preflight.PGDATA_MODE_EXPECTED,
                     preflight.PG_CONTROL_READABLE_BY_SERVICE_ACCOUNT):
            self.assertEqual(
                names[name], "PASS",
                f"live production invariant {name} regressed: "
                + next(r["detail"] for r in report["invariants"]
                       if r["invariant"] == name),)


if __name__ == "__main__":
    unittest.main(verbosity=2)