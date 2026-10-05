#!/bin/bash
# FIE wrapper rehearsal/test DB-target guard — 2026-10-04 incident closure;
# 2026-10-05 G6 PostgreSQL-DSN production-bypass remediation.
#
# Sourced by run.sh / run_weekly.sh / run_monthly.sh. Resolves the wrapper
# intelligence-store seed target with an execution-mode distinction keyed
# on the project's existing FIE_SERVICE_ENV contract
# (phase3/service/runtime_config.py, ADR-017):
#
#   unset | local | production  -> accepted production-class behavior:
#       SEED_DB = FIE_INTELLIGENCE_DB or the hard-coded production default
#       (decision D5). One all-mode tripwire applies (below). Production
#       resolution is unchanged by this remediation.
#   test | staging              -> rehearsal-class: the seed target MUST be
#       explicitly set; the production fallback default is FORBIDDEN, and
#       a target is refused (fail closed) unless it is PROVEN
#       non-Production:
#         - filesystem/SQLite targets: canonical-path / same-inode
#           equivalence against the production stores (as before), now
#           extended with production store paths resolved from the
#           production contract files;
#         - PostgreSQL DSNs: semantic production-equivalence on the
#           resolved backend identity (scheme class, host with local
#           aliases normalized, port, database) — user/role and password
#           are deliberately NOT part of the identity; credentials are
#           never emitted (fingerprints only). Alias DSN spellings that
#           resolve to the same production identity (extra query params,
#           keyword form, %-encodings, socket-dir hosts) are equivalent.
#   anything else               -> refused (fail closed; value withheld).
#
# All-mode tripwire (the 2026-10-04 incident signature): FIE_INTELLIGENCE_DB
# unset while the sibling HTTP-store variable FIE_DATABASE_URL is present is
# the misnamed-variable class that caused the incident — refused in every
# mode. Production cron invokes the wrappers bare (no FIE_* exports), so
# this tripwire can never fire for an accepted production run.
#
# Production contract resolution (2026-10-05 G6 bypass class): the
# production identities are read from the PRODUCTION contract files
# (FIE_PRODUCTION_DB_CONTRACT overrides; default:
# /home/ubuntu/fie-67b-upgrade/fie-service.env and
# /home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env), NEVER from the wrapper
# env file actually being sourced (which a rehearsal points at its own
# fixture). This makes the invariant wrapper-ordering-independent (S4):
# even if a production DSN is injected through wrapper env BEFORE the
# guard runs, test|staging mode still compares that effective target
# against the production contract and refuses it.
#
# On refusal: exit 78 with the machine-readable class
# FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED on stderr, BEFORE any DB write
# (the caller invokes this before Step 1). Error text names the
# configuration field/path class only — values are withheld (secrets never
# emitted); PostgreSQL identity fingerprints are one-way truncated SHA-256.
#
# PG target with an unresolvable production PG contract (no contract
# files present or DSN-free): refused fail closed — unknown != safe.
#
# Exports: SEED_DB (the resolved seed target).

FIE_GUARD_RC=78

# Locate this guard's own directory (the identity helper lives next to it).
# Set at source time: BASH_SOURCE[0] is this file even while being sourced.
_fie_guard_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

_fie_guard_fail() {
    echo "${FIE_GUARD_SCRIPT:-run-wrapper}: ${1}" >&2
    exit "${FIE_GUARD_RC}"
}

# Production DB contract files: file-based sources of the production
# backend identities this guard protects. They are distinct from
# FIE_WRAPPER_ENV (which a rehearsal points at its own fixture), so
# wrapper/env sourcing order can never redefine the production identity.
_fie_guard_default_prod_contracts() {
    printf '%s\n' "/home/ubuntu/fie-67b-upgrade/fie-service.env" \
                  "/home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env"
}

