#!/bin/bash
# FIE ephemeral test-PostgreSQL provisioner — Phase 6.9A-R3 (portable path).
#
# ONE canonical repository-owned boundary for isolated PostgreSQL
# validation infrastructure (ABACUS_FIE_6_9A_R3, §3/§6). Two shapes:
#
#   * sourceable library  ( . scripts/provision-test-postgres.sh
#                           && fie_test_pg_start          # start cluster
#                           && fie_test_pg_stop )         # own teardown
#   * standalone script   scripts/provision-test-postgres.sh --ensure-cache
#                         scripts/provision-test-postgres.sh --start
#                         scripts/provision-test-postgres.sh --stop <dir>
#                         scripts/provision-test-postgres.sh --run <child…>
#
# Contract (ABACUS_FIE_6_9A_R3 §6 — MUST-holds):
#   * NEVER reads/uses/falls back to a Production DSN, the fie_prod data
#     store, Production credentials, or generic ambient DB env vars as a
#     provisioning substitute. Any production-flavored candidate target in
#     the caller environment FAILS CLOSED (exit 78).
#   * Job-owned identity ONLY: per-run TMPDIR, random synthetic user/
#     database/password, loopback-only bind, UTF-8 pinned, job-owned port,
#     explicit lifecycle ownership marker.
#   * Server acquisition: deterministic discovery first (FIE_TEST_PG_BIN
#     explicit test-only override > /usr/lib/postgresql/<N>/bin > PATH),
#     otherwise PORTABLE distribution from Maven Central
#     (io.zonky.test :embedded-postgres-binaries-linux-*), version PINNED
#     to PORTABLE_PG_VERSION, sha256-verified, cached, no sudo, no
#     interactive prompt, no global daemon, no system PG config mutation.
#   * Explicit failure classifications (docs/architecture/
#     ephemeral-postgresql-provisioning.md) — never collapsed into a
#     generic test failure; no silent SQLite substitution (SQLite is
#     rollback/recovery only, handled by the resolver, not here).
#   * Deterministic readiness (process + TCP auth + identity + UTF-8) and
#     deterministic teardown (ownership-proven, idempotent; only ever stops
#    /removes the job-owned cluster and job dir).
#
# Credentials: the synthetic password is written ONLY to a 0600 file inside
# the job TMPDIR, used only for the readiness TCP-auth proof (as a child
# environment value, never argv) and never printed. Nothing here reads or
# stores any secret. The test DSN exported for children is the loopback
# socket form WITHOUT a password (socket auth is job-owned trust; TCP
# host-lines are pinned to scram-sha-256 for this job's pg_hba only).
set -u

PORTABLE_PG_VERSION="18.4.0"   # exact pin (== Production major.minor 18.4)
PORTABLE_PG_BASE_URL="https://repo1.maven.org/maven2/io/zonky/test/postgres"

# One library dir root; the repo root is its parent.
_PROVISIONER_LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-.}")" && pwd -P)"

PG_GUARD_EXIT=78               # shared fail-closed exit class (6.8A)
PG_EXIT_PLATFORM=90            # POSTGRESQL_PLATFORM_UNSUPPORTED
PG_EXIT_ARTIFACT=91            # POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE
PG_EXIT_INTEGRITY=92           # POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED
PG_EXIT_INITDB=93              # POSTGRESQL_INITDB_FAILED
PG_EXIT_START=94               # POSTGRESQL_START_FAILED
PG_EXIT_READINESS=95           # POSTGRESQL_READINESS_FAILED
PG_EXIT_TEARDOWN=96            # POSTGRESQL_TEARDOWN_FAILED

_die() { printf 'provision-test-postgres: %s\n' "${1}" >&2; }

_pg_repo_root() {
    printf '%s' "$(dirname -- "${_PROVISIONER_LIB_DIR}")"
}

_random_hex() { python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_hex(int(sys.argv[1])))' "${1-8}"; }

# ---------------------------------------------------------------- acquisition
_portable_pg_cache_dir() {
    printf '%s' "${FIE_TEST_PG_CACHE_DIR:-${HOME:-/tmp}/.cache/fie/test-postgres}"
}

