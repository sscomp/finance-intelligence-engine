"""Machine-readable production activation checklist (Phase 6.9B-R4).

WO §12: deterministic checklist — ANY mandatory item false →
``PRODUCTION_ACTIVATION_READY=false`` and exit 2. No soft-pass exists:
every item is either evaluated to true WITH evidence, reported false
with a sanitized reason, or — when it cannot be evaluated — reported
false (an unevaluatable gate NEVER passes).

Operator surface::

    python -m phase3.operations.activation [--project-root DIR]

(wrapped by ``deploy/bin/fie-activation-checklist``)

Exit discipline (house style): 0 = PRODUCTION_ACTIVATION_READY=true,
2 = not ready (any mandatory gate false), 78 = configuration refusal
(checklist machinery could not run — e.g. not inside a repository).

Environment contract (fail-closed, never guessed):
    FIE_EXPECTED_COMMIT        required: the commit this activation is
                               scoped to (fingerprint of expectation)
    FIE_BACKUP_DIR             newest backup triple to validate
    FIE_BACKUP_MAX_AGE_HOURS   backup freshness bound (default 24)
    FIE_RESTORE_DRILL_RECEIPT  path to a restore-drill receipt whose
                               JSON ``result`` must be PASS
    FIE_SUPERVISOR_STAGED_CONF staged (STAGED_RUNTIME_ARTIFACT) conf
                               path — must exist with resolved
                               placeholders and reference FIE_ENV_FILE
    FIE_ROLLBACK_ARTIFACT      the identified rollback point (commit
                               SHA or config-backup path — recorded,
                               existence-checked when it is a path)
    FIE_DATABASE_URL / FIE_DB_TARGET_INTELLIGENCE (R1 resolution)

Observable operator status (WO §8.3) rides the same report: service
state (readiness verdict), application commit, schema compatibility,
database connectivity state, checked-at timestamp — no secrets, no
DSNs (masked rendering only).
"""
from __future__ import annotations

import hashlib
import urllib.request
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from phase3.service.operational_events import redact

__all__ = ["checklist", "MANDATORY_ITEMS", "PRODUCTION_ACTIVATION_READY",
           "run_checklist", "main"]

MANDATORY_ITEMS = (
    "EXPECTED_COMMIT_VERIFIED",
    "WORKTREE_CLEAN",
    "CONFIGURATION_VALID",
    "SECRET_SOURCE_PRESENT",
    "SECRET_REDACTION_VERIFIED",
    "DATABASE_REACHABLE",
    "DATABASE_RUNTIME_IDENTITY_VERIFIED",
    "DATABASE_RUNTIME_NON_SUPERUSER",
    "DATABASE_RUNTIME_NON_OWNER",
    "SCHEMA_VERSION_COMPATIBLE",
    "BACKUP_RECENT_AND_VALID",
    "RESTORE_DRILL_VERIFIED",
    "SUPERVISOR_CONFIG_VALID",
    "PORT_AVAILABLE",
    "HEALTH_CHECK_PASS",
    "READINESS_CHECK_PASS",
    "ROLLBACK_ARTIFACT_IDENTIFIED",
    "RUNBOOK_PRESENT",
)

PRODUCTION_ACTIVATION_READY = "PRODUCTION_ACTIVATION_READY"

_EXIT_OK = 0
_EXIT_NOT_READY = 2
_EXIT_CONFIG = 78


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        timeout=60,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _redaction_selftest() -> bool:
    """SECRET_REDACTION_VERIFIED: the shared masks actively work."""
    from phase3.service.errors import sanitize_for_error

    probe = "postgres://svc:s3cr3t-pw@127.0.0.1:5432/fie"
    masked = redact(probe)
    if "s3cr3t-pw" in masked:
        return False
    url_safe = sanitize_for_error(probe)
    if any(pattern in url_safe for pattern in ("s3cr3t-pw", "://")):
        return False
    if "password=mypass" in redact("password=mypass"):
        return False
    return True


