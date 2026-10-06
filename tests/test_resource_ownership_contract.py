#!/usr/bin/env python3
"""Resource ownership + teardown contract — 6.9A-R4-R3-R1 Task H (WO Task H).

Covers the ownership half of the Task H list (items 8-22); items 1-7 live
in tests/test_rehearsal_wrapper_guard.py (hermetic wrapper-guard fixture)
and tests/test_rehearsal_guard_zero_write_invariant.py (synthetic
Production-shaped zero-write invariant); SuiteCompletenessTests asserts
those modules still define their mandatory checks so the focused suite
stays whole.

Canonical implementation under test: scripts/resource_ownership.py —
registry formats, four-way classification (JOB_OWNED / FOREIGN / ADOPTED /
UNKNOWN), no-bare-PID attribution, fail-closed preservation.

The registry is written at CREATION time by the canonical provisioner and
read at teardown; a live end-to-end ephemeral PostgreSQL instance proves
start -> registry -> live attribution -> stop -> idempotent stop against
a real server, all inside job-owned directories.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import resource_ownership as ro  # noqa: E402

PROVISIONER = REPO / "scripts" / "provision-test-postgres.sh"

CLEANUP_PROBE = ('. ' + repr(str(PROVISIONER)) + '\n'
                 'fie_test_pg_cache_cleanup >cleanup.log 2>&1; '
                 'echo "CLEANUP_RC=$?"; cat cleanup.log\n')
RESOLVE_PROBE = ('. ' + repr(str(PROVISIONER)) + '\n'
                 '_fie_test_pg_cache_resolve; echo "RESOLVE_RC=$?"\n'
                 'echo "MODE=${PG_CACHE_MODE}"\n'
                 'echo "PERSISTENT=${PG_CACHE_PERSISTENT}"\n'
                 'fie_test_pg_cache_cleanup >/dev/null 2>&1; '
                 'echo "CLEANUP_RC=$?"\n')
STOP_STATEMENTS = (
    'fie_test_pg_stop "$JD" >/dev/null 2>&1; echo "STOP_RC=$?"\n'
    'fie_test_pg_stop "$JD" >/dev/null 2>&1; echo "IDEMP_RC=$?"\n'
    'fie_test_pg_cache_cleanup >cleanup.log 2>&1; echo "CLEANUP_RC=$?"\n')


def _snap(pid, state="S", ppid=1, cmdline="", start_ticks=None, uid=0):
    return ro.ProcSnapshot(pid, state, ppid, cmdline, start_ticks, uid)


def _proc_entry(pid, start_ticks, data_dir):
    return {"kind": "postgres_server", "removable": True, "path": data_dir,
            "process": {"pid": pid, "start_ticks": start_ticks,
                        "command_signature": f"postgres -D {data_dir}",
                        "data_dir": data_dir}}


def _minimal_env():
    """Cloud-shaped shape reference (PATH-only defaults)."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": ""}


def _probe(body: str, extra_env: dict | None = None, timeout: int = 90):
    with tempfile.TemporaryDirectory(prefix="fie-ow-probe-") as td:
        td = Path(td)
        probe = td / "probe.sh"
        probe.write_text(body, encoding="utf-8")
        # per-probe HOME: probes share nothing on the operator host
        home = td / "home"
        home.mkdir()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": str(home)}
        if extra_env:
            env.update(extra_env)
        res = subprocess.run(["bash", str(probe)], env=env,
                             capture_output=True, text=True,
                             timeout=timeout, cwd=str(REPO))
        return res.returncode, res.stdout, res.stderr


def _marked_cache(dirpath: Path) -> None:
    """Write the canonical temp-cache ownership marker."""
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "fie_cache.owner").write_text(
        "format=fie-6-9a-cache-v1\ncreated_utc=now\nephemeral=yes\n")


def _shl(v: str) -> str:
    import shlex
    return shlex.quote(v)


class SuiteCompletenessTests(unittest.TestCase):
    """Items 1-7 are defined in the two hermeticity modules; assert the
    mandatory checks stay present there."""

    def test_hermetic_wrapper_guard_suite_present(self):
        text = (REPO / "tests" / "test_rehearsal_wrapper_guard.py"
                ).read_text()
        # the hermetic signature-rejection suite (no unconditional skips)
        for needle in ("test_g6_host_wrapper_env_file_signature_rejected",
                       "FIE_WRAPPER_ENV", "REFUSAL_RC"):
            self.assertIn(needle, text)

    def test_zero_write_invariant_module_present(self):
        text = (REPO / "tests" / "test_rehearsal_guard_zero_write_invariant.py"
                ).read_text()
        for needle in ("_refusal_leg", "hermetic_production_fixture",
                       "FIE_PRODUCTION_DB_CONTRACT", "_pg_fingerprints"):
            self.assertIn(needle, text)


