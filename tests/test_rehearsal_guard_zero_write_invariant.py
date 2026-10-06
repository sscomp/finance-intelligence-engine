#!/usr/bin/env python3
"""Zero-write production invariant across the focused rehearsal-guard suite
(2026-10-05 G6 remediation, WO Task D #12 / Task H; 2026-10-06 R4-R3-R1
hermeticization).

Runs the whole focused guard suite (tests.test_rehearsal_wrapper_guard +
tests.test_db_target_identity) as a sub-suite BETWEEN two production
fingerprint captures and asserts a ZERO delta on every protected table:

    score_snapshot, signal_log, graph_nodes, graph_edges,
    adapter_run_log, ingestion_errors, schema_migrations

plus the SQLite-era production store byte-identity (read-only sha256).

Production identity resolution — the REAL mechanism is never bypassed:

  * with a resolvable production contract (FIE_PRODUCTION_DB_CONTRACT or
    the operator defaults) the REAL production target is fingerprinted
    exactly as before — read-only (psql counts + server-side md5 row
    digests; only digests cross the process boundary);
  * WITHOUT one (credential-free Cloud hosts — the R4-R3 host-conditional
    skip) the SAME resolution/fingerprint code path is kept intact and
    aimed at a HERMETIC Production-SHAPED fixture: a real ephemeral
    PostgreSQL instance provisioned by the canonical portable provisioner
    (loopback-only, UTF-8, synthetic identity) holding the exact
    protected-table names, addressed through a synthetic 0600 contract
    file. Structurally representative, structurally local: no real
    credential, no external endpoint contacted.

  The hermetic leg additionally exercises the real guard BOUNDARY: a
  protected test-mode wrapper pipeline whose FIE_INTELLIGENCE_DB is the
  fixture's own Production-shaped DSN must be REFUSED (rc 78) before any
  write — Production classification recognized, guard executed, zero
  writes provable on the very instance the invariant protects.

This test NEVER skips on any host: the absence of a real production
contract SELECTS the hermetic leg (R4-R3-R1 remediation of the R4-R3
host-conditional skip; the hermetic leg is itself the zero-credential
proof)."""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.test_rehearsal_wrapper_guard import (  # noqa: E402
    GUARD_CODE,
    REFUSAL_RC,
    _make_stub_python,
    _ensure_prod_store_for_runtime,
    _run_wrapper,
    FIXTURE_PROD_DSN,
)

from scripts.db_target_identity import parse_postgres_dsn  # noqa: E402

PROD_TABLES = (
    "score_snapshot", "signal_log", "graph_nodes", "graph_edges",
    "adapter_run_log", "ingestion_errors", "schema_migrations",
)

DEFAULT_CONTRACT_FILES = (
    "/home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env",
)

# Hermetic fixture identity marker: an obviously-synthetic production-
# shaped name. The synthetic credential is a job-owned random value from
# the provisioner's 0600 auth.password file — no real credential involved
# anywhere in the fixture.
HERMETIC_DBNAME_PREFIX = "fie_prod_fixture_"
HERMETIC_USER_PREFIX = "fie_prod_fixture_"

_PROV_GLOBALS = (
    "FIE_TEST_PG_JOB_DIR", "FIE_TEST_PG_MODE", "FIE_TEST_PG_BIN_DIR",
    "FIE_TEST_PG_PORT", "FIE_TEST_PG_USER", "FIE_TEST_PG_DB",
    "FIE_TEST_PG_VERSION_STR", "FIE_TEST_PG_DSN",
    "PG_CACHE_DIR", "PG_CACHE_MODE", "PG_CACHE_PERSISTENT",
    "PG_CACHE_TEMP_CREATED", "_FIE_CACHE_RESOLVED_DIR",
    "_FIE_CACHE_RESOLVED_MODE", "_FIE_CACHE_RESOLVED_PERSISTENT",
)

