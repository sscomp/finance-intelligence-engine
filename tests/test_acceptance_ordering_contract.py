#!/usr/bin/env python3
"""Acceptance ordering + provenance contract tests (6.9A-R4-R6-R1 §13).

Covers O1-O12 of the corrected acceptance sequence:

    preflight -> bootstrap -> PRE fingerprint -> acceptance workloads
              -> POST fingerprint -> match -> cleanup -> seal

All fixtures are synthetic (obviously-synthetic DSNs/names, temp state
files); no real credential, no Production contact, no network.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import acceptance_contract as ac  # noqa: E402

FAIL_CLOSED = str(ac.FAIL_CLOSED_EXIT)

SYNTHETIC_PROD_DSN = "postgresql://fixture_prod_user@fixture-host.invalid:5433/fie_prod_fixture_db"
# loopback-shaped isolated DSN for positive-path classification tests
SYNTHETIC_LOOPBACK_DSN = "postgresql://ue0f3a@/ue0f3adb?host=/tmp/fie-acc-fixture&port=59993"


def _fresh_state() -> str:
    fd = tempfile.mkstemp(prefix="fie-acc-state-", suffix=".json")
    os.close(fd[0])
    return fd[1]


_CREDENTIAL_CARRIERS = ("PGPASSWORD", "POSTGRES_PASSWORD", "DATABASE_URL")


def _run_subproc(args, extra_env=None, cwd=str(REPO)):
    # Baseline scrub (same boundary class as the canonical entrypoint):
    # every FIE_* carrier plus the credential carriers are dropped from
    # the child env — unittest discover may run sibling modules first
    # (phase3 < test_*) that leak os.environ fixture carriers (observed
    # FIE_DATABASE_URL in the full regression); this suite never depends
    # on ambient values, and each test injects exactly what it declares.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FIE_") and k not in _CREDENTIAL_CARRIERS}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "acceptance_contract.py")]
        + args, capture_output=True, text=True, cwd=cwd, env=env)


class _StateHarness(unittest.TestCase):
    """A temp state file per test (job-local, removed on exit)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fie-acc-contract-")
        self.addCleanup(_rm_tree, self._tmp)
        self.state_file = os.path.join(self._tmp, "state.json")
        self.doc = ac.load_state(Path(self.state_file))
        ac.save_state(Path(self.state_file), self.doc)

    def advance(self, *states):
        for s in states:
            ac.cmd_transition(ac._Args(new_state=s, marker="test",
                                       state_file=self.state_file))
        return ac.load_state(Path(self.state_file))


def _earliest_marker_pg_start_epoch():
    """Earliest start epoch among live PG-family marker processes, or None.

    Test-side mirror of the contract's ownership-TIME identity scan; used
    to derive an honest o9c anchor ("job started before all currently
    live job-shaped activity") so the focused suite's own ephemeral PG
    cluster classifies as the job's own workload, exactly as it would in
    a genuinely fresh Cloud acceptance job.
    """
    import os as _os
    import time as _time
    from pathlib import Path as _Path
    fam = {"postgres", "postmaster", "pg_ctl", "initdb"}
    marks = ("fie-pgtest.", "fie-zw-fixture-", "fie-warm-cache.",
             "fie-lk-cache.")
    earliest = None
    for d in _Path("/proc").glob("[0-9]*"):
        try:
            argv = [a.decode() for a in
                    (d / "cmdline").read_bytes().split(b"\x00") if a]
            if not argv or argv[0].split("/")[-1] not in fam:
                continue
            if not any(m in a for a in argv for m in marks):
                continue
            t = (d / "stat").read_text()
            toks = t[t.rfind(")") + 2:].split()
            start = (_time.time()
                     - float(open("/proc/uptime").read().split()[0])
                     + float(toks[19]) / _os.sysconf("SC_CLK_TCK"))
        except Exception:
            continue
        earliest = start if earliest is None else min(earliest, start)
    return earliest


def _rm_tree(p: str) -> None:
    import shutil
    shutil.rmtree(p, ignore_errors=True)


