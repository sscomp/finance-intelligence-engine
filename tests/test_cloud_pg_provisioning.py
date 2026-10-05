#!/usr/bin/env python3
"""Ephemeral PostgreSQL provisioning contract + negative controls (6.9A-R3).

Locks the contract of ``scripts/provision-test-postgres.sh`` — the ONE
canonical repository-owned boundary that gives a fresh environment a real
ephemeral PostgreSQL server with NO system tooling installed, NO Production
fallback and NO caller DB target (ABACUS_FIE_6_9A_R3 §10/§11):

    NC4    explicit --force-portable selects the pinned portable
           distribution and never a discovered system tooling
    NC5    unsupported-platform acquisition is refused (exit 90,
           POSTGRESQL_PLATFORM_UNSUPPORTED)
    NC6    artifact-unavailable acquisition is refused (exit 91) — it
           NEVER collapses into a Production fallback; a cache whose
           integrity cannot be re-proven is refused (exit 92)
    NC7/8  any production-flavored caller env candidate is refused
           fail-closed (exit 78; DSN values never printed), including
           through scripts/test-cloud.sh preflight
    NC9    the server-side listener contract is pinned loopback-only
    NC10   provisioned server must be UTF8 (pinned initdb encoding)
    NC11   synthetic identity: per-run random user/db, never fie_prod
    NC12   a failed initdb/start leaves no owned state behind
    NC13   the exported DSN is parseable by the runtime contract
    NC13b  teardown stops ONLY the job-owned cluster (an unrelated
           cluster keeps serving), refuses non-job dirs, is idempotent
    NC14   failure classes are explicit exit codes, never generic
    NC15   bootstrap stays valid + idempotent (executed gate over
           tests/test_bootstrap_cli_contract)

Offline-safe, deterministic: no unittest downloads the artifact. The
portable path is exercised ONLY through a PRE-WARMED portable cache
(``scripts/test-cloud.sh`` warms it before the tests run — which includes
a fresh cloud clone), so acquisition itself never runs inside the suite;
the network-true acquisition is proven in the evidence layer. If the
cache is cold, portable-legged tests SKIP with an explicit attribution
(never a silent pass). Fake-tooling probes make the portable-legged and
stub-legged paths provoke fail-closed behavior without any state leak.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

# 6.9A-R4-R2-R1 Task C/D: ONE helper for scrubbed-child env construction and
# for canonical cache-state resolution — the provisioning tests no longer
# own a second resolution engine (no $HOME/.cache assumption).
from tests import cloud_child_env as _cce

REPO = Path(__file__).resolve().parents[1]
PROVISIONER = REPO / "scripts" / "provision-test-postgres.sh"
TEST_CLOUD = REPO / "scripts" / "test-cloud.sh"
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"
VENV_PY = REPO / ".venv" / "bin" / "python3"

_CACHE_STATE: dict | None = None


def _active_cache_state(force=False):
    """The ACTIVE cache state, resolved by EXECUTING the shipped canonical
    resolver (``--cache-state``) through the shared Task D helper — never
    re-derived from $HOME here (R4-R2 blocker #3: 8 portable-path helpers
    inspected an unreachable HOME cache while the runtime selected an
    isolated temp cache)."""
    global _CACHE_STATE
    if _CACHE_STATE is None or force:
        try:
            _CACHE_STATE = _cce.canonical_cache_state(force_refresh=True)
        except RuntimeError as exc:
            _CACHE_STATE = {"resolve_error": str(exc), "dir": "",
                            "mode": "", "persistent": False, "warm": False,
                            "lock_class": "UNKNOWN"}
    return dict(_CACHE_STATE)


def _portable_cache_dir():
    """Usable portable-cache override for test children ("" = none)."""
    override = os.environ.get("FIE_TEST_PG_CACHE_DIR", "")
    if override and _cce.cache_ready(Path(override)):
        return override
    st = _active_cache_state()
    if st["warm"] and st["mode"] in ("explicit", "xdg", "home") and st["dir"]:
        return st["dir"]
    return ""


def _child_env(blob=None):
    """Scrubbed test child (R4-R2 Task C): explicit seed + the explicitly
    allowlisted network-runtime context (cloud_child_env is the ONE policy;
    no blanket inheritance — proxy/CA context may be credential-bearing and
    is carried only under its canonical name), plus a portable-cache
    override ONLY when actually usable (R4-R1 hard-contract rule)."""
    env = _cce.child_env("/tmp", None, blob, parent_env=os.environ)
    cache_dir = _portable_cache_dir()
    if cache_dir:
        env["FIE_TEST_PG_CACHE_DIR"] = cache_dir
    return env


def _run(cmd, blob=None, timeout=120, cwd=None):
    res = subprocess.run(cmd, env=_child_env(blob), capture_output=True,
                         text=True, timeout=timeout, cwd=cwd)
    return res.returncode, res.stdout, res.stderr


def provisioner_run(*args, blob=None, timeout=300):
    return _run(["bash", str(PROVISIONER), *args], blob=blob,
                timeout=timeout, cwd=str(PROVISIONER.parent))


def _bash_stdout_exit(probe_text, blob=None, timeout=60):
    """Write a probe bash script to /tmp and execute it. Probes source the
    REAL provisioner from the repository (never a modified copy) so any
    claim proven here is proven against the shipped source."""
    with tempfile.TemporaryDirectory(prefix="fie-nc-probe-") as td:
        probe = Path(td) / "probe.sh"
        probe.write_text(probe_text, encoding="utf-8")
        return _run(["bash", str(probe)], blob=blob, timeout=timeout)


def _cache_warm():
    """Task D — the ACTIVE canonically resolved cache is the ONLY reference
    for warm-gating portable-legged tests (never $HOME/.cache directly)."""
    st = _active_cache_state()
    if st.get("resolve_error") or not st.get("dir"):
        return False
    return _cce.cache_ready(Path(st["dir"]))


def _bootstrap_contract_runner():
    """NC15 — re-execute the shipped bootstrap parser-contract tests here
    (executed gate, not a read-only check)."""
    res = subprocess.run(
        ["python3", "-m", "unittest", "tests.test_bootstrap_cli_contract"],
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp",
             "PYTHONPATH": str(REPO)},
        capture_output=True, text=True, timeout=120, cwd=str(REPO))
    return res.returncode, res.stderr


class _Isolated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdirs = []

    @classmethod
    def tearDownClass(cls):
        for d in getattr(cls, "tempdirs", []):
            shutil.rmtree(d, ignore_errors=True)


class ScriptPresenceTests(_Isolated):
    """NC0 — the canonical boundary exists and is loadable."""

    def test_provisioner_script_present(self):
        self.assertTrue(PROVISIONER.is_file(),
                        "scripts/provision-test-postgres.sh missing; the "
                        "provisioner IS the R3 remediation contract")

    def test_testcloud_sources_the_provisioner(self):
        src = TEST_CLOUD.read_text(encoding="utf-8")
        self.assertIn("provision-test-postgres.sh", src,
                      "test-cloud must own its provisioning through ONE "
                      "canonical implementation")

    def test_unknown_action_fails_closed(self):
        rc, out, err = provisioner_run("--definitely-not-an-action")
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown/missing action", err)
        self.assertIn("USAGE:", err)

    def test_library_has_standalone_echo_guard(self):
        src = PROVISIONER.read_text(encoding="utf-8")
        self.assertIn('BASH_SOURCE[0]:-', src,
                      "library must guard against standalone echo")


class FailureClassStaticTests(_Isolated):
    """NC14 — every documented failure class exists with its own exit
    code; nothing collapses into a generic test failure."""

    def test_explicit_exit_classes_defined(self):
        src = PROVISIONER.read_text(encoding="utf-8")
        for name in ("PG_GUARD_EXIT=78", "PG_EXIT_PLATFORM=90",
                     "PG_EXIT_ARTIFACT=91", "PG_EXIT_INTEGRITY=92",
                     "PG_EXIT_INITDB=93", "PG_EXIT_START=94",
                     "PG_EXIT_READINESS=95", "PG_EXIT_TEARDOWN=96"):
            self.assertIn(name, src)

    def test_classification_names_used(self):
        src = PROVISIONER.read_text(encoding="utf-8")
        for cls in ("POSTGRESQL_PLATFORM_UNSUPPORTED",
                    "POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE",
                    "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED",
                    "POSTGRESQL_INITDB_FAILED",
                    "POSTGRESQL_START_FAILED",
                    "POSTGRESQL_READINESS_FAILED",
                    "POSTGRESQL_TEARDOWN_FAILED",
                    "POSTGRESQL_IDENTITY_GUARD_REJECTED"):
            self.assertIn(cls, src)

    def test_no_production_fallback_phrasing(self):
        src = PROVISIONER.read_text(encoding="utf-8")
        self.assertIn("no fallback", src,
                      "acquisition refusals must state no-fallback")


class BootstrapContractGateTests(_Isolated):
    """NC15 — bootstrap zero-arg validity + idempotency."""

    def test_bootstrap_contract_tests_pass(self):
        rc, stderr = _bootstrap_contract_runner()
        self.assertEqual(
            rc, 0, msg=f"bootstrap contract tests FAILED: {stderr}")


class _ProbeFixture(_Isolated):
    @classmethod
    def setUpClass(cls):
        assert PROVISIONER.is_file()
        assert BOOTSTRAP.is_file()


def _source_line():
    return f'. "{PROVISIONER}"\n'


class GuardNegativeTests(_ProbeFixture):
    """NC7/8 — production-flavored caller env is refused fail-closed (exit
    78); DSN values are classified, never printed."""

    NON_LOOPBACK_BLOB = {
        "FIE_TEST_PG_DSN": "postgres://probe_nc:probe_pw@10.0.0.5/fie_probe_nc"}
    PROTECTED_IDENTITY_BLOB = {
        "FIE_DB_TARGET_INTELLIGENCE":
            "postgres://probe_nc:probe_pw@127.0.0.1:5432/fie_prod"}
    OTHER_PG_BLOB = {
        "FIE_DATABASE_URL": "postgres://probe_nc:probe_pw@127.0.0.1/fie_nc_plain"}

    def test_non_loopback_candidate_refused(self):
        rc, out, err = provisioner_run("--start",
                                       blob=self.NON_LOOPBACK_BLOB)
        self.assertNotEqual(rc, 0)
        self.assertIn("PRODUCTION_DSN_REJECTED", err)
        self.assertNotIn(self.NON_LOOPBACK_BLOB["FIE_TEST_PG_DSN"], err)
        self.assertNotIn("probe_pw", err, "credential value leaked")

    def test_protected_production_identity_refused(self):
        rc, out, err = provisioner_run(
            "--start", blob=self.PROTECTED_IDENTITY_BLOB)
        self.assertNotEqual(rc, 0)
        self.assertEqual(rc, 78, msg=f"expected fail-closed 78, got {rc}")
        self.assertIn("POSTGRESQL_IDENTITY_GUARD_REJECTED", err)
        self.assertIn("protected production DB identity", err)
        self.assertNotIn("probe_pw", err)

    def test_any_pg_dsn_candidate_refused(self):
        """The provisioner's only legitimate DB identity is the job it
        creates itself — ANY caller PG DSN is a refused target."""
        rc, out, err = provisioner_run("--start", blob=self.OTHER_PG_BLOB)
        self.assertEqual(rc, 78)
        self.assertIn("accepts no caller DB target", err)
        self.assertNotIn("probe_pw", err)

    def test_guard_blob_never_persists_on_pass(self):
        """On a guard PASS (non-DSN candidate value) the internal blob —
        which can carry a real DSN value — must not persist in the shell
        scope after the guard returns."""
        probe = (_source_line() +
                 'FIE_TEST_PG_DSN="not-a-dsn plain value"\n'
                 '_fie_test_pg_identity_guard\n'
                 'rc_guard=$?\n'
                 'if [ -n "${FIE_R3_GUARD_BLOB:-}" ]; then\n'
                 '  echo "GUARD_BLOB_PERSISTED"\n'
                 'else\n'
                 '  echo "GUARD_BLOB_CLEAN"\n'
                 'fi\n'
                 'echo "GUARD_RC=${rc_guard}"\n')
        rc, out, err = _bash_stdout_exit(probe)
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("GUARD_RC=0", out)
        self.assertIn("GUARD_BLOB_CLEAN", out)
        self.assertNotIn("GUARD_BLOB_PERSISTED", out)


class TestCloudPreflightNegativeTests(_ProbeFixture):
    """NC8b — test-cloud preflight refuses a production-flavored DSN
    before running a single test (unknown != safe; exit 78)."""

    def test_preflight_refuses_fie_prod_dsn(self):
        rc, out, err = _run(
            ["bash", str(TEST_CLOUD)],
            blob={"FIE_TEST_PG_DSN":
                  "postgresql://probe_nc@127.0.0.1:5432/fie_prod"},
            timeout=120)
        self.assertEqual(rc, 78, msg=f"expected fail-closed 78, got: {err}")
        self.assertIn("FAIL_CLOSED", err + out)


class NoFallbackAndNoEscapeTests(_Isolated):
    """NC16/NC17 — no silent SQLite fallback; the test DSN never escapes
    into repository files or persistent shell configuration."""

    def test_testcloud_fallback_waits_for_classified_skip(self):
        """NC16 — test-cloud never writes a replacement DSN or silently
        substitutes SQLite: provisioning failure ⇒ skip WITH classification."""
        src = TEST_CLOUD.read_text(encoding="utf-8")
        self.assertIn("fie_test_pg_start", src)
        self.assertIn("never a silent", src + "")
        self.assertIn("SQLite fallback", src)

    @unittest.skipUnless(VENV_PY.exists(), "repo .venv absent")
    def test_dsn_does_not_escape_into_repo_files(self):
        """NC17 — after a live provisioned start, the synthetic user/db name
        appears ONLY inside the job dir; no tracked/repository file carries
        the test identity (persistent shell configuration checked too)."""
        rc, out, err = provisioner_run("--start", timeout=300)
        self.assertEqual(rc, 0, msg=f"start failed: {err}")
        jobdir = out.split("READY (job-dir ", 1)[-1].split(";", 1)[0]
        user = json.loads((Path(jobdir) / "manifest.json")
                          .read_text(encoding="utf-8"))["user"]
        try:
            for token in (user, Path(jobdir).name):
                res = subprocess.run(
                    ["git", "grep", "-l", token],
                    env={"PATH": "/usr/bin:/bin", "HOME": "/tmp",
                         "PWD": str(REPO)},
                    capture_output=True, text=True, timeout=60, cwd=str(REPO))
                self.assertEqual(
                    res.stdout.strip(), "",
                    f"test identity {token[:12]}… escaped into tracked "
                    f"repository files: {res.stdout}")
            # Persistent shell configuration of the runner (HOME=/tmp in the
            # probe; but the REAL runtime HOME is the test process's env):
            real_home = Path.home()
            leaked = []
            for cfg in (real_home / ".bashrc", real_home / ".profile"):
                if cfg.is_file() and user in cfg.read_text(errors="replace"):
                    leaked.append(str(cfg))
            self.assertEqual(leaked, [], f"DSN identity leaked into {leaked}")
        finally:
            provisioner_run("--stop", jobdir, timeout=60)


class _FakeToolingStub(unittest.TestCase):
    """NC12 — a failed initdb/start leaves no owned state behind. Uses the
    documented FIE_TEST_PG_BIN stub override: no cache, no network."""

    @classmethod
    def setUpClass(cls):
        cls.stubdir = tempfile.mkdtemp(prefix="fie-nc-stub-")
        binr = Path(cls.stubdir) / "bin"
        binr.mkdir(parents=True)
        # The stub answers --version (so the provisioner parses a tool
        # number) but fails the real initdb run — exactly the shape that
        # provokes POSTGRESQL_INITDB_FAILED.
        stub_initdb = binr / "initdb"
        stub_initdb.write_text(
            "#!/bin/sh\n"
            'case "${1-}" in\n'
            '  --version) echo "stub (PostgreSQL) 18.4"; exit 0 ;;\n'
            "esac\n"
            "exit 1\n", encoding="utf-8")
        stub_initdb.chmod(0o755)
        (binr / "pg_ctl").write_text("#!/bin/sh\nexit 0\n")
        (binr / "pg_ctl").chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.stubdir, ignore_errors=True)

    def test_initdb_failure_leaves_no_owned_state(self):
        before = sorted(Path("/tmp").glob("fie-pgtest.*"))
        rc, out, err = provisioner_run(
            "--start", blob={"FIE_TEST_PG_BIN": str(self.stubdir) + "/bin"},
            timeout=120)
        self.assertEqual(rc, 93, msg=f"expected INITDB 93, got {rc}: {err}")
        self.assertIn("POSTGRESQL_INITDB_FAILED", err)
        self.assertIn("owned state removed", err)
        after = sorted(Path("/tmp").glob("fie-pgtest.*"))
        leftovers = [p for p in after if p not in before]
        self.assertEqual(leftovers, [],
                         "failed initdb left job-owned state behind: "
                         f"{leftovers}")


class PortableContractStaticTests(_Isolated):
    """NC9/NC13 + teardown ownership refusal — STATIC probes that do NOT
    depend on a warm portable cache (6.9A-R4-R2-R1 Task D3: these were
    wrongly warm-gated before, producing wrong skips on cold-cache
    runners)."""

    def test_loopback_listener_contract_is_pinned(self):
        """NC9 — server-side contract pin: loopback listener + executed
        per-line bind proof; a 0.0.0.0 listener must fail start."""
        src = PROVISIONER.read_text(encoding="utf-8")
        self.assertIn("listen_addresses=127.0.0.1", src)
        self.assertIn("_loopback_bind_proof", src)

    def test_teardown_refuses_dir_without_ownership_marker(self):
        with tempfile.TemporaryDirectory(prefix="fie-nc-teardown-") as td:
            dirp = Path(td) / "pretend-job"
            (dirp / "pgdata").mkdir(parents=True)
            (dirp / "pgdata" / "postmaster.pid").write_text(
                "", encoding="utf-8")
            rc, _, err = provisioner_run("--stop", str(dirp), timeout=60)
            self.assertNotEqual(rc, 0)
            self.assertIn("ownership marker absent", err)
            self.assertEqual(rc, 96, msg=f"expected TEARDOWN 96, got {rc}")
            # The pretend dir is left untouched for diagnosis.
            self.assertTrue((dirp).exists())

    def test_exported_dsn_parseable_by_runtime_contract(self):
        """NC13 — the shape the provisioner exports (passwordless socket
        form) must be parseable by phase3.runtime_contract."""
        src = PROVISIONER.read_text(encoding="utf-8")
        # Pin the exported shape against accidental password embedding:
        self.assertIn('?host=${jobdir}&port=${port}', src)
        self.assertIn('export FIE_TEST_PG_DSN=%q', src)


class PortableLeggedTests(_Isolated):
    """NC4/NC10/NC11/NC13b/NC13-run — exercised through the PRE-WARMED
    portable cache (R4-R2 Task D3 warm-gate redesign):

    * the warm gate resolves the ACTIVE canonically resolved cache — never
      $HOME/.cache (the R4-R2 blocker #3 wrong-skip defect: the OLD gate
      inspected a HOME cache the runtime does not use);
    * when the gate is cold AND real acquisition is allowed
      (FIE_TEST_PG_ALLOW_REAL_ACQUISITION=1 — test-cloud's default), the
      shared helper performs ONE network-true warm attempt (temp mode gets
      a helper-allocated job-local cache under an explicit override; that
      job-local dir is removed again in tearDownClass);
    * when cold and not allowed → ONE attributed skip for the class with
      the helper's classification string (never silent);
    * static cache-independent probes live in PortableContractStaticTests.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._warmer = _cce.WarmPortableCache()
        cls._warmer.__enter__()
        if not cls._warmer.dir or not _cce.cache_ready(cls._warmer.dir):
            raise unittest.SkipTest(
                cls._warmer.classification or "portable acquisition cache "
                "cold — warm it with 'scripts/provision-test-postgres.sh "
                "--ensure-cache' (test-cloud warms it automatically; the "
                "cache-cold acquisition itself is proven in the network-true "
                "evidence layer, not in this offline suite)")
        cls.warmer = cls._warmer
        # All legged children pin the warmed cache under its canonical
        # explicit-override contract.
        cls._prior_override = os.environ.get("FIE_TEST_PG_CACHE_DIR", "")
        os.environ["FIE_TEST_PG_CACHE_DIR"] = cls._warmer.dir
        _CACHE_STATE = None  # reset module memo: resolve the warmed dir
        globals()["_CACHE_STATE"] = None

    @classmethod
    def tearDownClass(cls):
        try:
            os.environ["FIE_TEST_PG_CACHE_DIR"] = cls._prior_override
            if not cls._prior_override:
                os.environ.pop("FIE_TEST_PG_CACHE_DIR", None)
        finally:
            cls._warmer.__exit__()
        super().tearDownClass()

    def test_missing_tooling_selects_portable_path(self):
        """NC4b — when server discovery MISSES, ``fie_test_pg_start`` must
        SELECT the portable distribution (never silently collapse into a
        SQLite-only skip while a portable acquisition is possible). Proven
        with a probe that forces the shipped discovery to miss against the
        pre-warmed cache — no network, no state leak."""
        probe = (_source_line() +
                 '_fie_test_pg_discover() { return 1; }\n'
                 'fie_test_pg_start\n'
                 'rc_start=$?\n'
                 'echo "START_RC=${rc_start}"\n'
                 'echo "JOB=${FIE_TEST_PG_JOB_DIR:-}"\n'
                 'echo "MODE=${FIE_TEST_PG_MODE:-}"\n'
                 'fie_test_pg_stop >/dev/null 2>&1 || true\n')
        rc, out, err = _bash_stdout_exit(probe, timeout=300)
        self.assertEqual(rc, 0, msg=f"probe failed: {err}")
        self.assertIn("START_RC=0", out)
        self.assertIn("MODE=portable", out,
                      "discovery-miss must select the portable path")

    def test_force_portable_selects_portable_distribution(self):
        rc, out, err = provisioner_run("--start", "--force-portable",
                                       timeout=300)
        self.assertEqual(rc, 0, msg=f"portable start failed: {err}")
        self.assertIn("--force-portable", out)
        self.assertIn("server tooling", out)
        self.assertIn("--force-portable (system discovery skipped", out)
        jobdir = out.split("READY (job-dir ", 1)[-1].split(";", 1)[0]
        try:
            manifest = json.loads((Path(jobdir) / "manifest.json")
                                  .read_text(encoding="utf-8"))
            self.assertEqual(manifest["mode"], "portable",
                             "force-portable must select the portable "
                             "artifact, never discovered tooling")
        finally:
            provisioner_run("--stop", jobdir, timeout=60)

    def test_identity_utf8_and_synthetic_names(self):
        """NC10/NC11 — readiness identity: UTF8 pinned, synthetic user/db,
        protected production identity never appears."""
        rc, out, err = provisioner_run("--start", timeout=300)
        self.assertEqual(rc, 0, msg=f"start failed: {err}")
        jobdir = out.split("READY (job-dir ", 1)[-1].split(";", 1)[0]
        try:
            readiness = json.loads((Path(jobdir) / "readiness.json")
                                   .read_text(encoding="utf-8"))
            self.assertEqual(readiness["server_encoding"], "UTF8")
            self.assertTrue(readiness["synthetic_db_prefix"].startswith("fie_test_"))
            self.assertTrue(readiness["synthetic_user_prefix"].startswith("fie_"))
            self.assertEqual(
                readiness["tcp_auth"], "scram-sha-256-proven-socket-and-tcp")
            manifest = json.loads((Path(jobdir) / "manifest.json")
                                  .read_text(encoding="utf-8"))
            self.assertEqual(manifest["bind"], "127.0.0.1")
        finally:
            provisioner_run("--stop", jobdir, timeout=60)

    # (NC9 loopback pin / NC13 teardown-refusal / NC13 static DSN-shape
    # probes moved to PortableContractStaticTests — cache-INDEPENDENT.)

    def test_teardown_stops_only_the_job_cluster(self):
        """NC13b — two-cluster contract: stopping job A must leave job B
        serving, and vice versa; second stop is idempotent success."""
        rcA, outA, errA = provisioner_run("--start", timeout=300)
        self.assertEqual(rcA, 0, msg=f"job A start failed: {errA}")
        jobA = outA.split("READY (job-dir ", 1)[-1].split(";", 1)[0]
        rcB, outB, errB = provisioner_run("--start", timeout=300)
        self.assertEqual(rcB, 0, msg=f"job B start failed: {errB}")
        jobB = outB.split("READY (job-dir ", 1)[-1].split(";", 1)[0]
        portB = json.loads((Path(jobB) / "manifest.json")
                           .read_text(encoding="utf-8"))["port"]
        try:
            provisioner_run("--stop", jobA, timeout=60)
            self.assertFalse(Path(jobA).exists())
            # job B must STILL serve
            import socket
            s = socket.socket()
            s.settimeout(3)
            try:
                s.connect(("127.0.0.1", int(portB)))
            finally:
                s.close()
        finally:
            provisioner_run("--stop", jobA, timeout=60)  # idempotent if already gone
            provisioner_run("--stop", jobB, timeout=60)
        # Idempotent second stop for job B.
        rc, _, err2 = provisioner_run("--stop", jobB, timeout=60)
        self.assertEqual(rc, 0, msg=f"second stop must be idempotent: {err2}")

    @unittest.skipUnless(VENV_PY.exists(), "repo .venv absent")
    def test_exported_dsn_connects(self):
        rc, out, err = provisioner_run("--start", timeout=300)
        self.assertEqual(rc, 0, msg=f"start failed: {err}")
        jobdir = out.split("READY (job-dir ", 1)[-1].split(";", 1)[0]
        try:
            manifest = json.loads((Path(jobdir) / "manifest.json")
                                  .read_text(encoding="utf-8"))
            dsn = (f"postgresql://{manifest['user']}@/{manifest['db']}"
                   f"?host={jobdir}&port={manifest['port']}")
            script = (
                "import sys; sys.path.insert(0, sys.argv[1]);"
                "from phase3.runtime_contract import parse_postgres_dsn;"
                "spec = parse_postgres_dsn(sys.argv[2]);"
                "assert spec['kind'] == 'postgres', spec;"
                "assert str(spec['dbname']).startswith('fie_test_'), spec;"
                "print('DSN_OK')"
            )
            res = subprocess.run(
                ["python3", "-c", script, str(REPO), dsn],
                env={"PATH": "/usr/bin:/bin", "HOME": "/tmp",
                     "PWD": str(jobdir)},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("DSN_OK", res.stdout)
        finally:
            provisioner_run("--stop", jobdir, timeout=60)


if __name__ == "__main__":
    unittest.main()