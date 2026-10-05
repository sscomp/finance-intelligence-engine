#!/usr/bin/env python3
"""FIE runtime fail-closed negative controls — Phase 6.8C (WO Task G).

Executable proof (not code-reading) that the canonical DB-target contract
(``phase3/runtime_contract.py``, the single resolver of record) refuses the
Production-routing failure classes a fresh cloud environment must not be
able to trigger:

  NC-1  no DB target provided            -> FAIL_CLOSED (never a default)
  NC-2  Production-like DSN into the
        test/rehearsal role              -> refused (identity guard)
  NC-3  contradictory/ambiguous targets  -> refused (fail closed)
  NC-4  "convenient" SQLite fallback for
        a missing Production target      -> refused (SQLite = rollback only)
  NC-5  Production contract unavailable
        (fresh clone)                    -> refused (unknown != safe)
  NC-6  unknown FIE_SERVICE_ENV value    -> refused (no implicit Production)
  NC-7  shell boundary (rehearsal_db_guard.sh): missing target in rehearsal
        mode                             -> exit 78 (guard precedes any write)

Every fixture value is synthetic (loopback port 59999 — an unbound endpoint
nothing dials; throwaway temp paths). No credential value is read, stored,
or emitted by this script; contract-file parsing never returns secret keys.

Exit: 0 iff every negative control actually refused; nonzero otherwise.
Run from the repository root (any environment; no Production access).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from phase3.runtime_contract import (  # noqa: E402
    ROLE_INTELLIGENCE,
    FailClosedTarget,
    assert_rehearsal_target_safe,
    load_production_identities,
    parse_target,
    resolve_role_target,
)

# synthetic fixture: loopback port 59999 is unbound; nothing dials it.
FIXTURE_PROD_DSN = "postgresql://fixture_prod@127.0.0.1:59999/fie_prod_fixture"
FIXTURE_SAFE_DSN = (
    "postgresql://fie_cloud_test@127.0.0.1:54330/fie_cloud_test_db"
    "?host=127.0.0.1&port=54330"
)
SYNTH_SQLITE = "sqlite:///:memory:"


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FIE_")}
    env.pop("PGPASSWORD", None)
    return env


RESULTS: list = []


def _record(nc_id: str, expected: str, fn) -> bool:
    try:
        fn()
    except FailClosedTarget as exc:
        code = exc.reason
        ok = expected in code or expected in str(exc)
        RESULTS.append({"id": nc_id, "expected": expected, "raised": code,
                        "pass": bool(ok)})
        return bool(ok)
    RESULTS.append({"id": nc_id, "expected": expected, "raised": None,
                    "pass": False})
    return False


def nc1_no_target() -> None:
    env = _clean_env()
    env["FIE_SERVICE_ENV"] = "test"
    resolve_role_target(ROLE_INTELLIGENCE, None, env)


def nc2_production_dsn(tmp: Path) -> None:
    # The authoritative production identity set comes ONLY from the
    # production contract files (FIE_PRODUCTION_DB_CONTRACT override →
    # synthetic fixture file here; never real credentials).
    contract = tmp / "fixture-prod-contract.env"
    contract.write_text(f"FIE_DB_PATH='{FIXTURE_PROD_DSN}'\n")
    env = _clean_env()
    env["FIE_PRODUCTION_DB_CONTRACT"] = str(contract)
    prod = load_production_identities(env)
    spec = parse_target(FIXTURE_PROD_DSN)
    assert_rehearsal_target_safe(spec, prod)
    raise RuntimeError("guard accepted the production-equivalent DSN")


def nc2b_alias_spelling(tmp: Path) -> None:
    """Same production identity by alias spelling must also be refused."""
    contract = tmp / "fixture-prod-contract2.env"
    contract.write_text(
        "FIE_DB_PATH='postgres://fixture_prod@localhost:59999/"
        "fie_prod_fixture'\n")
    env = _clean_env()
    env["FIE_PRODUCTION_DB_CONTRACT"] = str(contract)
    prod = load_production_identities(env)
    spec = parse_target(FIXTURE_PROD_DSN)
    assert_rehearsal_target_safe(spec, prod)
    raise RuntimeError("guard accepted an alias spelling of the prod DSN")


def nc3_contradictory_targets() -> None:
    env = _clean_env()
    env["FIE_SERVICE_ENV"] = "test"
    env["FIE_DB_TARGET_INTELLIGENCE"] = FIXTURE_SAFE_DSN
    env["FIE_INTELLIGENCE_DB"] = SYNTH_SQLITE
    resolve_role_target(ROLE_INTELLIGENCE, None, env)


def nc4_sqlite_not_a_fallback() -> None:
    # Missing production target + an ambient SQLite-flavored legacy alias
    # present nowhere: the resolver must refuse to invent a SQLite file
    # (SQLite = rollback/recovery ONLY; no silent fallback in any mode).
    env = _clean_env()
    env["FIE_SERVICE_ENV"] = "production"
    env["FIE_DATA_DIR"] = str(tmp_dir())
    resolve_role_target(ROLE_INTELLIGENCE, None, env)


def nc5_unknown_not_safe(tmp: Path) -> None:
    # A readable but DSN-free production contract set proves nothing about
    # the Production identity — a rehearsal PG target must be refused
    # (unknown != safe), never silently accepted (6.8C Task G).
    env = _clean_env()
    (tmp / "dsn-free-contract.env").write_text("# not a DB contract\n")
    env["FIE_PRODUCTION_DB_CONTRACT"] = str(tmp / "dsn-free-contract.env")
    assert_rehearsal_target_safe(parse_target(FIXTURE_SAFE_DSN), env=env)


def nc5b_no_cridental_no_fallback(tmp: Path) -> None:
    # Production credential/contract ABSENT must never cause a silent
    # SQLite substitution for the production role.
    env = _clean_env()
    env["FIE_SERVICE_ENV"] = "production"
    env["FIE_PRODUCTION_DB_CONTRACT"] = str(
        tmp / "does-not-exist-6c8.env")
    load_production_identities(env)


def nc6_unknown_service_env() -> None:
    env = _clean_env()
    env["FIE_SERVICE_ENV"] = "cloud-wannabe"
    from phase3.runtime_contract import classify_service_env  # late import
    classify_service_env(env.get("FIE_SERVICE_ENV", ""))


def nc7_shell_guard(tmp: Path) -> bool:
    # Shell boundary: the rehearsal guard refuses a missing intelligence
    # target in rehearsal mode with exit 78, before any write happens.
    env = _clean_env()
    env["FIE_SERVICE_ENV"] = "test"
    env.pop("FIE_INTELLIGENCE_DB", None)
    env.pop("FIE_DATABASE_URL", None)
    env["FIE_GUARD_PYTHON"] = sys.executable or "python3"
    guard = REPO / "scripts" / "rehearsal_db_guard.sh"
    proc = subprocess.run(
        ["bash", "-c",
         f". '{guard}' && fie_wrapper_seed_db_guard "
         f"'{tmp / 'int_store.sqlite'}'"],
        capture_output=True, text=True, env=env, timeout=60)
    blob = proc.stdout + proc.stderr
    return proc.returncode == 78 and (
        "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED" in blob)


def tmp_dir() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="fie_negctrl_")


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory(prefix="fie_negctrl_") as td:
        tmp = Path(td)
        cases = [
            ("NC-1-no-db-target", "FAIL_CLOSED_DB_TARGET_REQUIRED",
             nc1_no_target),
            ("NC-2-prod-dsn-in-rehearsal", "FAIL_CLOSED",
             lambda: nc2_production_dsn(tmp)),
            ("NC-2b-prod-dsn-alias-spelling", "FAIL_CLOSED",
             lambda: nc2b_alias_spelling(tmp)),
            ("NC-3-contradictory-targets", "FAIL_CLOSED_CONTRADICTORY",
             nc3_contradictory_targets),
            ("NC-4-sqlite-not-a-fallback", "FAIL_CLOSED_DB_TARGET_REQUIRED",
             nc4_sqlite_not_a_fallback),
            ("NC-5-unknown-not-safe", "FAIL_CLOSED",
             lambda: nc5_unknown_not_safe(tmp)),
            ("NC-5b-no-contract-no-sqlite-fallback",
             "FAIL_CLOSED_PRODUCTION_IDENTITY_UNAVAILABLE",
             lambda: nc5b_no_cridental_no_fallback(tmp)),
            ("NC-6-unknown-service-env", "FAIL_CLOSED_PRODUCTION_MODE",
             nc6_unknown_service_env),
        ]
        for nc_id, expected, fn in cases:
            ok = _record(nc_id, expected, fn) and ok

        # NC-7: shell boundary (subprocess; recorded separately from the
        # exception-protocol records above).
        shell_ok = nc7_shell_guard(tmp)
        RESULTS.append({"id": "NC-7-shell-guard-exit78",
                        "expected": "exit 78 + refusal", "raised": None,
                        "pass": bool(shell_ok)})
        ok = ok and shell_ok

    print(json.dumps({"cloud_negative_controls": RESULTS}, indent=2))
    print(("PASS: all negative controls refused as required" if ok
           else "FAIL: at least one negative control did not refuse"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())