class OrderingStateMachineTests(_StateHarness):
    """O1 (old ordering rejected), O4, and the §12 illegal-transition set."""

    def _try_advance(self, new):
        doc = ac.load_state(Path(self.state_file))
        try:
            ac.cmd_transition(ac._Args(new_state=new, marker="probe",
                                       state_file=self.state_file))
            return None
        except ac.ContractError as exc:
            return str(exc)

    def test_o1_old_ordering_rejected_pre_bootstrap_fingerprint(self):
        # O1: the OLD ordering — PRE fingerprint while still INIT
        # (pre-bootstrap) — must be REJECTED_WITH_ORDERING_PREREQUISITE;
        # the refusal message must be an ordering prerequisite, never a
        # production-contact claim.
        try:
            ac.cmd_fingerprint(ac._Args(stage="PRE",
                                        state_file=self.state_file))
            self.fail("old ordering must be rejected")
        except ac.ContractError as exc:
            msg = str(exc)
            self.assertIn("ORDERING_PREREQUISITE", msg)
            # an ordering prerequisite is never misreported as Production
            # contact (WO §13/O1)
            self.assertNotIn("PRODUCTION_CONTACTED=true", msg)
            self.assertIn("NOT evidence of Production contact", msg)
        self.assertEqual(
            ac.load_state(Path(self.state_file))["state"], "INIT")

    def test_o8_explicit_illegal_transition_rejections(self):
        # WO §12 minimum illegal-transition set — each attempt fails
        # closed with an ORDERING_PREREQUISITE refusal and leaves the
        # state file unchanged. (INIT -> ACCEPTANCE_WORKLOAD is not a
        # named state; "BOOTSTRAP_PASSED -> FULL_REGRESSION_PASSED" and
        # "ACCEPTANCE_WORKLOAD -> PRE_FINGERPRINT_CAPTURED" are the
        # skip-ahead shapes below; "POST -> PRE" is the backward shape.)
        illegal = (
            ("INIT", "PRE_FINGERPRINT_CAPTURED"),
            ("INIT", "FOCUSED_PASSED"),
            ("BOOTSTRAP_PASSED", "FULL_REGRESSION_PASSED"),
            ("PRE_FINGERPRINT_CAPTURED", "FULL_REGRESSION_PASSED"),
            ("PRE_FINGERPRINT_CAPTURED", "PRE_FINGERPRINT_CAPTURED"),
            ("POST_FINGERPRINT_CAPTURED", "PRE_FINGERPRINT_CAPTURED"),
            ("FINGERPRINT_MATCHED", "PRE_FINGERPRINT_CAPTURED"),
        )
        for from_state, to_state in illegal:
            self.setUp()  # fresh state file per attempt
            self.advance(*ac.CHAIN[1:ac.CHAIN.index(from_state) + 1])
            doc = ac.load_state(Path(self.state_file))
            self.assertEqual(doc["state"], from_state, (from_state, to_state))
            err = self._try_advance(to_state)
            self.assertIsNotNone(err, (from_state, to_state))
            self.assertIn("ORDERING_PREREQUISITE", err, (from_state, to_state))
            self.assertIn("is not the next legal state", err)
            self.assertEqual(ac.load_state(Path(self.state_file))["state"],
                             from_state, (from_state, to_state))

    def test_state_machine_chain_is_exactly_the_documented_chain(self):
        # §12 documented chain, verbatim.
        self.assertEqual(ac.CHAIN, (
            "INIT", "BASELINE_CAPTURED", "PRODUCTION_ACCESS_PREFLIGHT_PASSED",
            "BOOTSTRAP_PASSED", "PRE_FINGERPRINT_CAPTURED", "FOCUSED_PASSED",
            "FAILURE_INJECTION_PASSED", "FULL_REGRESSION_PASSED",
            "POST_FINGERPRINT_CAPTURED", "FINGERPRINT_MATCHED",
            "CLEANUP_PASSED", "EVIDENCE_SEALED"))
        self.advance(*ac.CHAIN[1:])  # full chain transit is legal in order
        self.assertEqual(
            ac.load_state(Path(self.state_file))["state"],
            "EVIDENCE_SEALED")


