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
#   * ISOLATED PostgreSQL (6.9A-R3): FIE_TEST_PG_DSN wins if provided (then
#     asserted non-Production: loopback socket/loopback TCP only — any other
#     host or an unprovable target fails closed before tests run). Otherwise
#     the canonical provisioner (scripts/provision-test-postgres.sh) owns
#     the target: discovered server tooling first, then a pinned,
#     sha256-verified portable PostgreSQL distribution — a host with NO
#     initdb/pg_ctl still gets a real ephemeral cluster (loopback-only,
#     UTF-8, synthetic identity; teardown removes ONLY that job). If even
#     acquisition cannot succeed the PG legs SKIP with their explicit
#     classification — never a silent SQLite fallback, never a Production
#     fallback.
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
UNITS=" "
for arg in ${1:-} ${2:-}; do
    case "${arg}" in
        -v|--verbose) UNITS=" -v " ;;
        --full) MODE="full" ;;
        *) echo "test-cloud: unknown option '${arg}'" >&2; exit 5 ;;
    esac
done

# Interpreter: the ONE canonical resolver (6.9A-R4-R2-R1 Task B) — explicit
# FIE_PYTHON (hard contract) > active venv > repo .venv > PATH python3.
. "${REPO}/scripts/fie_python_resolver.sh"
PY_BIN=""
fie_resolve_python || PY_RESOLVE_RC=$?
PY_BIN="${FIE_PYTHON_BIN:-}"
[ "${PY_RESOLVE_RC:-0}" -ne 0 ] && {
    echo "test-cloud: python interpreter resolution failed (canonical resolver diagnostics above)" >&2
    exit 3
}
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
CALLER_FORCE_PORTABLE="${FIE_TEST_PG_FORCE_PORTABLE:-}"
for v in $(env | grep -oE '^FIE_[A-Za-z0-9_]+' | tr '\n' ' '); do unset "${v}" || true; done
unset PGPASSWORD 2>/dev/null || true
export FIE_SERVICE_ENV=test
export FIE_HTTP_PORT="${FIE_HTTP_PORT:-18720}"
export PYTHONPATH="${REPO}"
export ARTIFACT_DIR=""

EPHEMERAL=0
cleanup() {
    if [ "${EPHEMERAL}" -eq 1 ]; then
        fie_test_pg_stop >/dev/null 2>&1 \
            || echo "test-cloud: WARNING — teardown of self-provisioned cluster reported failure" >&2
    fi
    # R4-R1: a temp-mode (job-local) portable cache created by THIS scope is
    # job-owned ephemeral runtime state — remove it ONLY after the job
    # cluster is stopped (the provisioner's cleanup removes ONLY the dir it
    # created and marker-proves; a persistent cache is never touched).
    fie_test_pg_cache_cleanup >/dev/null 2>&1 \
        || echo "test-cloud: WARNING — temp-mode cache cleanup reported failure" >&2
}
trap cleanup EXIT

# ---- isolated PostgreSQL provisioning (canonical provisioner, 6.9A-R3) ----
# One canonical implementation: scripts/provision-test-postgres.sh —
# sourceable library; discovery > Maven-Central portable distribution
# (pinned, sha256-verified). No caller DSN, no Production fallback,
# loopback-only, UTF-8 pinned, synthetic identity; teardown stops ONLY this
# job cluster.
if [ ! -x "${REPO}/scripts/provision-test-postgres.sh" ]; then
    echo "test-cloud: scripts/provision-test-postgres.sh missing (canonical provisioner)" >&2
    exit 5
fi
# shellcheck disable=SC1091
. "${REPO}/scripts/provision-test-postgres.sh"
command -v fie_test_pg_start >/dev/null 2>&1 || {
    echo "test-cloud: canonical provisioner library failed to load" >&2
    exit 5
}

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
    # No caller DSN: the canonical provisioner owns the target. It discovers
    # system PG tooling first (same preference order as before), then falls
    # back to the pinned, sha256-verified portable distribution (R3): a job
    # with NO initdb/pg_ctl on the host still gets a real PostgreSQL server,
    # never a silent SQLite fallback, never a Production fallback.
    if fie_test_pg_start ${CALLER_FORCE_PORTABLE:+--force-portable}; then
        export FIE_TEST_PG_DSN
        EPHEMERAL=1
    else
        echo "test-cloud: PG legs will SKIP — ephemeral PG provisioning" \
            "unavailable (classification printed above; never a silent" \
            "SQLite fallback, never a Production fallback)" >&2
    fi
fi

DUMP_TARGET="sqlite-only (PG legs skipped by documented reason)"
[ -n "${FIE_TEST_PG_DSN:-}" ] && DUMP_TARGET="isolated (${FIE_TEST_PG_DSN%%\?*}…)"
echo "test-cloud: postgres test target = ${DUMP_TARGET}"

# ---- portable distribution cache policy (6.9A-R4-R2-R1, Tasks C/D) --------
# The portable-legged contract tests run against the ACTIVE canonically
# resolved cache (tests/cloud_child_env.py is the only helper that resolves
# it — no second resolution engine). When the active cache is cold through
# unusable-HOME/XDG (isolated temp mode, per-invocation), ONE explicit
# job-local cache is warmed under this policy and exported for the suite:
#   FIE_TEST_PG_ALLOW_REAL_ACQUISITION=1 (test-cloud default) — the helper
#     may perform ONE network-true warm acquisition; failure ⇒ the
#     portable-legged tests skip with explicit attribution (never silent).
#   =0 (suite-direct default) — fully offline; cold cache ⇒ attributed skips.
export FIE_TEST_PG_ALLOW_REAL_ACQUISITION="${FIE_TEST_PG_ALLOW_REAL_ACQUISITION:-1}"

# ---- negative controls (executed, not read; WO Task G) -------------------
if ! "${PY_BIN}" scripts/cloud_negative_controls.py; then
    echo "test-cloud: FAIL — runtime negative controls" >&2
    exit 1
fi

# ---- tests ---------------------------------------------------------------
if [ "${MODE}" = "full" ]; then
    echo "test-cloud: full hermetic regression (unittest discover)"
    "${PY_BIN}" -m unittest discover${UNITS}-s tests --top-level-dir=.
    RC=$?
else
    echo "test-cloud: focused cloud-readiness set"
    "${PY_BIN}" -m unittest${UNITS}\
        tests.phase3.test_runtime_contract \
        tests.phase3.persistence.test_backend_contract \
        tests.phase3.persistence.test_rehearsal_db_target_guard \
        tests.phase3.persistence.test_67b_r4_sqlite_policy \
        tests.phase3.test_raw_layer_contract_matrix \
        tests.test_bootstrap_cli_contract \
        tests.test_rehearsal_wrapper_guard \
        tests.test_cloud_pg_provisioning \
        tests.test_cloud_pg_cache_resolver \
        tests.test_python_resolver_contract \
        tests.test_cloud_child_network_context \
        tests.test_pg_cache_lock_ownership \
        tests.test_rehearsal_guard_zero_write_invariant \
        tests.test_wrapper_guard_68a \
        tests.test_db_target_identity \
        tests.test_resource_ownership_contract
    RC=$?
fi

if [ "${RC}" -eq 0 ]; then
    echo "test-cloud: PASS (${MODE})"
else
    echo "test-cloud: FAIL (${MODE}) rc=${RC}" >&2
fi
exit "${RC}"