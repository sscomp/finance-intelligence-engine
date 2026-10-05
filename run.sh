#!/bin/bash
# 總體經濟晨報 wrapper — 供 cron job 使用
# 使用 venv 的 python 執行，輸出報告文字到 stdout
#
# v2026-07-11 (Phase 4 Operational A1): append Phase 3 pipeline artifact wiring
# - After macro_daily.py succeeds, render a Phase 3 pipeline JSON + Markdown
#   artifact to metadata/reports/artifacts/<YYYY-MM-DD>.intelligence_report.{json,md}
# - Dry-run mode: NO production DB writes; phase3/api.py:137 has the case-insensitive
#   macro_history.db refusal built in; pipeline-export does not touch any DB by default
# - macro_daily.py failure still fails the script; pipeline-export failure is explicit
#   and non-silent (own exit code propagated)
# - ARTIFACT_DATE can be overridden via env for deterministic testing; default uses
#   Asia/Taipei calendar date (morning brief is delivered at 08:00 TPE)

set -u  # fail on unset vars; do NOT set -e — we propagate each step's exit code explicitly

# Phase 6.1 portability: locate the repository from this script's own path and
# honor environment overrides — no user-specific home directory, no fixed venv:
#   FIE_PROJECT_ROOT — project root override (default: this script's directory)
#   FIE_PYTHON       — interpreter override (default: the ACTIVE environment's python3;
#                      `python3 -m venv .venv && . .venv/bin/activate` first, or export
#                      FIE_PYTHON=/path/to/venv/bin/python)
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="${FIE_PROJECT_ROOT:-$REPO_ROOT}"
# 2026-10-04 (Abacus 6.7B cutover): the invoking Hermes agent shells do not
# activate the venv, so "python3" resolves to the bare system interpreter
# without yfinance/PyYAML. Default to the provisioned production venv instead.
PYTHON_BIN="${FIE_PYTHON:-/home/ubuntu/macro-venv/bin/python3}"
cd -- "${PROJECT_ROOT}" || {
    echo "run.sh: failed to cd to ${PROJECT_ROOT}" >&2
    exit 5
}

# --- 2026-10-04 (incident closure WO): fail-closed rehearsal DB-target guard ---
# Resolve the intelligence-store seed target BEFORE any write (including
# Step 1). Uses the existing FIE_SERVICE_ENV execution-mode contract
# (phase3/service/runtime_config.py, ADR-017): unset|local|production ->
# accepted production fallback default (decision D5, unchanged); test|staging
# -> rehearsal-class: the target must be explicit (FIE_INTELLIGENCE_DB),
# malformed/missing/misnamed-only values are refused, and a target resolving
# to the live Production intelligence store is refused — exit 78 with
# FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED before anything is written.
# --- 2026-10-04 Production cutover (SQLite -> PostgreSQL), WO §23 ---
# The Production DB contract for this wrapper is now PostgreSQL: DSNs live in
# the 0600 env file below (outside git); sourcing it sets FIE_DB_PATH (raw)
# and FIE_INTELLIGENCE_DB (seed target) for the backend-aware paths (C1/C2).
# The fail-closed guard below still resolves the seed target first.
# Rollback (WO §21): remove/rename that env file, restore the SQLite service
# env, restart the FIE service; the SQLite-era literal defaults below this
# stanza become effective again.
WRAPPER_ENV="${FIE_WRAPPER_ENV:-/home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env}"
# 2026-10-05 (6.8A incident lesson): an ambient FIE_SERVICE_ENV declaration
# (e.g. a test harness exporting FIE_SERVICE_ENV=test) MUST survive the
# wrapper env file sourcing — the file's own declaration (production, for
# cron) never overrides the caller's explicit ambient declaration.
# Precedence: ambient FIE_SERVICE_ENV > wrapper env file > unset.
if [ -f "${WRAPPER_ENV}" ]; then
    _fie_wrapper_ambient_service_env="${FIE_SERVICE_ENV:-}"
    . "${WRAPPER_ENV}"
    if [ -n "${_fie_wrapper_ambient_service_env}" ]; then
        export FIE_SERVICE_ENV="${_fie_wrapper_ambient_service_env}"
    fi
    unset _fie_wrapper_ambient_service_env
fi

. "${REPO_ROOT}/scripts/rehearsal_db_guard.sh"
fie_wrapper_seed_db_guard "/home/ubuntu/macro-report/metadata/intelligence_store.db"

# Step 1: existing morning-brief generation. Failure must fail the script.
"${PYTHON_BIN}" "${PROJECT_ROOT}/macro_daily.py" 2>&1
MACRO_RC=$?
if [ "$MACRO_RC" -ne 0 ]; then
    echo "run.sh: macro_daily.py failed with exit code ${MACRO_RC}" >&2
    exit "${MACRO_RC}"
fi

# Step 2: Phase 3 pipeline artifact (Phase 4 Operational A1).
#   Reuses phase3.cli pipeline-export (dry-run by default) + the canonical
#   report naming convention <run_label>.intelligence_report.{json,md}
#   (phase3/pipeline/reporting.py:65). No DB writes, no scheduling.
ARTIFACT_DATE="${ARTIFACT_DATE:-$(TZ=Asia/Taipei date +%F)}"
ARTIFACT_DIR="${FIE_ARTIFACT_DIR:-${PROJECT_ROOT}/metadata/reports/artifacts}"

