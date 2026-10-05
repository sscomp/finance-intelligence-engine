"""Phase 6.8A Task F — raw-layer guard coverage matrix (+ I3 zero-write).

Proves, at identity/connection boundaries (never against real
production), that the raw layer and the intelligence layer are scoped
SEPARATELY: making one layer disposable never makes the other one
production-safe. Required combos:

- disposable intelligence + disposable raw          -> allowed
- production intelligence + disposable raw          -> rejected
- disposable intelligence + production raw          -> rejected
- production intelligence + production raw          -> rejected
- unresolved production identity                    -> rejected
- malformed target                                  -> rejected

Zero-write invariant (Task I3): every negative combo proves the writer
was NOT invoked (connection constructors mocked) and all disposable
stores' byte fingerprints remain unchanged. Production itself is never
the mutation target: fixture production identities point at an unbound
127.0.0.1:59999 endpoint and a tmp file no legitimate writer touches.
"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import sqlite3 as _sqlite3

from phase3.runtime_contract import (
    FailClosedTarget,
    assert_rehearsal_target_safe,
    load_production_identities,
    parse_target,
    resolve_intelligence_target,
    resolve_raw_target,
)

FIXTURE_PROD_INTEL = "postgresql://fixture_prod@127.0.0.1:59999/fie_prod_fixture"
FIXTURE_PROD_RAW = "postgresql://fixture_prod@127.0.0.1:59999/fie_prod_fixture_raw"
SAFE_INTEL = "postgresql://fixture_rehearsal@127.0.0.1:5432/fie_rehearsal_int"
SAFE_RAW = "postgresql://fixture_rehearsal@127.0.0.1:5432/fie_rehearsal_raw"


class MatrixBase(unittest.TestCase):
    """Shared tmp fixtures: production identity contract + disposable files."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="fie_68a_matrix_")
        self.tmp = Path(tmp.name)
        self.addCleanup(tmp.cleanup)
        # production identity: intel + raw PG DSNs and a sqlite store file,
        # all from the CONTRACT FILE only (env never re-labels production)
        self.prod_sqlite = self.tmp / "prod_store.db"
        self.prod_sqlite.write_bytes(b"prod fixture bytes")
        self.contract = self.tmp / "prod_contract.env"
        self.contract.write_text(
            f"FIE_DATABASE_URL='{FIXTURE_PROD_INTEL}'\n"
            f"FIE_DB_PATH='{FIXTURE_PROD_RAW}'\n"
            f"FIE_DB_TARGET_INTELLIGENCE='{FIXTURE_PROD_INTEL}'\n"
        )
        self.prod = load_production_identities({}, [str(self.contract)])
        # disposable stores pre-materialized as byte-fingerprint witnesses
        self.disposable_intel = self.tmp / "rehearsal_intel.db"
        self.disposable_intel.write_bytes(b"")
        self.disposable_raw = self.tmp / "rehearsal_raw.db"

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _file_witnesses(self):
        return {
            "disposable_intel": self._sha256(self.disposable_intel)
            if self.disposable_intel.exists() else None,
            "disposable_raw": self._sha256(self.disposable_raw)
            if self.disposable_raw.exists() else None,
            "prod_sqlite": self._sha256(self.prod_sqlite),
        }

    def _env(self, intel: str, raw: str):
        env = {
            "FIE_SERVICE_ENV": "test",
            "FIE_PRODUCTION_DB_CONTRACT": str(self.contract),
        }
        if intel is not None:
            env["FIE_DB_TARGET_INTELLIGENCE"] = intel
        if raw is not None:
            env["FIE_DB_TARGET_RAW"] = raw
        return env

    # --- boundary helpers -------------------------------------------------
    def _intel_rejected(self, env) -> FailClosedTarget:
        """Intelligence-side rehearsal boundary for the resolved spec."""
        env = dict(os.environ if env is None else env)
        env["FIE_SERVICE_ENV"] = "test"
        spec = resolve_intelligence_target(None, env)
        with self.assertRaises(FailClosedTarget) as ctx:
            assert_rehearsal_target_safe(spec, load_production_identities(env))
        return ctx.exception

    def _raw_rejected_via_get_db(self, raw_value: str) -> FailClosedTarget:
        """Raw-side boundary through db.get_db (THE writer entry point)."""
        with mock.patch.dict(os.environ, {
            "FIE_SERVICE_ENV": "test",
            "FIE_PRODUCTION_DB_CONTRACT": str(self.contract),
        }, clear=False):
            with mock.patch.object(db, "DB_PATH", raw_value), \
                 mock.patch.object(db.sqlite3, "connect") as conn, \
                 mock.patch.object(db, "_PGConnection") as pg:
                with self.assertRaises(FailClosedTarget) as ctx:
                    db.get_db()
                # zero-write invariant: no connection ever opened
                conn.assert_not_called()
                pg.assert_not_called()
        return ctx.exception