_portable_pg_arch() {
    case "$(uname -m)" in
        x86_64)  printf 'linux-amd64' ;;
        aarch64) printf 'linux-arm64v8' ;;
        *)       printf '' ;;
    esac
}

_cache_complete() {  # <cachedir>: 0 iff a usable portable install is present
    [ -x "$1/bin/initdb" ] && [ -x "$1/bin/pg_ctl" ] \
        && [ -x "$1/bin/postgres" ] && [ -f "$1/portable-postgres.jar" ] \
        && [ -f "$1/artifact.sha256" ]
}

_pg_download_to_stdout() {  # <url> -> body on stdout
    if command -v curl >/dev/null 2>&1; then
        curl -sf --max-time 300 "${1}" 2>/dev/null || return 1
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "${1}" || return 1
    else
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED neither curl nor wget available for portable acquisition"
        return 1
    fi
}

_pg_download_to_file() {  # <url> <dest>
    if command -v curl >/dev/null 2>&1; then
        curl -sf --max-time 600 -o "${2}" "${1}" 2>/dev/null || return 1
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "${2}" "${1}" || return 1
    else
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED neither curl nor wget available for portable acquisition"
        return 1
    fi
}

_cache_sha_verify() {  # <cachedir>: stored jar vs stored sidecar (quiet)
    (cd "$1" && printf '%s  portable-postgres.jar\n' "$(cat artifact.sha256)" |
        sha256sum -c --status - >/dev/null 2>&1)
}

# Acquire/verify the pinned portable PostgreSQL distribution. Prints the
# install dir to stdout. Idempotent: a cache HIT re-verifies the stored jar
# sha256 every time and uses no network.
_fie_test_pg_ensure_cache() {
    local cachedir base arch ver url sha_txt archive
    cachedir="$(_portable_pg_cache_dir)"
    base="${FIE_TEST_PG_TEST_BASE_URL:-${PORTABLE_PG_BASE_URL}}"
    arch="$(_portable_pg_arch)"
    ver="${PORTABLE_PG_VERSION}"
    if [ "$(uname -s)" != "Linux" ]; then
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED os=$(uname -s) (portable path: Linux x86_64/aarch64 only); acquisition refused"
        return "${PG_EXIT_PLATFORM}"
    fi
    if [ -z "${arch}" ]; then
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED arch=$(uname -m) (portable path: x86_64/aarch64 only); acquisition refused"
        return "${PG_EXIT_PLATFORM}"
    fi
    for tool in tar xz python3; do
        command -v "${tool}" >/dev/null 2>&1 || {
            _die "POSTGRESQL_PLATFORM_UNSUPPORTED required portable-install tool '${tool}' absent on this platform"
            return "${PG_EXIT_PLATFORM}"
        }
    done
    if _cache_complete "${cachedir}"; then
        if _cache_sha_verify "${cachedir}"; then
            printf '%s\n' "${cachedir}"
            return 0
        fi
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED cached artifact failed re-verification; refusing reuse"
        return "${PG_EXIT_INTEGRITY}"
    fi
    echo "provision-test-postgres: acquiring portable PostgreSQL ${ver} (${arch}) from Maven Central" >&2
    if ! mkdir -p "${cachedir}"; then
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED cannot create cache dir (path withheld)"
        return "${PG_EXIT_PLATFORM}"
    fi
    url="${base}/embedded-postgres-binaries-${arch}/${ver}/embedded-postgres-binaries-${arch}-${ver}.jar"
    sha_txt="$(_pg_download_to_stdout "${url}.sha256")" || {
        _die "POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE published sha256 sidecar not reachable (network/404${FIE_TEST_PG_TEST_BASE_URL:+, test-url-override}); acquisition refused, no fallback"
        return "${PG_EXIT_ARTIFACT}"
    }
    if ! printf '%s' "${sha_txt}" | grep -qE '^[0-9a-fA-F]{64}$'; then
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED published sha256 sidecar has unexpected form"
        return "${PG_EXIT_INTEGRITY}"
    fi
    _pg_download_to_file "${url}" "${cachedir}/portable-postgres.jar" || {
        _die "POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE artifact not reachable (network/404${FIE_TEST_PG_TEST_BASE_URL:+, test-url-override}); acquisition refused, no fallback"
        return "${PG_EXIT_ARTIFACT}"
    }
    if ! (cd "${cachedir}" &&
          printf '%s  portable-postgres.jar\n' "${sha_txt}" |
          sha256sum -c >/dev/null 2>&1); then
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED downloaded artifact does not match the published sha256 sidecar; refusing install"
        rm -f -- "${cachedir}/portable-postgres.jar"
        return "${PG_EXIT_INTEGRITY}"
    fi
    printf '%s\n' "${sha_txt}" > "${cachedir}/artifact.sha256" || {
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED cannot write artifact.sha256 into cache (path withheld)"
        return "${PG_EXIT_PLATFORM}"
    }
    archive="$(
        cd "${cachedir}" && python3 - "${cachedir}" <<'PY'
import sys, zipfile
jar = zipfile.ZipFile(sys.argv[1] + "/portable-postgres.jar")
names = [n for n in jar.namelist() if n.endswith(".txz") and "/" not in n]
if len(names) != 1:
    sys.exit(2)
jar.extract(names[0], sys.argv[1])
print(names[0])
PY
    )" || {
        rm -f -- "${cachedir}/portable-postgres.jar"
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED jar archive members unexpected"
        return "${PG_EXIT_INTEGRITY}"
    }
    tar -xJf "${cachedir}/${archive}" -C "${cachedir}" || {
        rm -f -- "${cachedir}/portable-postgres.jar" "${cachedir}/${archive}"
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED txz extraction failed"
        return "${PG_EXIT_INTEGRITY}"
    }
    rm -f -- "${cachedir}/${archive}"
    "${cachedir}/bin/initdb" --version >/dev/null 2>&1 &&
        "${cachedir}/bin/pg_ctl" --version >/dev/null 2>&1 || {
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED installed portable binaries did not validate"
        rm -rf -- "${cachedir}"
        return "${PG_EXIT_INTEGRITY}"
    }
    echo "provision-test-postgres: portable install verified: ${cachedir}" >&2
    echo "provision-test-postgres: $("${cachedir}/bin/initdb" --version 2>/dev/null | head -1)" >&2
    printf '%s\n' "${cachedir}"
    return 0
}