_FIXTURE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS score_snapshot (
    snapshot_id TEXT PRIMARY KEY, scorer TEXT, entity_id TEXT,
    score NUMERIC DEFAULT 0, notes TEXT);
CREATE TABLE IF NOT EXISTS signal_log (
    signal_id TEXT PRIMARY KEY, signal_type TEXT,
    value NUMERIC DEFAULT 0, entity_id TEXT);
CREATE TABLE IF NOT EXISTS graph_nodes (node_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS graph_edges (edge_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS adapter_run_log (run_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS ingestion_errors (error_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY);
INSERT INTO score_snapshot (snapshot_id, scorer, entity_id, score, notes)
VALUES ('hermetic-zw-1', 'zero_write_fixture', 'fixture-entity', 1.0,
        'synthetic zero-write fixture sentinel')
ON CONFLICT (snapshot_id) DO NOTHING;
INSERT INTO signal_log (signal_id, signal_type, value, entity_id)
VALUES ('hermetic-zw-s1', 'zero_write_fixture', 1.0, 'fixture-entity')
ON CONFLICT (signal_id) DO NOTHING;
"""


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
            raise RuntimeError(f"psql count failed: rc={out.returncode} "
                               f"table={table}")
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


# ---------------------------------------------------------------- hermetic
# Provisioner state export/import between the start and stop subshells so
# the temp-mode cache token + in-scope memo survive across separate bash
# invocations (the resolver memo is per-shell scope).
_PROV_GLOBALS_STR = " ".join(_PROV_GLOBALS)


def _hermetic_fixture_dir() -> str:
    env_dir = os.environ.get("FIE_ZERO_WRITE_FIXTURE_DIR", "")
    return env_dir or tempfile.mkdtemp(prefix="fie-zw-fixture-")


def _run_prov_script(fixture: dict, body: str, phase: str) -> None:
    """Run provisioner lifecycle statements in one bash subshell; the
    provisioner globals are read back from the ``_E`` dump."""
    script = (". '%s'\n%s\n"
              "for _v in %s; do printf '_E %%s=%%q\\n' \"$_v\" \"${!_v-}\"; done\n"
              % (REPO / "scripts" / "provision-test-postgres.sh", body,
                 _PROV_GLOBALS_STR))
    result = subprocess.run(["bash", "-c", script],
                            capture_output=True, text=True,
                            cwd=str(REPO), timeout=300)
    out = []
    env = {}
    for line in result.stdout.splitlines():
        if line.startswith("_E "):
            key, _, raw = line[3:].partition("=")
            import shlex
            env[key] = "" if raw in ("''", "\"\"") else \
                " ".join(shlex.split(raw))
            continue
        out.append(line)
    if result.returncode != 0:
        tail = (result.stderr or "\n".join(out))[-1500:]
        raise RuntimeError(
            f"hermetic zero-write fixture: {phase} failed "
            f"(rc={result.returncode}): {tail}")
    fixture["env"] = env
    fixture["stdout"] = "\n".join(out)
    fixture["stderr"] = result.stderr


def hermetic_production_fixture() -> dict:
    """Start the hermetic Production-SHAPED fixture (R4-R3-R1 Task C):
    canonical ephemeral PG (loopback-only, UTF-8, synthetic identity) +
    the protected-table DDL + a synthetic 0600 contract file shaped
    exactly like a real production contract. Returns a fixture dict with
    the resolved contract ({'dsn', 'pgpassword'}); never dials an
    external endpoint; never requires nor contains a real credential."""
    fixture: dict = {"fixture_dir": _hermetic_fixture_dir(),
                     "evidence_file": ""}
    fixture_dir = Path(fixture["fixture_dir"])
    fixture_dir.mkdir(parents=True, exist_ok=True)

    evidence = fixture_dir / "teardown_evidence.json"
    fixture["evidence_file"] = str(evidence)
    fixture["job_id"] = "zero-write-hermetic" + os.urandom(3).hex()

    fixture["prov_env_before"] = {
        k: os.environ.get(k, "") for k in _PROV_GLOBALS}
    fixture["path_before"] = os.environ.get("PATH", "")
    fixture["contract_env_before"] = os.environ.get(
        "FIE_PRODUCTION_DB_CONTRACT", "")
    # Pin an explicit job-local cache: the fixture must not depend on the
    # operator's ambient persistent cache (nor inherit its lock residue —
    # a legacy lock there fails closed, which is correct but not this
    # suite's subject).
    fixture["cache_env_before"] = os.environ.get("FIE_TEST_PG_CACHE_DIR", "")
    os.environ["FIE_TEST_PG_CACHE_DIR"] = str(fixture_dir / "provcache")

    # Start (one subshell; provisioner globals exported as the _E lines).
    _run_prov_script(
        fixture, "fie_test_pg_start || exit $?\n", "provision")
    env = fixture["env"]
    for k in _PROV_GLOBALS:
        if env.get(k):
            os.environ[k] = env[k]
    job_dir = env.get("FIE_TEST_PG_JOB_DIR", "")
    bindir = env.get("FIE_TEST_PG_BIN_DIR", "")
    if not job_dir:
        raise RuntimeError("hermetic zero-write fixture: no job dir")

    # psql resolution: the provisioner bindir first (portable legs have no
    # system psql on PATH).
    os.environ["PATH"] = (
        f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
        if bindir else os.environ.get("PATH", ""))

    # Production-SHAPED identity + protected-table DDL (structural
    # representativeness of Production): dedicated role and database,
    # obviously-synthetic names, using ONLY the provisioner's generated
    # ephemeral credential material.
    password_value = (Path(job_dir) / "auth.password").read_text().strip()
    psql_env = {**os.environ, "PGPASSWORD": password_value}
    prod_role = f"{HERMETIC_USER_PREFIX}{os.urandom(3).hex()}"
    prod_db = f"{HERMETIC_DBNAME_PREFIX}{os.urandom(3).hex()}"
    for sql in (
            f"CREATE ROLE {prod_role} LOGIN "
            f"PASSWORD '{password_value}';",
            f"CREATE DATABASE {prod_db} OWNER {prod_role};"):
        out = subprocess.run(
            ["psql", env["FIE_TEST_PG_DSN"], "-v", "ON_ERROR_STOP=1", "-q",
             "-c", sql],
            capture_output=True, text=True, env=psql_env, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(
                f"hermetic zero-write fixture: identity DDL failed ({sql[:20]}"
                f"…): {out.stderr[-800:]}")
    port = env.get("FIE_TEST_PG_PORT", "")
    dsn = f"postgresql://{prod_role}@/{prod_db}?host={job_dir}&port={port}"
    out = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-c",
         _FIXTURE_TABLE_DDL],
        capture_output=True, text=True,
        env={**os.environ, "PGPASSWORD": password_value}, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(
            "hermetic zero-write fixture: protected-table DDL failed: "
            f"{out.stderr[-800:]}")

    # Synthetic production-SHAPED contract file (0600): the SAME file shape
    # and resolution mechanism as a real production contract. Its only
    # credential is the fixture instance's own generated ephemeral value.
    contract_file = fixture_dir / "synthetic-production-contract.env"
    contract_file.write_text(
        "FIE_SERVICE_ENV='production'\n"
        f"FIE_INTELLIGENCE_DB={shlex.quote(dsn)}\n"
        f"PGPASSWORD={shlex.quote(password_value)}\n")
    os.chmod(contract_file, 0o600)
    fixture["contract_path"] = str(contract_file)
    fixture["dsn"] = dsn
    fixture["pgpassword"] = password_value
    return fixture


def teardown_hermetic_fixture(fixture: dict) -> dict:
    """Stop the fixture instance, clean its temp cache, restore the
    provisioner globals the suite owned, and return the teardown evidence."""
    evidence: dict = {}
    env = fixture.get("env", {})
    # Re-enter a scope holding THIS fixture's resolver memo/state tokens,
    # then tear down in that same scope (temp-cache token must survive).
    state_lines = "\n".join(
        (f"export {_k}={shlex.quote(env[_k])}" if env.get(_k)
         else f"unset {_k} 2>/dev/null || true")
        for _k in _PROV_GLOBALS)
    body = (f"{state_lines}\n"
            f"export FIE_TEST_PG_TEARDOWN_EVIDENCE="
            f"{shlex.quote(fixture['evidence_file'])}\n"
            "fie_test_pg_stop\n"
            "STOP_RC=$?\n"
            "fie_test_pg_cache_cleanup\n"
            "CACHE_RC=$?\n"
            "printf 'STOP_RC=%s CACHE_RC=%s\\n' \"$STOP_RC\" \"$CACHE_RC\"\n")
    try:
        _run_prov_script(fixture, body, "teardown")
        rcs = dict(tok.split("=", 1)
                   for tok in fixture.get("stdout", "").split()[-2:]
                   if "=" in tok)
        if rcs.get("STOP_RC", "1") != "0" or rcs.get("CACHE_RC", "1") != "0":
            raise RuntimeError(
                f"hermetic zero-write fixture teardown rc not clean: {rcs}"
                f" stderr={(fixture.get('stderr') or '')[-800:]}")
        evidence_file = Path(fixture["evidence_file"])
        if evidence_file.exists():
            evidence = json.loads(evidence_file.read_text())
    finally:
        prov_env = fixture.get("prov_env_before", {})
        for k in _PROV_GLOBALS:
            if prov_env.get(k):
                os.environ[k] = prov_env[k]
            else:
                os.environ.pop(k, None)
        if fixture.get("contract_env_before"):
            os.environ["FIE_PRODUCTION_DB_CONTRACT"] = \
                fixture["contract_env_before"]
        else:
            os.environ.pop("FIE_PRODUCTION_DB_CONTRACT", None)
        if fixture.get("cache_env_before"):
            os.environ["FIE_TEST_PG_CACHE_DIR"] = fixture["cache_env_before"]
        else:
            os.environ.pop("FIE_TEST_PG_CACHE_DIR", None)
        os.environ["PATH"] = fixture.get("path_before") or \
            os.environ.get("PATH", "")
        if not os.environ.get("FIE_ZERO_WRITE_FIXTURE_DIR"):
            # job-owned fixture workspace: removable ONLY here
            import shutil
            shutil.rmtree(fixture["fixture_dir"], ignore_errors=True)
    return evidence


class TestProductionZeroWriteInvariant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._real_contract = _resolve_prod_contract()
        cls._hermetic = None
        cls._contract = None
        if cls._real_contract.get("dsn"):
            cls._contract = cls._real_contract
        else:
            # Hermetic leg: synthetic Production-shaped fixture through the
            # SAME resolve/fingerprint mechanisms.
            cls._hermetic = hermetic_production_fixture()
            cls._contract = {"dsn": cls._hermetic["dsn"],
                             "pgpassword": cls._hermetic["pgpassword"]}
        cls._pg_before = _pg_fingerprints(
            cls._contract["dsn"], cls._contract.get("pgpassword"))
        # SQLite-era store byte-identity: real on the operator host,
        # synthetic (self-removed) on fresh clones — identical semantics.
        if not _sqlite_store_sha():
            _ensure_prod_store_for_runtime(cls)
        cls._sqlite_before = _sqlite_store_sha()
        cls.addClassCleanup(
            lambda: cls._stop_fixture())

    @classmethod
    def _stop_fixture(cls):
        hermetic, cls._hermetic = getattr(cls, "_hermetic", None), None
        if hermetic and hermetic.get("fixture_dir"):
            return teardown_hermetic_fixture(hermetic)
        return {}

    @classmethod
    def tearDownClass(cls):
        # Assertion lives here so a teardown defect fails the class even
        # after a green test body; the returned evidence is the residue
        # proof (verify-stopped gates job-owned-live leftovers already).
        if getattr(cls, "_hermetic", None):
            cls._stop_fixture()

    def _hermetic_fixture_assertions(self):
        """Credential-free + no-external-endpoint proof for the hermetic
        leg (assertions, not documentation)."""
        if not self._hermetic:
            return None
        ident = parse_postgres_dsn(self._contract["dsn"])
        host = ident.get("host", "")
        self.assertTrue(host == "loopback" or host.startswith("socket:"),
                        f"fixture target host is not local: {host}")
        self.assertIn(HERMETIC_DBNAME_PREFIX, ident.get("dbname", ""))
        self.assertIn(HERMETIC_DBNAME_PREFIX, self._contract["dsn"])
        contract_path = Path(self._hermetic["contract_path"])
        self.assertNotIn(str(contract_path), DEFAULT_CONTRACT_FILES)
        contract_text = contract_path.read_text()
        self.assertIn(HERMETIC_DBNAME_PREFIX, contract_text)
        # The only credential in the contract is the fixture's own
        # generated ephemeral value (job-owned, instance-local).
        self.assertEqual(contract_path.stat().st_mode & 0o777, 0o600)
        return ident

    def _delta(self, before: dict, after: dict) -> list:
        return [k for k in before if before.get(k) != after.get(k)]

    def test_focused_guard_suite_causes_zero_production_writes(self):
        # Guard-boundary leg first (hermetic leg): the fixture's own
        # Production-shaped DSN as an explicit rehearsal target must be
        # REFUSED before any write (classification recognized; guard
        # executed; refusal precedes the protected write path).
        self._hermetic_fixture_assertions()
        if self._hermetic:
            self._refusal_leg()
        # Run the focused guard suite as a proper sub-suite (same
        # interpreter, repo-rooted) between the fingerprint captures.
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
        pg_after = _pg_fingerprints(self._contract["dsn"],
                                    self._contract.get("pgpassword"))
        sqlite_after = _sqlite_store_sha()
        self.assertEqual(self._delta(self._pg_before, pg_after), [],
                         "production PG state changed across the focused "
                         "suite")
        self.assertEqual(self._delta(self._sqlite_before, sqlite_after), [],
                         "SQLite-era production store changed across the "
                         "focused suite")

    def _refusal_leg(self):
        prod = _ensure_prod_store_for_runtime(self)
        prod_sha_before = _sha256(prod)
        stub_dir = tempfile.mkdtemp(prefix="fpg_stub_")
        self.addCleanup(shutil.rmtree, stub_dir, True)
        stub = _make_stub_python(Path(stub_dir))
        # Pin the wrapper env file too: an ambient operator wrapper env
        # file must never redefine the candidate (the same hermeticity
        # rule the wrapper-guard fixture tests follow).
        wrapper_env = Path(tempfile.mkdtemp(prefix="fpg_env_")) / "w.env"
        wrapper_env.write_text("")
        self.addCleanup(shutil.rmtree, wrapper_env.parent, True)
        result = _run_wrapper("run.sh", {
            "FIE_SERVICE_ENV": "test",
            "FIE_INTELLIGENCE_DB": self._contract["dsn"],
            "FIE_WRAPPER_ENV": str(wrapper_env),
            "FIE_PRODUCTION_DB_CONTRACT": self._hermetic["contract_path"],
            "FIE_PYTHON": stub})
        self.assertEqual(result.returncode, REFUSAL_RC, result.stderr)
        self.assertIn(GUARD_CODE, result.stderr)
        self.assertIn("Production-equivalent PostgreSQL target",
                      result.stderr)
        self.assertNotIn("STUB_STEP1_SENTINEL", result.stdout)
        self.assertEqual(_sha256(prod), prod_sha_before,
                         "production store changed during the refusal leg")


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    unittest.main(verbosity=2)