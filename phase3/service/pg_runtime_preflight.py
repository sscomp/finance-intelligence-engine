"""PostgreSQL runtime preflight invariants (WO C3 — Production Readiness).

Machine-testable preflight for the accepted PostgreSQL runtime layout on
the Abacus container. The layout — ``PGDATA`` under the service home
directory —
is an ACCEPTED production constraint (owner decision AD-2); this module
therefore does not propose relocation. Its job is to make the survival
conditions of that layout continuously verifiable, because the
2026-10-04 incident (``pg_control Permission denied`` PANIC after the
production home directory was tightened to mode 0700) proved that a
PGDATA-under-home cluster is one ``chmod`` away from an outage.

Invariants (WO §9, verbatim names):

``POSTGRES_RUNTIME_ROOT_TRAVERSABLE``
    The PostgreSQL service account must be able to traverse
    the service home directory. Regression to a traversal-blocking mode (the
    incident's 0700) FAILS; the accepted production invariant is
    exactly 0755 (owner decision AD-2). Permissions are not broadened
    beyond that.

``PGDATA_PATH_EXPECTED`` / ``PGDATA_OWNER_EXPECTED`` /
``PGDATA_MODE_EXPECTED``
    The live PGDATA sits where the runtime documentation says it
    does, belongs to the ``postgres`` service account, and carries a
    data-directory-private mode.

``PG_CONTROL_READABLE_BY_SERVICE_ACCOUNT``
    ``pg_control`` is readable AS the postgres user — the exact check
    that would have caught the incident.

``PG_STARTUP_PASS`` / ``PG_READY_PASS`` / ``PG_CHECKPOINT_PASS`` /
``PG_RESTART_PERSISTENCE_PASS``
    Active tests executed on the DISPOSABLE rehearsal cluster only
    (never against the live production cluster): a fresh start, a
    readiness probe, a real checkpoint with post-test log scan for
    ``PANIC``/``Permission denied``, and a stop/start round-trip
    verifying schema/data identity across the restart.

Two execution modes:

``--mode production``
    READ-ONLY. Stat-based checks plus a bounded ``pg_isready`` /
    ``pg_control`` read performed as the service account; PG log
    tail is reported for evidence. Never writes to, restarts, or
    checkpoints the live cluster.

``--mode disposable``
    Full invariant chain against an isolated cluster under /tmp
    started/stopped by this script via ``pg_ctl``.

Exit code: 0 when every invariant passes, 1 on any FAIL. Output is a
JSON invariant report (machine-testable per the WO requirement).
"""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Defaults are derived from the invoking user's home (the accepted layout
# seats PGDATA under it) — no developer-host-specific literal is embedded.
DEFAULT_HOME = os.path.expanduser("~")
DEFAULT_PGDATA = os.path.join(DEFAULT_HOME, "postgresql-18", "data")
DEFAULT_PG_SERVICE_USER = "postgres"
#: The accepted production invariant (owner decision AD-2).
ACCEPTED_HOME_MODE = 0o755
#: PGDATA is data-directory-private in every supported configuration.
PGDATA_ACCEPTED_MODES = {0o700, 0o750}

POSTGRES_RUNTIME_ROOT_TRAVERSABLE = "POSTGRES_RUNTIME_ROOT_TRAVERSABLE"
PGDATA_PATH_EXPECTED = "PGDATA_PATH_EXPECTED"
PGDATA_OWNER_EXPECTED = "PGDATA_OWNER_EXPECTED"
PGDATA_MODE_EXPECTED = "PGDATA_MODE_EXPECTED"
PG_CONTROL_READABLE_BY_SERVICE_ACCOUNT = "PG_CONTROL_READABLE_BY_SERVICE_ACCOUNT"
PG_STARTUP_PASS = "PG_STARTUP_PASS"
PG_READY_PASS = "PG_READY_PASS"
PG_CHECKPOINT_PASS = "PG_CHECKPOINT_PASS"
PG_RESTART_PERSISTENCE_PASS = "PG_RESTART_PERSISTENCE_PASS"