# ---------------------------------------------------------------- discovery
# Prints <bin-dir> (usable initdb+pg_ctl) on stdout; nonzero otherwise.
_fie_test_pg_discover() {
    local d best=""
    if [ -n "${FIE_TEST_PG_BIN:-}" ]; then
        # Explicit test-only contract (documented; WO §6.3-adjacent).
        [ -x "${FIE_TEST_PG_BIN}/initdb" ] && [ -x "${FIE_TEST_PG_BIN}/pg_ctl" ] && {
            printf '%s\n' "${FIE_TEST_PG_BIN}"
            return 0
        }
        _die "POSTGRESQL_TOOLING_DISCOVERY_FAILED explicit FIE_TEST_PG_BIN override does not contain usable initdb+pg_ctl (path withheld)"
        return "${PG_GUARD_EXIT}"
    fi
    # /usr/lib/postgresql/<N>/bin, highest version first (deterministic).
    for d in $(ls -d /usr/lib/postgresql/*/bin 2>/dev/null |
               sed -n 's|/usr/lib/postgresql/\([0-9][0-9]*\)/bin$|\1 \0|p' |
               sort -n -r | awk '{print $2}'); do
        [ -x "${d}/initdb" ] && [ -x "${d}/pg_ctl" ] && { best="${d}"; break; }
    done
    if [ -n "${best}" ]; then printf '%s\n' "${best}"; return 0; fi
    d="$(command -v initdb 2>/dev/null || true)"
    if [ -n "${d}" ]; then
        d="$(dirname -- "${d}")"
        [ -x "${d}/pg_ctl" ] && { printf '%s\n' "${d}"; return 0; }
    fi
    return 1
}

# ---------------------------------------------------------------- identity guard
# Refuse a production-flavored or misaddressed candidate target from the
# caller environment (WO §6.1 — fail closed, never a substitute). Values are
# classified and never printed (targets may embed credentials). Candidate
# set is explicit; every PG-flavored nonempty value is refused, because the
# provisioner's only legitimate DB identity is the job it creates itself.
_CANDIDATE_ENV_VARS=(
    FIE_TEST_PG_DSN FIE_DB_TARGET_INTELLIGENCE FIE_DB_TARGET_RAW
    FIE_INTELLIGENCE_DB FIE_DB_PATH FIE_DATABASE_URL
)

_pg_py_guard() {  # PGPROTECT_ENV_BLOB env; stdout: CLASSIFICATION|<result>
    PGPROTECT_ENV_BLOB="${FIE_R3_GUARD_BLOB:-}" python3 - "$(_pg_repo_root)" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
from phase3.runtime_contract import (  # noqa: E402
    BACKEND_POSTGRES, Unparseable, identity_fingerprint,
    load_production_identities, parse_postgres_dsn)

def loopback(host):
    return (host.startswith("/")) or host == "localhost" \
        or host in ("127.0.0.1", "::1", "loopback") \
        or host.startswith("socket:") or host.startswith("127.")

blob = os.environ.get("PGPROTECT_ENV_BLOB", "")
rejection = ""
for line in (l.strip() for l in blob.splitlines()):
    if not line:
        continue
    var, _, value = line.partition("=")
    try:
        spec = parse_postgres_dsn(value)
    except Unparseable:
        continue  # not a PG DSN → not a candidate for the provisioner
    host = spec.get("host", "") or ""
    db = spec.get("dbname", "") or ""
    if not loopback(host):
        rejection = (
            f"PRODUCTION_DSN_REJECTED; {var} is a non-loopback PG target;"
            " the ephemeral provisioner accepts no caller DB target (value withheld)")
        break
    if db == "fie_prod" or db.startswith("fie_prod"):
        rejection = (
            f"POSTGRESQL_IDENTITY_GUARD_REJECTED; {var} names a protected"
            " production DB identity; refused fail-closed (value withheld)")
        break
    rejection = (
        f"POSTGRESQL_IDENTITY_GUARD_REJECTED; {var} carries a PG DSN in the"
        " caller environment; provisioning accepts no caller DB target (value withheld)")
    break
if rejection:
    print(f"CLASSIFICATION|{rejection}")
else:
    print("CLASSIFICATION|OK")
PY
}

_fie_test_pg_identity_guard() {
    local env_blob="" val out v
    for v in "${_CANDIDATE_ENV_VARS[@]}"; do
        val="${!v:-}"
        [ -n "${val}" ] && env_blob="${env_blob}${v}=${val}\n"
    done
    [ -n "${env_blob}" ] || return 0
    FIE_R3_GUARD_BLOB="${env_blob}"
    out="$(FIE_R3_GUARD_BLOB="${FIE_R3_GUARD_BLOB}" _pg_py_guard 2>&1)" || {
        unset FIE_R3_GUARD_BLOB
        _die "POSTGRESQL_IDENTITY_GUARD_REJECTED guard evaluation failed (out withheld; unknown != safe)"
        return "${PG_GUARD_EXIT}"
    }
    unset FIE_R3_GUARD_BLOB
    if [ "$(printf '%s' "${out}" | head -1)" = "CLASSIFICATION|OK" ]; then
        return 0
    fi
    _die "$(printf '%s' "${out}" | head -1 | sed 's/^CLASSIFICATION|//')"
    return "${PG_GUARD_EXIT}"
}

# ---------------------------------------------------------------- readiness
_pg_py() {
    local py venv
    if [ -n "${FIE_TEST_PG_PYTHON:-}" ]; then printf '%s\n' "${FIE_TEST_PG_PYTHON}"; return 0; fi
    venv="$(_pg_repo_root)/.venv/bin/python3"
    for py in "${FIE_PYTHON:-}" "${venv}" "python3"; do
        [ -n "${py}" ] || continue
        command -v "${py}" >/dev/null 2>&1 || continue
        "${py}" -c 'import psycopg' >/dev/null 2>&1 && { printf '%s\n' "${py}"; return 0; }
    done
    _die "POSTGRESQL_READINESS_FAILED no interpreter with the psycopg client is available (run scripts/bootstrap.sh --all first)"
    return "${PG_EXIT_READINESS}"
}

_loopback_bind_proof() {  # <port> -> proof lines on stdout; nonzero = violation
    local port="$1" ss_lines line addr
    if command -v ss >/dev/null 2>&1; then
        ss_lines="$(ss -ltn 2>/dev/null | awk -v p=":${port}$" '$4 ~ p')"
        if [ -n "${ss_lines}" ]; then
            while IFS= read -r line; do
                addr="$(printf '%s\n' "${line}" | awk '{print $4}')"
                if [ "${addr}" != "127.0.0.1:${port}" ]; then
                    printf 'bind|VIOLATION %s\n' "${line}"
                    return 1
                fi
            done <<PROOF_LINES
${ss_lines}
PROOF_LINES
            printf 'bind|127.0.0.1:%s LISTEN ONLY (ss proof)\n' "${port}"
            return 0
        fi
    fi
    # /proc fallback (portable; no ss dependency)
    python3 - "$1" <<'PY'
import sys
port = int(sys.argv[1])
listeners = []
for path in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = open(path).read().splitlines()[1:]
    except OSError:
        continue
    for ln in lines:
        parts = ln.split()
        if len(parts) < 4 or parts[3] != "0A":  # LISTEN state only
            continue
        addr, port_hex = parts[1].rsplit(":", 1)
        if int(port_hex, 16) == port:
            listeners.append((path, addr))
bad = [(p, a) for p, a in listeners if not a.endswith("0100007F")]
good = [(p, a) for p, a in listeners if a.endswith("0100007F")]
if bad or (not good):
    print(f"bind|VIOLATION listeners={listeners!r} (expect only 127.0.0.1)")
    sys.exit(1)
for p, a in good:
    print(f"bind|127.0.0.1:{port} LISTEN ONLY (proof via {p})")
PY
}

# ---------------------------------------------------------------- readiness probe
_pg_readiness_probe() {  # <bindir> <jobdir> <port> <user> <db> <expected_major>; PGPASSWORD in invocation env
    local bin_arg="$1" jobdir_arg="$2" port_arg="$3" user_arg="$4" db_arg="$5" major_arg="$6"
    "${PY_BIN_READY}" - "$@" <<'PY'
import json, os, sys
import psycopg
bindir, jobdir, port, user, db, major = sys.argv[1:7]
port = int(port)
# 1. socket auth (job-owned, trust) as the job's superuser on postgres DB
with psycopg.connect(host=jobdir, port=port, user=user, dbname="postgres",
                     autocommit=True) as c:
    # attach the synthetic password (job-scoped cred material from child env)
    from psycopg import sql
    c.execute(sql.SQL("ALTER USER {} WITH PASSWORD {}").format(
        sql.Identifier(user), sql.Literal(os.environ["PGPASSWORD"])))
    c.execute(
        f'CREATE DATABASE "{db}" ENCODING \'UTF8\' TEMPLATE template0')
# 2. re-connect to the SYNTHETIC DB and prove identity + UTF-8 + major
with psycopg.connect(host=jobdir, port=port, user=user, dbname=db) as c:
    cur_db, cur_user = c.execute(
        "SELECT current_database(), current_user").fetchone()
    enc = c.execute("SHOW server_encoding").fetchone()[0]
    ver = c.execute("SHOW server_version").fetchone()[0]
    assert cur_db == db and cur_user == user, (
        "synthetic identity mismatch (assertion failed; value synthetic)")
    assert enc == "UTF8", f"server_encoding {enc} is not UTF8"
    assert ver.split(".")[0] == major, f"unexpected PG major {ver}"
# 3. TCP auth proof (scram + synthetic password; 127.0.0.1 only)
with psycopg.connect(host="127.0.0.1", port=port, user=user,
                     password=os.environ["PGPASSWORD"],
                     dbname=db, sslmode="disable") as c:
    assert c.execute("SELECT current_database()").fetchone()[0] == db
print(json.dumps({
    "server_version": ver, "server_encoding": enc,
    "synthetic_db_prefix": db[:15], "synthetic_user_prefix": user[:10],
    "tcp_auth": "scram-sha-256-proven-socket-and-tcp",
    "dsn_class": "synthetic-loopback-socket-job-owned"}, indent=2))
PY
}

_pg_write_manifest() {  # <jobdir> <mode> <port> <user> <db> <bindir> <ver_str>
    python3 - "$@" <<'PY'
import datetime, json, sys
jobdir, mode, port, user, db, bindir, ver_str = sys.argv[1:8]
owner = open(f"{jobdir}/pgdata/fie_ephemeral_pg.owner").read()
manifest = {
    "job_id": owner.split("owner_job_id=", 1)[1].splitlines()[0],
    "mode": mode,
    "pg_version_string": ver_str,
    "bind": "127.0.0.1", "encoding": "UTF8",
    "port": int(port), "user": user, "db": db,
    "bin_dir": bindir, "created_utc": datetime.datetime.now(
        datetime.timezone.utc).isoformat(), "job_dir": jobdir,
}
with open(f"{jobdir}/manifest.json", "w") as f:
    json.dump(manifest, f, indent=2)
PY
}

# ---------------------------------------------------------------- start/stop
_pg_cleanup_failed_job() {  # <jobdir> <bindir>: deterministic owned-state cleanup
    local jobdir="$1" bindir="$2"
    [ -x "${bindir}/pg_ctl" ] &&
        "${bindir}/pg_ctl" -m fast stop -D "${jobdir}/pgdata" >/dev/null 2>&1 \
        || true
    rm -rf -- "${jobdir}"
    [ ! -d "${jobdir}" ] || _die "POSTGRESQL_TEARDOWN_FAILED job dir ${jobdir##*/} still present after failed-start cleanup" >&2
}

