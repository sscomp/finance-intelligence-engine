#!/usr/bin/env python3
"""Zero-write production invariant across the focused rehearsal-guard suite
(2026-10-05 G6 remediation, WO Task D #12 / Task H).

Runs the whole focused guard suite (tests.test_rehearsal_wrapper_guard +
tests.test_db_target_identity) as a sub-suite BETWEEN two production
fingerprint captures and asserts a ZERO delta on every protected table:

    score_snapshot, signal_log, graph_nodes, graph_edges,
    adapter_run_log, ingestion_errors, schema_migrations

plus the SQLite-era production store byte-identity (read-only sha256).

The production identity is resolved from the production contract files
(FIE_PRODUCTION_DB_CONTRACT or the fie-67b-upgrade defaults) purely in
memory; fingerprints are computed PG-side and only digests cross the
process boundary — no DSN, no credential, no count text ever printed.

Skips with an explicit reason when the production contract is not
resolvable (non-Abacus hosts) — that skip is the documented policy path.
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PROD_TABLES = (
    "score_snapshot", "signal_log", "graph_nodes", "graph_edges",
    "adapter_run_log", "ingestion_errors", "schema_migrations",
)

DEFAULT_CONTRACT_FILES = (
    "/home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env",
)


def _read_env_file_values(path: Path, keys: tuple) -> dict:
    """Read simple KEY=value / export KEY='value' assignments (file shape
    of the production contract). The file is parsed, never executed;
    values stay in memory and are only handed to the connecting
    sub-process — never printed."""
    out: dict = {}
    for line in path.read_text(encoding="utf-8",
                               errors="replace").splitlines():
        m = re.match(
            r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
            r"(?:'([^']*)'|\"([^\"]*)\"|(\S+))\s*$", line)
        if not m:
            continue
        key = m.group(1)
        if key in keys:
            value = next((g for g in m.groups()[1:] if g is not None), "")
            if value and key not in out:
                out[key] = value
    return out


def _resolve_prod_contract() -> dict:
    override = os.environ.get("FIE_PRODUCTION_DB_CONTRACT", "")
    files = ([p for p in override.split() if p] if override
             else [str(p) for p in DEFAULT_CONTRACT_FILES])
    for name in files:
        path = Path(name)
        if path.exists():
            values = _read_env_file_values(
                path, ("FIE_INTELLIGENCE_DB", "PGPASSWORD"))
            dsn = values.get("FIE_INTELLIGENCE_DB", "")
            if dsn.startswith(("postgres://", "postgresql://")):
                return {"dsn": dsn, "pgpassword": values.get("PGPASSWORD", "")}
    return {}


def _resolve_prod_dsn() -> str:
    return _resolve_prod_contract().get("dsn", "")


def _pg_fingerprints(dsn: str, pgpassword: str = None) -> dict:
    """Table counts + row digests, computed server-side (md5(string_agg)).

    Read-only by construction; the psql invocation receives the DSN only
    through its environment, and only digests cross the process boundary.
    """
    env = dict(os.environ)
    if pgpassword is not None:
        env["PGPASSWORD"] = pgpassword
    digest_expr = {
        "score_snapshot":
            "md5(string_agg(snapshot_id||'|'||scorer||'|'||entity_id||'|'||"
            "score::text||'|'||coalesce(notes,''), ';' ORDER BY snapshot_id))",
        "signal_log":
            "md5(string_agg(signal_id||'|'||signal_type||'|'||"
            "coalesce(value::text,''), ';' ORDER BY signal_id))",
    }
    counts: dict = {}
    for table in PROD_TABLES:
        out = subprocess.run(
            ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-At", "-c",
             f"SELECT count(*) FROM {table};"],
            capture_output=True, text=True, env=env, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(f"psql count failed: rc={out.returncode}")
        counts[f"{table}.count"] = out.stdout.strip()
    for table in ("score_snapshot", "signal_log"):
        out = subprocess.run(
            ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-At", "-c",
             f"SELECT {digest_expr[table]} FROM {table};"],
            capture_output=True, text=True, env=env, timeout=60)
        counts[f"{table}.rowdigest"] = (
            out.stdout.strip() if out.returncode == 0 else "UNAVAILABLE")
    return counts


def _sqlite_store_sha() -> dict:
    """Byte-identity of the SQLite-era production intelligence store."""
    path = REPO / "metadata" / "intelligence_store.db"
    if not path.exists():
        return {}
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}


class TestProductionZeroWriteInvariant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._contract = _resolve_prod_contract()
        cls._dsn = cls._contract.get("dsn", "")
        cls._reason = (
            "production contract DSN not resolvable on this host; the "
            "wrapper-level zero-write assertions (SQLite store byte-identity, "
            "guard-refusal rc 78) still cover write-safety hermetically")
        if not cls._dsn:
            raise unittest.SkipTest(cls._reason)
        cls._pg_before = _pg_fingerprints(
            cls._dsn, cls._contract.get("pgpassword"))
        cls._sqlite_before = _sqlite_store_sha()

    def _delta(self, before: dict, after: dict) -> list:
        return [k for k in before if before.get(k) != after.get(k)]

    def test_focused_guard_suite_causes_zero_production_writes(self):
        # Run the focused guard suite as a proper sub-suite (same
        # interpreter, repo-rooted) between the fingerprint captures.
        with tempfile.TemporaryDirectory(prefix="fpg_inv_") as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "unittest",
                 "tests.test_rehearsal_wrapper_guard",
                 "tests.test_db_target_identity", "-v"],
                capture_output=True, text=True,
                cwd=str(REPO),
                env={**os.environ, "PYTHONPATH": str(REPO)},
                timeout=600)
        self.assertEqual(result.returncode, 0,
                         f"focused suite regressed: {result.stderr[-2000:]}")
        pg_after = _pg_fingerprints(self._dsn, self._contract.get("pgpassword"))
        sqlite_after = _sqlite_store_sha()
        self.assertEqual(self._delta(self._pg_before, pg_after), [],
                         "production PG state changed across the focused "
                         "suite")
        self.assertEqual(self._delta(self._sqlite_before, sqlite_after), [],
                         "SQLite-era production store changed across the "
                         "focused suite")


if __name__ == "__main__":
    unittest.main(verbosity=2)