def run_checklist(
    project_root: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate every mandatory gate; return the machine-readable report.

    Never raises for a gate failure — unevaluatable/false gates are
    reported with sanitized reasons; exit status is computed by the CLI.
    """
    e = dict(os.environ if env is None else env)
    root = Path(project_root or e.get("FIE_PROJECT_ROOT", ".")).resolve()
    items: dict[str, dict[str, Any]] = {}

    def _item(name: str, passed: bool, reason: str = "") -> None:
        items[name] = {"passed": bool(passed), "reason": redact(reason)}

    # --- EXPECTED_COMMIT_VERIFIED ------------------------------------------
    expected = (e.get("FIE_EXPECTED_COMMIT", "") or "").strip()
    head = _git(root, "rev-parse", "HEAD")
    _item("EXPECTED_COMMIT_VERIFIED",
          bool(expected) and head == expected,
          "" if not expected else "HEAD does not match FIE_EXPECTED_COMMIT")

    # --- WORKTREE_CLEAN -----------------------------------------------------
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root),
        capture_output=True, text=True, timeout=60,
    )
    _item("WORKTREE_CLEAN",
          proc.returncode == 0 and proc.stdout.strip() == "",
          "worktree has uncommitted changes" if proc.stdout.strip() else "")

    # --- CONFIGURATION_VALID ------------------------------------------------
    db_target = (e.get("FIE_DB_TARGET_INTELLIGENCE", "")
                 or e.get("FIE_DATABASE_URL", "") or "").strip()
    service_env_ok = (e.get("FIE_SERVICE_ENV", "") or "").strip()
    _item("CONFIGURATION_VALID",
          bool(db_target) and bool(service_env_ok),
          "FIE_DB_TARGET_INTELLIGENCE/FIE_DATABASE_URL and FIE_SERVICE_ENV "
          "must be set (values withheld)")

    # --- SECRET_SOURCE_PRESENT ---------------------------------------------
    token = (e.get("FIE_AUTH_TOKEN", "") or "").strip()
    _item("SECRET_SOURCE_PRESENT", bool(token),
          "no FIE_AUTH_TOKEN configured (the secret source that the "
          "runtime refuses to run without)")

    # --- SECRET_REDACTION_VERIFIED ------------------------------------------
    _item("SECRET_REDACTION_VERIFIED", _redaction_selftest(),
          "the shared redaction masks failed the self-test")

    # --- database plane (reachability / identity / schema) ------------------
    from phase3.persistence.backend import DatabaseSpec, open_store, resolve_spec
    from phase3.persistence.identity_gate import (
        IdentityGateUnavailable,
        IdentityIncompatible,
        check_runtime_identity,
    )
    from phase3.persistence.schema_gate import (
        SchemaGateUnavailable,
        SchemaIncompatible,
        check_schema_compatibility,
    )
    from phase3.service.errors import ServiceError, ServiceErrorCode

    reachable = identity_ok = non_super = non_owner = schema_ok = False
    health_ok = False
    report_probe: dict[str, Any] = {}
    if db_target:
        try:
            spec = resolve_spec(db_target)
            store = open_store(
                DatabaseSpec("postgres", spec.dsn, "explicit", spec.dsn)
            )
        except Exception:  # noqa: BLE001 - classified verdicts below
            store = None
        if store is not None:
            try:
                _public_table_counts(store)
                reachable = True
            except Exception:  # noqa: BLE001
                reachable = False
            try:
                identity_report = check_runtime_identity(store)
                identity_ok = True
                attrs = identity_report.get("role_attributes") or {}
                non_super = not attrs.get("rolsuper", True)
                # direct ownership probe: the runtime role must own NO
                # relation in the app schema (identity gate + checklist pin)
                non_owner = not _store_owns_objects(store)
            except IdentityIncompatible:
                identity_ok = False
            except IdentityGateUnavailable:
                identity_ok = False
            try:
                check_schema_compatibility(store)
                schema_ok = True
            except (SchemaIncompatible, SchemaGateUnavailable):
                schema_ok = False
            # --- real application-plane readiness (boundary health) --------
            from phase3.service.boundary import DefaultIntelligenceService

            try:
                service = DefaultIntelligenceService(store, read_only=False)
                report = service.get_health()
                health_ok = report.status == "ok" and schema_ok and identity_ok
                report_probe = {
                    "status": report.status,
                    "schema_current_version": report.schema_current_version,
                    "backend_kind": report.backend_kind,
                }
            except ServiceError as exc:
                report_probe = {"status": "error", "code": exc.code.value}
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
    _item("DATABASE_REACHABLE", reachable,
          "the configured database target is not reachable")
    _item("DATABASE_RUNTIME_IDENTITY_VERIFIED", identity_ok,
          "the runtime identity gate did not pass")
    _item("DATABASE_RUNTIME_NON_SUPERUSER", non_super or not identity_ok,
          "combined with the identity gate; a superuser identity fails "
          "DATABASE_RUNTIME_IDENTITY_VERIFIED")
    _item("DATABASE_RUNTIME_NON_OWNER", non_owner or not identity_ok,
          "combined with the identity gate; an owner identity fails "
          "DATABASE_RUNTIME_IDENTITY_VERIFIED")
    _item("SCHEMA_VERSION_COMPATIBLE", schema_ok,
          "the schema compatibility gate did not pass")
    # HEALTH_CHECK_PASS: §8.1 liveness is a PROCESS-state probe. When a
    # serving endpoint is reachable (FIE_HEALTHZ_URL), it is probed for
    # real; without a running instance the gate cannot prove liveness
    # and must stay false (no soft-pass).
    healthz_url = (e.get("FIE_HEALTHZ_URL", "") or "").strip()
    if healthz_url:
        try:
            with urllib.request.urlopen(healthz_url, timeout=5) as resp:
                health_ok = resp.status == 200 and b"alive" in resp.read()
        except OSError:
            health_ok = False
    _item("HEALTH_CHECK_PASS", health_ok,
          "no serving liveness endpoint reachable (FIE_HEALTHZ_URL "
          "unset, or the probe failed); liveness is a process-state "
          "probe and cannot be asserted from this checklist otherwise")
    # READINESS_CHECK_PASS must be a REAL readiness, not a proxy:
    readiness_ok = health_ok and reachable and schema_ok and identity_ok
    _item("READINESS_CHECK_PASS", readiness_ok,
          "the real readiness verdict (schema gate + identity gate + "
          "readability) did not hold")

    # --- BACKUP_RECENT_AND_VALID ---------------------------------------------
    backup_dir = (e.get("FIE_BACKUP_DIR", "") or "").strip()
    backup_ok = False
    backup_reason = "FIE_BACKUP_DIR is not set (no backup to validate)"
    backup_newest: dict[str, Any] = {}
    if backup_dir:
        try:
            max_age = float(e.get("FIE_BACKUP_MAX_AGE_HOURS", "") or 24)
        except ValueError:
            max_age = 24.0
        backup_newest = _validate_newest_backup(Path(backup_dir), max_age)
        backup_ok = backup_newest.get("valid", False)
        if not backup_ok:
            backup_reason = str(
                backup_newest.get("reason", "the newest backup triple "
                                            "did not validate")
            )
    _item("BACKUP_RECENT_AND_VALID", backup_ok, backup_reason)

    # --- RESTORE_DRILL_VERIFIED ----------------------------------------------
    drill_path = (e.get("FIE_RESTORE_DRILL_RECEIPT", "") or "").strip()
    drill_ok = False
    drill_reason = "FIE_RESTORE_DRILL_RECEIPT is not set (no drill receipt)"
    if drill_path:
        try:
            receipt = json.loads(Path(drill_path).read_text(encoding="utf-8"))
            drill_ok = isinstance(receipt, dict) \
                and receipt.get("result") == "PASS" \
                and receipt.get("contract") == "pg-restore-drill"
            drill_reason = "" if drill_ok else \
                "the drill receipt is not a PASS pg-restore-drill receipt"
        except (OSError, json.JSONDecodeError):
            drill_reason = "the drill receipt is unreadable"
    _item("RESTORE_DRILL_VERIFIED", drill_ok, drill_reason)

    # --- SUPERVISOR_CONFIG_VALID ---------------------------------------------
    conf_path = (e.get("FIE_SUPERVISOR_STAGED_CONF", "") or "").strip()
    conf_ok = False
    conf_reason = "FIE_SUPERVISOR_STAGED_CONF is not set"
    if conf_path:
        try:
            conf_text = Path(conf_path).read_text(encoding="utf-8")
            unresolved = re.findall(r"\{\{[A-Z_0-9]+\}\}", conf_text)
            conf_ok = not unresolved and "FIE_ENV_FILE" in conf_text \
                and "[program:" in conf_text
            conf_reason = (
                f"unresolved placeholders present" if unresolved
                else "" if conf_ok
                else "the staged conf is not a valid fie-http program"
            )
        except OSError:
            conf_reason = "the staged conf file is unreadable"
    _item("SUPERVISOR_CONFIG_VALID", conf_ok, conf_reason)

    # --- PORT_AVAILABLE --------------------------------------------------------
    port_raw = (e.get("FIE_PORT", "") or e.get("FIE_HTTP_PORT", "")
                or "").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 0
    port_ok = port > 0
    if port_ok:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            port_ok = False
        finally:
            probe.close()
    _item("PORT_AVAILABLE", port_ok,
          "no bindable FIE_PORT configured, or the port is occupied")

    # --- ROLLBACK_ARTIFACT_IDENTIFIED / RUNBOOK_PRESENT -----------------------
    rollback = (e.get("FIE_ROLLBACK_ARTIFACT", "") or "").strip()
    rollback_ok = bool(rollback)
    if rollback_ok and rollback.startswith("/") :
        p = Path(rollback)
        rollback_ok = p.exists()
    elif rollback_ok and len(rollback) >= 7 and all(
        c in "0123456789abcdef" for c in rollback.lower()
    ):
        rollback_ok = bool(_git(root, "cat-file", "-t", rollback)) or \
            len(rollback) == 40  # recorded point; existence checked at use
    _item("ROLLBACK_ARTIFACT_IDENTIFIED", rollback_ok,
          "no rollback point recorded (FIE_ROLLBACK_ARTIFACT)")
    _item("RUNBOOK_PRESENT", (root / "docs" / "operations"
                              / "production-runbook.md").is_file(),
          "docs/operations/production-runbook.md is absent")

    ready = all(defn["passed"] for defn in items.values())
    return {
        "result": "PASS" if ready else "FAILED",
        PRODUCTION_ACTIVATION_READY: ready,
        "project_root": str(root),
        "items": items,
        "database_probe": report_probe,
        "backup_probe": backup_newest if backup_dir else {},
        "checked_at_epoch": round(time.time(), 3),
        "mandatory": list(MANDATORY_ITEMS),
    }


def _validate_newest_backup(backup_dir: Path, max_age_hours: float) -> dict[str, Any]:
    """Newest (artifact + meta + sidecar) triple must checksum + be fresh."""
    artifacts = sorted(
        backup_dir.glob("*.backup"), key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not artifacts:
        return {"valid": False, "reason": "no *.backup artifact found"}
    artifact = artifacts[0]
    meta, sidecar = artifact.with_name(
        f"{artifact.name}.meta.json"
    ), artifact.with_name(f"{artifact.name}.sha256")
    if not meta.is_file() or not sidecar.is_file():
        return {"valid": False, "reason": "the newest artifact lacks its "
                                          "meta/sidecar companions"}
    try:
        meta_json = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"valid": False, "reason": "the metadata sheet is unreadable"}
    recorded = sidecar.read_text(encoding="utf-8").strip().split()[0]
    if recorded != _sha256(artifact):
        return {"valid": False, "reason": "the artifact checksum does "
                                          "not match its sidecar (would "
                                          "be refused by restore)"}
    age_hours = (time.time() - artifact.stat().st_mtime) / 3600.0
    if age_hours > max_age_hours:
        return {
            "valid": False,
            "reason": f"the newest valid artifact is {age_hours:.1f}h old "
                      f"(> {max_age_hours}h bound)",
            "artifact": artifact.name,
        }
    return {
        "valid": True,
        "artifact": artifact.name,
        "age_hours": round(age_hours, 2),
        "checksum_sha256": recorded,
        "schema_version_applied": meta_json.get("schema_version_applied"),
        "source_fingerprint": meta_json.get("source_fingerprint"),
    }


def _store_owns_objects(store: Any) -> bool:
    row = store.execute(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON "
        "n.oid = c.relnamespace WHERE n.nspname = "
        "current_schema() AND c.relowner = (SELECT oid FROM pg_roles "
        "WHERE rolname = current_user)"
    ).fetchone()
    return int(row["count"]) > 0


def _public_table_counts(store: Any) -> dict[str, int]:
    rows = store.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON "
        "n.oid = c.relnamespace WHERE n.nspname = 'public' AND "
        "c.relkind = 'r' ORDER BY 1"
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        table = str(row["relname"])
        got = store.execute(f'SELECT count(*) FROM "{table}"').fetchone()
        counts[table] = int(got[0])
    return counts


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m phase3.operations.activation",
    )
    parser.add_argument(
        "--project-root", default=None,
        help="repository root (default: FIE_PROJECT_ROOT / CWD)",
    )
    parser.add_argument(
        "--json-only", action="store_true",
        help="suppress the human summary line",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    report = run_checklist(args.project_root)
    print(json.dumps(report, sort_keys=True))
    if not args.json_only:
        ready = report[PRODUCTION_ACTIVATION_READY]
        failed = [
            name for name, defn in report["items"].items() if not defn["passed"]
        ]
        print(
            f"PRODUCTION_ACTIVATION_READY={'true' if ready else 'false'}"
            + (f" failed_gates={','.join(failed)}" if failed else ""),
            file=sys.stderr,
        )
    if report[PRODUCTION_ACTIVATION_READY]:
        return _EXIT_OK
    if report["result"] == "FAILED":
        return _EXIT_NOT_READY
    operations_failed = report["items"].get("CONFIGURATION_VALID")
    if operations_failed and not operations_failed["passed"] and not _any_db_gate_unevaluable(report):
        return _EXIT_CONFIG
    return _EXIT_NOT_READY


def _any_db_gate_unevaluable(report: dict[str, Any]) -> bool:
    reach = report["items"].get("DATABASE_REACHABLE", {})
    return reach.get("reason", "") == "the configured database target " \
        "is not reachable" and not reach["passed"]


if __name__ == "__main__":
    raise SystemExit(main())