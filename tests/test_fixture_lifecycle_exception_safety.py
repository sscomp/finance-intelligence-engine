#!/usr/bin/env python3
"""Exception-safe fixture lifecycle + canonical SQL client contract
(6.9A-R4-R4-R1 Tasks 1-3 / WO §5-§11).

Remediates the two fresh-Codex-Cloud R4-R4 acceptance defects:

  * MANDATORY_TEST_FAILURE — hermetic validation shelled out to host
    ``psql`` (absent in fresh Cloud; the portable PG artifact ships only
    initdb/pg_ctl/postgres). The canonical execution boundary is now
    scripts/sql_exec.py (psycopg): it is exercised HERE against a real
    ephemeral server, and the zero-write fixture's own SQL path carries
    no host-psql dependence (proved end-to-end under the hostile
    no-psql simulation in the acceptance evidence).

  * RESOURCE_OWNERSHIP_FAILURE — a zero-write fixture failing during
    setUpClass leaked environment/cache/registry/instance state into the
    next lifecycle fixture (its registry carried the PREVIOUS fixture's
    cache path). The fixture lifecycle is now transactional (snapshot →
    registered teardown responsibility → fallible steps → deterministic
    ownership-aware restoration on ANY failure, original exception
    re-raised) and every provision subshell scrubs per-job provisioner
    globals — no ambient leak vector remains.

Failure-injection matrix (WO §7): FI-01..FI-10 — every case proves BOTH
that the original exception semantics are preserved AND that the
post-failure state is clean/attributed. Test-order independence (WO §8):
the exact historical failing sequence, its reverse, each class alone,
and a repeated alternation — all driven as child interpreters so no
in-process cross-test state can hide.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import sql_exec  # noqa: E402
from scripts import resource_ownership as ro  # noqa: E402
import tests.test_rehearsal_guard_zero_write_invariant as zw  # noqa: E402

PROVISIONER = REPO / "scripts" / "provision-test-postgres.sh"
NO_PRODUCTION_CONTRACT = "/nonexistent-fie-contract.env"
TEARDOWN_EVIDENCE_FORMAT = "fie-6-9a-teardown-evidence-v1"


def _proc_alive(pid: int) -> bool:
    snap = ro.read_proc(pid)
    return snap is not None and snap.state not in ("Z",)


def _shl(v: str) -> str:
    import shlex
    return shlex.quote(v)


def _env_snapshot() -> dict:
    """Every process-visible variable this remediation owns."""
    return {k: os.environ.get(k, "")
            for k in (*zw._PROV_GLOBALS, "FIE_PRODUCTION_DB_CONTRACT",
                      "PATH", "PGPASSWORD", "FIE_INTELLIGENCE_DB",
                      "FIE_SERVICE_ENV")}


def _run_probe(body: str, timeout: int = 180):
    """Run provisioner statements in a scrubbed scope (same job-state
    hygiene as zw._run_prov_script); returns (rc, stdout, stderr)."""
    env = {k: v for k, v in os.environ.items() if k not in zw._PROV_GLOBALS}
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    env.setdefault("HOME", "/tmp")
    res = subprocess.run(
        ["bash", "-c", ". %s\n%s" % (_shl(str(PROVISIONER)), body)],
        capture_output=True, text=True, env=env, timeout=timeout,
        cwd=str(REPO))
    return res.returncode, res.stdout, res.stderr


def _stop_prov_scope(fix_env: dict, evidence_path: str) -> tuple:
    """Granted stop + cache cleanup for a provisioned scope, carrying the
    scope's own state tokens (returns (probe_rc, stdout, stderr))."""
    state = "\n".join(
        (f"export {k}={_shl(fix_env[k])}" if fix_env.get(k)
         else f"unset {k} 2>/dev/null || true")
        for k in zw._PROV_GLOBALS)
    body = (". %s\n%s\nexport FIE_TEST_PG_TEARDOWN_EVIDENCE=%s\n"
            "fie_test_pg_stop\nSTOP_RC=$?\n"
            "fie_test_pg_cache_cleanup\nCACHE_RC=$?\n"
            "printf 'STOP_RC=%%s CACHE_RC=%%s\\n' \"$STOP_RC\" \"$CACHE_RC\"\n"
            % (_shl(str(PROVISIONER)), state, _shl(evidence_path)))
    return _run_probe(body)