class RegistryContractTests(unittest.TestCase):
    """(8) creation-time registry: format, mode 0600, round-trip, UNKNOWN."""

    def _registry(self, td: Path) -> dict:
        reg = ro.new_registry("job-8")
        entry = ro.record_resource(
            reg, "postgres_server", path=str(td),
            marker="pgdata/fie_ephemeral_pg.owner",
            marker_format=ro.PG_MARKER_FORMAT,
            process={"pid": 4321, "start_ticks": 1000,
                     "command_signature": f"postgres -D {td}/pgdata",
                     "data_dir": f"{td}/pgdata"})
        self.assertTrue(entry["removable"])
        ro.write_registry(reg, td / "ownership_registry.json")
        return reg

    def test_registry_shape_and_mode(self):
        with tempfile.TemporaryDirectory(prefix="fie-reg-") as td:
            td = Path(td)
            reg = self._registry(td)
            self.assertEqual(reg["format"], ro.REGISTRY_FORMAT)
            self.assertEqual(reg["job_id"], "job-8")
            self.assertEqual([r["kind"] for r in reg["resources"]],
                             ["postgres_server"])
            mode = (td / "ownership_registry.json").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_registry_round_trip_and_malformed_is_unknown(self):
        with tempfile.TemporaryDirectory(prefix="fie-reg-") as td:
            td = Path(td)
            reg = self._registry(td)
            self.assertEqual(ro.load_registry(td / "ownership_registry.json"),
                             reg)
            # foreign format / truncated payload => UNKNOWN (None); never a
            # guessed object teardown may claim
            (td / "foreign.json").write_text('{"format": "other-v9"}')
            self.assertIsNone(ro.load_registry(td / "foreign.json"))
            (td / "broken.json").write_text("{not json")
            self.assertIsNone(ro.load_registry(td / "broken.json"))

    def test_process_residue_inventory_and_counts(self):
        reg = {"format": ro.REGISTRY_FORMAT, "job_id": "j",
               "created_utc": "x", "resources": [_proc_entry(11, 100, "/d1")]}
        snaps = {11: _snap(11, "S", 1, "postgres -D /d1 -p 54001", 100),
                 12: _snap(12, "Z", 1, ""),
                 13: _snap(13, "Z", 999, "")}
        inv = ro.process_residue_inventory(snaps, reg, observer_pid=77)
        self.assertEqual(inv[11], ro.JOB_OWNED_LIVE)
        self.assertEqual(inv[12], ro.ADOPTED_ZOMBIE)
        self.assertEqual(inv[13], ro.ZOMBIE_FOREIGN_PARENT)
        counts = ro.residue_counts(inv)
        for cls, want in ((ro.JOB_OWNED_LIVE, 1), (ro.ADOPTED_ZOMBIE, 1),
                          (ro.ZOMBIE_FOREIGN_PARENT, 1),
                          (ro.UNKNOWN, 0), (ro.FOREIGN, 0)):
            self.assertEqual(counts[cls], want, cls)

    def test_teardown_evidence_shape(self):
        reg = {"format": ro.REGISTRY_FORMAT, "job_id": "j",
               "created_utc": "x", "resources": [_proc_entry(11, 100, "/d1")]}
        ev = ro.build_teardown_evidence(
            reg, "/job", removed_dirs=["/job"], observations=[{"p": 9}],
            reaped=[10], extra={"STOP_RC": 0})
        self.assertEqual(ev["format"], ro.EVIDENCE_FORMAT)
        self.assertEqual(ev["job_id"], "j")
        self.assertEqual(ev["removed"], ["/job"])
        self.assertEqual(ev["reaped"], [10])
        self.assertEqual(ev["STOP_RC"], 0)