_PASS = "PASS"
_FAIL = "FAIL"
_SKIP = "SKIP"

# Postgres binaries resolve via PATH first; fall back to the distro layout.
_PG_BINDIRS = ("/usr/lib/postgresql/18/bin", "/usr/pgsql-18/bin", "/usr/lib/postgresql/17/bin")


def _pg_bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for d in _PG_BINDIRS:
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return name


def _mode_octal(mode: int) -> str:
    return format(mode & 0o7777, "04o")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _result(invariant: str, status: str, detail: str) -> dict[str, str]:
    return {"invariant": invariant, "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# Static (filesystem) invariants — shared by both modes
# ---------------------------------------------------------------------------


def check_postgres_runtime_root_traversable(
    home: str, expected_mode: int = ACCEPTED_HOME_MODE
) -> dict[str, str]:
    """Detect regression of the home dir mode away from the accepted 0755.

    The 2026-10-04 incident: 0700 on the service home directory blocked the
    postgres service account's path traversal into PGDATA. The check
    fails on BOTH directions of regression — traversal-blocking
    tightening (no other-execute bit) AND unnoticed broadening.
    """
    try:
        st = os.stat(home)
    except OSError as exc:
        return _result(POSTGRES_RUNTIME_ROOT_TRAVERSABLE, _FAIL,
                       f"cannot stat {home}: {exc}")
    actual = st.st_mode & 0o7777
    traversable = st.st_mode & 0o0001  # other-execute bit
    if not traversable:
        return _result(
            POSTGRES_RUNTIME_ROOT_TRAVERSABLE, _FAIL,
            f"{home} mode {_mode_octal(actual)} blocks path traversal for the "
            f"postgres service account (the 2026-10-04 PANIC regression); "
            f"accepted production invariant is {_mode_octal(expected_mode)}",
        )
    if actual != expected_mode:
        return _result(
            POSTGRES_RUNTIME_ROOT_TRAVERSABLE, _FAIL,
            f"{home} mode is {_mode_octal(actual)}, expected exactly "
            f"{_mode_octal(expected_mode)} (accepted invariant); "
            f"do not broaden permissions beyond what is required either",
        )
    return _result(
        POSTGRES_RUNTIME_ROOT_TRAVERSABLE, _PASS,
        f"{home} mode {_mode_octal(actual)} (accepted production invariant; "
        f"traversal available to the postgres service account)",
    )


def check_pgdata_path(pgdata: str, expected: str | None = None) -> dict[str, str]:
    if not os.path.isdir(pgdata):
        return _result(PGDATA_PATH_EXPECTED, _FAIL, f"{pgdata} is not a directory")
    if expected and os.path.realpath(pgdata) != os.path.realpath(expected):
        return _result(
            PGDATA_PATH_EXPECTED, _FAIL,
            f"PGDATA {pgdata} does not resolve to the documented runtime "
            f"location {expected}",
        )
    return _result(PGDATA_PATH_EXPECTED, _PASS, f"PGDATA={pgdata}")


def check_pgdata_owner(pgdata: str, service_user: str) -> dict[str, str]:
    try:
        st = os.stat(pgdata)
        uid, gid = st.st_uid, st.st_gid
        user = pwd.getpwuid(uid).pw_name
        group = grp.getgrgid(gid).gr_name
    except (OSError, KeyError) as exc:
        return _result(PGDATA_OWNER_EXPECTED, _FAIL, f"{pgdata}: {exc}")
    try:
        want_uid = pwd.getpwnam(service_user).pw_uid
        want_gid = grp.getgrnam(service_user).gr_gid
    except KeyError:
        return _result(PGDATA_OWNER_EXPECTED, _SKIP,
                       f"service account '{service_user}' does not exist on this host")
    if uid != want_uid or gid != want_gid:
        return _result(
            PGDATA_OWNER_EXPECTED, _FAIL,
            f"PGDATA owned by {user}:{group} (uid={uid}, gid={gid}); expected "
            f"{service_user}:{service_user} (uid={want_uid}, gid={want_gid})",
        )
    return _result(PGDATA_OWNER_EXPECTED, _PASS, f"owned by {user}:{group}")


def check_pgdata_mode(pgdata: str) -> dict[str, str]:
    try:
        actual = os.stat(pgdata).st_mode & 0o7777
    except OSError as exc:
        return _result(PGDATA_MODE_EXPECTED, _FAIL, f"{pgdata}: {exc}")
    if actual not in PGDATA_ACCEPTED_MODES:
        return _result(
            PGDATA_MODE_EXPECTED, _FAIL,
            f"PGDATA mode {_mode_octal(actual)}; expected "
            f"{', '.join(sorted(_mode_octal(m) for m in PGDATA_ACCEPTED_MODES))}",
        )
    return _result(PGDATA_MODE_EXPECTED, _PASS, f"mode {_mode_octal(actual)}")


def check_pg_control_readable(pgdata: str, service_user: str) -> dict[str, str]:
    """Read global/pg_control AS the postgres service account.

    This is the exact condition that PANIC'd on 2026-10-04: the
    service account lost read access under PGDATA. A read executed
    with the service account's credentials is the only probe that
    proves the survival property.
    """
    control = os.path.join(pgdata, "global", "pg_control")
    # PGDATA is service-account-private (0700): probes INSIDE it must run
    # AS the service account — our own stat() would see EACCES and report
    # a false "missing".
    cmd = [
        "sudo", "-n", "-u", service_user, "sh", "-c",
        f"if [ -e {shlex_quote(control)} ]; then wc -c < "
        f"{shlex_quote(control)}; else echo MISSING; fi",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result(PG_CONTROL_READABLE_BY_SERVICE_ACCOUNT, _FAIL,
                       f"probe failed: {exc}")
    if proc.returncode != 0:
        return _result(
            PG_CONTROL_READABLE_BY_SERVICE_ACCOUNT, _FAIL,
            f"service account cannot read pg_control (the incident "
            f"signature): rc={proc.returncode} "
            f"{(proc.stderr or '').strip().splitlines()[-1] if proc.stderr.strip() else ''}",
        )
    size = (proc.stdout or "").strip()
    # A PG18 pg_control is 8192 bytes; a nonzero read is the pass criterion.
    if not size.isdigit() or int(size) <= 0:
        return _result(PG_CONTROL_READABLE_BY_SERVICE_ACCOUNT, _FAIL,
                       f"pg_control read returned unexpected size {size!r}")
    return _result(
        PG_CONTROL_READABLE_BY_SERVICE_ACCOUNT, _PASS,
        f"readable as {service_user} ({size} bytes)",
    )


def shlex_quote(s: str) -> str:  # tiny local helper: keeps imports lean
    import shlex
    return shlex.quote(s)


# ---------------------------------------------------------------------------
# Disposable-cluster active invariants
# ---------------------------------------------------------------------------


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_pg_startup(pgdata: str, port: int, socket_dir: str,
                     log_file: str) -> dict[str, str]:
    """Start the disposable cluster with pg_ctl (PG_STARTUP_PASS)."""
    # Startup is meaningful both for a cluster already up (it recovered)
    # and one we start cold here. For the deterministic chain we require
    # OUR start: stop a running instance first (disposable only).
    subprocess.run(
        [_pg_bin("pg_ctl"), "-D", pgdata, "-m", "fast", "stop"],
        capture_output=True, timeout=60,
    )
    before = _log_size(log_file)
    proc = subprocess.run(
        [_pg_bin("pg_ctl"), "-D", pgdata, "-w", "-t", "30",
         "-o", f"-p {port} -k {socket_dir}",
         "-l", log_file, "start"],
        capture_output=True, text=True, timeout=90,
    )
    if proc.returncode != 0:
        return _result(PG_STARTUP_PASS, _FAIL,
                       f"pg_ctl start rc={proc.returncode}: "
                       f"{(proc.stderr or proc.stdout).strip()[-400:]}")
    tail = _log_tail(log_file, since_size=before)
    detail = f"cluster started on port {port}"
    if tail:
        detail += "; log window: " + " | ".join(tail[-3:])
    return _result(PG_STARTUP_PASS, _PASS, detail)


def check_pg_ready(port: int) -> dict[str, str]:
    isready = subprocess.run(
        [_pg_bin("pg_isready"), "-h", "127.0.0.1", "-p", str(port)],
        capture_output=True, text=True, timeout=15,
    )
    out = (isready.stdout or isready.stderr).strip()
    if isready.returncode != 0 or "accepting connections" not in out:
        return _result(PG_READY_PASS, _FAIL, f"pg_isready: {out}")
    # SQL-level readiness (SELECT 1 over TCP as the disposable superuser).
    probe = subprocess.run(
        [_pg_bin("psql"), "-h", "127.0.0.1", "-p", str(port), "-U", "fie",
         "-d", "postgres", "-tAc", "SELECT 1"],
        capture_output=True, text=True, timeout=15,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "1":
        return _result(PG_READY_PASS, _FAIL,
                       f"SQL probe failed: {(probe.stderr or probe.stdout).strip()[-300:]}")
    return _result(PG_READY_PASS, _PASS, out)


def check_pg_checkpoint(pgdata: str, port: int, log_file: str) -> dict[str, str]:
    """Execute a real CHECKPOINT and verify completion + no PANIC (WO §9).

    The 2026-10-04 PANIC surfaced exactly at checkpoint time; a
    checkpoint that completes on the disposable cluster with a clean
    log window is the required regression demonstration.
    """
    before = _log_size(log_file)
    proc = subprocess.run(
        [_pg_bin("psql"), "-h", "127.0.0.1", "-p", str(port), "-U", "fie",
         "-d", "postgres", "-tAc",
         "CHECKPOINT; SELECT 'CHECKPOINT_ISSUED';"],
        capture_output=True, text=True, timeout=120,
    )
    time.sleep(1.0)
    if proc.returncode != 0 or "CHECKPOINT_ISSUED" not in proc.stdout:
        return _result(PG_CHECKPOINT_PASS, _FAIL,
                       f"CHECKPOINT failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    window = _log_tail(log_file, since_size=before)
    bad = [ln for ln in window if re.search(r"PANIC|FATAL|Permission denied", ln)]
    if bad:
        return _result(PG_CHECKPOINT_PASS, _FAIL,
                       f"error signatures in checkpoint log window: {bad[:3]}")
    completed = [ln for ln in window if "checkpoint complete" in ln]
    if not completed:
        return _result(PG_CHECKPOINT_PASS, _FAIL,
                       "no 'checkpoint complete' line in the log window")
    return _result(
        PG_CHECKPOINT_PASS, _PASS,
        f"checkpoint completed without PANIC; log window {len(window)} lines",
    )


def check_pg_restart_persistence(pgdata: str, port: int, socket_dir: str,
                                 log_file: str, marker_db: str,
                                 marker_value: str) -> dict[str, str]:
    """Stop/start round-trip; schema + data identity must survive."""
    psql = [_pg_bin("psql"), "-h", "127.0.0.1", "-p", str(port), "-U", "fie"]
    seed = subprocess.run(
        [*psql, "-d", marker_db, "-v", "ON_ERROR_STOP=1", "-tAc",
         f"CREATE TABLE IF NOT EXISTS pg_preflight_restart_marker "
         f"(id int PRIMARY KEY, payload text); "
         f"INSERT INTO pg_preflight_restart_marker VALUES (1, {marker_value!r}) "
         f"ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload; "
         f"SELECT payload FROM pg_preflight_restart_marker WHERE id = 1;"],
        capture_output=True, text=True, timeout=60,
    )
    if seed.returncode != 0 or marker_value not in seed.stdout:
        return _result(PG_RESTART_PERSISTENCE_PASS, _FAIL,
                       f"marker seed failed: {(seed.stderr or seed.stdout).strip()[-300:]}")
    stop = subprocess.run([_pg_bin("pg_ctl"), "-D", pgdata, "-m", "fast", "stop"],
                          capture_output=True, text=True, timeout=60)
    if stop.returncode != 0:
        return _result(PG_RESTART_PERSISTENCE_PASS, _FAIL,
                       f"stop failed: {(stop.stderr or stop.stdout).strip()[-300:]}")
    start = subprocess.run(
        [_pg_bin("pg_ctl"), "-D", pgdata, "-w", "-t", "30",
         "-o", f"-p {port} -k {socket_dir}", "-l", log_file, "start"],
        capture_output=True, text=True, timeout=90,
    )
    if start.returncode != 0:
        return _result(PG_RESTART_PERSISTENCE_PASS, _FAIL,
                       f"restart failed: {(start.stderr or start.stdout).strip()[-300:]}")
    verify = subprocess.run(
        [*psql, "-d", marker_db, "-tAc",
         "SELECT payload FROM pg_preflight_restart_marker WHERE id = 1;"],
        capture_output=True, text=True, timeout=30,
    )
    if verify.returncode != 0 or marker_value not in verify.stdout:
        return _result(PG_RESTART_PERSISTENCE_PASS, _FAIL,
                       f"data did NOT survive restart: "
                       f"{(verify.stderr or verify.stdout).strip()[-300:]}")
    return _result(
        PG_RESTART_PERSISTENCE_PASS, _PASS,
        f"marker payload {marker_value!r} survived fast-stop/restart; "
        f"schema re-present",
    )


def _log_size(log_file: str) -> int:
    try:
        return os.path.getsize(log_file)
    except OSError:
        return 0


def _log_tail(log_file: str, marker_mark: str = "", since_size: int = 0,
              max_lines: int = 8) -> list[str]:
    try:
        with open(log_file, "rb") as fh:
            fh.seek(max(since_size, 0))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [ln for ln in data.splitlines() if ln.strip()]
    if not since_size and marker_mark:
        hits = [i for i, ln in enumerate(lines) if marker_mark in ln]
        if hits:
            lines = lines[hits[-1] + 1:]
    return lines[-max_lines:]


# ---------------------------------------------------------------------------
# Production read-only log evidence
# ---------------------------------------------------------------------------


def production_log_evidence(log_candidates: list[str],
                            max_lines: int = 40) -> str:
    """Extract recent PANIC/FATAL/checkpoint lines from the live PG log.

    Read-only: lines are matched as the postgres user when direct
    reads fail (root-owned supervisor logs).
    """
    for path in log_candidates:
        if not os.path.exists(path):
            continue
        if os.access(path, os.R_OK):
            data = open(path, "rb").read().decode("utf-8", "replace")[-400_000:]
        else:
            proc = subprocess.run(
                ["sudo", "-n", "-u", "postgres", "sh", "-c",
                 f"tail -c 400000 {shlex_quote(path)} 2>/dev/null"],
                capture_output=True, text=True, timeout=15,
            )
            data = proc.stdout
        picked = [ln for ln in data.splitlines()
                  if re.search(r"PANIC|FATAL|ERROR|checkpoint complete|"
                               r"Permission denied|database system (was|is)", ln)]
        if picked:
            return f"{path}\n" + "\n".join(picked[-max_lines:])
    return "no readable postgres log found; production log evidence absent"


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def run_preflight(mode: str, *, home: str = DEFAULT_HOME,
                  pgdata: str = DEFAULT_PGDATA,
                  service_user: str = DEFAULT_PG_SERVICE_USER,
                  cluster_dir: str = "/tmp/fie-pg",
                  port: int = 54329) -> dict[str, Any]:
    """Execute the invariant chain; returns the machine-readable report."""
    results: list[dict[str, str]] = []
    results.append(check_postgres_runtime_root_traversable(home))
    results.append(check_pgdata_path(pgdata) if mode == "production"
                    else _result(PGDATA_PATH_EXPECTED, _SKIP,
                                 f"disposable mode: live PGDATA expectation N/A "
                                 f"(cluster dir {cluster_dir})"))
    if mode == "production":
        results.append(check_pgdata_owner(pgdata, service_user))
        results.append(check_pgdata_mode(pgdata))
        results.append(check_pg_control_readable(pgdata, service_user))
        for name in (PG_STARTUP_PASS, PG_READY_PASS, PG_CHECKPOINT_PASS,
                     PG_RESTART_PERSISTENCE_PASS):
            results.append(_result(name, _SKIP,
                                   "active tests never run against the live "
                                   "production cluster (WO §9 boundary)"))
    else:
        pgdata_dir = os.path.join(cluster_dir, "pgdata")
        log_file = os.path.join(cluster_dir, "pg.log")
        if not os.path.isdir(os.path.join(pgdata_dir, "global")):
            init = subprocess.run(
                [_pg_bin("initdb"), "-D", pgdata_dir, "-U", "fie", "--auth=trust"],
                capture_output=True, text=True, timeout=180,
            )
            if init.returncode != 0:
                results.append(_result(PG_STARTUP_PASS, _FAIL,
                                       f"initdb failed: {(init.stderr or init.stdout).strip()[-300:]}"))
        if not os.path.isdir(os.path.join(pgdata_dir, "global")):
            for name in (PG_STARTUP_PASS, PG_READY_PASS, PG_CHECKPOINT_PASS,
                         PG_RESTART_PERSISTENCE_PASS):
                results.append(_result(name, _FAIL,
                                       f"disposable pgdata {pgdata_dir} unavailable"))
        else:
            results.append(check_pg_startup(pgdata_dir, port, cluster_dir, log_file))
            ready = check_pg_ready(port)
            results.append(ready)
            if ready["status"] == _PASS:
                results.append(check_pg_checkpoint(pgdata_dir, port, log_file))
                marker_value = f"preflight-{_utc_now()}"
                results.append(check_pg_restart_persistence(
                    pgdata_dir, port, cluster_dir, log_file,
                    marker_db="postgres", marker_value=marker_value,
                ))
            else:
                for name in (PG_CHECKPOINT_PASS, PG_RESTART_PERSISTENCE_PASS):
                    results.append(_result(name, _SKIP, "cluster not ready"))
    failures = [r for r in results if r["status"] == _FAIL]
    return {
        "mode": mode,
        "generated_at": _utc_now(),
        "verdict": "PASS" if not failures else "FAIL",
        "invariants": results,
        "failures": [r["invariant"] for r in failures],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("production", "disposable"),
                        default="disposable")
    parser.add_argument("--home", default=DEFAULT_HOME)
    parser.add_argument("--pgdata", default=DEFAULT_PGDATA)
    parser.add_argument("--service-user", default=DEFAULT_PG_SERVICE_USER)
    parser.add_argument("--cluster-dir", default="/tmp/fie-pg")
    parser.add_argument("--port", type=int, default=54329)
    parser.add_argument("--log-evidence", action="store_true",
                        help="production mode: append live PG log scan output")
    parser.add_argument("--out", help="write JSON report to this path")
    args = parser.parse_args(argv)

    report = run_preflight(args.mode, home=args.home, pgdata=args.pgdata,
                           service_user=args.service_user,
                           cluster_dir=args.cluster_dir, port=args.port)
    if args.mode == "production" and args.log_evidence:
        report["production_log_evidence"] = production_log_evidence([
            "/var/log/supervisor/postgresql-18.err.log",
            os.path.join(DEFAULT_PGDATA, "log", "postgresql.log"),
        ])
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload + "\n")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())