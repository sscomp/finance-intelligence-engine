"""Phase 6.6R4 regression suite — read-only SQLite deployment contract.

Covers work-order FIE-6R2-001: a clean, DB-only, read-only SQLite
artifact (the declared ``-v <dir>:/data:ro`` reference deployment) must
satisfy the full readiness/query contract through the production open
path, with NO undeclared WAL/SHM/journal sidecar dependency — while
every writable (producer/batch/CLI) workflow keeps its historical
behavior and immutable semantics can only be engaged explicitly.

Cell (R4-08, container level) note: the container-level reproduction
(build the reference image, deploy a clean DB-only artifact on a real
read-only mount, non-root consumer, /readyz + representative query, and
inspect the deployed data directory for created/attempted sidecars) is
executed as the mandatory Phase 6.6R4 dynamic acceptance gate; this
automated module pins the same contract at host level through the exact
production open path (SQLiteStore access modes → open_store →
FIEReferenceRuntime) plus the FIE_SQLITE_ACCESS_MODE config plumbing.

All fixtures are synthetic disposable data created in tmp dirs and
removed afterwards (no production data, no machine-specific paths).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.request

from tests.phase3.service import helpers


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------

def _produce_artifact() -> tuple[str, str]:
    """Producer path: create+finalize a synthetic artifact via the
    production bootstrap (migration + ingest + scoring), then close it.

    Returns (artifact_path, producer_dir). The producer artifact is a
    WAL-mode SQLite database (the producer profile is the historical
    writable open) whose journal mode persists in the file header.
    """
    base = tempfile.mkdtemp(prefix="fie6r4_producer_")
    db_path = os.path.join(base, "intelligence.db")
    bundle = helpers.bootstrap_state(db_path)
    bundle["service"] = None
    bundle["store"].close()
    sidecars = [f for f in os.listdir(base) if f != "intelligence.db"]
    assert sidecars == [], f"producer finalization left sidecars: {sidecars}"
    return db_path, base


def _readonly_deployment(src_db: str) -> tuple[str, str]:
    """Deploy the clean artifact DB-ONLY into a strictly read-only
    directory (dir 0555 / file 0444) — the same POSIX file-access
    semantics a non-root container consumer sees on a ``:ro`` mount.

    Returns (deployed_db_path, deploy_dir). The caller restores
    permissions (see :func:`_restore_dir`).
    """
    deploy_dir = tempfile.mkdtemp(prefix="fie6r4_ro_deploy_")
    deployed = os.path.join(deploy_dir, "intelligence.db")
    shutil.copyfile(src_db, deployed)  # main .db only — no sidecars
    os.chmod(deployed, 0o444)
    os.chmod(deploy_dir, 0o555)
    return deployed, deploy_dir


def _restore_dir(deploy_dir: str, *, keep_dir: bool = False) -> None:
    """Restore write permission so temp cleanup can delete the tree."""
    try:
        os.chmod(deploy_dir, 0o755)
    except OSError:  # pragma: no cover - best-effort cleanup
        pass


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class TestCleanDBOnlyDeployment(unittest.TestCase):
    """R4-01/02/03/04/05 — the failed topology, verified end-to-end."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src_db, cls._src_dir = _produce_artifact()
        cls._deploy_dir = None

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._deploy_dir:
            _restore_dir(cls._deploy_dir, keep_dir=True)
        shutil.rmtree(cls._src_dir, ignore_errors=True)

    def _deploy(self) -> tuple[str, str]:
        if self._deploy_dir:  # fresh read-only deployment per test
            _restore_dir(self._deploy_dir)
            shutil.rmtree(self._deploy_dir, ignore_errors=True)
        self._deploy_dir = None
        deployed, deploy_dir = _readonly_deployment(self.src_db)
        self._deploy_dir = deploy_dir
        self.addCleanup(lambda: _restore_dir(deploy_dir, keep_dir=True))
        return deployed, deploy_dir

    # R4-01 — clean DB-only read-only artifact satisfies the query
    # contract through the production store seam.
    def test_r4_01_immutable_store_query_contract(self):
        deployed, deploy_dir = self._deploy()
        from phase3.persistence.sqlite import SQLiteStore, ACCESS_IMMUTABLE

        store = SQLiteStore(deployed, access_mode=ACCESS_IMMUTABLE)
        self.addCleanup(store.close)
        self.assertEqual(
            store.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()[0], 4
        )
        self.assertEqual(store.access_mode, ACCESS_IMMUTABLE)
        # readiness probe path (get_health → counts + schema version)
        from phase3.persistence.migrations import MigrationManager

        self.assertGreaterEqual(
            int(MigrationManager(store, []).current_version()), 1
        )

    # R4-02 — no hidden sidecar dependency: before AND after real
    # service traffic, the deployment holds only the declared artifact.
    def test_r4_02_no_sidecar_dependency(self):
        deployed, deploy_dir = self._deploy()
        runtime = self._open_immutable_runtime(deployed)
        env = runtime.service.get_health()
        self.assertEqual(env.status, "ok")
        runtime.service.get_entity_intelligence("company", "2330")
        runtime.close()
        found = sorted(os.listdir(deploy_dir))
        self.assertEqual(found, ["intelligence.db"], found)
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertNotIn(
                f"intelligence.db{suffix}", found, "undeclared sidecar"
            )

    # R4-03 — read-only filesystem/mount semantics: identical open set
    # through the store seam under dir 0555 / file 0444.
    def test_r4_03_readonly_mount_semantics(self):
        deployed, deploy_dir = self._deploy()
        from phase3.persistence.sqlite import (
            SQLiteStore,
            ACCESS_IMMUTABLE,
            ACCESS_READONLY,
        )

        # immutable_snapshot: succeeds on the strictly read-only
        # deployment without creating or requiring any sidecar state.
        store = SQLiteStore(deployed, access_mode=ACCESS_IMMUTABLE)
        self.addCleanup(store.close)
        self.assertEqual(
            store.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0], 3
        )
        self.assertEqual(sorted(os.listdir(deploy_dir)), ["intelligence.db"])
        # readonly (mode=ro): the WAL-header artifact legitimately needs
        # writable sidecar space — absent on this deployment, SQLite
        # raises OperationalError (documented, not silenced).
        with self.assertRaises(sqlite3.OperationalError):
            ro_store = SQLiteStore(deployed, access_mode=ACCESS_READONLY)
            try:
                ro_store.execute("SELECT COUNT(*) FROM signal_log").fetchone()
            finally:
                ro_store.close()
        self.assertEqual(sorted(os.listdir(deploy_dir)), ["intelligence.db"])

    # R4-04 — /readyz returns the documented success schema for the
    # valid clean artifact (full HTTP stack).
    def test_r4_04_readyz_success_schema(self):
        deployed, _ = self._deploy()
        server, port, runtime = _serve_immutable(deployed)
        try:
            status, env, headers = _full("http://127.0.0.1:%d/readyz" % port)
            self.assertEqual(status, 200)
            self.assertEqual(env["status"], "ok")
            self.assertEqual(env["kind"], "health")
            self.assertIn(headers.get("Content-Type", ""), (
                "application/json; charset=utf-8",
                "application/json",
            ))
            payload = env["payload"]
            self.assertEqual(payload["counts"]["scores"], 4)
            self.assertEqual(payload["counts"]["signals"], 3)
            self.assertGreaterEqual(payload["schema_current_version"], 1)
        finally:
            server.shutdown()
            server.server_close()
            runtime.close()

    # R4-05 — representative real intelligence query (not SELECT 1).
    def test_r4_05_representative_intelligence_query(self):
        deployed, _ = self._deploy()
        server, port, runtime = _serve_immutable(deployed)
        try:
            status, env, _headers = _full(
                "http://127.0.0.1:%d/v1/intelligence/entity/company/2330" % port
            )
            self.assertEqual(status, 200)
            self.assertEqual(env["kind"], "entity_intelligence")
            payload = env["payload"]
            self.assertEqual(payload["entity_id"], "2330")
            self.assertIsNotNone(payload.get("score"))
        finally:
            server.shutdown()
            server.server_close()
            runtime.close()

    # production runtime open helper (immutable snapshot contract)
    def _open_immutable_runtime(self, db_path: str):
        from phase3.transport.http import FIEReferenceRuntime
        from phase3.persistence.sqlite import ACCESS_IMMUTABLE

        runtime = FIEReferenceRuntime.open(db_path, access_mode=ACCESS_IMMUTABLE)
        self.addCleanup(runtime.close)
        return runtime