class ProcessAttributionTests(unittest.TestCase):
    """(9)(10)(17)(19)(20) identity rules stronger than a bare PID."""

    def test_live_job_owned_attribution(self):
        entry = _proc_entry(11, 100, "/d1")
        self.assertEqual(ro.classify_process(
            _snap(11, "S", 1, "postgres -D /d1 -p 5432 "
                             "-c listen_addresses=127.0.0.1", 100),
            entry), ro.JOB_OWNED_LIVE)

    def test_pid_reuse_is_unknown_never_job_owned(self):
        entry = _proc_entry(11, 100, "/d1")
        # same PID, different start lineage => UNKNOWN (must never claim)
        self.assertEqual(ro.classify_process(
            _snap(11, "S", 1, "postgres -D /d1", 999), entry), ro.UNKNOWN)
        # same PID, wrong command signature => UNKNOWN
        self.assertEqual(ro.classify_process(
            _snap(11, "S", 1, "bash other.sh", 100), entry), ro.UNKNOWN)
        # same PID, wrong lineage AND foreign cmdline => UNKNOWN doubly
        self.assertEqual(ro.classify_process(
            _snap(11, "S", 1, "bash other.sh", 999), entry), ro.UNKNOWN)

    def test_foreign_process_without_registry_entry_is_unknown(self):
        for state in ("S", "R", "D"):
            cls = ro.classify_process(
                _snap(31, state, 5, f"postgres -D /other/data", 70), None)
            self.assertEqual(cls, ro.UNKNOWN)
            self.assertNotIn(cls, (ro.JOB_OWNED_LIVE, ro.JOB_OWNED_DEAD,
                                   ro.JOB_OWNED_ZOMBIE_REAPABLE))

    def test_dead_process_classes(self):
        entry = _proc_entry(11, 100, "/d1")
        self.assertEqual(ro.classify_process(None, entry), ro.JOB_OWNED_DEAD)
        self.assertEqual(ro.classify_process(None, None), ro.UNKNOWN)

    def test_zombie_classification(self):
        entry = _proc_entry(11, 100, "/d1")
        self.assertEqual(ro.classify_process(
            _snap(11, "Z", 77, ""), entry, 77), ro.JOB_OWNED_ZOMBIE_REAPABLE)
        self.assertEqual(ro.classify_process(
            _snap(12, "Z", 1, ""), entry, 77), ro.ADOPTED_ZOMBIE)
        self.assertEqual(ro.classify_process(
            _snap(13, "Z", 999, ""), entry, 77), ro.ZOMBIE_FOREIGN_PARENT)

    def test_reap_only_observer_children(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        time.sleep(0.3)
        self.assertEqual(ro.read_proc(pid).state, "Z")
        deadline = time.monotonic() + 5
        while pid not in ro.reap_job_children(os.getpid()):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)


class TempDirClassificationTests(unittest.TestCase):
    """(11)(12) marker-proven temp dirs; classification never mutates."""

    def test_job_owned_vs_foreign_vs_unknown(self):
        with tempfile.TemporaryDirectory(prefix="fie-dirs-") as td:
            td = Path(td)
            owned, foreign, unknown = td / "own", td / "foreign", td / "unk"
            _marked_cache(owned)
            _marked_cache(foreign)
            unknown.mkdir()
            self.assertEqual(ro.classify_temp_dir(owned, {str(owned)}),
                             ro.DIR_JOB_OWNED)
            self.assertEqual(ro.classify_temp_dir(foreign, {str(owned)}),
                             ro.DIR_FOREIGN)
            self.assertEqual(ro.classify_temp_dir(unknown, frozenset()),
                             ro.DIR_UNKNOWN)

    def test_classification_is_non_mutating(self):
        with tempfile.TemporaryDirectory(prefix="fie-dirs-") as td:
            d = Path(str(td)) / "d"
            _marked_cache(d)
            before = sorted(os.listdir(d))
            before_owner = (d / "fie_cache.owner").read_text()
            ro.classify_temp_dir(d, frozenset())
            self.assertEqual(before, sorted(os.listdir(d)))
            self.assertEqual(before_owner, (d / "fie_cache.owner").read_text())


