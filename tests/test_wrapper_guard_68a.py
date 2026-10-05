"""Phase 6.8A Task I2 — wrapper guard negative-matrix extension.

Extends tests.test_rehearsal_wrapper_guard with the 6.8A classes:

- FAIL_CLOSED_PRODUCTION_MODE_DECLARATION_REQUIRED (exit 78): an UNSET
  FIE_SERVICE_ENV with any DB-related override present is fatal, not a
  warning (C-6 remediation);
- ambient-mode precedence: an ambient FIE_SERVICE_ENV=test SURVIVES a
  wrapper env file that declares FIE_SERVICE_ENV=production (the 6.8A
  incident root cause — regression-proofed at wrapper level);
- malformed production contract files fail closed for rehearsal PG
  targets.

Every invocation is hermetic: FIE_PYTHON is a stub (sentinel + exit 42),
fixture production identities point at the unbound 59999 endpoint, and
negative cases exit 78 before Step 1. Zero-write invariant asserts the
stub sentinel never appears (the writer was never invoked).
"""
import os
import unittest
from pathlib import Path

from tests.test_rehearsal_wrapper_guard import (
    _make_env_file,
    _make_stub_python,
    _run_wrapper,
    _fixture_prod_contract,
    FIXTURE_PROD_DSN,
)

from phase3.runtime_contract import parse_target, target_identity

REPO = Path(__file__).resolve().parents[1]
WRAPPERS = ("run.sh", "run_weekly.sh", "run_monthly.sh")
GUARD_CODE_68A = "FAIL_CLOSED_PRODUCTION_MODE_DECLARATION_REQUIRED"
GUARD_CODE_REHEARSAL = "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED"
REFUSAL_RC = 78


class ProductionModeDeclarationTests(unittest.TestCase):
    """C-6: unset mode + DB override of any class is FATAL (never a warning)."""

    # These fatal-class cases require the mode to be TRULY unset after the
    # wrapper env sourcing — FIE_WRAPPER_ENV points at a nonexistent file so
    # the host wrapper env (which now declares FIE_SERVICE_ENV=production for
    # cron) never declares a mode here. Otherwise the env file's declaration
    # would legitimately be accepted production-class behavior, not fatal.
    NO_WRAPPER_ENV = {"FIE_WRAPPER_ENV": "/nonexistent/fie_no_fixture.env"}

    def test_unset_with_fie_db_path_is_fatal(self) -> None:
        for wrapper in WRAPPERS:
            with self.subTest(wrapper=wrapper):
                proc = _run_wrapper(wrapper, {
                    "FIE_DB_PATH": "/tmp/fie_x.db", **self.NO_WRAPPER_ENV})
                self.assertEqual(proc.returncode, REFUSAL_RC, proc.stderr)
                self.assertIn(GUARD_CODE_68A, proc.stderr)
                self.assertNotIn("STUB_STEP1_SENTINEL", proc.stdout)

    def test_unset_with_fie_intelligence_db_is_fatal(self) -> None:
        proc = _run_wrapper(
            "run.sh", {"FIE_INTELLIGENCE_DB":
                       "postgresql://x@127.0.0.1:59999/whatever",
                       **self.NO_WRAPPER_ENV})
        self.assertEqual(proc.returncode, REFUSAL_RC, proc.stderr)
        self.assertIn(GUARD_CODE_68A, proc.stderr)

    def test_unset_with_misnamed_var_keeps_tripwire(self) -> None:
        proc = _run_wrapper(
            "run.sh", {"FIE_DATABASE_URL":
                       "postgresql://x@127.0.0.1:59999/whatever",
                       **self.NO_WRAPPER_ENV})
        self.assertEqual(proc.returncode, REFUSAL_RC, proc.stderr)
        self.assertIn(GUARD_CODE_REHEARSAL, proc.stderr)

    def test_env_file_declaration_saves_the_cron_path(self) -> None:
        """The host wrapper env's own FIE_SERVICE_ENV=production makes an
        otherwise-unset invocation accepted production-class — guard passes
        to the stub (never a real writer, never a real DB write)."""
        with tempfile_wrapper() as tmp:
            stub = _make_stub_python(tmp)
            proc = _run_wrapper("run.sh", {"FIE_PYTHON": stub})
            self.assertNotEqual(proc.returncode, REFUSAL_RC, proc.stderr)
            self.assertIn("STUB_STEP1_SENTINEL", proc.stdout)

    def test_local_mode_declared_is_accepted_production_class(self) -> None:
        """local is an accepted production-class declaration (ADR-017/D5);
        with an explicit target it passes the guard and reaches the stub."""
        with tempfile_wrapper() as tmp:
            stub = _make_stub_python(tmp)
            wrapper_env = _make_env_file(tmp, "wrapper.env", (
                "export FIE_INTELLIGENCE_DB='/tmp/fie_declared_local.db'\n"))
            proc = _run_wrapper("run.sh", {
                "FIE_SERVICE_ENV": "local",
                "FIE_WRAPPER_ENV": wrapper_env,
                "FIE_PYTHON": stub,
            })
            self.assertNotEqual(proc.returncode, REFUSAL_RC, proc.stderr)
            self.assertIn("STUB_STEP1_SENTINEL", proc.stdout)

    def test_explicit_production_declaration_accepted(self) -> None:
        """The env-file-declared production path (cron class) passes the
        guard through to Step 1 (stub proves no further processing died)."""
        with tempfile_wrapper() as tmp:
            stub = _make_stub_python(tmp)
            wrapper_env = _make_env_file(tmp, "wrapper.env", (
                "export FIE_SERVICE_ENV='production'\n"
                "export FIE_INTELLIGENCE_DB='/tmp/fie_declared_prod.db'\n"))
            proc = _run_wrapper("run.sh", {
                "FIE_WRAPPER_ENV": wrapper_env,
                "FIE_PYTHON": stub,
            })
            self.assertNotEqual(proc.returncode, REFUSAL_RC, proc.stderr)
            self.assertIn("STUB_STEP1_SENTINEL", proc.stdout)


