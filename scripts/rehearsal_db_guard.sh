#!/bin/bash
# FIE wrapper rehearsal/test DB-target guard — 2026-10-04 incident closure.
#
# Sourced by run.sh / run_weekly.sh / run_monthly.sh. Resolves the wrapper
# intelligence-store seed target with an execution-mode distinction keyed
# on the project's existing FIE_SERVICE_ENV contract
# (phase3/service/runtime_config.py, ADR-017):
#
#   unset | local | production  -> accepted production-class behavior:
#       SEED_DB = FIE_INTELLIGENCE_DB or the hard-coded production default
#       (decision D5). One all-mode tripwire applies (below).
#   test | staging              -> rehearsal-class: the seed target MUST be
#       explicitly set; the production fallback default is FORBIDDEN, and
#       a target resolving (canonical path / same inode) to the live
#       Production intelligence store is refused before ANY write.
#   anything else               -> refused (fail closed; value withheld).
#
# All-mode tripwire (the 2026-10-04 incident signature): FIE_INTELLIGENCE_DB
# unset while the sibling HTTP-store variable FIE_DATABASE_URL is present is
# the misnamed-variable class that caused the incident — refused in every
# mode. Production cron invokes the wrappers bare (no FIE_* exports), so
# this tripwire can never fire for an accepted production run.
#
# On refusal: exit 78 with the machine-readable class
# FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED on stderr, BEFORE any DB write
# (the caller invokes this before Step 1). Error text names the
# configuration field/path class only — values are withheld (secrets never
# emitted).
#
# Exports: SEED_DB (the resolved seed target).

FIE_GUARD_RC=78

_fie_guard_fail() {
    echo "${FIE_GUARD_SCRIPT:-run-wrapper}: ${1}" >&2
    exit "${FIE_GUARD_RC}"
}

fie_wrapper_seed_db_guard() {
    # $1 = the wrapper's accepted hard-coded production default target
    local fallback_default="$1"
    local service_env
    service_env="$(printf '%s' "${FIE_SERVICE_ENV:-}" | tr '[:upper:]' '[:lower:]')"
    case "${service_env}" in
        ""|local|production)
            if [ -z "${FIE_INTELLIGENCE_DB:-}" ] && [ -n "${FIE_DATABASE_URL:-}" ]; then
                _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB is not set while a FIE_DATABASE_URL-style variable is \
present — that variable is NOT the wrapper seed-target contract \
(misnamed-variable class, refused in every mode; value withheld)"
            fi
            if [ "${service_env}" = "" ] && \
               { [ -n "${FIE_DATABASE_URL:-}" ] || [ -n "${FIE_DB_PATH:-}" ]; }; then
                # Accepted production behavior is unchanged, but a
                # production-undeclared invocation with DB-related
                # overrides is visible on stderr (no exit-code change).
                echo "run-wrapper: WARNING: production fallback default in use " \
                    "while FIE_* DB overrides are present and FIE_SERVICE_ENV is " \
                    "unset; declare FIE_SERVICE_ENV=production explicitly " \
                    "(rehearsal/test runs must use FIE_SERVICE_ENV=test|staging)" >&2
            fi
            SEED_DB="${FIE_INTELLIGENCE_DB:-${fallback_default}}"
            return 0
            ;;
        test|staging)
            ;;
        *)
            _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_SERVICE_ENV has an invalid value; refusing to resolve the intelligence \
DB target (value withheld)"
            ;;
    esac

    # --- rehearsal/test mode: explicit, validated, production-forbidden ---
    if [ -z "${FIE_INTELLIGENCE_DB:-}" ]; then
        if [ -n "${FIE_DATABASE_URL:-}" ]; then
            _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB is not set; a FIE_DATABASE_URL-style variable does not \
select the seed target (misnamed-variable class; value withheld)"
        fi
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB is not set; the production fallback default is forbidden \
in rehearsal/test mode (value withheld)"
    fi

    case "${FIE_INTELLIGENCE_DB}" in
        *" "*)
            _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
malformed FIE_INTELLIGENCE_DB (whitespace/control characters; value withheld)"
            ;;
    esac
    if printf '%s' "${FIE_INTELLIGENCE_DB}" | grep -q '[[:space:][:cntrl:]]'; then
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
malformed FIE_INTELLIGENCE_DB (whitespace/control characters; value withheld)"
    fi

    local lowered
    lowered="$(printf '%s' "${FIE_INTELLIGENCE_DB}" | tr '[:upper:]' '[:lower:]')"
    local target_path="${FIE_INTELLIGENCE_DB}"
    case "${lowered}" in
        postgres://*|postgresql://*)
            # A DSN cannot be canonicalized here; the Python store layer is
            # the DSN boundary. Accept as an explicit rehearsal target.
            SEED_DB="${FIE_INTELLIGENCE_DB}"
            return 0
            ;;
        sqlite://*)
            target_path="${FIE_INTELLIGENCE_DB#sqlite://}"
            ;;
        sqlite:*)
            target_path="${FIE_INTELLIGENCE_DB#sqlite:}"
            ;;
        *://*)
            _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
malformed FIE_INTELLIGENCE_DB (unsupported URL scheme; value withheld)"
            ;;
    esac
    case "${target_path}" in
        /*) ;;
        *)
            _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
malformed FIE_INTELLIGENCE_DB (relative path; an absolute path or DSN is \
required; value withheld)"
            ;;
    esac

    local resolved prod_default proj_default
    resolved="$(realpath -m -- "${target_path}")" ||
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
malformed FIE_INTELLIGENCE_DB (unresolvable path; value withheld)"
    prod_default="$(realpath -m -- "${fallback_default}")"
    if [ "${resolved}" = "${prod_default}" ]; then
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB resolves to the live Production intelligence store \
(canonical-path equivalent; value withheld)"
    fi
    if [ -n "${PROJECT_ROOT:-}" ]; then
        proj_default="$(realpath -m -- "${PROJECT_ROOT}/metadata/intelligence_store.db")"
        if [ "${resolved}" = "${proj_default}" ] && [ "${proj_default}" != "${prod_default}" ]; then
            _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB resolves to the project-root Production intelligence \
store layout (value withheld)"
        fi
    fi
    # Same-inode equivalence (hard links / symlinked files that already exist).
    if [ -e "${prod_default}" ] && [ -e "${resolved}" ]; then
        local t_ino p_ino
        t_ino="$(stat -c '%d:%i' -- "${resolved}")" || t_ino=""
        p_ino="$(stat -c '%d:%i' -- "${prod_default}")" || p_ino=""
        if [ -n "${t_ino}" ] && [ "${t_ino}" = "${p_ino}" ]; then
            _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB resolves to the live Production intelligence store \
(same-device/inode equivalent; value withheld)"
        fi
    fi

    SEED_DB="${FIE_INTELLIGENCE_DB}"
    return 0
}