class LockResidueContractTests(unittest.TestCase):
    """(13)(14)(15) classify/removal cross-checks against the provisioner
    (complete semantics live in tests/test_pg_cache_lock_ownership.py)."""

    def _explicit_cache_env(self) -> dict:
        cachedir = Path(tempfile.mkdtemp(prefix="fie-lk-"))
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        _marked_cache(cachedir)
        return {"FIE_TEST_PG_CACHE_DIR": str(cachedir),
                "PG_CACHE_DIR": str(cachedir), "PG_CACHE_MODE": "explicit",
                "PG_CACHE_PERSISTENT": "yes"}

    def test_stale_job_owned_lock_removed_by_cleanup(self):
        env = self._explicit_cache_env()
        lock = Path(env["FIE_TEST_PG_CACHE_DIR"], ".acquire.lock")
        lock.write_text(
            "format=fie-6-9a-acquire-lock-v1\ninvocation=dead-job\n")
        rc, out, err = _probe(CLEANUP_PROBE, extra_env=env)
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("CLEANUP_RC=0", out)
        self.assertFalse(lock.exists(),
                         "stale job-owned lock removed (flock-free proved)")

    def _hold_flock(self, lock: Path):
        hold = subprocess.Popen(
            ["flock", str(lock), "-c", "sleep 8"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(hold.wait)
        self.addCleanup(hold.terminate)
        deadline = time.monotonic() + 5
        # wait until the holder's flock is visible to a fresh fuser probe
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["flock", "--nonblock", str(lock), "-c", "true"],
                capture_output=True, timeout=10)
            if probe.returncode != 0:
                return  # lock busy — the holder proved live
            time.sleep(0.1)
        self.fail("flock holder not established")

    def test_active_lock_preserved(self):
        env = self._explicit_cache_env()
        lock = Path(env["FIE_TEST_PG_CACHE_DIR"], ".acquire.lock")
        self._hold_flock(lock)
        rc, out, err = _probe(CLEANUP_PROBE, extra_env=env)
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("CLEANUP_RC=0", out)
        self.assertTrue(lock.exists(), "ACTIVE-classified lock preserved")

    def test_unknown_lock_fails_closed(self):
        env = self._explicit_cache_env()
        lock = Path(env["FIE_TEST_PG_CACHE_DIR"], ".acquire.lock")
        lock.write_bytes(b"")  # legacy 0-byte lock form
        rc, out, err = _probe(CLEANUP_PROBE, extra_env=env)
        self.assertEqual(rc, 0, msg=err)  # probe continues past cleanup
        self.assertIn("CLEANUP_RC=96", out)
        self.assertTrue(lock.exists(), "UNKNOWN lock preserved, fail-closed")
        self.assertTrue(Path(env["FIE_TEST_PG_CACHE_DIR"]).exists())


class EnvironmentRobustnessTests(unittest.TestCase):
    """(21)(22) scrubbed env runs clean; read-only HOME falls through to
    an isolated writable temp runtime (and its own temp cache is removed
    again by cleanup — zero residue)."""

    def test_cleanup_runs_clean_under_scrubbed_env(self):
        rc, out, err = _probe(CLEANUP_PROBE)
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("CLEANUP_RC=0", out)

    def test_readonly_home_resolves_to_writable_temp_runtime(self):
        with tempfile.TemporaryDirectory(prefix="fie-rohome-") as td:
            rohome = Path(str(td)) / "ro"
            rohome.mkdir()
            os.chmod(rohome, 0o555)
            self.addCleanup(
                lambda p=str(rohome): (os.chmod(p, 0o755), None)[1]
                if os.path.exists(p) else None)
            before = set(Path("/tmp").glob("fie-pgcache.*"))
            rc, out, err = _probe(RESOLVE_PROBE, extra_env={"HOME": str(rohome)})
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("RESOLVE_RC=0", out)
            self.assertIn("MODE=temp", out)
            self.assertIn("PERSISTENT=no", out)
            self.assertIn("CLEANUP_RC=0", out)
            after = {p for p in Path("/tmp").glob("fie-pgcache.*")
                     if p not in before}
            self.assertEqual(after, set(),
                             "cleanup removed its own temp cache dirs")

    def test_no_kill_by_pattern_anywhere_in_tooling(self):
        # (10) preservation is structural: owned-state teardown runs ONLY
        # the granted stop mechanism; no wildcard-kill escape hatches.
        for path in ("scripts/provision-test-postgres.sh",
                     "scripts/resource_ownership.py",
                     "scripts/rehearsal_db_guard.sh"):
            code = "\n".join(
                line.strip() for line in (REPO / path).read_text().splitlines()
                if line.strip() and not line.strip().startswith("#"))
            for banned in ("pkill", "killall"):
                self.assertNotIn(banned, code, f"{path} must not run {banned}")


class RealEphemeralPgLifecycleTests(unittest.TestCase):
    """(9)(16) real ephemeral PG against the canonical provisioner:
    creation-time registry, live attribution from /proc, stop, idempotent
    second stop, explicit-cache cleanup — inside job-owned directories."""

    @classmethod
    def setUpClass(cls):
        import tests.test_rehearsal_guard_zero_write_invariant as zw
        cls.zw = zw
        cls.fix: dict = {}
        cls.job_dir = None
        cls.fixture_dir = tempfile.mkdtemp(prefix="fie-ow-pg-")
        cls.cache_dir = os.path.join(cls.fixture_dir, "provcache")
        cls.hold = {k: os.environ.get(k, "")
                    for k in (*zw._PROV_GLOBALS, "FIE_TEST_PG_CACHE_DIR")}
        os.environ["FIE_TEST_PG_CACHE_DIR"] = cls.cache_dir
        try:
            zw._run_prov_script(
                cls.fix, "fie_test_pg_start || exit $?\n", "provision")
        except Exception:
            cls._restore_env()
            shutil.rmtree(cls.fixture_dir, ignore_errors=True)
            raise
        cls.fix_env = cls.fix["env"]
        cls.job_dir = cls.fix_env["FIE_TEST_PG_JOB_DIR"]
        for k in cls.zw._PROV_GLOBALS:
            if cls.fix_env.get(k):
                os.environ[k] = cls.fix_env[k]

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.job_dir:
                state = "\n".join(
                    (f"export {k}={_shl(cls.fix_env[k])}"
                     if cls.fix_env.get(k)
                     else f"unset {k} 2>/dev/null || true")
                    for k in cls.zw._PROV_GLOBALS)
                body = (". " + repr(str(PROVISIONER)) + "\n"
                        + state + "\n" +
                        f"export FIE_TEST_PG_CACHE_DIR={_shl(cls.cache_dir)}\n"
                        + f"JD={_shl(cls.job_dir)}\n" + STOP_STATEMENTS)
                rc, out, err = _probe(body, timeout=180)
                # classmethods run unbound — raise plain AssertionErrors
                def _fail(msg):
                    raise AssertionError(msg)
                if rc != 0:
                    _fail(f"teardown probe rc: {rc} err={err[-500:]}")
                for flag in ("STOP_RC=0",     # stop succeeded
                             "IDEMP_RC=0",    # (16) idempotent
                             "CLEANUP_RC=0"):  # explicit cache clean
                    if flag not in out:
                        _fail(f"{flag} missing; out={out[-500:]}")
                if os.path.isdir(cls.job_dir):
                    _fail("job-owned job dir removed after proof (16)")
        finally:
            cls._restore_env()
            shutil.rmtree(cls.fixture_dir, ignore_errors=True)

    @classmethod
    def _restore_env(cls):
        for k, v in cls.hold.items():
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def _server_entry(self) -> dict:
        self.assertTrue(self.job_dir, "provisioned")
        registry = ro.load_registry(
            Path(self.job_dir) / "ownership_registry.json")
        self.assertIsNotNone(registry)
        server = next(
            (r for r in registry["resources"]
             if r["kind"] == "postgres_server"), None)
        self.assertIsNotNone(server)
        return next((r for r in registry["resources"]
                     if r["kind"] == "postgres_server"))

    def test_creation_time_registry_records_all_job_state(self):
        job = Path(self.job_dir)
        registry = ro.load_registry(job / "ownership_registry.json")
        self.assertIsNotNone(registry)
        self.assertEqual(registry["format"], ro.REGISTRY_FORMAT)
        entry_kinds = [r["kind"] for r in registry["resources"]]
        for kind in ("job_dir", "pgdata_dir", "auth_password", "socket_dir",
                     "postgres_server"):
            self.assertIn(kind, entry_kinds)
        cache_entry = next((r for r in registry["resources"]
                            if r["kind"] == "cache_dir"), None)
        if cache_entry:
            # explicit override recorded WITH its actual path (evidence
            # quality); discovered system tooling records no cache entry
            self.assertEqual(cache_entry["path"], self.cache_dir)
        server = next(r for r in registry["resources"]
                      if r["kind"] == "postgres_server")["process"]
        self.assertGreater(int(server["pid"]), 0)
        self.assertIsNotNone(server["start_ticks"])
        self.assertIn(str(self.job_dir), server["command_signature"])
        self.assertEqual(server["data_dir"], f"{self.job_dir}/pgdata")

    def test_live_job_owned_process_attribution_from_proc(self):
        server = self._server_entry()["process"]
        snap = ro.read_proc(server["pid"])
        self.assertIsNotNone(snap, "postmaster alive during this suite")
        registry = ro.load_registry(
            Path(self.job_dir) / "ownership_registry.json")
        entry = ro.registry_process_entry(registry, server["pid"])
        self.assertEqual(ro.classify_process(snap, entry, os.getpid()),
                         ro.JOB_OWNED_LIVE)  # (9)
        # (20) no-PID-only: the same pid with a different recorded start
        # lineage must classify UNKNOWN even against a LIVE process
        forged = dict(entry)
        forged["process"] = dict(server, start_ticks=int(
            server["start_ticks"]) + 1)
        self.assertEqual(
            ro.classify_process(snap, forged, None), ro.UNKNOWN)


if __name__ == "__main__":
    unittest.main()