class RawLayerMatrixTests(MatrixBase):
    def test_combo_disposable_disposable_allowed(self) -> None:
        env = self._env(SAFE_INTEL, str(self.disposable_raw))
        before = self._file_witnesses()
        # both boundaries pass (allowed) ...
        for role_resolve, spec_value in (
            (resolve_intelligence_target, SAFE_INTEL),
            (resolve_raw_target, str(self.disposable_raw)),
        ):
            spec = role_resolve(None, env)
            assert_rehearsal_target_safe(spec, self.prod)
        # ... and the raw writer opens + writes ONLY the disposable store
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(db, "DB_PATH", str(self.disposable_raw)):
                db.init_db()
                conn = db.get_db()
                self.assertIsNotNone(conn)
                conn.close()
        self.assertTrue(self.disposable_raw.exists())
        # allowed combo may mutate ONLY its own raw target
        witnesses = self._file_witnesses()
        self.assertNotEqual(witnesses["disposable_raw"], before["disposable_raw"])
        self.assertEqual(witnesses["disposable_intel"], before["disposable_intel"])
        self.assertEqual(witnesses["prod_sqlite"], before["prod_sqlite"])

    def test_combo_prod_intel_disposable_raw_rejected(self) -> None:
        env = self._env(FIXTURE_PROD_INTEL, str(self.disposable_raw))
        before = self._file_witnesses()
        with mock.patch.dict(os.environ, env, clear=False):
            exc = self._intel_rejected(None)
        self.assertIn("REHEARSAL_DB_TARGET_REQUIRED", exc.reason)
        # zero-write: raw-layer disposable target neither created nor touched
        self.assertEqual(self._file_witnesses(), before)

    def test_combo_disposable_intel_prod_raw_rejected(self) -> None:
        env = self._env(SAFE_INTEL, FIXTURE_PROD_RAW)
        before = self._file_witnesses()
        exc = self._raw_rejected_via_get_db(FIXTURE_PROD_RAW)
        self.assertIn("REHEARSAL_DB_TARGET_REQUIRED", exc.reason)
        # zero-write: nothing created/mutated anywhere
        self.assertEqual(self._file_witnesses(), before)

    def test_combo_prod_prod_rejected(self) -> None:
        env = self._env(FIXTURE_PROD_INTEL, FIXTURE_PROD_RAW)
        before = self._file_witnesses()
        with mock.patch.dict(os.environ, env, clear=False):
            exc_int = self._intel_rejected(None)
        exc_raw = self._raw_rejected_via_get_db(FIXTURE_PROD_RAW)
        for exc in (exc_int, exc_raw):
            self.assertIn("REHEARSAL_DB_TARGET_REQUIRED", exc.reason)
        self.assertEqual(self._file_witnesses(), before)

    def test_combo_unresolved_production_identity_rejected(self) -> None:
        # no production contract readable anywhere -> cannot PROVE any
        # candidate safe -> fail closed, on both layers (the contract
        # override points ONLY at a nonexistent path — the host's real
        # contract files must never enter a hermetic test)
        before = self._file_witnesses()
        unavailable_env = {"FIE_PRODUCTION_DB_CONTRACT":
                           str(self.tmp / "no-such-contract.env")}
        with mock.patch.dict(os.environ, unavailable_env, clear=False):
            with self.assertRaises(FailClosedTarget) as ctx:
                load_production_identities(os.environ, [])
            self.assertIn("PRODUCTION_IDENTITY_UNAVAILABLE", ctx.exception.reason)
            with self.assertRaises(FailClosedTarget):
                assert_rehearsal_target_safe(
                    parse_target(SAFE_INTEL), None, env=os.environ)
            with mock.patch.object(db, "DB_PATH", str(self.disposable_raw)):
                with self.assertRaises(FailClosedTarget) as ctx:
                    db.get_db()
                self.assertIn("FAIL_CLOSED", ctx.exception.reason)
        self.assertEqual(self._file_witnesses(), before)

    def test_combo_malformed_target_rejected(self) -> None:
        before = self._file_witnesses()
        # malformed raw target through the writer entry point
        exc = self._raw_rejected_via_get_db("foo://host/value")
        self.assertIn("FAIL_CLOSED", exc.reason)
        # malformed raw target through the resolver
        with self.assertRaises(FailClosedTarget) as ctx:
            resolve_raw_target(None, {"FIE_DB_TARGET_RAW": "relative/path.db"})
        self.assertIn("MALFORMED", ctx.exception.reason)
        # relative-path CWD default class is dead, in both modes
        with mock.patch.dict(os.environ, {"FIE_SERVICE_ENV": "test",
                                          "FIE_PRODUCTION_DB_CONTRACT":
                                              str(self.contract)}, clear=False):
            with mock.patch.object(db, "DB_PATH", "macro_history.db"):
                with mock.patch.object(db.sqlite3, "connect") as conn:
                    with self.assertRaises(FailClosedTarget):
                        db.get_db()
                    conn.assert_not_called()
        self.assertEqual(self._file_witnesses(), before)

    def test_production_mode_boundary_is_business_path(self) -> None:
        """Accepted production execution keeps the D5 production default
        (guard-level, mode classification only — no write performed)."""
        from phase3.runtime_contract import classify_service_env
        self.assertEqual(classify_service_env("production"), "production")
        self.assertEqual(classify_service_env(""), "production")
        # and the raw rehearsal boundary is a no-op there (unit level)
        with mock.patch.dict(os.environ, {"FIE_SERVICE_ENV": "production"},
                             clear=False):
            self.assertEqual(
                db._assert_raw_layer_rehearsal_boundary(), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)