# ---------------------------------------------------------------------------
# helper: build a /readyz-capable HTTP server over an immutable runtime
# ---------------------------------------------------------------------------

def _serve_immutable(db_path: str):
    """Full transport stack (FIEReferenceRuntime + make_http_server) on an
    ephemeral loopback port, opened with the immutable_snapshot contract."""
    from phase3.transport.http import FIEReferenceRuntime, make_http_server
    from phase3.transport.config import TransportConfig
    from phase3.persistence.sqlite import ACCESS_IMMUTABLE

    runtime = FIEReferenceRuntime.open(db_path, access_mode=ACCESS_IMMUTABLE)
    server = make_http_server(runtime, TransportConfig(), host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, runtime


def _full(url: str) -> tuple[int, dict, dict]:
    """HTTP GET returning (status, JSON-envelope, headers)."""
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - loopback test server
        return (
            resp.status,
            json.loads(resp.read().decode("utf-8")),
            dict(resp.headers),
        )


# ---------------------------------------------------------------------------
# R4-06 — writable workflows unaffected
# ---------------------------------------------------------------------------

class TestWritableWorkflowsUnaffected(unittest.TestCase):
    """Default store behavior is byte-for-byte the historical profile."""

    def test_r4_06_default_profile_unchanged(self):
        from phase3.persistence.sqlite import (
            SQLiteStore,
            ACCESS_WRITABLE,
            ACCESS_MODES,
        )

        base = tempfile.mkdtemp(prefix="fie6r4_rw_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        db_path = os.path.join(base, "writable.db")
        store = SQLiteStore(db_path)  # default: writable, file auto-created
        self.addCleanup(store.close)
        self.assertEqual(store.access_mode, ACCESS_WRITABLE)
        self.assertEqual(store.pragma("journal_mode"), "wal")
        self.assertEqual(int(store.pragma("busy_timeout")), 5000)
        self.assertEqual(int(store.pragma("foreign_keys")), 1)
        self.assertEqual(ACCESS_MODES, ("writable", "readonly", "immutable_snapshot"))

    def test_r4_06_writable_transaction_path_works(self):
        from phase3.persistence.sqlite import SQLiteStore

        base = tempfile.mkdtemp(prefix="fie6r4_rw_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        store = SQLiteStore(os.path.join(base, "writable.db"))
        self.addCleanup(store.close)
        store.executescript("CREATE TABLE fie6r4_probe (id INTEGER PRIMARY KEY, v TEXT)")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO fie6r4_probe (v) VALUES (?)", ("synthetic",)
            )
        self.assertEqual(
            store.execute("SELECT COUNT(*) FROM fie6r4_probe").fetchone()[0], 1
        )

    def test_r4_06_wal_artifact_copy_behavior_unchanged(self):
        # a writable deployment (default mode) over the WAL-header
        # artifact keeps working with sidecar space available — the
        # producer/batch world does not change
        src_db, src_dir = _produce_artifact()
        self.addCleanup(shutil.rmtree, src_dir, ignore_errors=True)
        from phase3.persistence.sqlite import SQLiteStore

        store = SQLiteStore(src_db)  # writable default, original producer dir
        self.addCleanup(store.close)
        self.assertEqual(store.pragma("journal_mode"), "wal")
        self.assertEqual(
            store.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()[0], 4
        )


# ---------------------------------------------------------------------------
# R4-07 — incorrect immutable use protection
# ---------------------------------------------------------------------------

class TestIncorrectImmutableUseProtection(unittest.TestCase):
    """immutable_snapshot is explicit-only and never touches mutable DBs."""

    def test_r4_07_unknown_access_mode_rejected_before_open(self):
        from phase3.persistence.sqlite import SQLiteStore

        missing = os.path.join(
            tempfile.mkdtemp(prefix="fie6r4_badmode_"), "nope.db"
        )
        with self.assertRaises(ValueError):
            SQLiteStore(missing, access_mode="ro")  # typo — must be rejected
        # nothing was created by the rejected open
        self.assertFalse(os.path.exists(missing))

    def test_r4_07_immutable_open_never_creates_or_modifies(self):
        src_db, src_dir = _produce_artifact()
        self.addCleanup(shutil.rmtree, src_dir, ignore_errors=True)
        from phase3.persistence.sqlite import SQLiteStore, ACCESS_IMMUTABLE

        before = _sha256(src_db)
        store = SQLiteStore(src_db, access_mode=ACCESS_IMMUTABLE)
        store.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()
        store.close()
        self.assertEqual(_sha256(src_db), before)
        from phase3.persistence.migrations import MigrationManager
        # (migration read worked on the immutable snapshot too)
        store = SQLiteStore(src_db, access_mode=ACCESS_IMMUTABLE)
        self.addCleanup(store.close)
        self.assertGreaterEqual(int(MigrationManager(store, []).current_version()), 1)

    def test_r4_07_immutable_open_of_missing_artifact_fails(self):
        from phase3.persistence.sqlite import SQLiteStore, ACCESS_IMMUTABLE

        base = tempfile.mkdtemp(prefix="fie6r4_missing_")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        target = os.path.join(base, "absent.db")
        with self.assertRaises(sqlite3.OperationalError):
            store = SQLiteStore(target, access_mode=ACCESS_IMMUTABLE)
            store.close()
        self.assertFalse(os.path.exists(target), "read-only open must not create files")

    def test_r4_07_pg_spec_rejects_sqlite_access_mode(self):
        from phase3.persistence.backend import open_store, resolve_spec

        spec = resolve_spec("postgres://u:pw@localhost:5432/fie6r4_disposable")
        with self.assertRaises(ValueError):
            open_store(spec, access_mode="immutable_snapshot")
        with self.assertRaises(ValueError):
            open_store(spec, access_mode="readonly")


# ---------------------------------------------------------------------------
# R4-08 (config plumbing arm) — FIE_SQLITE_ACCESS_MODE contract
# ---------------------------------------------------------------------------

class TestAccessModeConfigPlumbing(unittest.TestCase):
    """Env contract: FIE_SQLITE_ACCESS_MODE → TransportConfig → runtime open."""

    def setUp(self) -> None:
        self._saved = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)

    def test_env_immutable_snapshot_plumbs_to_config(self):
        from phase3.transport.config import (
            load_transport_config,
            FIE_SQLITE_ACCESS_MODE,
        )

        os.environ[FIE_SQLITE_ACCESS_MODE] = "immutable_snapshot"
        config = load_transport_config()
        self.assertEqual(config.sqlite_access_mode, "immutable_snapshot")
        self.assertIn("sqlite_access_mode", config.to_dict())
        self.assertEqual(config.to_dict()["sqlite_access_mode"], "immutable_snapshot")

    def test_invalid_env_falls_back_to_writable(self):
        from phase3.transport.config import (
            load_transport_config,
            FIE_SQLITE_ACCESS_MODE,
        )
        from phase3.persistence.sqlite import ACCESS_WRITABLE

        os.environ[FIE_SQLITE_ACCESS_MODE] = "not-a-mode"
        config = load_transport_config()
        self.assertEqual(config.sqlite_access_mode, ACCESS_WRITABLE)

    def test_env_readonly_plumbs_to_config(self):
        from phase3.transport.config import (
            load_transport_config,
            FIE_SQLITE_ACCESS_MODE,
        )

        os.environ[FIE_SQLITE_ACCESS_MODE] = "readonly"
        self.assertEqual(load_transport_config().sqlite_access_mode, "readonly")

    def test_access_mode_vocabulary_matches_store_contract(self):
        # transport may import no persistence seam beyond `backend`; its
        # mirrored vocabulary is therefore cross-pinned to the store's
        from phase3.transport import config as transport_config
        from phase3.persistence.sqlite import ACCESS_MODES

        self.assertEqual(
            transport_config.VALID_ACCESS_MODES, tuple(ACCESS_MODES)
        )

    def test_runtime_open_honours_immutable_on_readonly_deployment(self):
        # the full env→config→runtime chain over the failed topology
        from phase3.transport.http import FIEReferenceRuntime
        from phase3.persistence.sqlite import ACCESS_IMMUTABLE

        src_db, src_dir = _produce_artifact()
        self.addCleanup(shutil.rmtree, src_dir, ignore_errors=True)
        deployed, deploy_dir = _readonly_deployment(src_db)
        self.addCleanup(lambda: _restore_dir(deploy_dir, keep_dir=True))
        runtime = FIEReferenceRuntime.open(deployed, access_mode=ACCESS_IMMUTABLE)
        self.addCleanup(runtime.close)
        health = runtime.service.get_health()
        self.assertEqual(health.status, "ok")
        self.assertEqual(health.backend_kind, "sqlite")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()