# Defense-in-depth: refuse if the artifact dir basename is the reserved
# production DB name. Mirrors the case-insensitive macro_history.db refusal
# used across the Phase 3 CLI surface (phase3/api.py:137 + phase3/cli.py:642).
ARTIFACT_BASENAME="$(basename -- "${ARTIFACT_DIR}")"
if [ "${ARTIFACT_BASENAME,,}" = "macro_history.db" ]; then
    echo "run.sh: REFUSING — artifact dir basename is the reserved 'macro_history.db'" >&2
    exit 3
fi

# Create parent directory safely. Idempotent: no error if it already exists.
mkdir -p -- "${ARTIFACT_DIR}" || {
    echo "run.sh: failed to mkdir -p ${ARTIFACT_DIR}" >&2
    exit 4
}

# --- MVR 2026-08-10: derive --company and --industry from config files ---
# Read taiwan50_config.json constituents → --company <code> per stock
# Read industry_config.json top-level keys → --industry <id> per industry
# Both config files are at the repo root (FIE_CONFIG_DIR overridable).
# Fallback: empty args (macro-only).
TW50_CONFIG="${FIE_CONFIG_DIR:-${PROJECT_ROOT}}/taiwan50_config.json"
INDUSTRY_CONFIG="${FIE_CONFIG_DIR:-${PROJECT_ROOT}}/industry_config.json"

if [ -f "${TW50_CONFIG}" ]; then
    COMPANY_ARGS=$("${PYTHON_BIN}" -c "
import json
with open('${TW50_CONFIG}') as f:
    data = json.load(f)
args = []
for c in data['constituents']:
    args.append('--company')
    args.append(c['code'])
print(' '.join(args))
") || COMPANY_ARGS=""
else
    COMPANY_ARGS=""
fi

if [ -f "${INDUSTRY_CONFIG}" ]; then
    INDUSTRY_ARGS=$("${PYTHON_BIN}" -c "
import json
with open('${INDUSTRY_CONFIG}') as f:
    data = json.load(f)
args = []
for k in data:
    args.append('--industry')
    args.append(k)
print(' '.join(args))
") || INDUSTRY_ARGS=""
else
    INDUSTRY_ARGS=""
fi

# --- FIE 2026-08-10: real-score bridge wiring ---
# Seed the signal_log from macro_history.db so pipeline-export produces
# real scores (not 0.0 defaults).  --persist is required because the
# seed step writes signals into the target DB before scoring.
# 2026-10-04 (Abacus 6.7B cutover, WO D5): SEED_DB now points at the
# PERSISTENT Phase 3B intelligence store (schema v1, created by the
# accepted `init-db` mechanism), so the daily FIE 6.7B HTTP API serves
# the accumulating intelligence instead of a per-run /tmp DB.
# seed-from-history uses upserts (idempotent); macro_history.db stays
# read-only throughout.
# SEED_DB is resolved fail-closed above by the rehearsal DB-target guard
# (production mode keeps the accepted D5 fallback default).
# WO C2 backend awareness: a postgres:// SEED_DB is a backend DSN, not a
# filesystem path — materializing/dereferencing it here is meaningless.
case "$(printf '%s' "${SEED_DB}" | tr '[:upper:]' '[:lower:]')" in
    postgres://*|postgresql://*) ;;
    *)
        mkdir -p -- "$(dirname -- "${SEED_DB}")" || {
            echo "run.sh: failed to mkdir for seed DB" >&2
            exit 4
        }
        ;;
esac
# --freshness-check (Workstream H): classification is warnings-only (stderr/JSON
# warnings; no exit-code change) so a stale/unreachable source is VISIBLE in the
# artifact and operator output instead of silently looking fresh.
# Phase 6.8A: no literal fallback for the raw source — a missing raw target
# contract (FIE_DB_TARGET_RAW / FIE_DB_PATH) is fail-closed exit 78
# (WO C-4 remediation; the historical ${PROJECT_ROOT}/macro_history.db
# substitution is abolished).
if [ -z "${FIE_DB_PATH:-}" ]; then
    echo "run.sh: FAIL_CLOSED_DB_TARGET_REQUIRED: no raw-layer target " \
        "contract (FIE_DB_TARGET_RAW / FIE_DB_PATH unset); refusing to " \
        "resolve an implicit source DB (value withheld)" >&2
    exit 78
fi
SEED_ARGS=(--seed-from-history --db-path "${SEED_DB}" --persist --freshness-check
    --source-db "${FIE_DB_PATH}"
    --industry-config "${FIE_CONFIG_DIR:-${PROJECT_ROOT}}/industry_config.json")

PYTHONPATH="${PROJECT_ROOT}" "${PYTHON_BIN}" -m phase3.cli pipeline-export \
    --date "${ARTIFACT_DATE}" \
    --run-label "${ARTIFACT_DATE}" \
    --output-dir "${ARTIFACT_DIR}" \
    "${SEED_ARGS[@]}" \
    ${COMPANY_ARGS} ${INDUSTRY_ARGS} 2>&1
PIPELINE_RC=$?
if [ "$PIPELINE_RC" -ne 0 ]; then
    echo "run.sh: phase3 pipeline-export failed with exit code ${PIPELINE_RC}" >&2
    exit "${PIPELINE_RC}"
fi

exit 0
