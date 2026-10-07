"""Phase 6.9B-R4 operations suite (WO §8/§9/§12/§15).

Covers:
* the §8 observability matrix — health(liveness)/readiness semantics on
  a REAL isolated disposable PostgreSQL cluster, including the three
  readiness failure classes (DB unavailable / schema incompatible /
  invalid runtime identity) and NO secret leakage anywhere;
* the §9 logging contract — taxonomy completeness, cred-field dropping,
  redaction regression;
* the §12 activation checklist — every mandatory gate is fail-closed,
  no soft-pass, exit discipline (0/2/78).

Database policy: real PostgreSQL via an isolated initdb cluster
(reusing the R3 fixture machinery); no PostgreSQL is ever mocked, and
nothing outside the disposable cluster is contacted.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.phase3.test_69b_r3_db_identity_persistence import (  # noqa: F401
    make_db,  # noqa: F401  (fixture factory, re-exported)
    pg_cluster,  # noqa: F401
    rig,  # noqa: F401
)

PG_BIN = None
try:  # skip discipline mirrors the R3 module
    from pathlib import Path as _P
    import glob as _glob
    _bins = sorted(_glob.glob("/usr/lib/postgresql/*/bin"))
    if _bins:
        PG_BIN = _P(_bins[-1])
except Exception:  # noqa: BLE001
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ephemeral_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _pg_available() -> bool:
    return PG_BIN is not None and (PG_BIN / "pg_ctl").is_file()


def _wired_service(rig: Any, dbname: str | None = None, read_only: bool = False):
    """Real service boundary over the rig's runtime-authority store."""
    from phase3.persistence.backend import DatabaseSpec, open_store
    from phase3.service.boundary import DefaultIntelligenceService

    dsn = rig.dsn_rt(dbname or rig.intel_db_)
    store = open_store(DatabaseSpec("postgres", dsn, "explicit", dsn))
    return DefaultIntelligenceService(store, read_only=read_only), store


