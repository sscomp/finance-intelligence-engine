#!/bin/bash
# FIE cloud-safe isolated test entrypoint — Phase 6.8C (WO Tasks F/H).
#
# One obvious entrypoint for a fresh clone / ephemeral cloud environment:
#
#     bash scripts/test-cloud.sh                # focused cloud-readiness set
#     bash scripts/test-cloud.sh --full         # full regression (hermetic)
#
# Contract:
#   * SCRUBBED environment: every FIE_* / PGPASSWORD variable from the
#     caller is dropped, then the contract-declared rehearsal profile is
#     set explicitly (FIE_SERVICE_ENV=test). Production credentials are
#     never required and never consulted.
#   * ISOLATED PostgreSQL: FIE_TEST_PG_DSN wins if provided (then asserted
#     non-Production: loopback socket/loopback TCP only — any other host or
#     an unprovable target fails closed before tests run); otherwise this
#     script provisions a fresh ephemeral cluster (trust auth, user-level,
#     $TMPDIR socket, per-run port) when PG tooling is available, or runs
#     SQLite-only with PG legs skipping by their documented reason.
#   * Production DSN negative controls are executed (run
#     scripts/cloud_negative_controls.py) — cloud-readiness set includes it,
#     full regression appends it as a final gate.
#   * No production mutation, no service interaction, no env-file I/O.
#   * Ephemeral resources are cleaned up on exit (trap; best effort for the
#     externally-provided DSN, complete for the self-provisioned cluster).
#   * Exit code mirrors the unittest result exactly; failures are visible.
set -u

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "${REPO}" || { echo "test-cloud: cannot cd ${REPO}" >&2; exit 5; }
MODE="focused"
[ -n "${1:-}" ] && [ "${1}" = "--full" ] && MODE="full"

# Interpreter: explicit FIE_PYTHON wins; else the canonical .venv created by
# scripts/bootstrap.sh; else bare python3 (and the deps check below decides).
PY_BIN="${FIE_PYTHON:-}"
[ -z "${PY_BIN}" ] && [ -x "${REPO}/.venv/bin/python3" ] && PY_BIN="${REPO}/.venv/bin/python3"
[ -z "${PY_BIN}" ] && PY_BIN="python3"
if ! "${PY_BIN}" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    echo "test-cloud: python3 >= 3.11 required (run scripts/bootstrap.sh first)" >&2
    exit 3
fi
# Test deps come through pip install -e ".[test,postgres]" (bootstrap.sh).
"${PY_BIN}" -c 'import yaml' 2>/dev/null || {
    echo "test-cloud: dependencies missing (run scripts/bootstrap.sh first)" >&2
    exit 3
}

