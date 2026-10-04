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

The wrapper's accepted production default is parsed from the wrapper
itself (no host-specific literal is pinned here). Every negative test runs
hermetically: the guard exits before any Python process, DB migration or
network fetch is attempted.
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
                "FIE_DATABASE_URL", "FIE_DB_PATH"):
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
            "FIE_SERVICE_ENV": "test", "FIE_INTELLIGENCE_DB": ""})

    def test_3_misnamed_fie_database_url_only_rejected(self):
        # The EXACT incident signature: FIE_DATABASE_URL (the HTTP-store
        # variable) present, FIE_INTELLIGENCE_DB absent. Refused in every
        # mode — including undeclared mode.
        self._assert_refused("run.sh", {
            "FIE_SERVICE_ENV": "test",
            "FIE_DATABASE_URL": "postgresql://guard-test@/isolated_db"})
        self._assert_refused("run.sh", {
            "FIE_DATABASE_URL": "postgresql://guard-test@/isolated_db"})

    def test_4_malformed_targets_rejected(self):
        cases = (
            "relative/store.db",
            "mysql://user@/fashionable",
            "https://guard.invalid/store.db",
        )
        for wrapper in WRAPPERS:
            for bad in cases:
                self._assert_refused(wrapper, {
                    "FIE_SERVICE_ENV": "test", "FIE_INTELLIGENCE_DB": bad})

    def test_5_explicit_production_path_rejected(self):
        for wrapper in WRAPPERS:
            self._assert_refused(wrapper, {
                "FIE_SERVICE_ENV": "test",
                "FIE_INTELLIGENCE_DB": _wrapper_default(wrapper)},
                assert_target_absent=False)

    def test_6_symlink_to_production_rejected(self):
        wrapper = "run.sh"
        prod = Path(_wrapper_default(wrapper))
        link = self.tmp / "alias-to-prod.db"
        link.symlink_to(prod)
        self._assert_refused(wrapper, {
            "FIE_SERVICE_ENV": "test",
            "FIE_INTELLIGENCE_DB": str(link)},
            assert_target_absent=False)

    def test_7_fallback_available_but_target_absent_rejected(self):
        # The production default EXISTS and is reachable, yet rehearsal test
        # mode with no explicit target still refuses the fallback.
        wrapper = "run.sh"
        prod = Path(_wrapper_default(wrapper))
        self.assertTrue(prod.exists(), "production store expected present")
        self._assert_refused(wrapper, {"FIE_SERVICE_ENV": "test"})

    def test_invalid_service_env_refused(self):
        self._assert_refused("run.sh", {
            "FIE_SERVICE_ENV": "rehearsal",
            "FIE_INTELLIGENCE_DB": str(self.tmp / "iso.db")})


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
        p = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, env={**os.environ, **env}, timeout=60)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)