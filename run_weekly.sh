#!/bin/bash
# 產業趨勢週報 wrapper — 供 cron job 使用
# 使用 venv 的 python 執行，輸出報告文字到 stdout
#
# v2026-07-11 (Phase 4 Operational A1 sibling — Weekly wrapper wiring):
# - After industry_weekly.py succeeds, render a Phase 3 pipeline JSON + Markdown
#   artifact to metadata/reports/artifacts/<YYYY-MM-DD>-weekly.intelligence_report.{json,md}
# - Dry-run mode: NO production DB writes; phase3/api.py:137 has the case-insensitive
#   macro_history.db refusal built in; pipeline-export does not touch any DB by default
# - industry_weekly.py failure still fails the script; pipeline-export failure is explicit
#   and non-silent (own exit code propagated)
# - ARTIFACT_DATE can be overridden via env for deterministic testing; default uses
#   Asia/Taipei calendar date (weekly report is delivered at 08:00 TPE every Monday)
# - Mirrors the run.sh (Op A1 daily) pattern exactly; same stub-and-restore + explicit
#   RC + canonical naming + TZ=Asia/Taipei + basename guard

set -u  # fail on unset vars; do NOT set -e — we propagate each step's exit code explicitly

# Phase 6.1 portability: locate the repository from this script's own path and
# honor environment overrides — no user-specific home directory, no fixed venv:
#   FIE_PROJECT_ROOT — project root override (default: this script's directory)
#   FIE_PYTHON       — interpreter override (default: the ACTIVE environment's python3;
#                      `python3 -m venv .venv && . .venv/bin/activate` first, or export
#                      FIE_PYTHON=/path/to/venv/bin/python)
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="${FIE_PROJECT_ROOT:-$REPO_ROOT}"
# 2026-10-04 (Abacus 6.7B cutover) → 2026-10-05 (6.9A-R4-R2-R1 Task B):
# interpreter resolution is the ONE canonical repository contract
# (scripts/fie_python_resolver.sh) — mirrors run.sh; no operator-host
# fallback path (R4-R2 blocker #1).
. "${REPO_ROOT}/scripts/fie_python_resolver.sh"
PYTHON_BIN=""
fie_resolve_python || PYTHON_RESOLVE_RC=$?
PYTHON_BIN="${FIE_PYTHON_BIN:-}"
if [ "${PYTHON_RESOLVE_RC:-0}" -ne 0 ]; then
    echo "run_weekly.sh: python interpreter resolution failed (canonical" \
        "resolver diagnostics above; exit ${PYTHON_RESOLVE_RC}); set" \
        "FIE_PYTHON (e.g. the repository venv interpreter) and retry" >&2
    exit 78
fi
cd -- "${PROJECT_ROOT}" || {
    echo "run_weekly.sh: failed to cd to ${PROJECT_ROOT}" >&2
    exit 5
}

# --- 2026-10-04 (incident closure WO): fail-closed rehearsal DB-target guard ---
# Resolve the intelligence-store seed target BEFORE any write (including
# Step 1); mirrors run.sh. Uses the existing FIE_SERVICE_ENV execution-mode
# contract: unset|local|production -> accepted production fallback default
# (D5, unchanged); test|staging -> rehearsal-class: explicit, validated,
# production-forbidden (exit 78, FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED).
# --- 2026-10-04 Production cutover (SQLite -> PostgreSQL), WO §23 ---
# The Production DB contract for this wrapper is now PostgreSQL: DSNs live in
# the 0600 env file below (outside git); sourcing it sets FIE_DB_PATH (raw)
# and FIE_INTELLIGENCE_DB (seed target) for the backend-aware paths (C1/C2).
# The fail-closed guard below still resolves the seed target first.
# Rollback (WO §21): remove/rename that env file, restore the SQLite service
# env, restart the FIE service; the SQLite-era literal defaults below this
# stanza become effective again.
WRAPPER_ENV="${FIE_WRAPPER_ENV:-/home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env}"
# 2026-10-05 (6.8C production hygiene): operator-owned 0600 asset outside git
# (see run.sh note). Keep going when absent — DB-target resolution fails
# closed if the production contract cannot be satisfied. Template:
# examples/fie-wrapper.env.example.
if [ ! -f "${WRAPPER_ENV}" ] && [ -z "${FIE_WRAPPER_ENV:-}" ]; then
    echo "run_weekly.sh: wrapper env file not found: ${WRAPPER_ENV}" >&2
    echo "run_weekly.sh: (operator bootstrap: FIE_WRAPPER_ENV=<0600 env file>; see examples/fie-wrapper.env.example for the documented shape)" >&2