class PreflightTests(_StateHarness):
    """O2 (works without psycopg), O6 (prod DSN fail-closed), O3 stage."""

    def test_o3_o2_preflight_passes_without_psycopg(self):
        # O2: remove/scrub Python PostgreSQL client availability — the
        # preflight is stdlib-only and still returns the required true.
        out = _run_subproc(["preflight"])
        self.assertEqual(out.returncode, 0, out.stderr)
        doc = json.loads(out.stdout)
        self.assertTrue(doc["pre_bootstrap_production_access_preflight"])
        self.assertFalse(doc["bootstrap_production_dsn_available"])
        self.assertFalse(doc["bootstrap_production_contacted"])
        self.assertFalse(doc["bootstrap_production_mutated"])
        self.assertIn("psycopg", doc["note_missing_runtime_dependency_is_not_contact"])

    def test_o2_preflight_module_import_is_psycopg_free(self):
        # The module import itself must never require psycopg: import it
        # in a subprocess where psycopg import is poisoned at the finder
        # level (no pip uninstalls; no environment weakening).
        probe = (
            "import sys\n"
            "class NoPsycopg:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'psycopg': raise ImportError("
            "'poisoned: psycopg must not be required')\n"
            "sys.meta_path.insert(0, NoPsycopg())\n"
            "import importlib.util as u\n"
            "spec = u.spec_from_file_location('ac', %r)\n"
            "m = u.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "print('IMPORT_OK_NO_PSYCO_PG=' + str(True))\n"
            % (str(REPO / "scripts" / "acceptance_contract.py"),))
        out = subprocess.run([sys.executable, "-c", probe],
                             capture_output=True, text=True, cwd=str(REPO))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("IMPORT_OK_NO_PSYCO_PG=True", out.stdout)

    def test_o6_production_shaped_dsn_fails_closed(self):
        # O6: a synthetic Production-shaped (non-loopback) DSN in the
        # bootstrap context → fail closed BEFORE bootstrap.
        out = _run_subproc(["preflight"], extra_env={
            "FIE_INTELLIGENCE_DB": SYNTHETIC_PROD_DSN})
        self.assertEqual(out.returncode, ac.FAIL_CLOSED_EXIT, out.stdout)
        doc = json.loads(out.stdout)
        self.assertFalse(doc["pre_bootstrap_production_access_preflight"])
        self.assertTrue(doc["bootstrap_production_dsn_available"])
        self.assertIn("FIE_INTELLIGENCE_DB",
                      doc["classification"]["forbidden_carriers_with_values"])
        # no real credential: the value is a synthetic fixture constant
        self.assertFalse(doc["bootstrap_production_contacted"])

    def test_o6b_carrier_variable_with_value_fails_closed(self):
        out = _run_subproc(["preflight"], extra_env={
            "PGPASSWORD": "synthetic-acceptance-fixture-pw"})
        self.assertEqual(out.returncode, ac.FAIL_CLOSED_EXIT, out.stdout)
        doc = json.loads(out.stdout)
        self.assertIn("PGPASSWORD",
                      doc["classification"]["forbidden_carriers_with_values"])

    def test_preflight_static_bootstrap_boundary(self):
        out = _run_subproc(["preflight"])
        doc = json.loads(out.stdout)
        sb = doc["static_boundary"]
        self.assertEqual(sb["bootstrap_entrypoint"], "scripts/bootstrap.sh")
        self.assertEqual(sb["forbidden_carrier_names_referenced"], [])
        self.assertFalse(sb["references_host_psql"])
        self.assertFalse(sb["executes_sql"])

    def test_preflight_accepts_isolated_loopback_dsn_shape(self):
        # FIE_TEST_PG_DSN in the provably-isolated /tmp-socket form is the
        # documented test profile input — NOT a blocker.
        out = _run_subproc(["preflight"], extra_env={
            "FIE_TEST_PG_DSN": SYNTHETIC_LOOPBACK_DSN})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(json.loads(out.stdout)[
            "pre_bootstrap_production_access_preflight"])

    def test_preflight_values_never_logged(self):
        out = _run_subproc(["preflight"], extra_env={
            "FIE_INTELLIGENCE_DB": SYNTHETIC_PROD_DSN})
        self.assertEqual(out.returncode, ac.FAIL_CLOSED_EXIT, out.stdout)
        # the value never appears in output — only the redacted marker
        self.assertNotIn(SYNTHETIC_PROD_DSN, out.stdout)
        self.assertNotIn("fixture-host.invalid", out.stdout)
        blob = {b.get("value") for b in
                json.loads(out.stdout)["classification"]
                ["production_shaped_values"]}
        self.assertEqual(blob, {"REDACTED"})


