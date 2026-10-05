#!/usr/bin/env python3
"""Wrapper-level rehearsal DB-target guard tests (2026-10-04 incident closure).

Runs the actual wrappers (run.sh / run_weekly.sh / run_monthly.sh) with
controlled environments and asserts:

negative (G6): rehearsal mode + missing/empty FIE_INTELLIGENCE_DB, only the
  misnamed sibling variable, malformed, production-targeted (incl. symlink),
  and the production fallback default are REFUSED with
  FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED and exit code 78 — BEFORE any
  DB write (Step 1 never runs: the guard precedes it). Refused runs must
  not create the requested target nor touch the production store.
positive (G6): the wrapper resolver keeps the accepted production fallback
  (FIE_SERVICE_ENV unset + FIE_INTELLIGENCE_DB unset resolves to the
  production default) and an explicit production-mode target passes.

2026-10-05 G6 remediation (PostgreSQL production-DSN bypass): the guard now
also compares the EFFECTIVE resolved target against the production contract
(FIE_PRODUCTION_DB_CONTRACT files; fixture-based in these tests) and refuses
Production-equivalent PostgreSQL DSNs — including DSNs injected through a
wrapper env file BEFORE the guard (S4 ordering-independence), alias DSN
spellings of the same production identity, missing/ambiguous targets, and
PG targets with no resolvable production contract. Safe isolated PG
rehearsal targets are accepted (stubbed Step-1 sentinel proves the wrapper
proceeds without any write).

The wrapper's accepted production default is parsed from the wrapper
itself (no host-specific literal is pinned here). Every negative test runs
hermetically: the guard exits before any Python process, DB migration or
network fetch is attempted; fixture production identities point at an
unbound 127.0.0.1:59999 endpoint that no test dials.
"""
import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WRAPPERS = ("run.sh", "run_weekly.sh", "run_monthly.sh")
GUARD_CODE = "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED"
REFUSAL_RC = 78
EXPECTED_PRODUCTION_DEFAULT = (
    "/home/ubuntu/macro-report/metadata/intelligence_store.db")

# --- 2026-10-05 G6 remediation fixtures -----------------------------------
# All fixture "production" identities point at port 59999 on loopback —
# an unbound fake endpoint that no test ever dials. No fixture reaches the
# real production contract files unless the host-default test does (see
# test_12, which only observes files, never pipelines).
FIXTURE_PROD_DSN = "postgresql://fixture_prod@127.0.0.1:59999/fie_prod_fixture"
FIXTURE_PROD_RAW_DSN = \
    "postgresql://fixture_prod@127.0.0.1:59999/fie_prod_fixture_raw"
FIXTURE_SAFE_PG_DSN = "postgresql://fie_rehearsal@127.0.0.1:5432/fie_rehearsal"

WRAPPERS_DEFAULT_ENV = "/home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env"


def _make_env_file(tmp: Path, name: str, body: str) -> str:
    path = tmp / name
    path.write_text(body)
    return str(path)


def _fixture_prod_contract(tmp: Path) -> str:
    """Contract file fixture modelling the production env-file shape."""
    return _make_env_file(tmp, "fixture-prod-contract.env", (
        "FIE_SERVICE_ENV='production'\n"
        f"FIE_DATABASE_URL='{FIXTURE_PROD_DSN}'\n"
        f"FIE_DB_PATH='{FIXTURE_PROD_RAW_DSN}'\n"))


def _fixture_wrapper_env(tmp: Path, intelligence_db: str = "") -> str:
    """Wrapper env fixture (the file run.sh sources before the guard)."""
    body = ""
    if intelligence_db:
        body = f"export FIE_INTELLIGENCE_DB='{intelligence_db}'\n"
    return _make_env_file(tmp, "fixture-wrapper.env", body)


def _make_stub_python(tmp: Path) -> str:
    """Step-1 interpreter stub: sentinel + exit 42, never touches DBs."""
    stub = tmp / "stub_python.sh"
    stub.write_text("#!/bin/bash\necho STUB_STEP1_SENTINEL\nexit 42\n")
    stub.chmod(0o755)
    return str(stub)