fie_test_pg_start() {  # [--force-portable]; sets FIE_TEST_PG_JOB_DIR + globals
    local force_portable=0 bindir mode port user db jobdir ver_str arg
    for arg in "$@"; do
        case "${arg}" in
            --force-portable) force_portable=1 ;;
            *) _die "unknown fie_test_pg_start option '${arg}'"; return 2 ;;
        esac
    done
    _fie_test_pg_identity_guard || return $?
    bindir=""
    if [ "${force_portable}" -eq 0 ]; then
        if bindir="$(_fie_test_pg_discover)"; then
            mode="discovered"
        else
            echo "provision-test-postgres: no preinstalled server tooling found; portable path ($(_portable_pg_arch))"
            bindir="$(_fie_test_pg_ensure_cache)" && mode="portable" || return $?
        fi
    else
        echo "provision-test-postgres: --force-portable (system discovery skipped; test-only contract)"
        bindir="$(_fie_test_pg_ensure_cache)" && mode="portable" || return $?
    fi
    # The portable path is an INSTALL ROOT (bin/ beneath it); discovery is a
    # DIR with initdb+pg_ctl directly. Normalize: everything downstream is a
    # BIN DIR.
    if [ "${mode}" = "portable" ]; then
        bindir="${bindir}/bin"
    fi
    ver_str="$("${bindir}/initdb" --version 2>/dev/null | head -1)"
    echo "provision-test-postgres: ${mode} server tooling: ${ver_str}"
    expected_major="$(printf '%s' "${ver_str}" | sed -n 's/.*PostgreSQL) \([0-9][0-9]*\).*/\1/p')"
    [ -n "${expected_major}" ] || {
        _pg_cleanup_failed_job "${jobdir}" "${bindir}" || true
        _die "POSTGRESQL_TOOLING_DISCOVERY_FAILED initdb --version output unusable; owned job state removed"
        return "${PG_EXIT_INTEGRITY}"
    }
    jobdir="$(mktemp -d "${TMPDIR:-/tmp}/fie-pgtest.XXXXXX")" || {
        _die "POSTGRESQL_START_FAILED cannot create job dir"
        return "${PG_EXIT_START}"
    }
    user="fie_$( _random_hex 6)"
    db="fie_test_$( _random_hex 6)"
    mkdir -p "${jobdir}/pgdata"
    (umask 077 && printf '%s\n' "$( _random_hex 24)" > "${jobdir}/auth.password") || {
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_START_FAILED cannot write 0600 credential material (value withheld)"
        return "${PG_EXIT_START}"
    }
    if ! "${bindir}/initdb" -D "${jobdir}/pgdata" -U "${user}" --auth=trust \
            --encoding=UTF8 --locale=C.UTF-8 \
            > "${jobdir}/initdb.log" 2>&1; then
        tail -5 "${jobdir}/initdb.log" >&2 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_INITDB_FAILED job-owned initdb failed (initdb.log tail above); owned state removed"
        return "${PG_EXIT_INITDB}"
    fi
    # Lifecycle ownership marker — created BEFORE start (proof-of-ownership
    # for teardown and for partial-init cleanup).
    printf 'owner_job_id=%s\ndata_dir=%s/\nformat=fie-6-9a-ephemeral-v1\n' \
        "$( _random_hex 12)" "${jobdir}" \
        > "${jobdir}/pgdata/fie_ephemeral_pg.owner"
    # Job-owned pg_hba: local/socket stays trust; host lines pinned to
    # scram-sha-256 so the TCP listener (127.0.0.1) authenticates with the
    # synthetic password only. System PostgreSQL configuration untouched.
    python3 - "${jobdir}/pgdata/pg_hba.conf" <<'PY'