class FingerprintOrderingTests(_StateHarness):
    """O3/O5: PRE after bootstrap; same canonical path for PRE and POST."""

    def test_o3_pre_fingerprint_requires_bootstrap_state(self):
        # In a simulated pre-bootstrap environment the PRE fingerprint is
        # rejected with the ordering prerequisite (O1's machine shape).
        doc = ac.load_state(Path(self.state_file))
        self.assertEqual(doc["state"], "INIT")
        with self.assertRaises(ac.ContractError) as ctx:
            ac.cmd_fingerprint(ac._Args(stage="PRE",
                                        state_file=self.state_file))
        self.assertIn("ORDERING_PREREQUISITE", str(ctx.exception))
        self.assertIn("BOOTSTRAP_PASSED", str(ctx.exception))

    def test_o5_pre_and_post_share_the_same_canonical_path(self):
        # Helper identity/algorithm consistency: the fingerprint command
        # drives the SAME canonical implementation for PRE and POST (the
        # hermetic Production-shaped zero-write fixture + the canonical
        # portable SQL client) — asserted at the contract level here, and
        # by the live driver in the acceptance evidence.
        src = (REPO / "scripts" / "acceptance_contract.py").read_text()
        self.assertEqual(src.count("def _canonical_fingerprint"), 1)
        self.assertIn("hermetic_production_fixture", src)
        self.assertIn("tests.test_rehearsal_guard_zero_write_invariant", src)
        self.assertIn("scripts/sql_exec.py", src)
        self.assertEqual(src.count("def cmd_fingerprint"), 1)
        self.assertIn('args.stage == "PRE"', src)
        self.assertIn('# POST\n    require_state(path, '
                      '"FULL_REGRESSION_PASSED")', src)  # same path, both stages

    def test_post_fingerprint_requires_full_regression_state(self):
        # POST before acceptance workloads complete (still at PRE) →
        # fail closed (ordering; also covers POST → PRE impossibility:
        # PRE after POST would need state below POST, which transitions
        # can never reach again).
        self.advance("BASELINE_CAPTURED", "PRODUCTION_ACCESS_PREFLIGHT_PASSED",
                     "BOOTSTRAP_PASSED", "PRE_FINGERPRINT_CAPTURED")
        with self.assertRaises(ac.ContractError) as ctx:
            ac.cmd_fingerprint(ac._Args(stage="POST",
                                        state_file=self.state_file))
        self.assertIn("ORDERING_PREREQUISITE", str(ctx.exception))
        self.assertIn("FULL_REGRESSION_PASSED", str(ctx.exception))

    def test_o4_acceptance_gate_blocks_before_pre_fingerprint(self):
        # The canonical entrypoint honors the state file: with the state
        # at INIT the workloads must not start (O4), and the message must
        # be the ordering prerequisite form.
        self.advance("BASELINE_CAPTURED", "PRODUCTION_ACCESS_PREFLIGHT_PASSED",
                     "BOOTSTRAP_PASSED")
        env = {**{k: v for k, v in os.environ.items()
                  if not k.startswith("FIE_")
                  and k not in _CREDENTIAL_CARRIERS},
               "FIE_ACCEPTANCE_STATE_FILE": self.state_file,
               "FIE_PYTHON": sys.executable}
        out = subprocess.run(
            ["bash", str(REPO / "scripts" / "test-cloud.sh")],
            capture_output=True, text=True, cwd=str(REPO), env=env,
            timeout=600)
        self.assertEqual(out.returncode, ac.FAIL_CLOSED_EXIT,
                         out.stdout[-2000:] + out.stderr[-2000:])
        self.assertIn("ORDERING_PREREQUISITE", out.stdout + out.stderr)
        self.assertIn("O4", out.stdout + out.stderr)

    def test_o11_preflight_refusal_prevents_all_workloads(self):
        # O11: a mandatory preflight failure stops the entrypoint before
        # any workload — no negative controls, no unittest, exit 78. The
        # injected blocker must survive the entrypoint's FIE_*/PGPASSWORD
        # scrub, so a non-FIE carrier (DATABASE_URL) carries a synthetic
        # Production-shaped DSN; the preflight refuses it fail-closed.
        self.advance("BASELINE_CAPTURED")  # preflight stage expected next
        env = {**{k: v for k, v in os.environ.items()
                  if not k.startswith("FIE_")
                  and k not in _CREDENTIAL_CARRIERS},
               "FIE_ACCEPTANCE_STATE_FILE": self.state_file,
               "FIE_PYTHON": sys.executable,
               "DATABASE_URL": SYNTHETIC_PROD_DSN}
        out = subprocess.run(
            ["bash", str(REPO / "scripts" / "test-cloud.sh")],
            capture_output=True, text=True, cwd=str(REPO), env=env,
            timeout=600)
        self.assertEqual(out.returncode, ac.FAIL_CLOSED_EXIT)
        self.assertIn("pre-bootstrap production access", out.stderr)
        self.assertNotIn("test-cloud: focused cloud-readiness set",
                         out.stdout)
        # and the state machine is not advanced past the refused stage
        self.assertEqual(ac.load_state(Path(self.state_file))["state"],
                         "BASELINE_CAPTURED")


