#!/bin/bash
# FIE canonical bootstrap — Phase 6.8C production hygiene (Task D).
#
# One obvious, idempotent, non-interactive entrypoint for a FRESH clone:
#
#     bash scripts/bootstrap.sh                 # runtime deps only
#     bash scripts/bootstrap.sh --with-test     # + pytest/pandas
#     bash scripts/bootstrap.sh --with-postgres # + psycopg (PG parity legs)
#     bash scripts/bootstrap.sh --all           # test + postgres
#
# Contract:
#   * creates `<repo>/.venv` (idempotent: reuses an existing, healthy venv);
#   * installs exactly what pyproject.toml declares — no ad-hoc extras;
#   * verifies the engine runtime imports at the end and prints next steps;
#   * NEVER provisions or touches a database, never reads/writes any env
#     file, never requires operator-host state or Production credentials;
#   * fail-fast with clear messages (no silent continuation).
#
# The canonical TEST entrypoint is `scripts/test-cloud.sh` (isolated,
# cloud-safe; see README "Testing" and
# docs/architecture/cloud-execution-contract.md).
set -euo pipefail
IFS=$'\n\t'

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${FIE_VENV_DIR:-${REPO}/.venv}"
MODE_TEST=0; MODE_PG=0
for arg in "${@:-}"; do
    case "${arg}" in
        --with-test) MODE_TEST=1 ;;
        --with-postgres) MODE_PG=1 ;;
        --all) MODE_TEST=1; MODE_PG=1 ;;
        *) echo "bootstrap: unknown option '${arg}' (see header usage)" >&2; exit 2 ;;
    esac
done

echo "bootstrap: repo=${REPO}"
echo "bootstrap: venv=${VENV}"

# 1. venv (idempotent) — python 3.11+ required (pyproject requires-python >=3.11).
PY_BIN="${FIE_PYTHON:-python3}"
"${PY_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "bootstrap: python3 is older than 3.11 (pyproject requires-python >=3.11)" >&2
    exit 3
}
if [ ! -x "${VENV}/bin/python3" ]; then
    echo "bootstrap: creating venv"
    "${PY_BIN}" -m venv "${VENV}"
fi
# A broken/incomplete venv is replaced, not trusted (idempotency).
"${VENV}/bin/python3" -m pip --version >/dev/null 2>&1 || {
    echo "bootstrap: venv unusable (no pip); recreating" >&2
    rm -rf "${VENV}"
    "${PY_BIN}" -m venv "${VENV}"
}

# 2. install exactly the declared dependency surface (pyproject.toml).
EXTRAS=""
[ "${MODE_TEST}" -eq 1 ] && EXTRAS="test"
[ "${MODE_PG}" -eq 1 ] && EXTRAS="${EXTRAS:+${EXTRAS},}postgres"
echo "bootstrap: pip install -e .${EXTRAS:+[${EXTRAS}]}"
"${VENV}/bin/python3" -m pip install --quiet --upgrade pip
if [ -n "${EXTRAS}" ]; then
    "${VENV}/bin/python3" -m pip install -q -e "${REPO}[${EXTRAS}]"
else
    "${VENV}/bin/python3" -m pip install -q -e "${REPO}"
fi

# 3. verify the engine runtime imports with only the declared deps.
echo "bootstrap: verifying runtime imports"
"${VENV}/bin/python3" - "${REPO}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import phase3.cli  # noqa: F401  (engine + CLI import-safe, offline)
print("bootstrap: import check OK (python", sys.version.split()[0], ")")
PY

echo "bootstrap: DONE"
echo "bootstrap: next — bash ${REPO}/scripts/test-cloud.sh  (cloud-safe isolated test entrypoint)"
echo "bootstrap: NOTE — runtime use needs explicit DB targets via FIE_DB_TARGET_*"
echo "bootstrap:        env (fail-closed; see README 'Current production state' and"
echo "bootstrap:         examples/fie-wrapper.env.example for the operator shape)."