class AmbientPrecedenceTests(unittest.TestCase):
    """6.8A incident regression: ambient test mode must survive a wrapper
    env file declaring production; production-equivalent targets are then
    refused with the REHEARSAL class (mode flipped = acceptance + writes)."""

    def test_ambient_test_survives_env_file_production(self) -> None:
        with tempfile_wrapper() as tmp:
            stub = _make_stub_python(tmp)
            fixture_wrapper_env = _make_env_file(tmp, "wrapper.env", (
                "export FIE_SERVICE_ENV='production'\n"
                f"export FIE_INTELLIGENCE_DB='{FIXTURE_PROD_DSN}'\n"))
            prod_contract = _fixture_prod_contract(tmp)
            proc = _run_wrapper("run.sh", {
                "FIE_SERVICE_ENV": "test",          # ambient declaration
                "FIE_WRAPPER_ENV": fixture_wrapper_env,
                "FIE_PRODUCTION_DB_CONTRACT": prod_contract,
                "FIE_PYTHON": stub,
            })
            self.assertEqual(proc.returncode, REFUSAL_RC, proc.stderr)
            # REHEARSAL-class refusal proves the guard saw test mode —
            # had the env-file production won, this would be accepted.
            self.assertIn(GUARD_CODE_REHEARSAL, proc.stderr)
            self.assertNotIn("STUB_STEP1_SENTINEL", proc.stdout)

    def test_ambient_test_with_safe_target_proceeds(self) -> None:
        """The same precedence with a SAFE target: guard accepts exactly
        once (test mode) and the stub — never a real writer — runs."""
        with tempfile_wrapper() as tmp:
            stub = _make_stub_python(tmp)
            disposable = tmp / "ambient_safe_target.db"
            fixture_wrapper_env = _make_env_file(tmp, "wrapper.env", (
                "export FIE_SERVICE_ENV='production'\n"
                f"export FIE_INTELLIGENCE_DB='{disposable}'\n"))
            proc = _run_wrapper("run.sh", {
                "FIE_SERVICE_ENV": "test",
                "FIE_WRAPPER_ENV": fixture_wrapper_env,
                "FIE_PYTHON": stub,
            })
            self.assertNotEqual(proc.returncode, REFUSAL_RC, proc.stderr)
            self.assertIn("STUB_STEP1_SENTINEL", proc.stdout)


class MalformedProductionContractTests(unittest.TestCase):
    def test_garbage_contract_file_fails_closed(self) -> None:
        with tempfile_wrapper() as tmp:
            stub = _make_stub_python(tmp)
            garbage = _make_env_file(tmp, "garbage.env", "not a dsn ]][[\n")
            proc = _run_wrapper("run.sh", {
                "FIE_SERVICE_ENV": "test",
                "FIE_INTELLIGENCE_DB":
                    "postgresql://x@127.0.0.1:59999/rehearsal_x",
                "FIE_PRODUCTION_DB_CONTRACT": garbage,
                "FIE_PYTHON": stub,
            })
            self.assertEqual(proc.returncode, REFUSAL_RC, proc.stderr)
            self.assertIn(GUARD_CODE_REHEARSAL, proc.stderr)
            self.assertNotIn("STUB_STEP1_SENTINEL", proc.stdout)


class tempfile_wrapper:
    """Context manager for a shared TemporaryDirectory in tests above."""

    def __enter__(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="fie_68a_guard_")
        return Path(self._tmp.__enter__())

    def __exit__(self, *exc):
        return self._tmp.__exit__(*exc)


if __name__ == "__main__":
    unittest.main(verbosity=2)