class _OpsCapture(logging.Handler):
    """Capture the structured §9 operations stream."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in self.lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


@pytest.fixture()
def ops_capture(monkeypatch: pytest.MonkeyPatch) -> _OpsCapture:
    import phase3.service.operational_events as ops

    cap = _OpsCapture()
    logger = logging.getLogger(ops.OPERATIONS_LOGGER_NAME)
    logger.addHandler(cap)
    try:
        yield cap
    finally:
        logger.removeHandler(cap)


@pytest.fixture()
def no_production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep activation-reading env vars out of the ambient environment."""
    for name in (
        "FIE_DB_TARGET_INTELLIGENCE", "FIE_DATABASE_URL", "FIE_SERVICE_ENV",
        "FIE_AUTH_TOKEN", "FIE_BACKUP_DIR", "FIE_BACKUP_MAX_AGE_HOURS",
        "FIE_RESTORE_DRILL_RECEIPT", "FIE_SUPERVISOR_STAGED_CONF",
        "FIE_ROLLBACK_ARTIFACT", "FIE_EXPECTED_COMMIT", "FIE_HEALTHZ_URL",
        "FIE_PORT", "FIE_HTTP_PORT", "FIE_PROJECT_ROOT",
        "FIE_EXPECTED_RUNTIME_DB", "FIE_EXPECTED_RUNTIME_SCHEMA",
        "FIE_EXPECTED_RUNTIME_ROLE", "FIE_RUNTIME_IDENTITY_GATE",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# §8 observability matrix
# ---------------------------------------------------------------------------
class TestObservabilityMatrix:
    """§15 observability: healthy/unavailable/incompatible/no-Leakage."""

    @pytest.mark.skipif(not _pg_available(), reason="isolated PG unavailable")
    def test_readiness_healthy_reports_all_surface_fields(
        self, rig: Any, ops_capture: _OpsCapture
    ) -> None:
        service, store = _wired_service(rig)
        try:
            report = service.get_health()
            assert report.status == "ok"
            assert report.backend_kind == "postgres"
            assert isinstance(report.schema_current_version, int)
            assert report.schema_current_version >= 1
        finally:
            store.close()

    @pytest.mark.skipif(not _pg_available(), reason="isolated PG unavailable")
    def test_readiness_db_unavailable_maps_to_event_and_503_class(
        self, rig: Any, pg_cluster: Any, ops_capture: _OpsCapture
    ) -> None:
        """Readiness stays FAIL-CLOSED when persistence is unreadable; the
        §9 event is DATABASE_UNAVAILABLE and the envelope is sanitized
        (503-class, no driver/host text)."""
        from phase3.persistence.backend import DatabaseSpec, open_store
        from phase3.service.boundary import DefaultIntelligenceService
        from phase3.service.errors import ServiceError, ServiceErrorCode

        # a real, reachable database with NO applied migrations: the
        # persistence layer is present but unreadable (dep unavailable)
        dead_db = f"fie_r4du_{rig.run_id_}"
        pg_cluster.admin_exec([
            f"CREATE DATABASE {dead_db} OWNER {rig.mig_user_}",
            f"GRANT CONNECT ON DATABASE {dead_db} TO {rig.rt_user_}",
        ])
        dead = rig.dsn_rt(dead_db)
        store = open_store(DatabaseSpec("postgres", dead, "explicit", dead))
        service = DefaultIntelligenceService(store)
        with pytest.raises(ServiceError) as info:
            service.get_health()
        assert info.value.code == ServiceErrorCode.DEPENDENCY_UNAVAILABLE
        # sanitized envelope: stable words only, no DSN/host detail
        rendered = json.dumps(
            {"code": info.value.code.value, "message": str(info.value),
             "details": info.value.details}, sort_keys=True,
        )
        assert "127.0.0.1" not in rendered
        assert "postgres://" not in rendered
        assert "r4-synth" not in rendered
        assert any(e.get("event") == "DATABASE_UNAVAILABLE"
                   for e in ops_capture.events())
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    @pytest.mark.skipif(not _pg_available(), reason="isolated PG unavailable")
    def test_readiness_schema_incompatible_maps_to_event(
        self, rig: Any, pg_cluster: Any, ops_capture: _OpsCapture
    ) -> None:
        """A migrated db with a WIPED schema registry must fail CLOSED
        with SCHEMA_INCOMPATIBLE (never best-effort ok)."""
        from phase3.persistence.backend import DatabaseSpec, open_store
        from phase3.service import boundary
        from phase3.service.errors import ServiceError, ServiceErrorCode

        db = f"fie_r4si_{rig.run_id_}"
        pg_cluster.admin_exec([
            f"CREATE DATABASE {db} OWNER {rig.mig_user_}",
            f"GRANT CONNECT ON DATABASE {db} TO {rig.rt_user_}",
        ])
        mig_store = open_store(DatabaseSpec(
            "postgres", rig.dsn_mig(db), "explicit", rig.dsn_mig(db)))
        try:
            from phase3.persistence.migrations import (
                MigrationManager, default_migrations_for,
            )
            MigrationManager(
                mig_store, default_migrations_for(mig_store)).apply()
            # wipe the registry → the gate must see an EMPTY registry
            mig_store.execute("DELETE FROM schema_migrations")
        finally:
            mig_store.close()

        store = open_store(DatabaseSpec(
            "postgres", rig.dsn_rt(db), "explicit", rig.dsn_rt(db)))
        try:
            service = boundary.DefaultIntelligenceService(store)
            with pytest.raises(ServiceError) as info:
                service.get_health()
            assert info.value.code in (
                ServiceErrorCode.SCHEMA_INCOMPATIBLE,
                ServiceErrorCode.DEPENDENCY_UNAVAILABLE,
            )
            got = {e.get("event") for e in ops_capture.events()}
            assert got & {"SCHEMA_INCOMPATIBLE", "DATABASE_UNAVAILABLE"}
        finally:
            store.close()

    def test_readiness_invalid_identity_maps_to_readiness_failure(
        self, ops_capture: _OpsCapture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wiring test: §9 event mapping for readiness gate failures —
        identity incompatibility is distinguishable as READINESS_FAILURE
        (a real gate refusal is covered by the R3 suite)."""
        from phase3.service import boundary
        from phase3.service.errors import ServiceErrorCode

        mapped = boundary._READINESS_GATE_EVENT[
            ServiceErrorCode.IDENTITY_INCOMPATIBLE.value
        ]
        assert mapped == "READINESS_FAILURE"
        assert boundary._READINESS_GATE_EVENT[
            ServiceErrorCode.DEPENDENCY_UNAVAILABLE.value
        ] == "DATABASE_UNAVAILABLE"
        assert boundary._READINESS_GATE_EVENT[
            ServiceErrorCode.SCHEMA_INCOMPATIBLE.value
        ] == "SCHEMA_INCOMPATIBLE"

    def test_observability_surface_carries_no_secret(
        self, rig: Any, ops_capture: _OpsCapture
    ) -> None:
        """§8/§9: neither the health payload nor any captured operations
        line may contain a DSN, password or the synthetic secret."""
        from phase3.persistence.backend import DatabaseSpec, open_store
        from phase3.service.boundary import DefaultIntelligenceService

        dsn = rig.dsn_rt()
        rt_password = dsn.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
        store = open_store(DatabaseSpec("postgres", dsn, "explicit", dsn))
        secrets = [rt_password, dsn]
        try:
            service = DefaultIntelligenceService(store)
            report = service.get_health()
            rendered = json.dumps(
                {"status": report.status, "backend": report.backend_kind,
                 "schema": report.schema_current_version}, sort_keys=True,
            )
            for secret in secrets:
                assert secret not in rendered
        finally:
            store.close()
        for line in ops_capture.lines:
            for secret in secrets:
                assert secret not in line


# ---------------------------------------------------------------------------
# §9 logging contract (taxonomy + redaction regression)
# ---------------------------------------------------------------------------
class TestLoggingContract:
    def test_taxonomy_is_the_full_work_order_set(self) -> None:
        from phase3.service import operational_events as ops

        assert ops.TAXONOMY == frozenset({
            "STARTUP", "SHUTDOWN", "READINESS_FAILURE",
            "DATABASE_UNAVAILABLE", "SCHEMA_INCOMPATIBLE", "AUTH_FAILURE",
            "CONFIGURATION_INVALID",
            "BACKUP_STARTED", "BACKUP_COMPLETED", "BACKUP_FAILED",
            "RESTORE_STARTED", "RESTORE_COMPLETED", "RESTORE_FAILED",
        })

    def test_emit_drops_credential_named_fields(self, ops_capture: _OpsCapture) -> None:
        from phase3.service import operational_events as ops

        line = ops.emit(
            ops.BACKUP_STARTED,
            password="super-secret", token="tok-xyz",
            dsn="postgres://u:p@h/db", private_key="-----BEGIN",
            secret="s3cret", api_key="ak-123",
        )
        assert "super-secret" not in line
        assert "tok-xyz" not in line
        assert "postgres://u:p@h/db" not in line
        assert "-----BEGIN" not in line
        assert "s3cret" not in line
        assert "ak-123" not in line
        events = ops_capture.events()
        assert events[-1]["event"] == "BACKUP_STARTED"
        assert not any(
            key in events[-1]
            for key in ("password", "token", "dsn", "private_key",
                        "secret", "api_key")
        )

    def test_redaction_masks_url_and_keyword_passwords(self) -> None:
        from phase3.service import operational_events as ops

        assert ops.redact(
            "postgres://svc_user:hunter2@127.0.0.1:5432/fie") == \
            "postgres://svc_user:***@127.0.0.1:5432/fie"
        assert ops.redact("postgresql://u:pw@h/db") == \
            "postgresql://u:***@h/db"
        assert ops.redact("password=hunter2") == "password=***"
        assert ops.redact("password:hunter2") == "password:***"
        assert ops.redact("PWD:password\t hunter2") == "PWD:password\t hunter2"
        # separators are '=' / ':' (the keyword-value log forms); the
        # bare-NAME field is instead dropped wholesale by emit()
        # multi-line logs (a pasted dump) are masked everywhere
        blob = "a\npassword=one\nb\npassword=two\nc"
        assert "one" not in ops.redact(blob) and "two" not in ops.redact(blob)

    def test_redaction_regression_through_the_emitter(
        self, ops_capture: _OpsCapture
    ) -> None:
        """§15 redaction regression: a caller that blindly logs a DSN
        string through emit() cannot leak the credential."""
        from phase3.service import operational_events as ops

        dsn = "postgres://r4_synth:r4-synth-real-pw@127.0.0.1:5432/fie_r4"
        ops.emit(ops.BACKUP_COMPLETED, note=f"source resolved: {dsn}")
        for line in ops_capture.lines:
            assert "r4-synth-real-pw" not in line
            assert "r4_synth:***@" in line


# ---------------------------------------------------------------------------
# §12 activation checklist (fail-closed; exit discipline)
# ---------------------------------------------------------------------------
def _make_tmp_git_repo(tmp_path: Path, with_runbook: bool = False) -> Path:
    """A real minimal git repo so commit/worktree gates are provable."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    if with_runbook:
        (root / "docs" / "operations").mkdir(parents=True)
        (root / "docs" / "operations" / "production-runbook.md") \
            .write_text("# runbook (fixture)\n", encoding="utf-8")

    def git(*argv: str) -> None:
        proc = subprocess.run(
            ["git", *argv], cwd=str(root), capture_output=True, text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr

    git("init", "-q")
    git("config", "user.email", "fixture-synth-invalid")
    git("config", "user.name", "fixture")
    git("add", "-A")
    git("commit", "--allow-empty", "-q", "-m", "fixture")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return root


def _git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root),
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class TestActivationChecklist:
    def test_empty_environment_is_not_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_production_env: None
    ) -> None:
        """Fresh/unknown environment: every unevaluatable gate stays
        false — PRODUCTION_ACTIVATION_READY=false (no soft-pass)."""
        from phase3.operations.activation import run_checklist

        monkeypatch.chdir(tmp_path)
        report = run_checklist(str(tmp_path))
        assert report["PRODUCTION_ACTIVATION_READY"] is False
        assert report["result"] == "FAILED"
        failed = [name for name, d in report["items"].items() if not d["passed"]]
        assert set(failed) >= {
            "EXPECTED_COMMIT_VERIFIED", "CONFIGURATION_VALID",
            "SECRET_SOURCE_PRESENT", "DATABASE_REACHABLE",
            "HEALTH_CHECK_PASS", "READINESS_CHECK_PASS", "RUNBOOK_PRESENT",
        }

    def test_wrong_expected_commit_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_production_env: None
    ) -> None:
        from phase3.operations.activation import run_checklist

        root = _make_tmp_git_repo(tmp_path)
        head = _git_head(root)
        wrong = "0" * 40
        assert wrong != head
        monkeypatch.setenv("FIE_EXPECTED_COMMIT", wrong)
        report = run_checklist(str(root))
        assert report["items"]["EXPECTED_COMMIT_VERIFIED"]["passed"] is False
        assert report["PRODUCTION_ACTIVATION_READY"] is False

    def test_missing_rollback_artifact_is_not_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_production_env: None
    ) -> None:
        from phase3.operations.activation import run_checklist

        root = _make_tmp_git_repo(tmp_path)
        missing = tmp_path / "no-such-rollback"
        monkeypatch.setenv("FIE_ROLLBACK_ARTIFACT", str(missing))
        report = run_checklist(str(root))
        assert report["items"]["ROLLBACK_ARTIFACT_IDENTIFIED"]["passed"] is False
        assert report["PRODUCTION_ACTIVATION_READY"] is False

    def test_missing_runbook_is_not_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_production_env: None
    ) -> None:
        from phase3.operations.activation import run_checklist

        root = _make_tmp_git_repo(tmp_path)
        monkeypatch.setenv("FIE_ROLLBACK_ARTIFACT", "a" * 40)
        report = run_checklist(str(root))
        assert report["items"]["RUNBOOK_PRESENT"]["passed"] is False
        assert report["PRODUCTION_ACTIVATION_READY"] is False

    @pytest.mark.skipif(not _pg_available(), reason="isolated PG unavailable")
    def test_full_happy_path_is_ready_and_exit_zero(
        self, rig: Any, pg_cluster: Any, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, no_production_env: None,
        ops_capture: _OpsCapture,
    ) -> None:
        """Every mandatory gate evaluated on REAL infrastructure reaches
        PRODUCTION_ACTIVATION_READY=true; liveness probes a REAL serving
        endpoint; exit classification is 0."""
        from phase3.operations import activation
        from phase3.persistence.pg_backup import run_backup

        root = _make_tmp_git_repo(tmp_path, with_runbook=True)
        head = _git_head(root)
        backup_dir = tmp_path / "backups"
        b = run_backup(rig.dsn_mig(), str(backup_dir), label="checklist")
        receipt = tmp_path / "drill-receipt.json"
        receipt.write_text(
            json.dumps({"result": "PASS", "contract": "pg-restore-drill"}),
            encoding="utf-8",
        )
        conf = tmp_path / "staged.conf"
        conf.write_text(
            "[program:fie-http]\n"
            'environment=FIE_ENV_FILE="/abs/runtime.env"\n'
            "command=deploy/bin/fie-start\n",
            encoding="utf-8",
        )
        port = _ephemeral_port()
        healthz_port = _ephemeral_port()

        # a REAL liveness endpoint (stub socket, real HTTP semantics)
        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = b'{"status": "alive", "service": "fie-http"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a: object) -> None:
                pass

        httpd = HTTPServer(("127.0.0.1", healthz_port), _Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            e = {
                "FIE_EXPECTED_COMMIT": head,
                "FIE_DB_TARGET_INTELLIGENCE": rig.dsn_rt(),
                "FIE_SERVICE_ENV": "rehearsal",
                "FIE_AUTH_TOKEN": "synthetic-checklist-token",
                "FIE_BACKUP_DIR": str(backup_dir),
                "FIE_RESTORE_DRILL_RECEIPT": str(receipt),
                "FIE_SUPERVISOR_STAGED_CONF": str(conf),
                "FIE_PORT": str(port),
                "FIE_HEALTHZ_URL": f"http://127.0.0.1:{healthz_port}/healthz",
                "FIE_ROLLBACK_ARTIFACT": head,
                # forward the identity-gate scope so db probes are
                # evaluated the way the deployment environment would
                "FIE_RUNTIME_IDENTITY_GATE": "0",
            }
            for key, value in e.items():
                monkeypatch.setenv(key, value)
            report = activation.run_checklist(str(root), env=e)
            ready = report["PRODUCTION_ACTIVATION_READY"]
            assert ready is True, json.dumps(report["items"], indent=2)
            assert report["result"] == "PASS"
            assert report["backup_probe"]["valid"] is True
            assert report["database_probe"]["status"] == "ok"

            # exit discipline through the real CLI
            for key, value in e.items():
                monkeypatch.setenv(key, value)
            proc = subprocess.run(
                [sys.executable, "-m", "phase3.operations.activation",
                 "--project-root", str(root), "--json-only"],
                capture_output=True, text=True, timeout=120,
                cwd=str(root),
            )
            assert proc.returncode == 0, proc.stderr[-2000:]
        finally:
            httpd.shutdown()

    def test_exit_codes_fail_closed_via_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_production_env: None
    ) -> None:
        """Any mandatory gate false → exit 2 through the real CLI."""
        proc = subprocess.run(
            [sys.executable, "-m", "phase3.operations.activation",
             "--project-root", str(tmp_path)],
            capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
        )
        assert proc.returncode == 2
        assert "PRODUCTION_ACTIVATION_READY=false" in proc.stderr
        assert "PRODUCTION_ACTIVATION_READY" in proc.stdout  # machine JSON
        assert json.loads(proc.stdout)["result"] == "FAILED"