# ---------------------------------------------------------------- Task 1
class CanonicalSqlClientContractTests(unittest.TestCase):
    """WO §11 (2)(3)(4) + §5.2: the canonical portable SQL client executes
    required test SQL against a real ephemeral server, can never select a
    Production target by fallback, preserves original error semantics,
    never emits secrets, and fails closed (precise contract error) when
    the driver is unavailable."""

    @classmethod
    def setUpClass(cls):
        cls.hold = {k: os.environ.get(k, "")
                    for k in (*zw._PROV_GLOBALS,
                              "FIE_TEST_PG_CACHE_DIR",
                              "FIE_PRODUCTION_DB_CONTRACT")}
        os.environ["FIE_PRODUCTION_DB_CONTRACT"] = NO_PRODUCTION_CONTRACT
        cls.fixture_dir = tempfile.mkdtemp(prefix="fie-sqlclient-")
        cls.cache_dir = os.path.join(cls.fixture_dir, "provcache")
        os.environ["FIE_TEST_PG_CACHE_DIR"] = cls.cache_dir
        cls.fix: dict = {}
        try:
            zw._run_prov_script(
                cls.fix, "fie_test_pg_start || exit $?\n", "provision")
        except Exception:
            cls.tearDownClass()
            raise
        cls.job_dir = cls.fix["env"]["FIE_TEST_PG_JOB_DIR"]
        cls.dsn = cls.fix["env"]["FIE_TEST_PG_DSN"]
        cls.password = (Path(cls.job_dir) /
                        "auth.password").read_text().strip()

    @classmethod
    def tearDownClass(cls):
        try:
            if getattr(cls, "job_dir", None):
                ev_path = str(Path(cls.fixture_dir) /
                              "teardown_evidence.json")
                rc, out, err = _stop_prov_scope(cls.fix["env"], ev_path)
                if rc != 0 or "STOP_RC=0" not in out or \
                        "CACHE_RC=0" not in out:
                    raise AssertionError(
                        f"sql-client probe teardown unclean rc={rc} "
                        f"out={out[-400:]} err={err[-400:]}")
        finally:
            shutil.rmtree(cls.fixture_dir, ignore_errors=True)
            for k, v in cls.hold.items():
                if v:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_canonical_helper_executes_required_sql_utf8(self):
        # (11.2) canonical SQL helper executes required test SQL, UTF-8
        # preserved end-to-end (defect 1's psql-shaped contract, now
        # driver-based).
        rows = sql_exec.fetch_rows(
            self.dsn, "SELECT current_setting('server_encoding');",
            password=self.password)
        self.assertEqual(rows[0][0], "UTF8")
        sql_exec.execute_statements(
            self.dsn,
            ["CREATE TABLE fie_sql_probe (id int PRIMARY KEY, note text)",
             "INSERT INTO fie_sql_probe VALUES (1, 'canonical-測試')"],
            password=self.password)
        rows = sql_exec.fetch_rows(
            self.dsn, "SELECT id, note FROM fie_sql_probe ORDER BY id;",
            password=self.password)
        self.assertEqual(rows, [(1, "canonical-測試")])
        self.assertEqual(
            sql_exec.query_scalar(self.dsn, "SELECT 42::int;",
                                  password=self.password), 42)
        rows = sql_exec.fetch_rows(
            self.dsn, "SELECT count(*) FROM fie_sql_probe;",
            password=self.password)
        self.assertEqual(rows[0][0], 1)

    def test_sql_failure_preserves_original_error(self):
        # (11.4) SQL execution failure preserves the original error.
        with self.assertRaises(sql_exec.SqlExecError) as caught:
            sql_exec.execute_statements(
                self.dsn, ["SELECT * FROM definitely_not_here"],
                password=self.password)
        self.assertIn("POSTGRESQL_SQL_EXEC_FAILED", str(caught.exception))
        self.assertIn("definitely_not_here", str(caught.exception))
        self.assertIsNotNone(caught.exception.__cause__,
                              "original server error must be chained")

    def test_no_environment_fallback_and_fail_closed_target(self):
        # (11.3) Production DSN cannot be selected by fallback: even with
        # production-shaped ambient variables present, the helper only ever
        # addresses the EXPLICIT dsn argument; an ambiguous target fails
        # closed BEFORE dialing anything.
        with mock.patch.dict(os.environ, {
                "FIE_PRODUCTION_DB_CONTRACT": NO_PRODUCTION_CONTRACT,
                "FIE_INTELLIGENCE_DB":
                    "postgresql://prod-fixture@/prod-fixture-db"
                    "?host=127.0.0.1&port=5432"}):
            with self.assertRaises(sql_exec.SqlExecError) as caught:
                sql_exec.query_scalar(None, "SELECT 1")
            self.assertIn("POSTGRESQL_SQL_TARGET_UNRESOLVED",
                          str(caught.exception))
            with self.assertRaises(sql_exec.SqlExecError) as caught:
                sql_exec.query_scalar(
                    "postgresql://nobody@/nodb?host=/tmp/"
                    "no-such-socket-r4r4r1",
                    "SELECT 1")
            # the failure names the EXPLICIT target, never the ambient
            # production-shaped value
            self.assertIn("POSTGRESQL_SQL_CONNECT_FAILED",
                          str(caught.exception))
            self.assertNotIn("prod-fixture-db",
                             str(caught.exception))

    def test_secrets_never_emitted(self):
        # (11.15) DSN-embedded passwords never reach a message.
        dsn = ("postgresql://u@/d?password=secret-value-9f&host="
               "/tmp/no-such-socket-r4r4r1")
        with self.assertRaises(sql_exec.SqlExecError) as caught:
            sql_exec.fetch_rows(dsn, "SELECT 1")
        self.assertNotIn("secret-value-9f", str(caught.exception))
        self.assertIn("password=***", sql_exec.redact_dsn(dsn))
        self.assertNotIn("secret-value-9f", sql_exec.redact_dsn(dsn))

    def test_missing_driver_fails_closed_with_contract_error(self):
        # §5.3(6): a missing client fails with a precise contract error,
        # never a generic FileNotFoundError.
        with mock.patch.object(sql_exec, "_DRIVER", "no-such-driver-r4r4r1"):
            with self.assertRaises(sql_exec.SqlClientUnavailable) as caught:
                sql_exec.resolve_client()
            self.assertIn("POSTGRESQL_SQL_CLIENT_UNAVAILABLE",
                          str(caught.exception))
        self.assertEqual(sql_exec.resolve_client(), "psycopg")


