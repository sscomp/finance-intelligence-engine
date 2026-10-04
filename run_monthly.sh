#!/bin/bash
# 公司研究月報 wrapper — 供 cron job 使用
# 使用 venv 的 python 執行，輸出月報文字到 stdout
#
# v2026-07-11 (Phase 4 Operational A3, FIE-P4-OPS-006):
#  - Reuse the Phase 3 pipeline-export artifact contract to produce a
#    deterministic JSON + Markdown envelope alongside the existing
#    monthly fundamentals text (the file naming follows
#    `<run_label>.intelligence_report.{json,md}` per
#    phase3/pipeline/reporting.py:65).
#  - 5 patterns adopted from `references/phase4-runsh-pipeline-artifact-wiring.md`:
#    1. set -u (NOT set -e) + explicit per-step RC propagation, distinct
#       exit codes for distinct failure modes (cd=5, basename guard=3,
#       mkdir=4, step RC propagated).
#    2. Reuse the canonical `<run_label>.intelligence_report.{json,md}`
#       naming convention (--run-label "${ARTIFACT_DATE}-monthly").
#    3. TZ=Asia/Taipei inline, ${ARTIFACT_DATE:-default} env-overrideable.
#    4. Defense-in-depth basename refusal of `macro_history.db` (case-
#       insensitive), exit code 3.
#    5. Pipeline export step does NOT replace the existing company_monthly
#       text step — it is a strict additive second step. Failure of
#       step 1 still fails the script; failure of step 2 is non-silent
#       and propagates its own exit code.
#  - production safety: company_monthly.py writes to macro_history.db
#    via db.save_stock_monthly / save_institutional_snapshot (intentional,
#    pre-existing contract — the row is the monthly snapshot). The
#    pipeline-export step runs DRY-RUN (default) and never touches a DB,
#    so no new writes are introduced by A3.

set -u  # fail on unset vars; do NOT set -e — we propagate each step's exit code explicitly

# Phase 6.1 portability: locate the repository from this script's own path and
# honor environment overrides — no user-specific home directory, no fixed venv:
#   FIE_PROJECT_ROOT — project root override (default: this script's directory)
#   FIE_PYTHON       — interpreter override (default: the ACTIVE environment's python3;
#                      `python3 -m venv .venv && . .venv/bin/activate` first, or export
#                      FIE_PYTHON=/path/to/venv/bin/python)
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="${FIE_PROJECT_ROOT:-$REPO_ROOT}"
# 2026-10-04 (Abacus 6.7B cutover): pin the provisioned production venv —
# the invoking Hermes agent shell does not activate it (see run.sh).
PYTHON_BIN="${FIE_PYTHON:-/home/ubuntu/macro-venv/bin/python3}"
cd -- "${PROJECT_ROOT}" || {
    echo "run_monthly.sh: failed to cd to ${PROJECT_ROOT}" >&2
    exit 5
}

# --- 2026-10-04 (incident closure WO): fail-closed rehearsal DB-target guard ---
# Resolve the intelligence-store seed target BEFORE any write (including
# Step 1); mirrors run.sh. Uses the existing FIE_SERVICE_ENV execution-mode
# contract: unset|local|production -> accepted production fallback default
# (D5, unchanged); test|staging -> rehearsal-class: explicit, validated,
# production-forbidden (exit 78, FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED).
. "${REPO_ROOT}/scripts/rehearsal_db_guard.sh"
fie_wrapper_seed_db_guard "/home/ubuntu/macro-report/metadata/intelligence_store.db"

# Step 1: existing company-monthly generation. Failure must fail the script.
# company_monthly.py is interactive (yfinance + TWSE T86 network calls);
# the cron entry-point prompt for 5eaa5fa9a50d warns that this step
# needs at least 300s timeout.
"${PYTHON_BIN}" "${PROJECT_ROOT}/company_monthly.py" 2>&1
COMPANY_RC=$?
if [ "$COMPANY_RC" -ne 0 ]; then
    echo "run_monthly.sh: company_monthly.py failed with exit code ${COMPANY_RC}" >&2
    exit "${COMPANY_RC}"
fi

# Step 2: Phase 3 pipeline artifact (Phase 4 Operational A3).
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
    echo "run_monthly.sh: REFUSING — artifact dir basename is the reserved 'macro_history.db'" >&2
    exit 3
fi

# Create parent directory safely. Idempotent: no error if it already exists.
mkdir -p -- "${ARTIFACT_DIR}" || {
    echo "run_monthly.sh: failed to mkdir -p ${ARTIFACT_DIR}" >&2
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
            echo "run_monthly.sh: failed to mkdir for seed DB" >&2
            exit 4
        }
        ;;
esac
# --freshness-check (Workstream H): classification is warnings-only (stderr/JSON
# warnings; no exit-code change) so a stale/unreachable source is VISIBLE in the
# artifact and operator output instead of silently looking fresh.
SEED_ARGS=(--seed-from-history --db-path "${SEED_DB}" --persist --freshness-check
    --source-db "${FIE_DB_PATH:-${PROJECT_ROOT}/macro_history.db}"
    --industry-config "${FIE_CONFIG_DIR:-${PROJECT_ROOT}}/industry_config.json")

PYTHONPATH="${PROJECT_ROOT}" "${PYTHON_BIN}" -m phase3.cli pipeline-export \
    --date "${ARTIFACT_DATE}" \
    --run-label "${ARTIFACT_DATE}-monthly" \
    --output-dir "${ARTIFACT_DIR}" \
    "${SEED_ARGS[@]}" \
    ${COMPANY_ARGS} ${INDUSTRY_ARGS} 2>&1
PIPELINE_RC=$?
if [ "$PIPELINE_RC" -ne 0 ]; then
    echo "run_monthly.sh: phase3 pipeline-export failed with exit code ${PIPELINE_RC}" >&2
    exit "${PIPELINE_RC}"
fi

exit 0
