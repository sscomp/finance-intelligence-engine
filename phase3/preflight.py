"""FIE deployment preflight — Phase 6.9B-R2 (WO §8).

ONE deterministic pre-flight command for an operator or deployment
script. Validates the serve-plane runtime contract in the caller's
environment and prints a sanitized JSON report:

    PRECHECK_RESULT=PASS  (exit 0)
    PRECHECK_RESULT=FAIL  (exit 2, refusals listed)

Contract (what preflight validates):

* configuration is parseable (the exact ``load_transport_config`` startup
  path — same resolution precedence as the canonical entrypoint);
* the Phase 3 kill switch is present and valid (``enabled.yaml`` — R1
  fail-closed semantics);
* the required DB targets are EXPLICIT (canonical or legacy alias — never
  a CWD fallback; explicitness only, never a connection attempt);
* authentication material is referenced/available — presence boolean only;
* host/port are valid;
* runtime directories (config/data) exist and are usable by the runtime
  user — read-only access probes, nothing is mutated;
* the interpreter can import the runtime packages (imports only — no
  database connection, no bind);
* no prohibited production fallback occurs (resolved config/data roots
  are environment- or repo-derived, never derived from ``os.getcwd()``;
  an implicit production profile refusal propagates as-is).

Preflight MUST NOT (and does not):

* mutate schema;
* contact any database (explicitness is parsed, not dialed);
* start the long-running service;
* print secrets — values are withheld; refusal codes and variable names
  only (ADR-017), DSNs only in the masked ``...:***@`` form.

Usage: ``python -m phase3.preflight`` (JSON report on stdout, exit 0/2),
wrapped by ``deploy/scripts/fie-preflight`` for operators.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _json_fail(refusals: list[str], report: dict) -> int:
    report["precheck_result"] = "FAIL"
    report["refusals"] = refusals
    print(json.dumps(report, sort_keys=True))
    return 2


def _mask_dsn(spec: object) -> str:
    """Masked, log-safe rendering of a resolved DB spec (R1 discipline)."""
    text = str(spec)
    if "@" in text and "://" in text:
        scheme, _, rest = text.partition("://")
        creds, _, hostpart = rest.rpartition("@")
        user = creds.split(":", 1)[0] if creds else creds
        return f"{scheme}://{user}:***@{hostpart}"
    return "<non-dsn>"


def _db_role_presence(env: dict[str, str], role: str) -> dict:
    """Presence/source/legacy-alias metadata for one DB role (no values)."""
    canonical_by_role = {
        "raw": "FIE_DB_TARGET_RAW",
        "intelligence": "FIE_DB_TARGET_INTELLIGENCE",
        "test": "FIE_DB_TARGET_TEST",
        "rollback": "FIE_DB_TARGET_ROLLBACK",
    }
    legacy_by_role = {
        "raw": ["FIE_DB_PATH"],
        "intelligence": ["FIE_DATABASE_URL", "FIE_INTELLIGENCE_DB"],
        "test": [],
        "rollback": [],
    }
    canonical = canonical_by_role.get(role, "")
    if env.get(canonical):
        return {"env_var": canonical, "present": True, "source": "canonical"}
    for alias in legacy_by_role.get(role, []):
        if env.get(alias):
            return {
                "env_var": alias,
                "present": True,
                "source": "legacy_alias",
                "legacy_alias_in_use": True,
            }
    return {
        "env_var": canonical,
        "present": False,
        "source": "absent",
        "refusal": "DB target not explicitly supplied",
    }


def run_preflight(env: dict[str, str] | None = None) -> tuple[dict, int]:
    """Execute the preflight checks against ``env`` (default: os.environ).

    Returns (report, exit_code) — no printing, no mutation; callers do
    the printing. Pure/observable so tests can pin every refusal.
    """
    env = dict(env) if env is not None else dict(os.environ)
    refusals: list[str] = []
    report: dict = {
        "precheck_result": "PASS",
        "version": "6.9B-R2",
        "secrets_printed": False,
    }
    checks: dict = {}

    # 1/2/5 — configuration parseable, host/port valid (startup path).
    config = None
    try:
        from phase3.transport.config import load_transport_config

        config = load_transport_config()
        checks["config_valid"] = True
        checks["transport"] = {
            "host": config.host,
            "port": config.port,
            "auth_mode": config.auth_mode,
            "service_env": config.service_env,
            "log_level": config.log_level,
            "request_timeout": config.request_timeout,
            "sqlite_access_mode": config.sqlite_access_mode,
        }
    except Exception as exc:  # noqa: BLE001 - refusal type is what we report
        code = getattr(exc, "code", "CONFIG_INVALID")
        checks["config_valid"] = False
        refusals.append(str(code))

    # 3 — required DB targets explicit (presence/source; never dialed).
    db_roles = {}
    for role in ("raw", "intelligence"):
        role_state = _db_role_presence(env, role)
        db_roles[role] = role_state
        if not role_state["present"]:
            refusals.append(f"{role}_DB_TARGET_NOT_EXPLICIT")
    checks["db_targets"] = db_roles
    if config is not None:
        # masked resolved DSN (log-safe; value withheld) — serves as the
        # "which target did the contract select" observability surface
        runtime = getattr(config, "runtime", {}) or {}
        checks["database_spec_masked"] = _mask_dsn(runtime.get("database_spec", ""))
        checks["database_source"] = runtime.get("database_source", "unknown")

    # 4 — auth material referenced/available (presence boolean only).
    if config is not None:
        auth_ok = config.auth_mode != "token" or bool(config.auth_token)
        checks["auth_material_available"] = auth_ok
        checks["auth_mode"] = config.auth_mode
        if not auth_ok:
            refusals.append("AUTH_CREDENTIAL_MISSING")
        # (auth_token itself is NEVER included in the report)

    # 6 — runtime directories usable (probe-only; nothing is created).
    try:
        from phase3.paths import config_dir, data_dir

        probes = {}
        for label, path in (
            ("config_dir", Path(config_dir())),
            ("data_dir", Path(data_dir())),
        ):
            probes[label] = {
                "path": str(path),
                "exists": path.is_dir(),
                "writable": os.access(path, os.W_OK) if path.is_dir() else False,
            }
            if not probes[label]["writable"] and path.is_dir():
                refusals.append(f"RUNTIME_DIR_NOT_WRITABLE_{label.upper()}")
            if not path.is_dir():
                refusals.append(f"RUNTIME_DIR_MISSING_{label.upper()}")
        checks["runtime_dirs"] = probes
    except Exception as exc:  # noqa: BLE001
        refusals.append(f"RUNTIME_DIRS_UNRESOLVED:{type(exc).__name__}")

    # 2 — kill switch valid (R1 fail-closed semantics at the same root).
    try:
        from phase3.config.loader import ConfigLoader, DEFAULT_CONFIG_ROOT

        root = Path(DEFAULT_CONFIG_ROOT)
        checks["kill_switch"] = {
            "config_root": str(root),
            "valid": True,  # flipped below on refusal
        }
        try:
            enabled = ConfigLoader(root=root)._read_enabled()
            checks["kill_switch"]["enabled"] = bool(enabled)
            checks["kill_switch"]["state"] = (
                "enabled" if enabled else "disabled"
            )
        except ValueError as exc:
            checks["kill_switch"]["valid"] = False
            refusals.append("KILL_SWITCH_INVALID")
            checks["kill_switch"]["refusal"] = str(exc).split(":")[0]
    except Exception as exc:  # noqa: BLE001
        refusals.append("KILL_SWITCH_UNRESOLVED")
        checks["kill_switch"] = {"valid": False, "refusal": type(exc).__name__}

    # 7 — interpreter can import the runtime (imports only).
    imports = []
    for modname in (
        "phase3",
        "phase3.paths",
        "phase3.config.loader",
        "phase3.transport.http",
    ):
        try:
            __import__(modname)
            imports.append({"module": modname, "ok": True})
        except Exception as exc:  # noqa: BLE001
            imports.append({"module": modname, "ok": False, "error": type(exc).__name__})
            refusals.append(f"IMPORT_MISSING:{modname}")
    checks["imports"] = imports

    # 8 — no prohibited production fallback: resolved roots must never be
    # CWD-derived; the profile must not be production-by-fallthrough.
    try:
        from phase3.paths import config_dir
        from phase3.config.loader import DEFAULT_CONFIG_ROOT

        resolved = str(Path(config_dir()))
        # CWD-independence (R1 property): path resolution must not depend
        # on the INVOCATION cwd. A repo/project-root-derived default is
        # legitimate (FIE_PROJECT_ROOT or phase3-file location) — but if
        # re-resolution from an unrelated cwd differs, the config root is
        # truly cwd-derived and must refuse.
        probe = tempfile.mkdtemp(prefix="fie-pf-cwdprobe-")
        try:
            original = os.getcwd()
            try:
                os.chdir(probe)
                re_resolved = str(Path(config_dir()))
            finally:
                os.chdir(original)
            cwd_independent = re_resolved == resolved
        finally:
            shutil.rmtree(probe, ignore_errors=True)
        checks["cwd_independence"] = {
            "resolved_config_dir": resolved,
            "probe_cwd": probe,
            "independent": cwd_independent,
        }
        if not cwd_independent:
            refusals.append("CWD_DERIVED_CONFIG_ROOT_FORBIDDEN")
        service_env = env.get("FIE_SERVICE_ENV", "")
        checks["service_env_explicit"] = bool(service_env)
        profile = getattr(config, "service_env", None) if config else None
        checks["implicit_production_fallback"] = (
            profile == "production" and not service_env
        )
        if checks["implicit_production_fallback"]:
            refusals.append("IMPLICIT_PRODUCTION_FALLBACK")
    except Exception as exc:  # noqa: BLE001
        refusals.append(f"FALLBACK_CHECK_FAILED:{type(exc).__name__}")

    report["checks"] = checks
    if refusals:
        report["precheck_result"] = "FAIL"
        report["refusals"] = refusals
        return report, 2
    report["precheck_result"] = "PASS"
    return report, 0


def main(argv: list[str] | None = None) -> int:
    """Print the sanitized preflight report; exit 0 PASS / 2 FAIL."""
    report, rc = run_preflight()
    print(json.dumps(report, sort_keys=True))
    return rc


if __name__ == "__main__":  # `python -m phase3.preflight`
    raise SystemExit(main())