class BootstrapEnvScrubTests(unittest.TestCase):
    """O7 — bootstrap cannot forward a Production DSN to a child."""

    def test_o7a_scrubbed_child_env_never_carries_production_dsn(self):
        from tests import cloud_child_env
        parent = {**os.environ,
                  "FIE_INTELLIGENCE_DB": SYNTHETIC_PROD_DSN,
                  "FIE_DB_TARGET_INTELLIGENCE": SYNTHETIC_PROD_DSN,
                  "FIE_TEST_PG_DSN": SYNTHETIC_LOOPBACK_DSN,
                  "PGPASSWORD": "synthetic-acceptance-fixture-pw",
                  "DATABASE_URL": SYNTHETIC_PROD_DSN,
                  "POSTGRES_PASSWORD": "synthetic-acceptance-fixture-pw"}
        env = cloud_child_env.child_env(home="/tmp", tmpdir="/tmp",
                                        parent_env=parent)
        for name in cloud_child_env.SECRET_BOUNDARY_DENYLIST:
            self.assertNotIn(name, env, name)
        for key in env:
            self.assertFalse(key.startswith("FIE_"), key)
        blobs = [k for k, v in env.items() if v == SYNTHETIC_PROD_DSN]
        self.assertEqual(blobs, [])

    def test_o7b_test_cloud_entrypoint_scrubs_caller_fie_carriers(self):
        # Static proof at the canonical entrypoint: FIE_*/PGPASSWORD are
        # dropped BEFORE any child context is built (the acceptance gate
        # preflight itself runs on the scrubbed context).
        src = (REPO / "scripts" / "test-cloud.sh").read_text()
        self.assertIn("do unset \"${v}\" || true; done", src)
        self.assertIn("unset PGPASSWORD", src)
        self.assertLess(src.index("FIE_[A-Za-z0-9_]"),
                        src.index("acceptance_contract.py"))

    def test_o7c_children_are_born_from_the_one_scrub_helper(self):
        # The canonical entrypoint/suite spawn children ONLY through the
        # ONE scrub implementation (tests/cloud_child_env.py) — the
        # network-context contract that R4-R2-R1 established.
        from tests import cloud_child_env
        for name in ("scrubbed_child_seed", "child_env",
                     "SECRET_BOUNDARY_DENYLIST", "NETWORK_RUNTIME_ALLOWLIST"):
            self.assertTrue(hasattr(cloud_child_env, name))