import sys
p = sys.argv[1]
out = []
for line in open(p):
    if line.split()[:1] == ["host"] and line.rstrip().rsplit(None, 1)[-1] == "trust":
        out.append(line.rstrip().rsplit(None, 1)[0] + " scram-sha-256\n")
    else:
        out.append(line)
open(p, "w").write("".join(out))
PY
    port="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
    if ! "${bindir}/pg_ctl" -D "${jobdir}/pgdata" -l "${jobdir}/pg.log" \
            -w -t 60 -o "-p ${port} -k ${jobdir} -c listen_addresses=127.0.0.1" \
            start > "${jobdir}/pg_ctl_start.log" 2>&1; then
        tail -5 "${jobdir}/pg.log" >&2 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_START_FAILED pg_ctl start failed on loopback port ${port}; owned state removed"
        return "${PG_EXIT_START}"
    fi
    if ! _loopback_bind_proof "${port}" > "${jobdir}/bind_proof.txt" 2>&1; then
        "${bindir}/pg_ctl" -m fast stop -D "${jobdir}/pgdata" >/dev/null 2>&1 || true
        cat "${jobdir}/bind_proof.txt" >&2 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_START_FAILED loopback-only bind proof FAILED; job cluster stopped (listener must be 127.0.0.1 only)"
        return "${PG_EXIT_START}"
    fi
    PY_BIN_READY="$(_pg_py)" || {
        # No psycopg interpreter: a running job cluster would leak. Stop
        # and remove owned state; keep _pg_py's classification (95).
        fie_test_pg_stop "${jobdir}" >/dev/null 2>&1 || true
        return "${PG_EXIT_READINESS}"
    }
    if ! PGPASSWORD="$(cat "${jobdir}/auth.password")" _pg_readiness_probe \
            "${bindir}" "${jobdir}" "${port}" "${user}" "${db}" \
            "${expected_major}" \
            > "${jobdir}/readiness.json" 2>&1; then
        "${bindir}/pg_ctl" -m fast stop -D "${jobdir}/pgdata" >/dev/null 2>&1 || true
        tail -5 "${jobdir}/readiness.json" >&2 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_READINESS_FAILED readiness probe failed (job-owned; stopped and removed)"
        return "${PG_EXIT_READINESS}"
    fi
    _pg_write_manifest "${jobdir}" "${mode}" "${port}" "${user}" "${db}" \
        "${bindir}" "${ver_str}" || {
        "${bindir}/pg_ctl" -m fast stop -D "${jobdir}/pgdata" >/dev/null 2>&1 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_START_FAILED manifest write failed; owned state removed"
        return "${PG_EXIT_START}"
    }
    FIE_TEST_PG_JOB_DIR="${jobdir}"
    FIE_TEST_PG_MODE="${mode}"
    FIE_TEST_PG_BIN_DIR="${bindir}"
    FIE_TEST_PG_PORT="${port}"
    FIE_TEST_PG_USER="${user}"
    FIE_TEST_PG_DB="${db}"
    FIE_TEST_PG_VERSION_STR="${ver_str}"
    FIE_TEST_PG_DSN="postgresql://${user}@/${db}?host=${jobdir}&port=${port}"
    (umask 077 && printf 'export FIE_TEST_PG_DSN=%q\n' "${FIE_TEST_PG_DSN}" \
        > "${jobdir}/test_pg.env")
    cat "${jobdir}/readiness.json"
    echo "provision-test-postgres: READY (job-dir ${jobdir}; bind 127.0.0.1:${port}; encoding UTF8; identity proven by construction + guard pass)"
    return 0
}