# ---- scrubbed, contract-declared environment -----------------------------
CALLER_TEST_PG_DSN="${FIE_TEST_PG_DSN:-}"
CALLER_TEST_PG_PORT="${FIE_TEST_PG_PORT:-}"
for v in $(env | grep -oE '^FIE_[A-Za-z0-9_]+' | tr '\n' ' '); do unset "${v}" || true; done
unset PGPASSWORD 2>/dev/null || true
export FIE_SERVICE_ENV=test
export FIE_HTTP_PORT="${FIE_HTTP_PORT:-18720}"
export PYTHONPATH="${REPO}"
export ARTIFACT_DIR=""

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/fie-cloud-test.XXXXXX")"
EPHEMERAL=0
cleanup() {
    if [ "${EPHEMERAL}" -eq 1 ]; then
        "${PG_BINDIR}/pg_ctl" -D "${TMPROOT}/pgdata" -m fast stop \
            >/dev/null 2>&1 || true
        rm -rf -- "${TMPROOT}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---- isolated PostgreSQL provisioning (Task F) ---------------------------
if [ -n "${CALLER_TEST_PG_DSN}" ]; then
    case "${CALLER_TEST_PG_DSN}" in
        *host=/tmp/*)
            :  # documented loopback-socket form — provable isolation
            ;;
        *)
            case "${CALLER_TEST_PG_DSN}" in
                postgresql://*127.0.0.1*|postgresql://*localhost*|\
postgres://*127.0.0.1*|postgres://*localhost*)
                    :  # loopback TCP form — provable isolation
                    ;;
                *)
                    echo "test-cloud: FAIL_CLOSED — FIE_TEST_PG_DSN host is neither" \
                        "a /tmp socket nor loopback; target identity unprovable" >&2
                    exit 78
                    ;;
            esac
            ;;
    esac
    export FIE_TEST_PG_DSN="${CALLER_TEST_PG_DSN}"
    # Preflight: the caller-provided test target must be PROVEN
    # non-Production against the production identity contract. Fail closed
    # before any test runs (WO Task F; unknown != safe). This is where an
    # accidentally-production DSN (e.g. real loopback:5432 DSN) is refused.
    if ! "${PY_BIN}" - "${REPO}" <<'PREFLIGHT'
import sys
sys.path.insert(0, sys.argv[1])
import os
from phase3.runtime_contract import (
    assert_rehearsal_target_safe, load_production_identities, parse_target)
spec = parse_target(os.environ["FIE_TEST_PG_DSN"])
prod = load_production_identities()
assert_rehearsal_target_safe(spec, prod)
# 6.8C fresh-clone finding: a SQL_ASCII test server makes psycopg return
# TEXT as bytes (text-comparison contract tests break). The test target
# must be UTF8 — refuse otherwise (fail closed before tests run).
try:
    import psycopg
except ImportError:
    # No psycopg in this environment → PG parity legs will skip anyway.
    psycopg = None
if psycopg is None:
    print("test-cloud: preflight — test PG target proven non-Production"
          " (identity OK; encoding unprobeable: psycopg absent)")
else:
    try:
        with psycopg.connect(os.environ["FIE_TEST_PG_DSN"]) as conn:
            enc = conn.execute("SHOW server_encoding").fetchone()[0]
            if enc != "UTF8":
                print(f"FAIL_CLOSED_TEST_PG_ENCODING_NOT_UTF8 (server_encoding={enc})")
                raise SystemExit(2)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"FAIL_CLOSED_TEST_PG_ENCODING_PROBE_FAILED ({exc})")
        raise SystemExit(2)
    print("test-cloud: preflight — test PG target proven non-Production"
          " (identity OK; server_encoding=UTF8)")
PREFLIGHT
    then
        echo "test-cloud: FAIL_CLOSED — FIE_TEST_PG_DSN could not be proven" \
            "non-Production; refusing to run tests (unknown != safe)" >&2
        exit 78
    fi
    echo "test-cloud: using caller-provided isolated PG (FIE_TEST_PG_DSN)"
else
    PG_BINDIR=""
    for d in /usr/lib/postgresql/18/bin /usr/lib/postgresql/*/bin; do
        [ -x "${d}/initdb" ] && PG_BINDIR="${d}" && break
    done
    PGBIN_ALT="$(PATH="${PATH}" command -v initdb 2>/dev/null || true)"
    if [ -z "${PG_BINDIR}" ] && [ -n "${PGBIN_ALT}" ]; then
        PG_BINDIR="$(dirname -- "${PGBIN_ALT}")"
    fi
    if [ -n "${PG_BINDIR}" ]; then
        PGPORT_TEST="${CALLER_TEST_PG_PORT:-54331}"
        echo "test-cloud: provisioning ephemeral PG cluster (${PG_BINDIR}, port ${PGPORT_TEST})"
        # Encoding is pinned explicitly, NOT inherited from ambient locale
        # (6.8C fresh-clone finding): under a scrubbed env (env -i / POSIX
        # locale) initdb leaves server_encoding=SQL_ASCII, and psycopg then
        # returns TEXT values as bytes — every text-comparison contract test
        # fails. C.UTF-8 is provided by base glibc (>=2.35), no locale-gen
        # service needed.
        mkdir -p "${TMPROOT}/pgdata"
        if ! "${PG_BINDIR}/initdb" -D "${TMPROOT}/pgdata" -U fie --auth=trust \
                --encoding=UTF8 --locale=C.UTF-8 \
                >/dev/null 2>&1; then
            echo "test-cloud: initdb failed; PG legs will skip (documented reason)" >&2
            "${PG_BINDIR}/initdb" -D "${TMPROOT}/pgdata" -U fie --auth=trust \
                --encoding=UTF8 --locale=C.UTF-8 2>&1 | tail -5 >&2 || true
        else
            "${PG_BINDIR}/pg_ctl" -D "${TMPROOT}/pgdata" -l "${TMPROOT}/pg.log" \
                -w -t 60 -o "-p ${PGPORT_TEST} -k ${TMPROOT}" start >/dev/null 2>&1 \
                && "${PG_BINDIR}/createdb" -h "${TMPROOT}" -p "${PGPORT_TEST}" -U fie fie_test \
                && ENC="$("${PG_BINDIR}/psql" -h "${TMPROOT}" -p "${PGPORT_TEST}" \
                        -U fie -d postgres -Atc "SHOW server_encoding" 2>/dev/null)" \
                && [ "${ENC}" = "UTF8" ] \
                && export FIE_TEST_PG_DSN="postgresql://fie@/fie_test?host=${TMPROOT}&port=${PGPORT_TEST}" \
                && EPHEMERAL=1 \
                || { echo "test-cloud: ephemeral cluster start/encoding check failed" \
                           "(server_encoding must be UTF8); PG legs skip" >&2; }
        fi
    else
        echo "test-cloud: no PG tooling found; running SQLite-only (PG legs skip with documented reason)"
    fi
fi

DUMP_TARGET="sqlite-only (PG legs skipped by documented reason)"
[ -n "${FIE_TEST_PG_DSN:-}" ] && DUMP_TARGET="isolated (${FIE_TEST_PG_DSN%%\?*}…)"
echo "test-cloud: postgres test target = ${DUMP_TARGET}"

# ---- negative controls (executed, not read; WO Task G) -------------------
if ! "${PY_BIN}" scripts/cloud_negative_controls.py; then
    echo "test-cloud: FAIL — runtime negative controls" >&2
    exit 1
fi

# ---- tests ---------------------------------------------------------------
if [ "${MODE}" = "full" ]; then
    echo "test-cloud: full hermetic regression (unittest discover)"
    "${PY_BIN}" -m unittest discover -s tests --top-level-dir=.
    RC=$?
else
    echo "test-cloud: focused cloud-readiness set"
    "${PY_BIN}" -m unittest \
        tests.phase3.test_runtime_contract \
        tests.phase3.persistence.test_backend_contract \
        tests.phase3.persistence.test_rehearsal_db_target_guard \
        tests.phase3.persistence.test_67b_r4_sqlite_policy \
        tests.phase3.test_raw_layer_contract_matrix \
        tests.test_rehearsal_wrapper_guard \
        tests.test_rehearsal_guard_zero_write_invariant \
        tests.test_wrapper_guard_68a \
        tests.test_db_target_identity
    RC=$?
fi

if [ "${RC}" -eq 0 ]; then
    echo "test-cloud: PASS (${MODE})"
else
    echo "test-cloud: FAIL (${MODE}) rc=${RC}" >&2
fi
exit "${RC}"