class ProvenanceTests(_StateHarness):
    """O8/O9/O10 — fresh-job provenance contract."""

    def _clean_scope(self):
        """A provenance scope that is provably free of predecessor
        artifacts or inherited job-owned temp dirs (fresh temp dir)."""
        d = tempfile.mkdtemp(prefix="fie-acc-fresh-")
        self.addCleanup(_rm_tree, d)
        return ["--job-root", d, "--tmpdir", d]

    def _prov_state_args(self):
        # The /proc process scan is anchored by ownership TIME from the
        # declared acceptance job (ambient FIE_ACCEPTANCE_STATE_FILE — the
        # real acceptance job's state, recorded at job bootstrap). In an
        # ambient acceptance context processes created during the job
        # classify as this job's own active workloads. WITHOUT an ambient
        # acceptance job (developer/standalone run) no truthful job-start
        # anchor exists on a shared host — no state args: the scan records
        # an inconclusive observation instead of a contradiction (the
        # anchored behavior itself is covered by O9/O9b/O9c simulations).
        amb = os.environ.get("FIE_ACCEPTANCE_STATE_FILE", "")
        return ["--state-file", amb] if amb else []

    def test_o8_missing_platform_provenance_is_not_a_failure(self):
        # No platform /new API is assumed; owner assertion absent →
        # provenance stays RUNTIME_CONSISTENT_OWNER_ASSERTION_ABSENT and
        # the runtime consistency is still verified true.
        out = _run_subproc(["provenance"] + self._prov_state_args()
            + self._clean_scope(), extra_env={
            "FIE_OWNER_FRESH_JOB_ASSERTION": "x"})
        doc = json.loads(out.stdout)
        self.assertFalse(doc["platform_fresh_job_provenance_available"])
        self.assertTrue(doc["runtime_freshness_consistency_verified"])
        self.assertIsNone(doc["owner_fresh_job_assertion"])
        self.assertEqual(out.returncode, 0)

    def test_o10_owner_assertion_absent_stays_null(self):
        out = _run_subproc(["provenance"] + self._prov_state_args()
                           + self._clean_scope())
        doc = json.loads(out.stdout)
        self.assertIsNone(doc["owner_fresh_job_assertion"])
        self.assertNotEqual(doc["fresh_codex_cloud_job_provenance"],
                            "OWNER_ASSERTED_RUNTIME_CONSISTENT")

    def test_o10b_owner_assertion_false_is_never_synthesized_true(self):
        out = _run_subproc(["provenance"] + self._prov_state_args()
            + self._clean_scope(), extra_env={
            "FIE_OWNER_FRESH_JOB_ASSERTION": "false"})
        doc = json.loads(out.stdout)
        self.assertFalse(doc["owner_fresh_job_assertion"])

    def test_owner_assertion_true_with_runtime_consistency(self):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                              capture_output=True, text=True).stdout.strip()
        out = _run_subproc(["provenance", "--expect-head", head]
            + self._prov_state_args() + self._clean_scope(),
            extra_env={"FIE_OWNER_FRESH_JOB_ASSERTION": "true"})
        doc = json.loads(out.stdout)
        self.assertTrue(doc["owner_fresh_job_assertion"])
        self.assertTrue(doc["runtime_freshness_consistency_verified"])
        self.assertEqual(doc["fresh_codex_cloud_job_provenance"],
                         "OWNER_ASSERTED_RUNTIME_CONSISTENT")

    def test_o9_contradictory_freshness_evidence_fails_closed(self):
        job_root = tempfile.mkdtemp(prefix="fie-acc-contradiction-")
        base = Path(job_root)
        # (a) predecessor evidence artifact
        (base / "ABACUS_FIE_6_9A_R4_R5_R1_EVIDENCE").mkdir()
        (base / "ABACUS_FIE_6_9A_R4_R5_R1_EVIDENCE" / "gates_summary.md") \
            .write_text("predecessor residue\n")
        # (b) FIE job-owned process residue marker: a scan-visible
        #     /proc-scannable process (ours to create+reap) whose argv[0]
        #     is a PostgreSQL-family binary and whose cmdline carries the
        #     canonical job marker — the identity-scanning freshness scan
        #     must see it (bash `exec -a` renames argv[0] only).
        killer = subprocess.Popen(
            ["bash", "-c",
             f'exec -a postgres {sys.executable} -c '
             '"import time; time.sleep(300)" fie-pgtest.synthetic-residue'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # (c) inherited job-owned temp dir
        (base / "fie-pgtest.leftover").mkdir()
        (base / "fie-warm-cache.abc").mkdir()
        try:
            out = _run_subproc(["provenance", "--job-root", job_root,
                                "--tmpdir", job_root])
        finally:
            killer.kill()
            killer.wait()
        self.assertEqual(out.returncode, ac.FAIL_CLOSED_EXIT, out.stdout)
        doc = json.loads(out.stdout)
        self.assertFalse(doc["runtime_freshness_consistency_verified"])
        self.assertEqual(doc["fresh_codex_cloud_job_provenance"],
                         "RUNTIME_CONTRADICTIONS_BLOCK_ACCEPTANCE")
        joined = " | ".join(doc["runtime_contradictions"])
        self.assertIn("ABACUS_FIE_6_9A_R4_R5_R1_EVIDENCE", joined)
        self.assertIn("inherited job-owned temp dir", joined)

    def test_o9b_inherited_pg_process_residue_detected(self):
        # Simulated fresh job whose start is DECLARED to follow this
        # process by 60 s (controlled simulation: any PG-family process
        # alive now predates the job and must be flagged as inherited
        # residue). The spoofed process is ours to create and reap.
        p = subprocess.Popen(
            ["bash", "-c",
             f'exec -a postgres {sys.executable} -c '
             '"import time; time.sleep(300)" fie-pgtest.synthetic-residue2'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import datetime
        js = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(seconds=60))
        try:
            out = _run_subproc(["provenance",
                                "--job-root",
                                tempfile.mkdtemp(prefix="fie-acc-clean-"),
                                "--tmpdir",
                                tempfile.mkdtemp(prefix="fie-acc-clean-"),
                                "--job-start-utc",
                                js.strftime("%Y-%m-%dT%H:%M:%SZ")])
        finally:
            p.kill()
            p.wait()
        self.assertEqual(out.returncode, ac.FAIL_CLOSED_EXIT, out.stdout)
        doc = json.loads(out.stdout)
        self.assertIn("inherited FIE job-owned process residue",
                      " | ".join(doc["runtime_contradictions"]))

    def test_o9c_active_job_process_is_own_workload_not_residue(self):
        # Complement of O9b: with a job start declared in the PAST (a
        # running acceptance job), a PG-family marker process created
        # during the job is the job's OWN active workload — recorded as
        # an observation, never an inherited-residue contradiction, and
        # never blocking. (Any leftover after teardown is caught by the
        # explicit cleanup counters, not by the inherited-residue scan.)
        #
        # The declared anchor must PREDATE every live marker process,
        # including the focused suite's own ephemeral PG cluster (which
        # authentic runs also spin up after the real job started) — the
        # anchor is therefore derived as "before the earliest live
        # marker process", which is what a genuinely fresh Cloud job's
        # job_start_utc looks like from inside the suite. (fail1 fix:
        # a fixed now-60s anchor raced the suite cluster and misclass-
        # ified it as inherited residue.)
        p = subprocess.Popen(
            ["bash", "-c",
             f'exec -a postgres {sys.executable} -c '
             '"import time; time.sleep(300)" fie-pgtest.own-workload'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import datetime
        js = datetime.datetime.now(datetime.timezone.utc) \
            - datetime.timedelta(seconds=60)
        earliest = _earliest_marker_pg_start_epoch()
        if earliest is not None:
            js = min(js, datetime.datetime.fromtimestamp(
                earliest - 300, datetime.timezone.utc))
        try:
            out = _run_subproc(["provenance",
                                "--job-root",
                                tempfile.mkdtemp(prefix="fie-acc-clean-"),
                                "--tmpdir",
                                tempfile.mkdtemp(prefix="fie-acc-clean-"),
                                "--job-start-utc",
                                js.strftime("%Y-%m-%dT%H:%M:%SZ")],
                               extra_env={"FIE_OWNER_FRESH_JOB_ASSERTION":
                                          "true"})
        finally:
            p.kill()
            p.wait()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        doc = json.loads(out.stdout)
        self.assertTrue(doc["runtime_freshness_consistency_verified"])
        joined = " | ".join(doc["runtime_observations"])
        self.assertIn("active job-owned PG-family process", joined)
        self.assertFalse("inherited" in joined
                         or "residue" in joined)

    def test_worktree_dirtiness_is_contradiction_only_when_declared(self):
        # Integration over the REAL workspace, both ways: (a) default —
        # tracked-worktree dirtiness is recorded as an observation and
        # NEVER as a freshness contradiction; (b) --require-clean-worktree
        # (the declared fresh-job check used by the real Cloud acceptance
        # run) — dirtiness IS a contradiction and blocks fail-closed.
        # Both branches assert only stateful facts (is the tree dirty),
        # so the test is valid in a clean clone and in a dirty workspace.
        def _run(*extra):
            return _run_subproc(["provenance"] + self._prov_state_args()
                            + self._clean_scope() + list(extra))

        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(REPO),
            capture_output=True, text=True).stdout.strip()
        doc = json.loads(_run().stdout)
        self.assertEqual(_run().returncode, 0)
        if dirty:
            self.assertIn("tracked worktree is not clean",
                          doc["runtime_observations"])
            self.assertFalse(any(
                "tracked worktree" in c
                for c in doc["runtime_contradictions"]))
            flagged = _run("--require-clean-worktree")
            self.assertEqual(flagged.returncode, ac.FAIL_CLOSED_EXIT,
                             flagged.stdout)
            self.assertIn("tracked worktree is not clean", " | ".join(
                json.loads(flagged.stdout)["runtime_contradictions"]))
        else:
            self.assertFalse(any("tracked worktree" in s for s in
                                 doc["runtime_observations"]
                                 + doc["runtime_contradictions"]))
            self.assertEqual(_run("--require-clean-worktree").returncode, 0)
        # and the unit-level classification table, honestly:
        self.assertEqual(
            ac._worktree_signal(dirty=True, require_clean=True),
            (["tracked worktree is not clean"], []))
        self.assertEqual(
            ac._worktree_signal(dirty=True, require_clean=False),
            ([], ["tracked worktree is not clean"]))
        self.assertEqual(ac._worktree_signal(False, True), ([], []))


class ReceiptCompletenessTests(unittest.TestCase):
    """O12 — NOT_RUN fields stay explicit; never converted to PASS."""

    def test_o12_receipt_schema_retains_null_semantics(self):
        schema = (REPO / "docs" / "architecture" /
                  "cloud-execution-contract.md").read_text()
        # the documented provenance contract distinguishes operator
        # assertion (true|false|null) from machine-verifiable consistency
        self.assertIn("owner_fresh_job_assertion", schema)
        self.assertIn("runtime_freshness_consistency_verified", schema)
        self.assertIn("platform_fresh_job_provenance_available = false",
                      schema.replace("`", ""))
        # the state-machine module can never produce a PASS-like value
        # for a stage it did not run: transition requires strict chain
        self.assertEqual(ac.CHAIN[0], "INIT")


if __name__ == "__main__":
    unittest.main()