# Print the production-relevant values from the contract files, one per
# line. Never printed to the user-facing stderr; consumed only here.
_fie_guard_prod_contract_values() {
    local contract_files f
    contract_files="${FIE_PRODUCTION_DB_CONTRACT:-$(_fie_guard_default_prod_contracts | tr '\n' ' ')}"
    for f in ${contract_files}; do
        [ -f "${f}" ] || continue
        # Sourcing happens in a subshell with the relevant environment
        # variables sanitised FIRST: the contract file is the sole source
        # of these values (an ambient FIE_INTELLIGENCE_DB from the wrapper
        # env must never be re-labeled a production contract value — that
        # self-reference both breaks safe targets and masks the bypass).
        bash -c 'unset FIE_DATABASE_URL FIE_DB_PATH FIE_INTELLIGENCE_DB
            . "$1"
            printf "%s\n" "${FIE_DATABASE_URL-}" "${FIE_DB_PATH-}" "${FIE_INTELLIGENCE_DB-}"' \
            _ "${f}" || return 1
    done
}

# PostgreSQL production-equivalence check for rehearsal/target modes
# (2026-10-05 G6 root-cause closure). $1 = candidate PG DSN; remaining
# args = the production contract PG DSNs. Returns 0 only when the
# candidate is proven non-Production; every refusal is exit 78
# fail-closed (S6: unknown != safe).
_fie_guard_pg_rehearsal_check() {
    local candidate="$1"
    shift
    local helper out rc v fp
    helper="${_fie_guard_dir}/db_target_identity.py"
    [ -f "${helper}" ] || \
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
guard identity helper missing; cannot prove the PG target non-Production \
(internal error; value withheld)"
    if ! command -v python3 >/dev/null 2>&1; then
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
python3 unavailable; cannot semantically compare a PostgreSQL target to \
the production contract (unknown != safe; value withheld)"
    fi
    # No production PG contract resolvable at all -> ambiguous (S6).
    if [ "$#" -eq 0 ]; then
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
no production PostgreSQL contract resolvable (production contract files \
absent or DSN-free); a PostgreSQL rehearsal target cannot be proven \
non-Production (fail closed; value withheld)"
    fi
    # DSNs travel one per line on the helper's stdin (whitespace is
    # rejected upstream); only fingerprints and verdicts come back.
    out="$( { printf 'CANDIDATE %s\n' "${candidate}";
              for v in "$@"; do printf 'PROD %s\n' "${v}"; done; } \
            | python3 "${helper}")"
    rc=$?
    if [ "${rc}" -ne 0 ] || ! printf '%s' "${out}" | grep -q '^STATUS ok'; then
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
malformed or unparseable PostgreSQL target or contract (fail closed; \
value withheld)"
    fi
    if printf '%s' "${out}" | grep -q '^EQUIVALENT 1'; then
        fp="$(printf '%s' "${out}" | grep '^CAND_FP ' | awk '{print $2}')"
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB resolves to a Production-equivalent PostgreSQL target \
(same backend identity: host/port/database class; identity fingerprint \
${fp:-unknown}, value withheld)"
    fi
    return 0
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
               { [ -n "${FIE_DATABASE_URL:-}" ] || [ -n "${FIE_DB_PATH:-}" ] || [ -n "${FIE_INTELLIGENCE_DB:-}" ]; }; then
                # Phase 6.8A (C-6 remediation): an undeclared execution mode
                # with DB-related overrides present is an INDETERMINATE
                # target context — the former non-fatal WARNING is abolished.
                # Accepted production runs declare FIE_SERVICE_ENV=production
                # explicitly (the wrapper env file does); refusal happens
                # BEFORE any write.
                _fie_guard_fail "FAIL_CLOSED_PRODUCTION_MODE_DECLARATION_REQUIRED: \
FIE_SERVICE_ENV is unset while DB-related overrides are present; declaring \
the production fallback default implicitly is no longer accepted — declare \
FIE_SERVICE_ENV=production explicitly (rehearsal/test must use \
FIE_SERVICE_ENV=test|staging; value withheld)"
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

    # --- production contract: resolve the identities to protect ----------
    local contract_value prod_dsns=() prod_paths=()
    contract_value="$(_fie_guard_prod_contract_values)"
    if [ $? -ne 0 ]; then
        _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
