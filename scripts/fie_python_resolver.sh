#!/bin/bash
# FIE canonical Python interpreter resolver — Phase 6.9A-R4-R2-R1 (Task B).
#
# ONE repository-owned resolution contract for every runtime/entrypoint that
# needs a Python interpreter (run.sh / run_weekly.sh / run_monthly.sh,
# scripts/test-cloud.sh, and the psycopg-preference iterator of
# scripts/provision-test-postgres.sh). Replaces the per-wrapper operator-host
# default (`/home/ubuntu/macro-venv/bin/python3`) exposed by the R4-R2 Codex
# Cloud run — no user-/host-specific absolute path may remain as a usable
# fallback.
#
# Sourceable library: `. scripts/fie_python_resolver.sh`
#
#   fie_python_version_ok <bin>      — 0 iff interpreter >= 3.11 (pyproject)
#   fie_python_candidates            — prints candidate list (one per line,
#                                      precedence order; no version check)
#   fie_resolve_python               — resolves the interpreter; sets
#                                      FIE_PYTHON_BIN (and FIE_PYTHON_SOURCE
#                                      to override|venv|repo-venv|path);
#                                      nonzero = no usable interpreter
#                                      (callers exit 78, fail closed)
#
# Precedence (ABACUS_FIE_6_9A_R4_R2_R1 §7/B1):
#   1. explicit FIE_PYTHON override — HARD contract (like
#      FIE_TEST_PG_CACHE_DIR): usable → honored; usable-but-incompatible with
#      the repository floor → fail closed; set-but-not-executable → fail
#      closed. NEVER a silent substitution of another interpreter.
#   2. the caller's active virtual environment ($VIRTUAL_ENV), when valid;
#   3. the repository venv (<repo>/.venv/bin/python3, created by
#      scripts/bootstrap.sh) when valid;
#   4. `python3` from PATH, when valid;
#   5. fail closed with actionable diagnostics (exit class 78).
#
# Contract notes:
#   * There is no operator-host fallback: `/home/ubuntu/macro-venv/...` and
#     any other user-specific absolute path are deliberately absent. A host
#     that wants the historical interpreter pins it explicitly via FIE_PYTHON.
#   * Validity = executable AND version >= 3.11 (pyproject requires-python).
#     A bare interpreter without the runtime deps (yfinance/PyYAML) still
#     resolves but fails later at import — visibly, never silently (the
#     wrappers propagate Step-1 exit codes; bootstrap declares the dep set).
#   * Paths with spaces are safe: every use is quoted; the candidates are
#     one-per-line, never word-split.
set -u

FIE_PYTHON_EXIT_FAIL_CLOSED=78   # shared fail-closed exit class (6.8A/6.9A)

 fie_python_version_ok() {  # <bin>
    "${1}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

_fie_resolver_repo_root() {
    printf '%s' "$(dirname -- "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)")"
}

fie_python_candidates() {  # prints candidate list, precedence order (no check)
    # 1. explicit override (hard contract; the resolver itself validates it)
    [ -n "${FIE_PYTHON:-}" ] && printf '%s\n' "${FIE_PYTHON}"
    # 2. active virtual environment
    [ -n "${VIRTUAL_ENV:-}" ] && printf '%s/bin/python3\n' "${VIRTUAL_ENV%/}"
    # 3. repository venv
    printf '%s/.venv/bin/python3\n' "$(_fie_resolver_repo_root)"
    # 4. PATH python3
    printf 'python3\n'
}

fie_resolve_python() {  # -> FIE_PYTHON_BIN; 0 ok / 78 fail closed
    local repo_root repo_venv_python cand active_venv
    repo_root="$(_fie_resolver_repo_root)"
    repo_venv_python="${repo_root}/.venv/bin/python3"
    # 1. explicit FIE_PYTHON — hard contract, never silently substituted
    if [ -n "${FIE_PYTHON:-}" ]; then
        if fie_python_version_ok "${FIE_PYTHON}"; then
            FIE_PYTHON_BIN="${FIE_PYTHON}"
            FIE_PYTHON_SOURCE="override"
            return 0
        fi
        printf '%s\n' \
            "fie_python_resolver: FIE_PYTHON override is set but unusable (not executable or older than 3.11; value withheld); refusing to substitute another interpreter (override is a hard contract — unset FIE_PYTHON or point it at a usable interpreter)" >&2
        return "${FIE_PYTHON_EXIT_FAIL_CLOSED}"
    fi
    # 2. active virtual environment
    active_venv="${VIRTUAL_ENV:+${VIRTUAL_ENV%/}/bin/python3}"
    if [ -n "${active_venv}" ] && fie_python_version_ok "${active_venv}"; then
        FIE_PYTHON_BIN="${active_venv}"
        FIE_PYTHON_SOURCE="venv"
        return 0
    fi
    # 3. repository venv (scripts/bootstrap.sh)
    if fie_python_version_ok "${repo_venv_python}"; then
        FIE_PYTHON_BIN="${repo_venv_python}"
        FIE_PYTHON_SOURCE="repo-venv"
        return 0
    fi
    # 4. PATH python3
    cand="$(command -v python3 2>/dev/null || true)"
    if [ -n "${cand}" ] && fie_python_version_ok "${cand}"; then
        FIE_PYTHON_BIN="${cand}"
        FIE_PYTHON_SOURCE="path"
        return 0
    fi
    printf '%s\n' \
        "fie_python_resolver: no usable Python interpreter found (checked: FIE_PYTHON override [unset], \$VIRTUAL_ENV, ${repo_venv_python}, PATH python3);" >&2
    printf '%s\n' \
        "         create one with: python3 -m venv .venv && . .venv/bin/activate && pip install -e ." >&2
    return "${FIE_PYTHON_EXIT_FAIL_CLOSED}"
}