# Stop ONLY the job-owned cluster and remove ONLY its job dir. Idempotent;
# refuses (exit 96) any dir whose ownership cannot be proven.
fie_test_pg_stop() {
    local dir
    if [ -n "${1:-}" ]; then dir="$1"; else dir="${FIE_TEST_PG_JOB_DIR:-}"; fi
    if [ -z "${dir:-}" ]; then
        _die "POSTGRESQL_TEARDOWN_FAILED no job dir known (nothing stopped)"
        return "${PG_EXIT_TEARDOWN}"
    fi
    if [ ! -d "${dir}" ]; then
        echo "provision-test-postgres: teardown idempotent — job ${dir##*/} state already gone"
        return 0
    fi
    local pgdata="${dir}/pgdata" marker_bin
    if [ ! -f "${pgdata}/fie_ephemeral_pg.owner" ]; then
        _die "POSTGRESQL_TEARDOWN_FAILED ownership marker absent; refusing to touch (ownership unprovable; dir left untouched for diagnosis)"
        return "${PG_EXIT_TEARDOWN}"
    fi
    if [ -f "${pgdata}/postmaster.pid" ]; then
        # Ownership: the running server must claim THIS job dir as data_dir.
        if ! grep -q "^$(printf '%s' "${pgdata}")$\|^$(printf '%s' "${pgdata}/")$" \
                "${pgdata}/postmaster.pid" 2>/dev/null; then
            _die "POSTGRESQL_TEARDOWN_FAILED postmaster.pid data_dir identity does not match this job dir; refusing stop (unrelated protected)"
            return "${PG_EXIT_TEARDOWN}"
        fi
    fi
    marker_bin="${FIE_TEST_PG_BIN_DIR:-}"
    [ -n "${marker_bin}" ] && [ -x "${marker_bin}/pg_ctl" ] || {
        marker_bin="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("bin_dir",""))' "${dir}/manifest.json" 2>/dev/null || true)"
    }
    if [ -n "${marker_bin}" ] && [ -x "${marker_bin}/pg_ctl" ]; then
        "${marker_bin}/pg_ctl" -D "${pgdata}" -m fast stop \
            > "${dir}/pg_ctl_stop.log" 2>&1 || true
    fi
    rm -rf -- "${dir}" || true
    if [ -d "${dir}" ]; then
        _die "POSTGRESQL_TEARDOWN_FAILED job dir still present after cleanup"
        return "${PG_EXIT_TEARDOWN}"
    fi
    echo "provision-test-postgres: stopped and removed ONLY job-owned state ($(date -u +%H:%M:%SZ))"
    return 0
}

