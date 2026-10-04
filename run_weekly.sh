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
# 2026-10-04 (Abacus 6.7B cutover): pin the provisioned production venv —
# the invoking Hermes agent shell does not activate it (see run.sh).
PYTHON_BIN="${FIE_PYTHON:-/home/ubuntu/macro-venv/bin/python3}"
cd -- "${PROJECT_ROOT}" || {
    echo "run_weekly.sh: failed to cd to ${PROJECT_ROOT}" >&2
    exit 5
}

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
SEED_DB="${FIE_INTELLIGENCE_DB:-/home/ubuntu/macro-report/metadata/intelligence_store.db}"
mkdir -p -- "$(dirname -- "${SEED_DB}")" || {
    echo "run_weekly.sh: failed to mkdir for seed DB" >&2
    exit 4
}
# --freshness-check (Workstream H): classification is warnings-only (stderr/JSON
# warnings; no exit-code change) so a stale/unreachable source is VISIBLE in the
# artifact and operator output instead of silently looking fresh.
SEED_ARGS=(--seed-from-history --db-path "${SEED_DB}" --persist --freshness-check
    --source-db "${FIE_DB_PATH:-${PROJECT_ROOT}/macro_history.db}"
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