# ---------------------------------------------------------------- Task 3
class FixtureLifecycleExceptionSafetyTests(unittest.TestCase):
    """Failure-injection matrix FI-01..FI-10 (WO §7): deterministic
    restoration at every intermediate setup point; original exception
    preserved; no leaked state into process-visible globals, fixture
    workspaces, or job-owned instances."""

    def setUp(self):
        self.before_dirs = set(Path("/tmp").glob("fie-zw-fixture-*"))
        self.before_env = _env_snapshot()

    def tearDown(self):
        after_dirs = set(Path("/tmp").glob("fie-zw-fixture-*"))
        self.assertEqual(after_dirs - self.before_dirs, set(),
                         "leaked fixture workspace after injection case")
        after_env = _env_snapshot()
        self.assertEqual(
            after_env, self.before_env,
            "environment not restored: delta=" +
            str({k: (self.before_env.get(k), after_env.get(k))
                 for k in set(after_env) | set(self.before_env)
                 if after_env.get(k) != self.before_env.get(k)}))

    # -- helpers ---------------------------------------------------------
    def _injected(self, step: str):
        """Run hermetic_production_fixture with <step> patched to raise
        RuntimeError(<marker>) BEFORE the real step executes."""
        marker = f"FI-INJECT/{step}"
        holder: dict = {}
        with mock.patch.object(zw, step,
                               side_effect=RuntimeError(marker)) as m:
            with self.assertRaises(RuntimeError) as caught:
                zw.hermetic_production_fixture(holder)
        self.assertEqual(m.call_count, 1,
                         f"patched step '{step}' was not the injection "
                         "boundary")
        self.assertEqual(str(caught.exception), marker,
                         "original exception semantics not preserved")
        return holder, marker, caught.exception

    def _injected_after(self, step: str):
        """Provision for REAL (job-owned instance exists), execute the
        real step, THEN raise at that step's boundary. Before raising,
        the job-owned instance identity is captured from the creation-time
        registry so the post-teardown stop can be PROVED even though the
        granted teardown removes the job dir (and the registry with it)."""
        marker = f"FI-INJECT/{step}"
        holder: dict = {}
        real = getattr(zw, step)

        def fake(fixture, *args, **kwargs):
            real(fixture, *args, **kwargs)
            jd = (fixture.get("env") or {}).get("FIE_TEST_PG_JOB_DIR", "")
            if jd:
                fixture["_pre_teardown_job_dir"] = jd
                reg = ro.load_registry(
                    Path(jd, "ownership_registry.json"))
                server = next((r for r in (reg or {"resources": []})
                               ["resources"]
                               if r["kind"] == "postgres_server"), None)
                if server and server.get("process", {}).get("pid"):
                    fixture["_pre_teardown_server_pid"] = \
                        int(server["process"]["pid"])
            raise RuntimeError(marker)

        with mock.patch.object(zw, step, fake):
            with self.assertRaises(RuntimeError) as caught:
                zw.hermetic_production_fixture(holder)
        self.assertEqual(str(caught.exception), marker)
        return holder, caught.exception

    def _assert_instance_stopped(self, holder) -> str:
        """The job-owned instance captured before teardown must be dead
        and its job dir gone after the failure-path teardown."""
        job_dir = holder.get("_pre_teardown_job_dir", "")
        self.assertTrue(job_dir, "real provision did not run")
        env = holder.get("env") or {}
        self.assertEqual(env.get("FIE_TEST_PG_JOB_DIR", ""), job_dir)
        pid = holder.get("_pre_teardown_server_pid")
        if pid:
            self.assertFalse(_proc_alive(pid),
                             f"job-owned instance leaked: pid {pid}")
        return job_dir

    def _assert_env_clean(self):
        for k in zw._PROV_GLOBALS:
            self.assertEqual(os.environ.get(k, ""),
                             self.before_env.get(k, ""),
                             f"provisioner global {k} leaked")

    # -- FI-01: before PostgreSQL acquisition ----------------------------
    def test_fi01_failure_before_acquisition_no_leaked_state(self):
        holder, _, _ = self._injected("_fixture_pin_cache")
        self.assertEqual(holder.get("env") or {}, {})
        self.assertTrue(holder.get("torn_down"))
        self.assertFalse(holder.get("teardown_error"),
                         f"teardown error: {holder.get('teardown_error')}")

    # -- FI-02: after cache path selection -------------------------------
    def test_fi02_failure_after_cache_path_selection_restores_env(self):
        holder, _, _ = self._injected("_fixture_provision")
        # the cache path was selected (pin ran), then restored
        self.assertEqual(
            os.environ.get("FIE_TEST_PG_CACHE_DIR", ""),
            self.before_env.get("FIE_TEST_PG_CACHE_DIR", ""),
            "cache path selection state leaked")
        self.assertTrue(holder.get("torn_down"))

    # -- FI-03/FI-04: after portable PG acquisition / cluster creation ---
    def test_fi03_failure_after_acquisition_no_registry_cache_leak(self):
        holder, _ = self._injected_after("_fixture_apply_prov_env")
        job_dir = self._assert_instance_stopped(holder)
        self.assertFalse(os.path.isdir(job_dir), "job dir leaked (FI-03)")
        # the job dir is REMOVED by the granted teardown — the registry
        # (inside it) is gone too: no attribution ambiguity remains
        self.assertFalse(
            Path(job_dir, "ownership_registry.json").exists(),
            "registry leaked outside the removed job dir")
        self._assert_env_clean()

    def test_fi04_failure_after_cluster_creation_stops_owned_instance(self):
        holder, _ = self._injected_after("_fixture_create_production_identity")
        job_dir = self._assert_instance_stopped(holder)
        self.assertFalse(os.path.isdir(job_dir))
        self._assert_env_clean()

    def test_fi05_failure_after_dsn_creation_restores_dsn_env(self):
        holder, _ = self._injected_after("_fixture_create_protected_tables")
        # the production-SHAPED DSN was created (holder carries it), and
        # the FIE_TEST_PG_DSN / role / db job state is fully restored
        self.assertIn("postgresql://", holder["dsn"])
        self.assertIn(zw.HERMETIC_DBNAME_PREFIX, holder["dsn"])
        job_dir = self._assert_instance_stopped(holder)
        self.assertFalse(os.path.isdir(job_dir))
        self.assertEqual(os.environ.get("FIE_TEST_PG_DSN", ""),
                         self.before_env.get("FIE_TEST_PG_DSN", ""),
                         "DSN env leaked after FI-05")

    def test_fi06_failure_during_sql_initialization(self):
        # the canonical SQL client raising DURING fixture SQL preparation
        # (identity/DDL stage) re-raises the ORIGINAL error semantics and
        # tears down the already-created instance.
        marker = "FI-06-SQL-INIT"
        holder: dict = {}
        captured: dict = {}

        def fake_sql(*args, **kwargs):
            # the provision state is already applied when fixture SQL runs
            jd = os.environ.get("FIE_TEST_PG_JOB_DIR", "")
            captured["job_dir"] = jd
            if jd:
                reg = ro.load_registry(Path(jd) / "ownership_registry.json")
                server = next((r for r in (reg or {"resources": []})
                               ["resources"]
                               if r["kind"] == "postgres_server"), None)
                if server and server.get("process", {}).get("pid"):
                    captured["pid"] = int(server["process"]["pid"])
            raise RuntimeError(marker)

        with mock.patch.object(zw.sql_exec, "execute_statements", fake_sql):
            with self.assertRaises(RuntimeError) as caught:
                zw.hermetic_production_fixture(holder)
        self.assertEqual(str(caught.exception), marker)
        holder["_pre_teardown_job_dir"] = captured.get("job_dir", "")
        holder["_pre_teardown_server_pid"] = captured.get("pid")
        self._assert_instance_stopped(holder)
        self.assertFalse(
            os.path.isdir(captured.get("job_dir") or "FI-06-unsentinel"))
        self._assert_env_clean()

    def test_fi07_fully_built_fixture_teardown_is_deterministic(self):
        # failure "immediately before the test body": the fixture is fully
        # built, then the class fails anyway; the teardown reclaims
        # EVERYTHING from a fully built fixture (and FI-08 below proves the
        # repeated call is a no-op).
        holder: dict = {}
        zw.hermetic_production_fixture(holder)
        self.assertIn("postgresql://", holder["dsn"])
        self.assertIn(zw.HERMETIC_DBNAME_PREFIX, holder["dsn"])
        evidence = zw.teardown_hermetic_fixture(holder)
        self.assertEqual(evidence.get("format"), TEARDOWN_EVIDENCE_FORMAT)
        self.assertTrue(holder["torn_down"])
        job_dir = (holder.get("env") or {}).get("FIE_TEST_PG_JOB_DIR", "")
        self.assertTrue(job_dir)
        self.assertFalse(os.path.isdir(job_dir),
                         "job dir left behind after teardown")
        self.assertIn(job_dir, evidence.get("removed", []),
                      "teardown evidence must record the job dir removal")
        self.assertEqual(evidence.get("job_dir"), job_dir)
        self._assert_env_clean()

    # -- FI-08: teardown invoked twice ------------------------------------
    def test_fi08_teardown_idempotent(self):
        holder: dict = {}
        zw.hermetic_production_fixture(holder)
        first = zw.teardown_hermetic_fixture(holder)
        self.assertTrue(first)
        second = zw.teardown_hermetic_fixture(holder)
        self.assertEqual(second, {}, "second teardown must be a no-op")
        self.assertEqual(
            os.environ.get("FIE_TEST_PG_CACHE_DIR", ""),
            self.before_env.get("FIE_TEST_PG_CACHE_DIR", ""))

    # -- FI-09: foreign/unknown resource present --------------------------
    def test_fi09_foreign_workspace_preserved_fail_closed(self):
        opdir = Path(tempfile.mkdtemp(prefix="fie-fi09-foreign-"))
        self.addCleanup(shutil.rmtree, opdir, ignore_errors=True)
        foreign = opdir / "operator-file.txt"
        foreign.write_text("operator-owned, not removable by any fixture")
        holder: dict = {}
        with mock.patch.dict(os.environ,
                             {"FIE_ZERO_WRITE_FIXTURE_DIR": str(opdir)}):
            with mock.patch.object(zw, "_fixture_provision",
                                   side_effect=RuntimeError("FI-09")):
                with self.assertRaises(RuntimeError):
                    zw.hermetic_production_fixture(holder)
        self.assertTrue(foreign.exists(),
                        "foreign operator file must never be removed")
        self.assertTrue(opdir.exists(),
                        "operator-designated workspace dir must be kept")
        # env restored even though the operator workspace is preserved
        self.assertEqual(
            os.environ.get("FIE_TEST_PG_CACHE_DIR", ""),
            self.before_env.get("FIE_TEST_PG_CACHE_DIR", ""))

    def test_fi09_unknown_cache_lock_preserved_when_cleanup_runs(self):
        # FI-09 + (11.14): UNKNOWN ownership remains fail-closed even on
        # the failure path that DOES run cache cleanup (a legacy 0-byte
        # lock in the fixture's own pinned cache: preserved, never claimed).
        opdir = Path(tempfile.mkdtemp(prefix="fie-fi09-lock-"))
        self.addCleanup(shutil.rmtree, opdir, ignore_errors=True)
        # a foreign operator file lives BELOW the fixture workspace root
        foreign = opdir / "operator-note.txt"
        foreign.write_text("foreign")
        lock = opdir / "provcache" / ".acquire.lock"
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b"")  # legacy 0-byte lock form: UNKNOWN
        holder: dict = {}
        with mock.patch.dict(os.environ,
                             {"FIE_ZERO_WRITE_FIXTURE_DIR": str(opdir)}):
            holder, _ = self._injected_after(
                "_fixture_create_production_identity")
            self._assert_instance_stopped(holder)
        self.assertTrue(
            foreign.exists(),
            "foreign file inside the designated workspace is preserved")
        self.assertTrue(lock.exists(),
                        "UNKNOWN-classified lock must be preserved "
                        "(fail-closed, never claimed by cleanup)")
        self._assert_env_clean()

    # -- FI-10: failed setup followed by another fixture class ------------
    def test_fi10_failed_setup_then_next_fixture_sees_clean_state(self):
        # reproduce the R4-R4 RESOURCE_OWNERSHIP_FAILURE semantically, but
        # with the remediated fixture: a failing zero-write setup, then a
        # freshly provisioned lifecycle-style fixture must see its OWN
        # cache path in the creation-time registry (never the failed
        # fixture's), and never the failed fixture's instance.
        self._injected_after("_fixture_apply_prov_env")
        own_dir = tempfile.mkdtemp(prefix="fie-fi10-next-")
        self.addCleanup(shutil.rmtree, own_dir, ignore_errors=True)
        own_cache = os.path.join(own_dir, "provcache")
        os.environ["FIE_TEST_PG_CACHE_DIR"] = own_cache  # lifecycle pattern
        self.addCleanup(os.environ.pop, "FIE_TEST_PG_CACHE_DIR", None)
        try:
            fix: dict = {}
            zw._run_prov_script(
                fix, "fie_test_pg_start || exit $?\n", "provision")
            job_dir = fix["env"]["FIE_TEST_PG_JOB_DIR"]
            registry = ro.load_registry(
                Path(job_dir) / "ownership_registry.json")
            cache_entry = next((r for r in registry["resources"]
                                if r["kind"] == "cache_dir"), None)
            if cache_entry:
                self.assertEqual(
                    cache_entry["path"], own_cache,
                    "next fixture contaminated by the failed fixture's "
                    "cache path (R4-R4 RESOURCE_OWNERSHIP_FAILURE shape)")
            else:
                # no cache entry is also clean — but then NO registry
                # entry may reference the failed fixture's workspace
                # either way, the contamination check below is binding.
                self.assertNotIn("fie-zw-fixture",
                                 json.dumps(registry, default=str))
            server = next(r for r in registry["resources"]
                          if r["kind"] == "postgres_server")
            self.assertNotIn("fie-zw-fixture",
                             server["process"]["command_signature"])
            rc, out, err = _stop_prov_scope(
                fix["env"], str(Path(own_dir) / "teardown_evidence.json"))
            self.assertEqual(rc, 0, msg=err[-400:])
            self.assertIn("STOP_RC=0", out)
            self.assertIn("CACHE_RC=0", out)
        finally:
            for k, v in self.before_env.items():
                if v:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)