# ---------------------------------------------------------------- standalone
_provisioner_main() {
    local action="${1:-}"
    case "${action}" in
        --ensure-cache)
            _fie_test_pg_ensure_cache ;;
        --start)
            shift
            fie_test_pg_start "$@" ;;
        --stop)
            shift
            fie_test_pg_stop "${1:-}" ;;
        --run)
            shift
            [ -n "${1:-}" ] || { _die "no child command for --run"; return 2; }
            fie_test_pg_start || return $?
            trap 'fie_test_pg_stop >/dev/null 2>&1 || true' EXIT
            export FIE_TEST_PG_DSN
            echo "provision-test-postgres: synthetic DSN exported to child scope only (no production fallback possible)"
            "${@}"
            rc=$?
            fie_test_pg_stop >/dev/null 2>&1 || true
            return "${rc}"
            ;;
        ""|-h|--help|*)
            _die "unknown/missing action '${action}' (fail closed)"
            printf 'USAGE:\n' >&2
            printf '  scripts/provision-test-postgres.sh --ensure-cache\n' >&2
            printf '  scripts/provision-test-postgres.sh --start [--force-portable]\n' >&2
            printf '  scripts/provision-test-postgres.sh --stop <job-dir>\n' >&2
            printf '  scripts/provision-test-postgres.sh --run <child…>\n' >&2
            return 2
            ;;
    esac
}

if [ "${BASH_SOURCE[0]:-}" = "${0}" ]; then
    _provisioner_main "$@" || exit $?
fi