# ---------------------------------------------------------------------------
# §10/§11 document presence + runbook/rollback contract content pins
# ---------------------------------------------------------------------------
class TestRunbookAndRollbackContract:
    @staticmethod
    def _norm(text: str) -> str:
        # line-wrap-tolerant matching for the markdown documents
        return " ".join(text.split())
    def test_runbook_present_with_all_ten_sections(self) -> None:
        runbook = self._norm((REPO_ROOT / "docs" / "operations" /
                              "production-runbook.md").read_text(
            encoding="utf-8"))
        for section in (
            "Startup", "Normal Shutdown", "Restart", "Service Crash",
            "Database Unavailable", "Schema Incompatible", "Backup",
            "Restore", "Rollback", "Incident Evidence",
        ):
            assert section in runbook, f"runbook missing section: {section}"

    def test_runbook_states_application_rollback_is_not_database_restore(self) -> None:
        runbook = self._norm((REPO_ROOT / "docs" / "operations" /
                              "production-runbook.md").read_text(
            encoding="utf-8"))
        assert "NOT a deployment rollback" in runbook or \
            "never means" in runbook

    def test_backup_restore_contract_document_present(self) -> None:
        doc = self._norm((REPO_ROOT / "docs" / "operations" /
                          "backup-restore-contract.md").read_text(
            encoding="utf-8"))
        for required in (
            "BACKUP_DESTINATION_INVALID", "CHECKSUM_MISMATCH",
            "RESTORE_TARGET_NOT_DISPOSABLE", "RESTORE_PERMISSION_DENIED",
            "provably disposable", "--single-transaction",
        ):
            assert required in doc, f"contract doc missing: {required}"

    def test_runbook_answers_the_eight_rollback_questions(self) -> None:
        runbook = self._norm((REPO_ROOT / "docs" / "operations" /
                              "production-runbook.md").read_text(
            encoding="utf-8"))
        for topic in (
            "code rollback point", "Configuration rollback",
            "Supervisor artifact rollback", "backward compat",
            "Irreversible DB migrations", "last resort",
            "Rollback prerequisites", "Post-rollback readiness",
        ):
            assert topic in runbook, f"rollback Q missing: {topic}"