# ------------------------------------------------------- WO §8 (orders)
class FixtureOrderIndependenceTests(unittest.TestCase):
    """The exact historical failing sequence (zero-write → lifecycle), its
    reverse, each class alone, and a repeated alternation — all green in
    hermetic mode, each driven as a CHILD interpreter so no in-process
    cross-test state can hide."""

    HERMETIC_ENV = {
        "FIE_PRODUCTION_DB_CONTRACT": NO_PRODUCTION_CONTRACT,
        "FIE_SERVICE_ENV": "test",
        "ARTIFACT_DIR": "",
    }

    def _run_order(self, modules: list) -> str:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("FIE_") and k != "PGPASSWORD"}
        env["PYTHONPATH"] = str(REPO)
        env.update(self.HERMETIC_ENV)
        res = subprocess.run(
            [sys.executable, "-m", "unittest", *modules],
            capture_output=True, text=True, cwd=str(REPO), env=env,
            timeout=1800)
        self.assertEqual(
            res.returncode, 0,
            f"order {modules} failed rc={res.returncode}\n"
            f"{res.stderr[-2500:]}")
        return res.stderr

    def test_historical_failing_order_is_green(self):
        # WO §8: the exact sequence that failed in Codex Cloud.
        out = self._run_order([
            "tests.test_rehearsal_guard_zero_write_invariant",
            "tests.test_resource_ownership_contract"])
        self.assertIn("OK", out)

    def test_reverse_order_is_green(self):
        out = self._run_order([
            "tests.test_resource_ownership_contract",
            "tests.test_rehearsal_guard_zero_write_invariant"])
        self.assertIn("OK", out)

    def test_each_class_alone_is_green(self):
        for module in ("tests.test_rehearsal_guard_zero_write_invariant",
                       "tests.test_resource_ownership_contract"):
            self.assertIn("OK", self._run_order([module]))

    def test_repeated_alternation_is_green(self):
        pair = ["tests.test_rehearsal_guard_zero_write_invariant",
                "tests.test_resource_ownership_contract"]
        self.assertIn("OK", self._run_order(pair * 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)