def _wrapper_default(wrapper: str) -> str:
    """Extract the accepted production fallback default from the wrapper."""
    text = (REPO / wrapper).read_text()
    m = re.search(
        r'fie_wrapper_seed_db_guard\s+"([^"]+)"', text)
    if m:
        return m.group(1)
    m = re.search(
        r'SEED_DB="\$\{FIE_INTELLIGENCE_DB:-(.*?)"', text)
    if m:
        return m.group(1).strip()
    raise AssertionError(f"{wrapper}: could not parse production default")


def _run_wrapper(wrapper: str, env_overrides: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for key in ("FIE_SERVICE_ENV", "FIE_INTELLIGENCE_DB",
                "FIE_DATABASE_URL", "FIE_DB_PATH",
                "FIE_WRAPPER_ENV", "FIE_PRODUCTION_DB_CONTRACT",
                "FIE_GUARD_PYTHON"):
        env.pop(key, None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(REPO / wrapper)],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=60,
    )


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class WrapperRehearsalGuardTests(unittest.TestCase):
    """Negative + positive wrapper-resolver behaviors across all wrappers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wrapper_guard_")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_wrappers_carry_the_guard(self):
        for wrapper in WRAPPERS:
            text = (REPO / wrapper).read_text()
            self.assertIn("scripts/rehearsal_db_guard.sh", text,
                          f"{wrapper}: guard not sourced")
            self.assertIn("fie_wrapper_seed_db_guard", text,
                          f"{wrapper}: guard not invoked")

    def test_production_default_preserved(self):
        for wrapper in WRAPPERS:
            self.assertEqual(_wrapper_default(wrapper),
                             EXPECTED_PRODUCTION_DEFAULT,
                             f"{wrapper}: accepted production default drifted")


class WrapperNegativeTests(unittest.TestCase):
    """Each negative case: refusal rc + code + zero writes (G6)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wrapper_neg_")
        self.tmp = Path(self._tmp.name)

    def _assert_refused(self, wrapper: str, overrides: dict,
                        *, expect_code=GUARD_CODE,
                        assert_target_absent=True):
        prod = Path(_wrapper_default(wrapper))
        prod_sha_before = _sha256(prod)
        sidecars = {}
        for suffix in ("-wal", "-shm"):
            side = prod.with_name(prod.name + suffix)
            if side.exists():
                st = side.stat()
                sidecars[suffix] = (st.st_size, st.st_mtime_ns)
        # The production store and its sidecars stay byte-identical
        # afterwards (no write occurs before rejection).
        result = _run_wrapper(wrapper, overrides)
        self.assertNotEqual(result.returncode, 0,
                            f"{wrapper}: negative case unexpectedly succeeded "
                            f"(stdout: {result.stdout[-400:]})")
        self.assertEqual(result.returncode, REFUSAL_RC,
                         f"{wrapper}: refusal must exit 78, got "
                         f"{result.returncode}")
        self.assertIn(expect_code, result.stderr,
                      f"{wrapper}: refusal code missing in stderr: {result.stderr}")
        self.assertEqual(_sha256(prod), prod_sha_before,
                         f"{wrapper}: the live production store changed")
        for suffix, before in sidecars.items():
            side = prod.with_name(prod.name + suffix)
            st = side.stat()
            self.assertEqual((st.st_size, st.st_mtime_ns), before,
                             f"{wrapper}: production sidecar {suffix} changed")
        # The rejected requested target must not have been created.
        req = overrides.get("FIE_INTELLIGENCE_DB")
        if assert_target_absent and req and req.startswith("/"):
            self.assertFalse(Path(req).exists(),
                             f"{wrapper}: rejected target materialized")
        return result

    def test_1_missing_target_rejected_daily(self):
        self._assert_refused("run.sh", {"FIE_SERVICE_ENV": "test"})

    def test_1b_missing_target_rejected_weekly_monthly(self):
        self._assert_refused("run_weekly.sh", {"FIE_SERVICE_ENV": "staging"})
        self._assert_refused("run_monthly.sh", {"FIE_SERVICE_ENV": "test"})

    def test_2_empty_target_rejected(self):
        self._assert_refused("run.sh", {
            "FIE_SERVICE_ENV": "test", "FIE_INTELLIGENCE_DB": "",
            "FIE_WRAPPER_ENV": _fixture_wrapper_env(self.tmp)})

    def test_3_misnamed_fie_database_url_only_rejected(self):
        # The EXACT incident signature: FIE_DATABASE_URL (the HTTP-store
        # variable) present, FIE_INTELLIGENCE_DB absent. Refused in every
        # mode — including undeclared mode. (Wrapper env pinned to an
        # empty fixture so the candidate the guard sees is the caller's.)
        empty_env = _fixture_wrapper_env(self.tmp)
        self._assert_refused("run.sh", {
            "FIE_SERVICE_ENV": "test",
            "FIE_WRAPPER_ENV": empty_env,
            "FIE_DATABASE_URL": "postgresql://guard-test@/isolated_db"})
        self._assert_refused("run.sh", {
            "FIE_WRAPPER_ENV": empty_env,
            "FIE_DATABASE_URL": "postgresql://guard-test@/isolated_db"})

    def test_4_malformed_targets_rejected(self):
        cases = (
            "relative/store.db",
            "mysql://user@/fashionable",
            "https://guard.invalid/store.db",
        )
        empty_env = _fixture_wrapper_env(self.tmp)
        for wrapper in WRAPPERS:
            for bad in cases:
                self._assert_refused(wrapper, {
                    "FIE_SERVICE_ENV": "test", "FIE_INTELLIGENCE_DB": bad,
                    "FIE_WRAPPER_ENV": empty_env})

    def test_5_explicit_production_path_rejected(self):
        empty_env = _fixture_wrapper_env(self.tmp)
        for wrapper in WRAPPERS:
            self._assert_refused(wrapper, {
                "FIE_SERVICE_ENV": "test",
                "FIE_WRAPPER_ENV": empty_env,
                "FIE_INTELLIGENCE_DB": _wrapper_default(wrapper)},
                assert_target_absent=False)

    def test_6_symlink_to_production_rejected(self):
        empty_env = _fixture_wrapper_env(self.tmp)
        wrapper = "run.sh"
        prod = Path(_wrapper_default(wrapper))
        link = self.tmp / "alias-to-prod.db"
        link.symlink_to(prod)
        self._assert_refused(wrapper, {
            "FIE_SERVICE_ENV": "test",
            "FIE_WRAPPER_ENV": empty_env,
            "FIE_INTELLIGENCE_DB": str(link)},
            assert_target_absent=False)

    def test_7_fallback_available_but_target_absent_rejected(self):
        # The production default EXISTS and is reachable, yet rehearsal test
        # mode with no explicit target still refuses the fallback.
        wrapper = "run.sh"
        prod = Path(_wrapper_default(wrapper))
        self.assertTrue(prod.exists(), "production store expected present")
        self._assert_refused(wrapper, {
            "FIE_SERVICE_ENV": "test",
            "FIE_WRAPPER_ENV": _fixture_wrapper_env(self.tmp)})

    def test_invalid_service_env_refused(self):
        self._assert_refused("run.sh", {
            "FIE_SERVICE_ENV": "rehearsal",
            "FIE_INTELLIGENCE_DB": str(self.tmp / "iso.db"),
            "FIE_WRAPPER_ENV": _fixture_wrapper_env(self.tmp)})

    # --- 2026-10-05 G6 PostgreSQL-DSN production-bypass closure ------------

    def test_g6_wrapper_env_prod_dsn_before_guard_rejected(self):
        # The EXACT G6 bypass signature: the wrapper env file sourced
        # BEFORE the guard (run.sh ordering, S4) carries a
        # production-equivalent PostgreSQL DSN. The guard must compare the
        # EFFECTIVE target against the production contract and refuse —
        # regardless of wrapper/env ordering. Zero writes to the real
        # production store are asserted by _assert_refused; the fixture
        # endpoint (127.0.0.1:59999) is never dialled.
        for wrapper in WRAPPERS:
            for mode in ("test", "staging"):
                self._assert_refused(wrapper, {
                    "FIE_SERVICE_ENV": mode,
                    "FIE_WRAPPER_ENV": _fixture_wrapper_env(
                        self.tmp, FIXTURE_PROD_DSN),
                    "FIE_PRODUCTION_DB_CONTRACT": _fixture_prod_contract(
                        self.tmp)},
                    assert_target_absent=False)

    def test_g6_host_wrapper_env_file_signature_rejected(self):
        # Host-specific variant of the G6 signature: with the default
        # wrapper env file in place (FIE_WRAPPER_ENV unset), whatever
        # FIE_INTELLIGENCE_DB it declares must not reach a test-mode
        # pipeline. When the wrapper env file is absent the wrapper hits
        # the missing-target refusal instead — both are rc 78 refusals,
        # so this holds on every host; skip only when NEITHER path exists.
        if not Path(WRAPPERS_DEFAULT_ENV).exists():
            self.skipTest(
                "host wrapper env file absent; G6 signature covered by "
                "test_g6_wrapper_env_prod_dsn_before_guard_rejected")
        self._assert_refused("run.sh", {"FIE_SERVICE_ENV": "test"},
                             assert_target_absent=False)
        self._assert_refused("run_weekly.sh", {"FIE_SERVICE_ENV": "test"},
                             assert_target_absent=False)

    def test_g6_alias_dsn_forms_of_prod_identity_rejected(self):
        # Alternate PostgreSQL DSN spellings resolving to the SAME
        # production identity as the contract fixture (Task D #9).
        empty_env = _fixture_wrapper_env(self.tmp)
        contract = _fixture_prod_contract(self.tmp)
        aliases = (
            FIXTURE_PROD_DSN + "?sslmode=disable",
            "postgresql://fixture_prod@localhost:59999/fie_prod_fixture",
            "postgresql://fixture_prod@[::1]:59999/fie_prod_fixture",
            "postgresql://fixture_prod@127.0.0.2:59999/fie_prod_fixture",
            "postgresql://fixture_prod%40fingerprint-only@127.0.0.1:59999/fie_prod_fixture",
            "postgresql://unused-user@127.0.0.1:59999/fie_prod_fixture?host=127.0.0.1",
        )
        for wrapper in WRAPPERS:
            for alias in aliases:
                self._assert_refused(wrapper, {
                    "FIE_SERVICE_ENV": "test",
                    "FIE_WRAPPER_ENV": empty_env,
                    "FIE_PRODUCTION_DB_CONTRACT": contract,
                    "FIE_INTELLIGENCE_DB": alias},
                    assert_target_absent=False)

    def test_rehearsal_safe_pg_dsn_reaches_step1_with_zero_writes(self):
        # A safe isolated PostgreSQL rehearsal target is ACCEPTED: the
        # wrapper proceeds to Step 1 (stubbed interpreter: sentinel +
        # exit 42), the guard never fires, and the live production store
        # stays byte-identical (no write on the accepted path either).
        prod = Path(_wrapper_default("run.sh"))
        before = _sha256(prod)
        self.assertIsNotNone(before)
        for wrapper in WRAPPERS:
            result = _run_wrapper(wrapper, {
                "FIE_SERVICE_ENV": "test",
                "FIE_WRAPPER_ENV": _fixture_wrapper_env(
                    self.tmp, FIXTURE_SAFE_PG_DSN),
                "FIE_PRODUCTION_DB_CONTRACT": _fixture_prod_contract(self.tmp),
                "FIE_PYTHON": _make_stub_python(self.tmp)})
            self.assertNotIn(GUARD_CODE, result.stderr, result.stderr)
            self.assertIn("STUB_STEP1_SENTINEL", result.stdout)
            self.assertEqual(result.returncode, 42,
                             f"{wrapper}: stub sentinel exit expected")
        self.assertEqual(_sha256(prod), before,
                         "production store changed on the accepted path")

    def test_rehearsal_malformed_pg_dsn_fail_closed(self):
        # A PostgreSQL DSN with no database (ambiguous) is refused
        # fail-closed (S6); the libpq keyword form carries spaces, which
        # this wrapper contract refuses upstream (documented behaviour).
        empty_env = _fixture_wrapper_env(self.tmp)
        contract = _fixture_prod_contract(self.tmp)
        for bad in ("postgresql://fixture_prod@127.0.0.1:59999/",
                    "host=127.0.0.1 port=59999 dbname=fie_prod_fixture"):
            self._assert_refused("run.sh", {
                "FIE_SERVICE_ENV": "test",
                "FIE_WRAPPER_ENV": empty_env,
                "FIE_PRODUCTION_DB_CONTRACT": contract,
                "FIE_INTELLIGENCE_DB": bad},
                assert_target_absent=False)

    def test_prod_contract_absent_pg_target_fail_closed(self):
        # No production PG contract resolvable -> a PG rehearsal target
        # cannot be PROVEN non-Production -> refused (S6: unknown != safe).
        result = _run_wrapper("run.sh", {
            "FIE_SERVICE_ENV": "test",
            "FIE_WRAPPER_ENV": _fixture_wrapper_env(self.tmp,
                                                    FIXTURE_SAFE_PG_DSN),
            "FIE_PRODUCTION_DB_CONTRACT": str(self.tmp / "nonexistent.env")})
        self.assertEqual(result.returncode, 78)
        self.assertIn(GUARD_CODE, result.stderr)
        self.assertIn("no production PostgreSQL contract", result.stderr)