unreadable production contract file (fail closed; value withheld)"
    fi
    while IFS= read -r contract_value; do
        [ -n "${contract_value}" ] || continue
        case "$(printf '%s' "${contract_value}" | tr '[:upper:]' '[:lower:]')" in
            postgres://*|postgresql://*)
                prod_dsns+=("${contract_value}")
                ;;
            sqlite://*)
                prod_paths+=("${contract_value#sqlite://}")
                ;;
            sqlite:*)
                prod_paths+=("${contract_value#sqlite:}")
                ;;
            /*)
                prod_paths+=("${contract_value}")
                ;;
        esac
    done <<EOF
${contract_value}
EOF

    local lowered
    lowered="$(printf '%s' "${FIE_INTELLIGENCE_DB}" | tr '[:upper:]' '[:lower:]')"
    local target_path="${FIE_INTELLIGENCE_DB}"
    case "${lowered}" in
        postgres://*|postgresql://*)
            # A DSN cannot be canonicalized in shell: the identity helper
            # is the DSN boundary. Fail closed unless semantic comparison
            # against the production contract proves the target safe.
            _fie_guard_pg_rehearsal_check \
                "${FIE_INTELLIGENCE_DB}" \
                ${prod_dsns[@]+"${prod_dsns[@]}"}
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
    for prod_default in ${prod_paths[@]+"${prod_paths[@]}"}; do
        prod_default="$(realpath -m -- "${prod_default}")"
        if [ "${resolved}" = "${prod_default}" ]; then
            _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB resolves to a Production store from the production \
contract (canonical-path equivalent; value withheld)"
        fi
    done
    prod_default="$(realpath -m -- "${fallback_default}")"
    if [ -n "${PROJECT_ROOT:-}" ]; then
        proj_default="$(realpath -m -- "${PROJECT_ROOT}/metadata/intelligence_store.db")"
        if [ "${resolved}" = "${proj_default}" ] && [ "${proj_default}" != "${prod_default}" ]; then
            _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB resolves to the project-root Production intelligence \
store layout (value withheld)"
        fi
    fi
    # Same-inode equivalence (hard links / symlinked files that already
    # exist): candidate against the production fallback default and the
    # contract-declared production stores.
    local inode_targets=""
    if [ -e "${prod_default}" ]; then
        inode_targets="${prod_default}"
    fi
    if [ -n "${PROJECT_ROOT:-}" ]; then
        proj_default="$(realpath -m -- "${PROJECT_ROOT}/metadata/intelligence_store.db")"
        if [ -e "${proj_default}" ] && [ "${proj_default}" != "${prod_default}" ]; then
            inode_targets="${inode_targets}
${proj_default}"
        fi
    fi
    for p in ${prod_paths[@]+"${prod_paths[@]}"}; do
        p="$(realpath -m -- "${p}")"
        if [ -e "${p}" ]; then
            inode_targets="${inode_targets}
${p}"
        fi
    done
    if [ -e "${resolved}" ]; then
        local t_ino
        t_ino="$(stat -c '%d:%i' -- "${resolved}")" || t_ino=""
        while IFS= read -r p; do
            [ -n "${p}" ] || continue
            local p_ino
            p_ino="$(stat -c '%d:%i' -- "${p}")" || p_ino=""
            if [ -n "${t_ino}" ] && [ -n "${p_ino}" ] && [ "${t_ino}" = "${p_ino}" ]; then
                _fie_guard_fail "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED: \
FIE_INTELLIGENCE_DB resolves to the live Production intelligence store \
(same-device/inode equivalent; value withheld)"
            fi
        done <<EOF
${inode_targets}
EOF
    fi

    SEED_DB="${FIE_INTELLIGENCE_DB}"
    return 0
}