fi
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
# 2026-10-05 (6.8C production hygiene): D5 fallback default now derives from
# PROJECT_ROOT (identical on the operator host, portable elsewhere); still
# PRODUCTION_ONLY — rehearsal-class service envs refuse it.
fie_wrapper_seed_db_guard "${PROJECT_ROOT}/metadata/intelligence_store.db"

# Step 1: existing industry-weekly generation. Failure must fail the script.
"${PYTHON_BIN}" "${PROJECT_ROOT}/industry_weekly.py" 2>&1
INDUSTRY_RC=$?
if [ "$INDUSTRY_RC" -ne 0 ]; then
    echo "run_weekly.sh: industry_weekly.py failed with exit code ${INDUSTRY_RC}" >&2
    exit "${INDUSTRY_RC}"
fi

# Step 2: Phase 3 pipeline artifact (Phase 4 Operational A1 sibling — Weekly).
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
    echo "run_weekly.sh: REFUSING — artifact dir basename is the reserved 'macro_history.db'" >&2
    exit 3
fi

# Create parent directory safely. Idempotent: no error if it already exists.
mkdir -p -- "${ARTIFACT_DIR}" || {
    echo "run_weekly.sh: failed to mkdir -p ${ARTIFACT_DIR}" >&2
    exit 4
}

# --- MVR 2026-08-10: derive --company and --industry from config files ---
# Read taiwan50_config.json constituents → --company <code> per stock
# Read industry_config.json top-level keys → --industry <id> per industry
# Both config files are at the repo root. Fallback: empty args (macro-only).
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
# real scores (not 0.0 defaults).  Uses a per-run temp intelligence DB so
# macro_history.db stays read-only.  --persist is required because the
# seed step writes signals into the target DB before scoring.
# SEED_DB is resolved fail-closed above by the rehearsal DB-target guard
# (production mode keeps the accepted D5 fallback default). DSN targets
# skip the filesystem mkdir (backend specifier, not a path) — same case
# rule as run.sh (WO C2 backend awareness).
case "$(printf '%s' "${SEED_DB}" | tr '[:upper:]' '[:lower:]')" in
    postgres://*|postgresql://*) ;;
    *)
        mkdir -p -- "$(dirname -- "${SEED_DB}")" || {
            echo "run_weekly.sh: failed to mkdir for seed DB" >&2
            exit 4
        }
        ;;
esac
# --freshness-check (Workstream H): classification is warnings-only (stderr/JSON
# warnings; no exit-code change) so a stale/unreachable source is VISIBLE in the
# artifact and operator output instead of silently looking fresh.
# Phase 6.8A: no literal fallback for the raw source — fail-closed exit 78
# when the raw target contract is absent (WO C-4 remediation).
if [ -z "${FIE_DB_PATH:-}" ]; then
    echo "run_weekly.sh: FAIL_CLOSED_DB_TARGET_REQUIRED: no raw-layer " \
        "target contract (FIE_DB_TARGET_RAW / FIE_DB_PATH unset); refusing " \
        "to resolve an implicit source DB (value withheld)" >&2
    exit 78
fi
SEED_ARGS=(--seed-from-history --db-path "${SEED_DB}" --persist --freshness-check
    --source-db "${FIE_DB_PATH}"
    --industry-config "${FIE_CONFIG_DIR:-${PROJECT_ROOT}}/industry_config.json")

PYTHONPATH="${PROJECT_ROOT}" "${PYTHON_BIN}" -m phase3.cli pipeline-export \
    --date "${ARTIFACT_DATE}" \
    --run-label "${ARTIFACT_DATE}-weekly" \
    --output-dir "${ARTIFACT_DIR}" \
    "${SEED_ARGS[@]}" \
    ${COMPANY_ARGS} ${INDUSTRY_ARGS} 2>&1
PIPELINE_RC=$?
if [ "$PIPELINE_RC" -ne 0 ]; then
    echo "run_weekly.sh: phase3 pipeline-export failed with exit code ${PIPELINE_RC}" >&2
    exit "${PIPELINE_RC}"
fi

exit 0