class WrapperResolverPositiveTests(unittest.TestCase):
    """Production-class resolution is unchanged (hermetic function-level)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wrapper_pos_")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, env: dict, default: str) -> tuple[int, str, str]:
        script = f'''
set -u
. "{REPO}/scripts/rehearsal_db_guard.sh"
PROJECT_ROOT="{self.tmp}"
fie_wrapper_seed_db_guard "{default}"
echo "SEED_DB=${{SEED_DB}}"
'''
        clean = os.environ.copy()
        for key in ("FIE_SERVICE_ENV", "FIE_INTELLIGENCE_DB",
                    "FIE_DATABASE_URL", "FIE_DB_PATH",
                    "FIE_WRAPPER_ENV", "FIE_PRODUCTION_DB_CONTRACT"):
            clean.pop(key, None)
        clean.update(env)
        p = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, env=clean, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_production_mode_fallback_unchanged(self):
        prod_default = _wrapper_default("run.sh")
        rc, out, err = self._resolve({"FIE_SERVICE_ENV": "production"},
                                     prod_default)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"SEED_DB={prod_default}", out)
        rc, out, err = self._resolve({}, prod_default)  # unset = historical
        self.assertEqual(rc, 0, err)
        self.assertIn(f"SEED_DB={prod_default}", out)

    def test_production_mode_explicit_target_passes(self):
        target = self.tmp / "explicit-prod-mode.db"
        rc, out, err = self._resolve(
            {"FIE_SERVICE_ENV": "production",
             "FIE_INTELLIGENCE_DB": str(target)},
            _wrapper_default("run.sh"))
        self.assertEqual(rc, 0, err)
        self.assertIn(f"SEED_DB={target}", out)

    def test_rehearsal_mode_explicit_target_passes(self):
        target = self.tmp / "explicit-rehearsal.db"
        rc, out, err = self._resolve(
            {"FIE_SERVICE_ENV": "test",
             "FIE_INTELLIGENCE_DB": str(target)},
            _wrapper_default("run.sh"))
        self.assertEqual(rc, 0, err)
        self.assertIn(f"SEED_DB={target}", out)


class WrapperPostgresContractResolverTests(unittest.TestCase):
    """PostgreSQL production-equivalence contract (hermetic resolver-level).

    All identities are fixtures on the unbound 127.0.0.1:59999 endpoint —
    the real production contract is never contacted or written.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="wrapper_pg_")
        self.tmp = Path(self._tmp.name)
        self.contract = _fixture_prod_contract(self.tmp)
        self.default = _wrapper_default("run.sh")

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, env: dict) -> tuple[int, str, str]:
        clean = os.environ.copy()
        for key in ("FIE_SERVICE_ENV", "FIE_INTELLIGENCE_DB",
                    "FIE_DATABASE_URL", "FIE_DB_PATH",
                    "FIE_WRAPPER_ENV", "FIE_PRODUCTION_DB_CONTRACT"):
            clean.pop(key, None)
        clean.update(env)
        p = subprocess.run(
            ["bash", "-c", f'''
set -u
. "{REPO}/scripts/rehearsal_db_guard.sh"
PROJECT_ROOT="{self.tmp}"
fie_wrapper_seed_db_guard "{self.default}"
echo "SEED_DB=${{SEED_DB}}"
'''], capture_output=True, text=True, env=clean, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def _prod_equivalent(self, dsn: str, env_extra: dict | None = None):
        env = {"FIE_SERVICE_ENV": "test",
               "FIE_PRODUCTION_DB_CONTRACT": self.contract,
               "FIE_INTELLIGENCE_DB": dsn}
        if env_extra:
            env.update(env_extra)
        return self._resolve(env)

    def test_prod_equivalent_dsn_refused(self):
        rc, out, err = self._prod_equivalent(FIXTURE_PROD_DSN)
        self.assertEqual(rc, 78, err)
        self.assertIn("Production-equivalent PostgreSQL target", err)
        self.assertNotIn(FIXTURE_PROD_DSN, err)

    def test_prod_equivalent_dsn_refused_staging_mode(self):
        rc, out, err = self._prod_equivalent(
            FIXTURE_PROD_DSN, {"FIE_SERVICE_ENV": "staging"})
        self.assertEqual(rc, 78, err)

    def test_safe_rehearsal_dsn_accepted(self):
        rc, out, err = self._prod_equivalent(FIXTURE_SAFE_PG_DSN)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"SEED_DB={FIXTURE_SAFE_PG_DSN}", out)

    def test_same_host_different_port_and_db_accepted(self):
        rc, out, err = self._prod_equivalent(
            "postgresql://fixture_prod@127.0.0.1:59998/fie_prod_fixture")
        self.assertEqual(rc, 0, err)

    def test_contract_override_is_the_rejected_source(self):
        # The rejected identity comes from the CONTRACT fixture file, not
        # the (real) host contract; swapping the fixture swaps the verdict.
        other = self.tmp / "other-contract.env"
        other.write_text(
            "FIE_DATABASE_URL='postgresql://p@127.0.0.1:59997/other_prod'\n")
        rc, _, err = self._prod_equivalent(
            "postgresql://p@127.0.0.1:59997/other_prod",
            {"FIE_PRODUCTION_DB_CONTRACT": str(other)})
        self.assertEqual(rc, 78, err)
        rc, out, err = self._prod_equivalent(
            "postgresql://p@127.0.0.1:59997/other_prod_db2",
            {"FIE_PRODUCTION_DB_CONTRACT": str(other)})
        self.assertEqual(rc, 0, err)

    def test_missing_contract_pg_dsn_fail_closed(self):
        rc, _, err = self._prod_equivalent(
            FIXTURE_SAFE_PG_DSN,
            {"FIE_PRODUCTION_DB_CONTRACT": str(self.tmp / "absent.env")})
        self.assertEqual(rc, 78, err)
        self.assertIn("no production PostgreSQL contract", err)

    def test_sqlite_era_contract_rejects_path_targets(self):
        # A SQLite-era contract (filesystem store declared): path targets
        # equal to the contract store are refused; distinct paths pass.
        sqlite_contract = self.tmp / "sqlite-contract.env"
        store = self.tmp / "legacy_store" / "intelligence_store.db"
        sqlite_contract.write_text(
            f"FIE_DATABASE_URL='{store}'\n")
        empty_env = _fixture_wrapper_env(self.tmp)
        rc, _, err = self._resolve({
            "FIE_SERVICE_ENV": "test", "FIE_WRAPPER_ENV": empty_env,
            "FIE_PRODUCTION_DB_CONTRACT": str(sqlite_contract),
            "FIE_INTELLIGENCE_DB": str(store)})
        self.assertEqual(rc, 78, err)
        other = self.tmp / "legacy_store" / "rehearsal_store.db"
        rc, out, err = self._resolve({
            "FIE_SERVICE_ENV": "test", "FIE_WRAPPER_ENV": empty_env,
            "FIE_PRODUCTION_DB_CONTRACT": str(sqlite_contract),
            "FIE_INTELLIGENCE_DB": str(other)})
        self.assertEqual(rc, 0, err)
        self.assertIn(f"SEED_DB={other}", out)

    def test_credentials_never_in_refusal_output(self):
        secret_dsn = ("postgresql://fie_prod:supersecret-pw-value@"
                      "127.0.0.1:59999/fie_prod_fixture")
        rc, _, err = self._prod_equivalent(secret_dsn)
        self.assertEqual(rc, 78, err)
        self.assertNotIn("supersecret-pw-value", err)
        self.assertNotIn("postgresql://fie_prod", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)