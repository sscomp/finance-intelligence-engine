#!/bin/bash
# FIE ephemeral test-PostgreSQL provisioner — Phase 6.9A-R3 (portable path).
#
# ONE canonical repository-owned boundary for isolated PostgreSQL
# validation infrastructure (ABACUS_FIE_6_9A_R3, §3/§6). Two shapes:
#
#   * sourceable library  ( . scripts/provision-test-postgres.sh
#                           && fie_test_pg_start          # start cluster
#                           && fie_test_pg_stop )         # own teardown
#   * standalone script   scripts/provision-test-postgres.sh --ensure-cache
#                         scripts/provision-test-postgres.sh --start
#                         scripts/provision-test-postgres.sh --stop <dir>
#                         scripts/provision-test-postgres.sh --run [--force-portable] <child…>
#
# Contract (ABACUS_FIE_6_9A_R3 §6 — MUST-holds):
#   * NEVER reads/uses/falls back to a Production DSN, the fie_prod data
#     store, Production credentials, or generic ambient DB env vars as a
#     provisioning substitute. Any production-flavored candidate target in
#     the caller environment FAILS CLOSED (exit 78).
#   * Job-owned identity ONLY: per-run TMPDIR, random synthetic user/
#     database/password, loopback-only bind, UTF-8 pinned, job-owned port,
#     explicit lifecycle ownership marker.
#   * Server acquisition: deterministic discovery first (FIE_TEST_PG_BIN
#     explicit test-only override > /usr/lib/postgresql/<N>/bin > PATH),
#     otherwise PORTABLE distribution from Maven Central
#     (io.zonky.test :embedded-postgres-binaries-linux-*), version PINNED
#     to PORTABLE_PG_VERSION, sha256-verified, cached, no sudo, no
#     interactive prompt, no global daemon, no system PG config mutation.
#   * Explicit failure classifications (docs/architecture/
#     ephemeral-postgresql-provisioning.md) — never collapsed into a
#     generic test failure; no silent SQLite substitution (SQLite is
#     rollback/recovery only, handled by the resolver, not here).
#   * Deterministic readiness (process + TCP auth + identity + UTF-8) and
#     deterministic teardown (ownership-proven, idempotent; only ever stops
#    /removes the job-owned cluster and job dir).
#
# Credentials: the synthetic password is written ONLY to a 0600 file inside
# the job TMPDIR, used only for the readiness TCP-auth proof (as a child
# environment value, never argv) and never printed. Nothing here reads or
# stores any secret. The test DSN exported for children is the loopback
# socket form WITHOUT a password (socket auth is job-owned trust; TCP
# host-lines are pinned to scram-sha-256 for this job's pg_hba only).
set -u

PORTABLE_PG_VERSION="18.4.0"   # exact pin (== Production major.minor 18.4)
PORTABLE_PG_BASE_URL="https://repo1.maven.org/maven2/io/zonky/test/postgres"

# One library dir root; the repo root is its parent.
_PROVISIONER_LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-.}")" && pwd -P)"

PG_GUARD_EXIT=78               # shared fail-closed exit class (6.8A)
PG_EXIT_PLATFORM=90            # POSTGRESQL_PLATFORM_UNSUPPORTED
PG_EXIT_CACHE_PATH=97          # POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE
PG_EXIT_ARTIFACT=91            # POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE
PG_EXIT_INTEGRITY=92           # POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED
PG_EXIT_INITDB=93              # POSTGRESQL_INITDB_FAILED
PG_EXIT_START=94               # POSTGRESQL_START_FAILED
PG_EXIT_READINESS=95           # POSTGRESQL_READINESS_FAILED
PG_EXIT_TEARDOWN=96            # POSTGRESQL_TEARDOWN_FAILED

_die() { printf 'provision-test-postgres: %s\n' "${1}" >&2; }

# ONE canonical interpreter resolution contract (6.9A-R4-R2-R1 Task B):
# fie_python_candidates / fie_python_version_ok come from the shared
# resolver library — no repository-local ordering engine here.
ife_python_resolver_lib="${_PROVISIONER_LIB_DIR}/fie_python_resolver.sh"
if [ ! -f "${ife_python_resolver_lib}" ]; then
    _die "POSTGRESQL_PLATFORM_UNSUPPORTED canonical resolver library missing: scripts/fie_python_resolver.sh"
    exit 5
fi
. "${ife_python_resolver_lib}"
unset ife_python_resolver_lib

_pg_repo_root() {
    printf '%s' "$(dirname -- "${_PROVISIONER_LIB_DIR}")"
}

_random_hex() { python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_hex(int(sys.argv[1])))' "${1-8}"; }

# ---------------------------------------------------------------- canonical cache resolver (R4-R1)
# ONE canonical writable-runtime resolver (ABACUS_FIE_6_9A_R4_R1 §6). The R4
# defect: the shipped default cache root was `${HOME}/.cache/...` and a mere
# `mkdir` failure there collapsed into POSTGRESQL_PLATFORM_UNSUPPORTED (exit
# 90) — "$HOME is set" was treated as evidence of writability. In some
# managed job filesystems HOME exists but the default cache location is on a
# read-only filesystem (R4 blocker: `/home/agent/.cache` EROFS/EROFS-equivalent).
# Resolution PROBES actual usability (creatable + write + unlink probe), so:
#
#   1. explicit FIE_TEST_PG_CACHE_DIR override — HARD contract: usable or
#      fail closed (exit 97), NEVER a silent fallback;
#   2. XDG_CACHE_HOME (only when defined) — `.../fie/test-postgres`;
#   3. `${HOME}/.cache/fie/test-postgres` — only when probe-proven usable;
#   4. isolated job-local temp fallback — `mktemp`-allocated FIE-owned
#      directory under `${TMPDIR:-/tmp}` (unique per invocation, i.e.
#      collision-safe for concurrent jobs; NEVER a static `/tmp/fie`).
#
# Sourceable-shape contract (globals set by the resolver):
#   PG_CACHE_DIR            resolved cache root (probe-proven usable)
#   PG_CACHE_MODE           explicit | xdg | home | temp
#   PG_CACHE_PERSISTENT     yes (reusable across jobs) | no (job-local temp)
#   PG_CACHE_TEMP_CREATED   set ONLY when THIS invocation created a temp dir
#   PG_CACHE_TRACE          ';'-joined per-candidate rejection reasons
#                           (class labels only; no paths, no values)
#
# TEMP-mode dirs carry a `fie_cache.owner` marker (format
# `fie-6-9a-cache-v1`): they are reusable within this shell scope only
# (memoized resolution), and `fie_test_pg_cache_cleanup` removes ONLY a dir
# THIS invocation created and marker-proves — cleanup never touches a
# persistent cache or a foreign temp dir.
#
# Lock ownership contract (ABACUS_FIE_6_9A_R4_R2_R1 Task E; R4-R5-R1 Task D
# canonicalization): `.acquire.lock` in a persistent cache carries ownership
# metadata (format `fie-6-9a-acquire-lock-v1` + invocation id / pid /
# created_utc), written under the held flock. Classification
# (fie_test_pg_cache_lock_classify):
#
#   ABSENT            no lock file — the current job may create it
#                     EXCLUSIVELY (O_EXCL/noclobber); the creating
#                     invocation owns the inode by construction.
#   ACTIVE            flock is held by a live acquirer (never touched)
#   STALE_JOB_OWNED   fie-format metadata AND flock-free — adoptable per
#                     the documented stale-owner protocol: acquire holds
#                     the flock, re-proves the fie marker UNDER the flock
#                     against the live inode, then refreshes metadata;
#                     cleanup holds the flock, re-proves the marker, then
#                     removes it (flock/dead-owner proof + the acquirers'
#                     inode-verification loop make removal race-free);
#   UNKNOWN legacy-empty/foreign/empty — fail closed: ownership metadata
#                     is NEVER written in place (writability does not
#                     prove ownership; an existing unknown object cannot
#                     become job-owned through mutation), the object is
#                     never claimed and never deleted; the resolver
#                     rejects the candidate and falls back to an isolated
#                     job-local cache (R4-R5-R1 Task D); cleanup preserves
#                     + reports it (exit 96)
PG_CACHE_TEMP_CREATED=""

_cache_trace_add() {  # <reason> (class labels only)
    _FIE_CACHE_TRACE="${_FIE_CACHE_TRACE:+${_FIE_CACHE_TRACE};}${1}"
}

_cache_dir_usable_probe() {  # <label> <dir>: 0 iff creatable AND writable
    local label="$1" dir="$2" probe
    if [ -e "${dir}" ] && [ ! -d "${dir}" ]; then
        _cache_trace_add "${label}:file-occupies-path"; return 1
    fi
    if ! mkdir -p -- "${dir}" 2>/dev/null; then
        _cache_trace_add "${label}:not-creatable"; return 1
    fi
    probe="$(mktemp -q "${dir}/.fie-writable-probe.XXXXXX" 2>/dev/null)" || {
        _cache_trace_add "${label}:not-writable"; return 1
    }
    rm -f -- "${probe}"
    return 0
}

# R4-R5-R1 Task D — the "safe isolated alternative" leg of the ownership
# contract: when the resolved persistent cache's pre-existing acquisition
# lock cannot be claimed (UNKNOWN content: legacy 0-byte / foreign /
# unparseable — ownership unprovable), provisioning falls back to an
# isolated job-local temp cache. The unknown object is NEVER claimed
# (ownership cannot be inferred from writability — the R4-R5 FI09
# violation) and NEVER deleted; it stays exactly as found for cleanup's
# fail-closed preserve-and-report (exit 96).
_cache_fallback_temp_alloc() {  # sets PG_CACHE_* to job-local temp; 0 ok
    local tmproot="${TMPDIR:-/tmp}" cand
    if [ ! -d "${tmproot}" ] ||         ! _cache_dir_usable_probe temp-root "${tmproot}"; then
        _cache_trace_add "temp-root-unusable"
        _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE no creatable/writable cache-runtime candidate exists on this filesystem (rejected: ${_FIE_CACHE_TRACE}); provisioning refused"
        return "${PG_EXIT_CACHE_PATH}"
    fi
    cand="$(mktemp -d "${tmproot%/}/fie-pgcache.XXXXXX" 2>/dev/null)" || {
        _cache_trace_add "temp-unique-alloc-failed"
        _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE no creatable/writable cache-runtime candidate exists on this filesystem (rejected: ${_FIE_CACHE_TRACE}); provisioning refused"
        return "${PG_EXIT_CACHE_PATH}"
    }
    chmod 700 "${cand}" 2>/dev/null || true
    (umask 077 && printf 'format=fie-6-9a-cache-v1\ncreated_utc=%s\nephemeral=yes\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)") > "${cand}/fie_cache.owner"
    PG_CACHE_DIR="${cand}"
    PG_CACHE_MODE="temp"
    PG_CACHE_PERSISTENT="no"
    PG_CACHE_TEMP_CREATED="${cand}"
    echo "provision-test-postgres: isolated job-local cache fallback (pre-existing acquisition lock unclaimable; R4-R5-R1 Task D): ${cand}" >&2
    return 0
}

_fie_test_pg_cache_resolve() {  # sets PG_CACHE_DIR/MODE/PERSISTENT (+ trace)
    local cand
    _FIE_CACHE_TRACE=""
    # In-scope memo: a dir resolved by THIS shell scope stays valid while it
    # remains usable; the TEMP-mode memo gives one acquisition per scope
    # (test-cloud / --run provision exactly once per scope).
    if [ -n "${_FIE_CACHE_RESOLVED_DIR:-}" ] &&
        _cache_dir_usable_probe memo "${_FIE_CACHE_RESOLVED_DIR}"; then
        PG_CACHE_DIR="${_FIE_CACHE_RESOLVED_DIR}"
        PG_CACHE_MODE="${_FIE_CACHE_RESOLVED_MODE}"
        PG_CACHE_PERSISTENT="${_FIE_CACHE_RESOLVED_PERSISTENT}"
        return 0
    fi
    if [ -n "${FIE_TEST_PG_CACHE_DIR:-}" ]; then
        # 1. explicit override — hard contract; unusable => fail closed.
        #    (An UNKNOWN pre-existing acquisition LOCK inside it does NOT
        #    authorize claiming: the claim protocol fails closed and the
        #    provisioning path selects the isolated job-local fallback;
        #    R4-R5-R1 Task D. The resolver itself keeps returning the
        #    candidate so the cleanup contract can still inspect + report
        #    the foreign lock fail-closed.)
        if _cache_dir_usable_probe override "${FIE_TEST_PG_CACHE_DIR}"; then
            PG_CACHE_DIR="${FIE_TEST_PG_CACHE_DIR}"
            PG_CACHE_MODE="explicit"
            PG_CACHE_PERSISTENT="yes"
        else
            _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE explicit FIE_TEST_PG_CACHE_DIR override is not creatable/writable on this filesystem (value withheld); refusing to fall back (override is a hard contract)"
            return "${PG_EXIT_CACHE_PATH}"
        fi
    elif [ -n "${XDG_CACHE_HOME:-}" ] && \
        _cache_dir_usable_probe xdg "${XDG_CACHE_HOME%/}/fie/test-postgres"; then
        # 2. XDG cache (probe-proven usable)
        PG_CACHE_DIR="${XDG_CACHE_HOME%/}/fie/test-postgres"
        PG_CACHE_MODE="xdg"
        PG_CACHE_PERSISTENT="yes"
    elif [ -n "${HOME:-}" ] && \
        _cache_dir_usable_probe home "${HOME%/}/.cache/fie/test-postgres"; then
        # 3. HOME cache — only when actually unusable rejects fall through;
        #    "$HOME is set" is NOT evidence of writability (R4 root cause)
        PG_CACHE_DIR="${HOME%/}/.cache/fie/test-postgres"
        PG_CACHE_MODE="home"
        PG_CACHE_PERSISTENT="yes"
    else
        # 4. isolated job-local temp fallback
        local tmproot="${TMPDIR:-/tmp}"
        if [ ! -d "${tmproot}" ] ||
            ! _cache_dir_usable_probe temp-root "${tmproot}"; then
            _cache_trace_add "temp-root-unusable"
            _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE no creatable/writable cache-runtime candidate exists on this filesystem (rejected: ${_FIE_CACHE_TRACE}); provisioning refused"
            return "${PG_EXIT_CACHE_PATH}"
        fi
        cand="$(mktemp -d "${tmproot%/}/fie-pgcache.XXXXXX" 2>/dev/null)" || {
            _cache_trace_add "temp-unique-alloc-failed"
            _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE no creatable/writable cache-runtime candidate exists on this filesystem (rejected: ${_FIE_CACHE_TRACE}); provisioning refused"
            return "${PG_EXIT_CACHE_PATH}"
        }
        chmod 700 "${cand}" 2>/dev/null || true
        (umask 077 && printf 'format=fie-6-9a-cache-v1\ncreated_utc=%s\nephemeral=yes\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)") > "${cand}/fie_cache.owner"
        PG_CACHE_DIR="${cand}"
        PG_CACHE_MODE="temp"
        PG_CACHE_PERSISTENT="no"
        PG_CACHE_TEMP_CREATED="${cand}"
    fi
    _FIE_CACHE_RESOLVED_DIR="${PG_CACHE_DIR}"
    _FIE_CACHE_RESOLVED_MODE="${PG_CACHE_MODE}"
    _FIE_CACHE_RESOLVED_PERSISTENT="${PG_CACHE_PERSISTENT}"
    echo "provision-test-postgres: cache resolver: mode=${PG_CACHE_MODE} persistent=${PG_CACHE_PERSISTENT}${_FIE_CACHE_TRACE:+ (rejected: ${_FIE_CACHE_TRACE})}" >&2
    return 0
}

# TEMP-mode cache cleanup: removes ONLY the temp dir THIS invocation created,
# and only when the marker proves it is FIE-owned (ownership unprovable => refuse).
# 6.9A-R4-R2-R1 (Task E): additionally cleans ONLY proven job-owned stale
# acquisition-lock residue in the RESOLVED persistent cache (metadata +
# flock-free proof); ACTIVE/UNKNOWN/legacy-empty locks are preserved and
# reported (fail closed), never destructively removed.
fie_test_pg_cache_cleanup() {
    local dir="${PG_CACHE_TEMP_CREATED:-}"
    if [ -n "${dir}" ]; then
        if [ ! -d "${dir}" ]; then
            PG_CACHE_TEMP_CREATED=""
        elif ! grep -q "^format=fie-6-9a-cache-v1$" "${dir}/fie_cache.owner" 2>/dev/null; then
            _die "POSTGRESQL_TEARDOWN_FAILED temp cache ${dir##*/} lacks the FIE ownership marker; refusing removal (ownership unprovable)"
            return "${PG_EXIT_TEARDOWN}"
        elif ! rm -rf -- "${dir}" 2>/dev/null; then
            _die "POSTGRESQL_TEARDOWN_FAILED temp cache cleanup failed"
            return "${PG_EXIT_TEARDOWN}"
        elif [ -d "${dir}" ]; then
            _die "POSTGRESQL_TEARDOWN_FAILED temp cache still present after cleanup"
            return "${PG_EXIT_TEARDOWN}"
        else
            PG_CACHE_TEMP_CREATED=""
            echo "provision-test-postgres: temp-mode cache removed (job-owned ephemeral only)"
        fi
    fi
    # Task E: stale job-owned acquisition-lock residue in a resolved
    # persistent cache ( fie-format + flock-free => the locking invocation
    # is provably dead; the acquirers' readlink-deleted-marker loop makes
    # removal race-free). Everything else is preserved.
    # A cleanup-only invocation (run without a prior in-scope resolution)
    # still OWNS this residue check: resolve the cache through the ONE
    # canonical resolver first (R4-R2 blocker #3: no $HOME re-derivation).
    if [ -z "${PG_CACHE_DIR:-}" ]; then
        PG_CACHE_DIR=""; PG_CACHE_MODE=""; PG_CACHE_PERSISTENT=""
        _FIE_CACHE_TRACE=""
        if _fie_test_pg_cache_resolve; then
            if [ "${PG_CACHE_MODE}" = "temp" ]; then
                # This cleanup invocation just allocated a job-local temp
                # cache it never used — remove it immediately (own marker).
                rm -rf -- "${PG_CACHE_DIR}" 2>/dev/null || true
                PG_CACHE_TEMP_CREATED=""
                PG_CACHE_DIR=""; PG_CACHE_MODE=""; PG_CACHE_PERSISTENT=""
            fi
        else
            # Resolution failed (e.g. unusable explicit override): the same
            # fail-closed contract as provisioning (value withheld).
            PG_CACHE_DIR=""; PG_CACHE_MODE=""; PG_CACHE_PERSISTENT=""
            return "${PG_EXIT_CACHE_PATH}"
        fi
    fi
    if [ -n "${PG_CACHE_DIR:-}" ] && [ "${PG_CACHE_PERSISTENT:-no}" = "yes" ]; then
        case "$(fie_test_pg_cache_lock_classify "${PG_CACHE_DIR}")" in
            LOCK_CLASS=STALE_JOB_OWNED)
                if _fie_cache_lock_remove_if_stale_owned "${PG_CACHE_DIR}"; then
                    echo "provision-test-postgres: job-owned stale .acquire.lock removed (metadata + dead-owner proven)"
                else
                    _die "POSTGRESQL_TEARDOWN_FAILED job-owned stale .acquire.lock removal failed (preserved; nothing foreign touched)"
                    return "${PG_EXIT_TEARDOWN}"
                fi
                ;;
            LOCK_CLASS=UNKNOWN)
                _die "POSTGRESQL_TEARDOWN_FAILED cache acquisition lock ownership UNKNOWN (legacy/empty form; preserved and reported; no removal performed)"
                return "${PG_EXIT_TEARDOWN}"
                ;;
            LOCK_CLASS=ACTIVE|LOCK_CLASS=ABSENT) : ;;
        esac
    fi
    return 0
}

# ---------------------------------------------------------------- acquisition

# Lock ownership metadata (R4-R2 Task E). Written UNDER the held flock so
# the metadata provably belongs to the locking invocation.
_fie_cache_lock_write_metadata() {  # <lockfile>
    : > "${1}"
    printf 'format=fie-6-9a-acquire-lock-v1\ninvocation=%s\npid=%s\ncreated_utc=%s\ncache_mode=%s\n' \
        "$( _random_hex 12)" "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "${PG_CACHE_MODE:-}" >> "${1}"
}

# Classify a cache's .acquire.lock WITHOUT mutating anything. Prints
# "LOCK_CLASS=<class>" (ABSENT|UNKNOWN|ACTIVE|STALE_JOB_OWNED) on stdout,
# "0" proven-safe-to-remove; every non-empty class carries the field so it
# is usable from `--cache-state` and from the cleanup contract.
fie_test_pg_cache_lock_classify() {  # <cachedir>
    local cachedir="$1" lock="${1}/.acquire.lock" fmt
    if [ ! -f "${lock}" ]; then printf 'LOCK_CLASS=ABSENT\n'; return 0; fi
    fmt="$(grep -m1 '^format=' "${lock}" 2>/dev/null || true)"
    # Probe: is the flock currently free? (advisory lock on a fresh fd)
    if ( exec 8>>"${lock}" && flock -n 8 ) 2>/dev/null; then
        :
    else
        printf 'LOCK_CLASS=ACTIVE\n'
        return 0
    fi
    if [ "${fmt}" = "format=fie-6-9a-acquire-lock-v1" ] &&
        [ -n "$(grep -m1 '^invocation=' "${lock}" 2>/dev/null | cut -d= -f2)" ]; then
        printf 'LOCK_CLASS=STALE_JOB_OWNED\n'
        return 0
    fi
    printf 'LOCK_CLASS=UNKNOWN\n'
    return 0
}

# Safe stale-lock removal (flock-proven dead owner): hold the flock, prove
# the fie ownership marker, and prove our open fd still resolves to the
# LIVE path (readlink — no "(deleted)" suffix; stat -c %i on /proc/self/fd/N
# does NOT follow to the target inode on every kernel), then unlink.
# Never touches ACTIVE or UNKNOWN locks (fail closed: preserved + reported).
_fie_cache_lock_remove_if_stale_owned() {  # <cachedir>; 0 removed / 1 kept
    # ALL steps run in THIS shell (the fd, the flock, the marker check, the
    # unlink): a subshell-held fd is invisible to the caller's checks, and
    # the flock must still be HELD at unlink time to exclude new acquirers.
    local lock="${1}/.acquire.lock" fd_target fmt
    if ! { exec 8>>"${lock}"; } 2>/dev/null; then
        return 1
    fi
    if ! { flock -n 8; } 2>/dev/null; then
        exec 8>&- 8<&-
        return 1
    fi
    fmt="$(grep -m1 '^format=' "${lock}" 2>/dev/null || true)"
    if [ "${fmt}" != "format=fie-6-9a-acquire-lock-v1" ]; then
        exec 8>&- 8<&-
        return 1
    fi
    fd_target="$(readlink "/proc/self/fd/8" 2>/dev/null || true)"
    if [ "${fd_target}" != "${lock}" ]; then
        # our flock is against a replaced/dead inode — leave the LIVE file
        # to the acquirers' retry loop
        exec 8>&- 8<&-
        return 1
    fi
    # Final re-proof immediately before the unlink (R4-R5-R1 Task D): a
    # replacement between the marker/inode proof and the removal fails
    # closed — the object that would be unlinked must still be the proven
    # fie-owned one at THIS moment (flock is still held; only the two
    # adjacent syscalls remain as window for an actor that ignores the
    # flock protocol; honest maintainers are excluded by it).
    if [ "$(readlink "/proc/self/fd/8" 2>/dev/null || true)" != "${lock}" ] || \
        [ "$(grep -m1 '^format=' "${lock}" 2>/dev/null || true)" != \
          "format=fie-6-9a-acquire-lock-v1" ]; then
        exec 8>&- 8<&-
        return 1
    fi
    local rc=0
    rm -f -- "${lock}" || rc=1
    exec 8>&- 8<&-
    return "${rc}"
}

# Canonical acquisition-ownership claim (R4-R5-R1 Task D). Must run in THIS
# shell (the claiming fd lives here, as with the removal protocol above).
#
#   ABSENT            -> create the lock file exclusively (noclobber
#                        O_EXCL semantics): the creating invocation owns
#                        the inode BY CONSTRUCTION, never by writability.
#   STALE_JOB_OWNED   -> adopt per the documented stale-owner protocol:
#                        hold the flock, re-prove the fie marker UNDER the
#                        flock against the live inode, refresh metadata.
#   ACTIVE            -> flock -w waits for the holder; on grant the
#                        content is re-classified (fie-owned => adopt;
#                        anything else => fail closed, preserved).
#   UNKNOWN (incl. a foreign object appearing in the classify->mutate
#                     window) => fail closed BEFORE any metadata write:
#                        the lock is preserved untouched and never claimed
#                        (defense in depth against the resolver's
#                        TOCTOU; PG_EXIT_CACHE_PATH).
#
#   returns 0: THIS invocation owns the lock; ownership metadata present;
#              fd 9 HELD — the caller must close fd 9 when done.
#   nonzero:   fail closed — no metadata was written into an unproven
#              object (the resolver-level isolated fallback may still have
#              produced a usable cache for the caller upstream).
_fie_cache_lock_acquisition_claim() {  # <cachedir>
    local cachedir="$1" lock="${1}/.acquire.lock" target tries=0 created=0 fmt
    local wait="${FIE_TEST_PG_LOCK_WAIT_SEC:-300}"
    if [ ! -e "${lock}" ] && \
        ( set -o noclobber; : > "${lock}" ) 2>/dev/null; then
        created=1
    fi
    # NOTE (LK6 pin): the null-command `exec` applies redirections
    # PERMANENTLY — keep the open inside a `{ ...; } 2>/dev/null` group so
    # the shell's own stderr (diagnostics!) is never silenced, and open
    # by the literal path shape the contract pins.
    if ! { exec 9>>"${cachedir}/.acquire.lock"; } 2>/dev/null; then
        _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE cannot open cache acquisition lock file"
        return "${PG_EXIT_CACHE_PATH}"
    fi
    while : ; do
        tries=$(( tries + 1 ))
        if ! flock -w "${wait}" 9; then
            exec 9>&- 9<&-
            _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE cache acquisition lock unavailable after ${wait}s; refusing unsynchronized destructive promotion"
            return "${PG_EXIT_CACHE_PATH}"
        fi
        # Deleted-inode detection via the /proc magic-symlink marker
        # (R4-R2 Task E): stat -c %i /proc/self/fd/9 does NOT resolve to
        # the target inode on every kernel — the portable, exact test is
        # readlink's "(deleted)" suffix. Retries keep mutual exclusion
        # against the LIVE path inode.
        target="$(readlink "/proc/self/fd/9" 2>/dev/null || true)"
        if [ "${target}" = "${lock}" ]; then
            # --- ownership classification UNDER the held flock (Task D) ---
            # fie_test_pg_cache_lock_classify would self-report ACTIVE
            # here (we hold the flock), so the FILE CONTENT decides:
            #   fie-format with an invocation  => proven fie-owned
            #                                     (stale adopt/refresh);
            #   empty AND exclusively created by
            #   THIS invocation                 => ours by construction;
            #   anything else (incl. legacy
            #   0-byte we did not create)       => UNKNOWN in the
            #                                     classify->mutate window:
            #                                     fail closed, preserved.
            fmt="$(grep -m1 '^format=' "${lock}" 2>/dev/null || true)"
            if [ "${fmt}" = "format=fie-6-9a-acquire-lock-v1" ]; then
                break
            fi
            if [ "${created}" -eq 1 ] && [ ! -s "${lock}" ]; then
                break
            fi
            exec 9>&- 9<&-
            _die "POSTGRESQL_PORTABLE_CACHE_LOCK_OWNERSHIP_UNPROVABLE cache acquisition lock content does not prove fie ownership (legacy/foreign/unattributable; value withheld); preserved untouched; claiming refused (R4-R5-R1 Task D)"
            return "${PG_EXIT_CACHE_PATH}"
        fi
        if [ "${tries}" -ge 25 ]; then
            exec 9>&- 9<&-
            _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE cache acquisition lock kept being replaced during acquisition (concurrent maintenance); refusing acquisition"
            return "${PG_EXIT_CACHE_PATH}"
        fi
        exec 9>&- 9<&-
        created=0  # a replaced inode is NOT the inode this job created
        if ! { exec 9>>"${lock}"; } 2>/dev/null; then
            _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE cannot reopen cache acquisition lock file"
            return "${PG_EXIT_CACHE_PATH}"
        fi
    done
    # Ownership metadata is written UNDER the held flock, AFTER content
    # ownership was proven (R4-R5-R1 Task D: never ownership-by-writability).
    _fie_cache_lock_write_metadata "${lock}" || true
    return 0
}

_portable_pg_arch() {
    case "$(uname -m)" in
        x86_64)  printf 'linux-amd64' ;;
        aarch64) printf 'linux-arm64v8' ;;
        *)       printf '' ;;
    esac
}

_cache_complete() {  # <cachedir>: 0 iff a usable portable install is present
    [ -x "$1/bin/initdb" ] && [ -x "$1/bin/pg_ctl" ] \
        && [ -x "$1/bin/postgres" ] && [ -f "$1/portable-postgres.jar" ] \
        && [ -f "$1/artifact.sha256" ]
}

_pg_download_to_stdout() {  # <url> -> body on stdout
    if command -v curl >/dev/null 2>&1; then
        curl -sf --max-time 300 "${1}" 2>/dev/null || return 1
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "${1}" || return 1
    else
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED neither curl nor wget available for portable acquisition"
        return 1
    fi
}

_pg_download_to_file() {  # <url> <dest>
    if command -v curl >/dev/null 2>&1; then
        curl -sf --max-time 600 -o "${2}" "${1}" 2>/dev/null || return 1
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "${2}" "${1}" || return 1
    else
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED neither curl nor wget available for portable acquisition"
        return 1
    fi
}

_cache_sha_verify() {  # <cachedir>: stored jar vs stored sidecar (quiet)
    (cd "$1" && printf '%s  portable-postgres.jar\n' "$(cat artifact.sha256)" |
        sha256sum -c --status - >/dev/null 2>&1)
}

# R4-R1: only FIE portable-artifact-class names may live in a cache dir that
# the cold path will create/replace entries in. ANY other (foreign) entry
# makes the cache dir unprovable-to-own: fail closed, never delete anything.
_cache_dir_entries_check() {  # <cachedir>: prints "OK" or "FOREIGN|<class>"
    python3 - "$1" <<'PY'
import re, sys
allowed = re.compile(
    r"(portable-postgres\.jar|artifact\.sha256|\.acquire\.lock|"
    r"fie_cache\.(owner|format)|\.fie-writable-probe\.[A-Za-z0-9]{6}|"
    r"\.partial\.\d+\.[0-9a-f]{8}\.(jar|sha256)|\.stage\.\d+\.[0-9a-f]{8}|"
    r"embedded-postgres-binaries-[A-Za-z0-9._-]+\.txz)"
    r"\Z").match
for name in sorted(_ for _ in __import__("os").listdir(sys.argv[1])):
    if name in ("bin", "lib", "share"):
        continue
    if not allowed(name):
        print(f"FOREIGN|{name}")
        sys.exit(0)
print("OK")
PY
}

# Cold-path acquire. Caller holds the acquisition lock (persistent caches
# with flock available) or has an exclusive temp dir. Atomic-promotion
# contract (R4-R1 §6.4): download to a NEVER-final `.partial.*` name, verify
# sha256, extract/validate in a `.stage.*` dir, then promote bin/, lib/,
# share/ first and the verified jar + sidecar LAST (completeness markers) —
# an interrupted promotion is never mistaken for a valid cache entry.
_cache_cold_acquire() {  # <cachedir> <base> <arch> <ver> <locked 0|1>
    local cachedir="$1" base="$2" arch="$3" ver="$4" locked="$5"
    local url sha_txt archive stage partial sidecar_tmp entry tmp_rnd
    tmp_rnd="$( _random_hex 8)"
    if _cache_complete "${cachedir}"; then  # re-check: an earlier lock waiter may have finished
        if _cache_sha_verify "${cachedir}"; then
            printf '%s\n' "${cachedir}"
            return 0
        fi
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED cached artifact failed re-verification; refusing reuse"
        return "${PG_EXIT_INTEGRITY}"
    fi
    case "$(_cache_dir_entries_check "${cachedir}")" in
        OK) : ;;
        FOREIGN*)
            _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE resolved cache dir already contains non-portable-artifact content (name withheld); refusing to modify or clean anything (ownership unprovable)"
            return "${PG_EXIT_CACHE_PATH}"
            ;;
    esac
    url="${base}/embedded-postgres-binaries-${arch}/${ver}/embedded-postgres-binaries-${arch}-${ver}.jar"
    sha_txt="$(_pg_download_to_stdout "${url}.sha256")" || {
        _die "POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE published sha256 sidecar not reachable (network/404${FIE_TEST_PG_TEST_BASE_URL:+, test-url-override}); acquisition refused, no fallback"
        return "${PG_EXIT_ARTIFACT}"
    }
    if ! printf '%s' "${sha_txt}" | grep -qE '^[0-9a-fA-F]{64}$'; then
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED published sha256 sidecar has unexpected form"
        return "${PG_EXIT_INTEGRITY}"
    fi
    echo "provision-test-postgres: acquiring portable PostgreSQL ${ver} (${arch}) into ${PG_CACHE_MODE}-mode cache" >&2
    stage="${cachedir}/.stage.$$.${tmp_rnd}"
    if ! mkdir -- "${stage}" 2>/dev/null; then
        _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE cannot create acquisition stage dir in cache"
        return "${PG_EXIT_CACHE_PATH}"
    fi
    partial="${cachedir}/.partial.$$.${tmp_rnd}.jar"
    sidecar_tmp="${cachedir}/.partial.$$.${tmp_rnd}.sha256"
    printf '%s\n' "${sha_txt}" > "${sidecar_tmp}"
    if ! _pg_download_to_file "${url}" "${partial}"; then
        rm -f -- "${partial}" "${sidecar_tmp}"; rm -rf -- "${stage}"
        _die "POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE artifact not reachable (network/404${FIE_TEST_PG_TEST_BASE_URL:+, test-url-override}); acquisition refused, no fallback"
        return "${PG_EXIT_ARTIFACT}"
    fi
    if ! printf '%s  %s\n' "${sha_txt}" "$(basename -- "${partial}")" |
            (cd "${cachedir}" && sha256sum -c --status >/dev/null 2>&1); then
        rm -f -- "${partial}" "${sidecar_tmp}"; rm -rf -- "${stage}"
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED downloaded artifact does not match the published sha256 sidecar; refusing install"
        return "${PG_EXIT_INTEGRITY}"
    fi
    archive="$(python3 - "${partial}" "${stage}" <<'PY'
import sys, zipfile
jar, stage = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(jar) as f:
    names = [n for n in f.namelist() if n.endswith(".txz") and "/" not in n]
if len(names) != 1:
    sys.exit(2)
with zipfile.ZipFile(jar) as f:
    f.extract(names[0], stage)
print(names[0])
PY
    )" || {
        rm -f -- "${partial}" "${sidecar_tmp}"; rm -rf -- "${stage}"
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED jar archive members unexpected"
        return "${PG_EXIT_INTEGRITY}"
    }
    if ! tar -xJf "${stage}/${archive}" -C "${stage}"; then
        rm -f -- "${partial}" "${sidecar_tmp}"; rm -rf -- "${stage}"
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED txz extraction failed"
        return "${PG_EXIT_INTEGRITY}"
    fi
    rm -f -- "${stage}/${archive}"
    if ! "${stage}/bin/initdb" --version >/dev/null 2>&1 ||
        ! "${stage}/bin/pg_ctl" --version >/dev/null 2>&1; then
        rm -f -- "${partial}" "${sidecar_tmp}"; rm -rf -- "${stage}"
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED extracted portable binaries did not validate; installation refused"
        return "${PG_EXIT_INTEGRITY}"
    fi
    # ---- promotion (validate-then-atomically-place) ---------------------
    for entry in bin lib share; do
        if [ -e "${stage}/${entry}" ]; then
            if [ "${locked}" -eq 1 ] && [ -e "${cachedir}/${entry}" ]; then
                rm -rf -- "${cachedir}/${entry}"   # stale crashed-promotion leftovers; lock proves exclusivity
            fi
            if [ -e "${cachedir}/${entry}" ]; then
                rm -f -- "${partial}" "${sidecar_tmp}"; rm -rf -- "${stage}"
                _die "POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE cache already contains '${entry}' and no acquisition lock is available; refusing to overwrite (ownership unprovable)"
                return "${PG_EXIT_CACHE_PATH}"
            fi
            mv -- "${stage}/${entry}" "${cachedir}/${entry}"
        fi
    done
    mv -- "${partial}" "${cachedir}/portable-postgres.jar"
    mv -- "${sidecar_tmp}" "${cachedir}/artifact.sha256"
    rmdir -- "${stage}" 2>/dev/null || rm -rf -- "${stage}"
    if ! "${cachedir}/bin/initdb" --version >/dev/null 2>&1 ||
        ! "${cachedir}/bin/pg_ctl" --version >/dev/null 2>&1 ||
        [ ! -f "${cachedir}/portable-postgres.jar" ]; then
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED installed portable binaries did not validate"
        rm -rf -- "${cachedir}/bin" "${cachedir}/lib" "${cachedir}/share"
        rm -f -- "${cachedir}/portable-postgres.jar" "${cachedir}/artifact.sha256"
        return "${PG_EXIT_INTEGRITY}"
    fi
    echo "provision-test-postgres: portable install verified (sha256 sidecar-checked, promoted atomically): ${cachedir}" >&2
    echo "provision-test-postgres: $("${cachedir}/bin/initdb" --version 2>/dev/null | head -1)" >&2
    printf '%s\n' "${cachedir}"
    return 0
}

# Acquire/verify the pinned portable PostgreSQL distribution. Prints the
# install dir to stdout. Idempotent: a cache HIT re-verifies the stored jar
# sha256 every time and uses no network. Cache/runtime path comes from the
# ONE canonical resolver (R4-R1 §6); cold acquisitions into a PERSISTENT
# cache are serialized by flock so concurrent jobs cannot race promotion;
# temp-mode caches are unique per invocation (exclusive by construction).
_fie_test_pg_ensure_cache() {
    local cachedir base arch ver rc
    base="${FIE_TEST_PG_TEST_BASE_URL:-${PORTABLE_PG_BASE_URL}}"
    arch="$(_portable_pg_arch)"
    ver="${PORTABLE_PG_VERSION}"
    if [ "$(uname -s)" != "Linux" ]; then
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED os=$(uname -s) (portable path: Linux x86_64/aarch64 only); acquisition refused"
        return "${PG_EXIT_PLATFORM}"
    fi
    if [ -z "${arch}" ]; then
        _die "POSTGRESQL_PLATFORM_UNSUPPORTED arch=$(uname -m) (portable path: x86_64/aarch64 only); acquisition refused"
        return "${PG_EXIT_PLATFORM}"
    fi
    for tool in tar xz python3; do
        command -v "${tool}" >/dev/null 2>&1 || {
            _die "POSTGRESQL_PLATFORM_UNSUPPORTED required portable-install tool '${tool}' absent on this platform"
            return "${PG_EXIT_PLATFORM}"
        }
    done
    PG_CACHE_DIR=""; PG_CACHE_MODE=""; PG_CACHE_PERSISTENT=""; _FIE_CACHE_TRACE=""
    if ! _fie_test_pg_cache_resolve; then
        return "${PG_EXIT_CACHE_PATH}"
    fi
    cachedir="${PG_CACHE_DIR}"
    if _cache_complete "${cachedir}"; then
        if _cache_sha_verify "${cachedir}"; then
            echo "provision-test-postgres: portable cache HIT (stored jar sha256 re-verified; no network): ${cachedir}" >&2
            printf '%s\n' "${cachedir}"
            return 0
        fi
        _die "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED cached artifact failed re-verification; refusing reuse"
        return "${PG_EXIT_INTEGRITY}"
    fi
    if [ "${PG_CACHE_PERSISTENT}" = "yes" ]; then
        if command -v flock >/dev/null 2>&1; then
            # Canonical ownership claim (R4-R5-R1 Task D): exclusive-create
            # for ABSENT / stale-owner adoption for proven fie-owned /
            # flock-wait for ACTIVE / fail closed for UNKNOWN — never
            # in-place claiming of an unproven pre-existing lock (the
            # R4-R5 FI09 defect: fie metadata written into a legacy
            # 0-byte lock converted UNKNOWN into apparently-job-owned
            # state that a later cleanup then removed). The inode-verify
            # retry loop (R4-R2 Task E) stays intact inside the claim.
            _fie_cache_lock_acquisition_claim "${cachedir}"
            rc=$?
            if [ "${rc}" -ne 0 ]; then
                # Fail-closed on the shared persistent cache (unproven /
                # replaced object — incl. the create/create race window
                # where an unattributable empty lock can appear): the
                # shared lock object was never touched by this invocation;
                # claiming or cleaning it is forbidden. Select the safe
                # isolated alternative (R4-R5-R1 Task D): a fresh job-local
                # temp cache owns nothing of the shared object's business.
                if ! _cache_fallback_temp_alloc; then
                    return "${PG_EXIT_CACHE_PATH}"
                fi
                cachedir="${PG_CACHE_DIR}"
                _FIE_CACHE_RESOLVED_DIR="${PG_CACHE_DIR}"
                _FIE_CACHE_RESOLVED_MODE="${PG_CACHE_MODE}"
                _FIE_CACHE_RESOLVED_PERSISTENT="${PG_CACHE_PERSISTENT}"
            fi
            _cache_cold_acquire "${cachedir}" "${base}" "${arch}" "${ver}" 1
            rc=$?
            exec 9>&- 9<&- 2>/dev/null || true
            return "${rc}"
        fi
        _cache_cold_acquire "${cachedir}" "${base}" "${arch}" "${ver}" 0
        return $?
    fi
    # temp mode: unique mktemp dir => exclusive by construction
    _cache_cold_acquire "${cachedir}" "${base}" "${arch}" "${ver}" 1
}

# ---------------------------------------------------------------- discovery
# Prints <bin-dir> (usable initdb+pg_ctl) on stdout; nonzero otherwise.
_fie_test_pg_discover() {
    local d best=""
    if [ -n "${FIE_TEST_PG_BIN:-}" ]; then
        # Explicit test-only contract (documented; WO §6.3-adjacent).
        [ -x "${FIE_TEST_PG_BIN}/initdb" ] && [ -x "${FIE_TEST_PG_BIN}/pg_ctl" ] && {
            printf '%s\n' "${FIE_TEST_PG_BIN}"
            return 0
        }
        _die "POSTGRESQL_TOOLING_DISCOVERY_FAILED explicit FIE_TEST_PG_BIN override does not contain usable initdb+pg_ctl (path withheld)"
        return "${PG_GUARD_EXIT}"
    fi
    # /usr/lib/postgresql/<N>/bin, highest version first (deterministic).
    for d in $(ls -d /usr/lib/postgresql/*/bin 2>/dev/null |
               sed -n 's|/usr/lib/postgresql/\([0-9][0-9]*\)/bin$|\1 \0|p' |
               sort -n -r | awk '{print $2}'); do
        [ -x "${d}/initdb" ] && [ -x "${d}/pg_ctl" ] && { best="${d}"; break; }
    done
    if [ -n "${best}" ]; then printf '%s\n' "${best}"; return 0; fi
    d="$(command -v initdb 2>/dev/null || true)"
    if [ -n "${d}" ]; then
        d="$(dirname -- "${d}")"
        [ -x "${d}/pg_ctl" ] && { printf '%s\n' "${d}"; return 0; }
    fi
    return 1
}

# ---------------------------------------------------------------- identity guard
# Refuse a production-flavored or misaddressed candidate target from the
# caller environment (WO §6.1 — fail closed, never a substitute). Values are
# classified and never printed (targets may embed credentials). Candidate
# set is explicit; every PG-flavored nonempty value is refused, because the
# provisioner's only legitimate DB identity is the job it creates itself.
_CANDIDATE_ENV_VARS=(
    FIE_TEST_PG_DSN FIE_DB_TARGET_INTELLIGENCE FIE_DB_TARGET_RAW
    FIE_INTELLIGENCE_DB FIE_DB_PATH FIE_DATABASE_URL
)

_pg_py_guard() {  # PGPROTECT_ENV_BLOB env; stdout: CLASSIFICATION|<result>
    PGPROTECT_ENV_BLOB="${FIE_R3_GUARD_BLOB:-}" python3 - "$(_pg_repo_root)" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
from phase3.runtime_contract import (  # noqa: E402
    BACKEND_POSTGRES, Unparseable, identity_fingerprint,
    load_production_identities, parse_postgres_dsn)

def loopback(host):
    return (host.startswith("/")) or host == "localhost" \
        or host in ("127.0.0.1", "::1", "loopback") \
        or host.startswith("socket:") or host.startswith("127.")

blob = os.environ.get("PGPROTECT_ENV_BLOB", "")
rejection = ""
for line in (l.strip() for l in blob.splitlines()):
    if not line:
        continue
    var, _, value = line.partition("=")
    try:
        spec = parse_postgres_dsn(value)
    except Unparseable:
        continue  # not a PG DSN → not a candidate for the provisioner
    host = spec.get("host", "") or ""
    db = spec.get("dbname", "") or ""
    if not loopback(host):
        rejection = (
            f"PRODUCTION_DSN_REJECTED; {var} is a non-loopback PG target;"
            " the ephemeral provisioner accepts no caller DB target (value withheld)")
        break
    if db == "fie_prod" or db.startswith("fie_prod"):
        rejection = (
            f"POSTGRESQL_IDENTITY_GUARD_REJECTED; {var} names a protected"
            " production DB identity; refused fail-closed (value withheld)")
        break
    rejection = (
        f"POSTGRESQL_IDENTITY_GUARD_REJECTED; {var} carries a PG DSN in the"
        " caller environment; provisioning accepts no caller DB target (value withheld)")
    break
if rejection:
    print(f"CLASSIFICATION|{rejection}")
else:
    print("CLASSIFICATION|OK")
PY
}

_fie_test_pg_identity_guard() {
    local env_blob="" val out v
    for v in "${_CANDIDATE_ENV_VARS[@]}"; do
        val="${!v:-}"
        [ -n "${val}" ] && env_blob="${env_blob}${v}=${val}\n"
    done
    [ -n "${env_blob}" ] || return 0
    FIE_R3_GUARD_BLOB="${env_blob}"
    out="$(FIE_R3_GUARD_BLOB="${FIE_R3_GUARD_BLOB}" _pg_py_guard 2>&1)" || {
        unset FIE_R3_GUARD_BLOB
        _die "POSTGRESQL_IDENTITY_GUARD_REJECTED guard evaluation failed (out withheld; unknown != safe)"
        return "${PG_GUARD_EXIT}"
    }
    unset FIE_R3_GUARD_BLOB
    if [ "$(printf '%s' "${out}" | head -1)" = "CLASSIFICATION|OK" ]; then
        return 0
    fi
    _die "$(printf '%s' "${out}" | head -1 | sed 's/^CLASSIFICATION|//')"
    return "${PG_GUARD_EXIT}"
}

# ---------------------------------------------------------------- readiness
_pg_py() {
    local py
    if [ -n "${FIE_TEST_PG_PYTHON:-}" ]; then printf '%s\n' "${FIE_TEST_PG_PYTHON}"; return 0; fi
    # 6.9A-R4-R2-R1 (Task B): the candidate ORDER is the ONE canonical
    # resolver contract; read line-wise (paths with spaces are safe); the
    # psycopg-capability check below is the reason this consumer iterates
    # instead of taking the first candidate.
    while IFS= read -r py; do
        [ -n "${py}" ] || continue
        command -v "${py}" >/dev/null 2>&1 || continue
        fie_python_version_ok "${py}" >/dev/null 2>&1 || continue
        "${py}" -c 'import psycopg' >/dev/null 2>&1 && { printf '%s\n' "${py}"; return 0; }
    done < <(fie_python_candidates)
    _die "POSTGRESQL_READINESS_FAILED no interpreter with the psycopg client is available (run scripts/bootstrap.sh --all first)"
    return "${PG_EXIT_READINESS}"
}

_loopback_bind_proof() {  # <port> -> proof lines on stdout; nonzero = violation
    local port="$1" ss_lines line addr
    if command -v ss >/dev/null 2>&1; then
        ss_lines="$(ss -ltn 2>/dev/null | awk -v p=":${port}$" '$4 ~ p')"
        if [ -n "${ss_lines}" ]; then
            while IFS= read -r line; do
                addr="$(printf '%s\n' "${line}" | awk '{print $4}')"
                if [ "${addr}" != "127.0.0.1:${port}" ]; then
                    printf 'bind|VIOLATION %s\n' "${line}"
                    return 1
                fi
            done <<PROOF_LINES
${ss_lines}
PROOF_LINES
            printf 'bind|127.0.0.1:%s LISTEN ONLY (ss proof)\n' "${port}"
            return 0
        fi
    fi
    # /proc fallback (portable; no ss dependency)
    python3 - "$1" <<'PY'
import sys
port = int(sys.argv[1])
listeners = []
for path in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = open(path).read().splitlines()[1:]
    except OSError:
        continue
    for ln in lines:
        parts = ln.split()
        if len(parts) < 4 or parts[3] != "0A":  # LISTEN state only
            continue
        addr, port_hex = parts[1].rsplit(":", 1)
        if int(port_hex, 16) == port:
            listeners.append((path, addr))
bad = [(p, a) for p, a in listeners if not a.endswith("0100007F")]
good = [(p, a) for p, a in listeners if a.endswith("0100007F")]
if bad or (not good):
    print(f"bind|VIOLATION listeners={listeners!r} (expect only 127.0.0.1)")
    sys.exit(1)
for p, a in good:
    print(f"bind|127.0.0.1:{port} LISTEN ONLY (proof via {p})")
PY
}

# ---------------------------------------------------------------- readiness probe
_pg_readiness_probe() {  # <bindir> <jobdir> <port> <user> <db> <expected_major>; PGPASSWORD in invocation env
    local bin_arg="$1" jobdir_arg="$2" port_arg="$3" user_arg="$4" db_arg="$5" major_arg="$6"
    "${PY_BIN_READY}" - "$@" <<'PY'
import json, os, sys
import psycopg
bindir, jobdir, port, user, db, major = sys.argv[1:7]
port = int(port)
# 1. socket auth (job-owned, trust) as the job's superuser on postgres DB
with psycopg.connect(host=jobdir, port=port, user=user, dbname="postgres",
                     autocommit=True) as c:
    # attach the synthetic password (job-scoped cred material from child env)
    from psycopg import sql
    c.execute(sql.SQL("ALTER USER {} WITH PASSWORD {}").format(
        sql.Identifier(user), sql.Literal(os.environ["PGPASSWORD"])))
    c.execute(
        f'CREATE DATABASE "{db}" ENCODING \'UTF8\' TEMPLATE template0')
# 2. re-connect to the SYNTHETIC DB and prove identity + UTF-8 + major
with psycopg.connect(host=jobdir, port=port, user=user, dbname=db) as c:
    cur_db, cur_user = c.execute(
        "SELECT current_database(), current_user").fetchone()
    enc = c.execute("SHOW server_encoding").fetchone()[0]
    ver = c.execute("SHOW server_version").fetchone()[0]
    assert cur_db == db and cur_user == user, (
        "synthetic identity mismatch (assertion failed; value synthetic)")
    assert enc == "UTF8", f"server_encoding {enc} is not UTF8"
    assert ver.split(".")[0] == major, f"unexpected PG major {ver}"
# 3. TCP auth proof (scram + synthetic password; 127.0.0.1 only)
with psycopg.connect(host="127.0.0.1", port=port, user=user,
                     password=os.environ["PGPASSWORD"],
                     dbname=db, sslmode="disable") as c:
    assert c.execute("SELECT current_database()").fetchone()[0] == db
print(json.dumps({
    "server_version": ver, "server_encoding": enc,
    "synthetic_db_prefix": db[:15], "synthetic_user_prefix": user[:10],
    "tcp_auth": "scram-sha-256-proven-socket-and-tcp",
    "dsn_class": "synthetic-loopback-socket-job-owned"}, indent=2))
PY
}

_pg_write_manifest() {  # <jobdir> <mode> <port> <user> <db> <bindir> <ver_str>
    python3 - "$@" <<'PY'
import datetime, json, sys
jobdir, mode, port, user, db, bindir, ver_str = sys.argv[1:8]
owner = open(f"{jobdir}/pgdata/fie_ephemeral_pg.owner").read()
manifest = {
    "job_id": owner.split("owner_job_id=", 1)[1].splitlines()[0],
    "mode": mode,
    "pg_version_string": ver_str,
    "bind": "127.0.0.1", "encoding": "UTF8",
    "port": int(port), "user": user, "db": db,
    "bin_dir": bindir, "created_utc": datetime.datetime.now(
        datetime.timezone.utc).isoformat(), "job_dir": jobdir,
}
with open(f"{jobdir}/manifest.json", "w") as f:
    json.dump(manifest, f, indent=2)
PY
}

# ---------------------------------------------------------------- ownership registry (R4-R3-R1)
# Creation-time ownership registry (ONE canonical implementation:
# scripts/resource_ownership.py). Recorded AT PROVISIONING TIME — teardown
# and evidence generation never infer ownership after the fact:
#   job_dir / pgdata_dir / auth_password / log files   (removable paths)
#   postgres_server process  (pid + start ticks + command signature —
#                            identity evidence stronger than a bare PID)
#   socket_dir (= job dir), port, version, mode
#   cache_dir (TEMP: job-owned removable; PERSISTENT: borrowed evidence only)
#   lock file (.acquire.lock) of a persistent cache (metadata-proven)
_pg_write_ownership_registry() {  # <jobdir> <mode> <bindir> [cache_lock] [cache_dir] [cache_temp_created] [cache_mode] [cache_persistent]
    python3 - "${_PROVISIONER_LIB_DIR}" "${_PROVISIONER_LIB_DIR}/resource_ownership.py" "$@" <<'PY'
def main():
    import json, os, sys
    # Module import by absolute path AND as scripts.resource_ownership (the
    # namespace-package identity tests import) — the registry FORMAT and
    # the teardown-evidence semantics belong to that ONE implementation.
    scripts_dir, module_path = sys.argv[1:3]
    sys.path.insert(0, scripts_dir)
    import argparse  # kept import surface minimal on purpose
    from pathlib import Path
    if str(Path(scripts_dir).parent) not in sys.path:
        sys.path.insert(0, str(Path(scripts_dir).parent))
    from scripts.resource_ownership import (
        CACHE_MARKER_FORMAT, new_registry, record_resource, write_registry)
    jobdir, mode, bindir = sys.argv[3:6]
    cache_lock = sys.argv[6] if len(sys.argv) > 6 else ""
    cache_dir = sys.argv[7] if len(sys.argv) > 7 else ""
    cache_temp = sys.argv[8] if len(sys.argv) > 8 else ""
    cache_state = dict(zip(("cache_mode", "cache_persistent"),
                           (sys.argv[9:11] if len(sys.argv) > 10
                            else ("", ""))))
    registry = new_registry(
        json.load(open(f"{jobdir}/manifest.json"))["job_id"])
    # Removable job dirs + the identity marker files that prove them.
    record_resource(registry, "job_dir", path=jobdir,
                    marker="pgdata/fie_ephemeral_pg.owner",
                    metadata={"server_mode": mode})
    record_resource(registry, "pgdata_dir", path=f"{jobdir}/pgdata")
    record_resource(registry, "auth_password", path=f"{jobdir}/auth.password")
    for name in ("initdb.log", "pg.log", "pg_ctl_start.log",
                 "readiness.json", "bind_proof.txt", "test_pg.env"):
        p = f"{jobdir}/{name}"
        if os.path.exists(p):
            record_resource(registry, "log_file", path=p, removable=True)
    record_resource(registry, "socket_dir", path=jobdir,
                    metadata={"purpose": "postgres unix socket directory"})
    if cache_dir:
        # discovered system tooling: the job created NO cache dir — record
        # nothing (nothing job-owned to tear down)
        record_resource(registry, "cache_dir",
                        path=cache_dir,
                        removable=bool(cache_temp),
                        metadata={"mode": cache_state.get("cache_mode", ""),
                                  "persistable":
                                      cache_state.get("cache_persistent", ""),
                                  "temp_created": bool(cache_temp)})
    if cache_lock and os.path.exists(cache_lock):
        record_resource(registry, "lock_file", path=cache_lock,
                        marker=".acquire.lock",
                        removable=True,
                        metadata={"format": "fie-6-9a-acquire-lock-v1"})
    # The long-lived server process itself: identity evidence read from
    # postmaster.pid (pid / data_dir / start timestamp) + /proc cmdline.
    pm_pid, pm_data_dir = "0", ""
    try:
        lines = open(f"{jobdir}/pgdata/postmaster.pid").read().splitlines()
        pm_pid, pm_data_dir = lines[0], lines[1]
    except (OSError, IndexError):
        pass
    proc = {"pid": int(pm_pid) if pm_pid.isdigit() else None,
            "data_dir": pm_data_dir,
            "start_ticks": None, "command_signature": ""}
    if proc["pid"]:
        try:
            base = f"/proc/{proc['pid']}"
            import time as _t
            stat = open(f"{base}/stat").read()
            fields = stat[stat.rindex(")") + 2:].split()
            proc["start_ticks"] = int(fields[19])
            proc["command_signature"] = " ".join(
                open(f"{base}/cmdline", "rb").read().decode(
                    "utf-8", "replace").split("\0")).strip()
        except (OSError, IndexError, ValueError):
            pass
    record_resource(registry, "postgres_server", process=proc,
                    path=jobdir,
                    marker="pgdata/fie_ephemeral_pg.owner",
                    metadata={"bindir": bindir, "mode": mode})
    write_registry(registry, f"{jobdir}/ownership_registry.json")
main()
PY
}

# ---------------------------------------------------------------- start/stop
_pg_cleanup_failed_job() {  # <jobdir> <bindir>: deterministic owned-state cleanup
    local jobdir="$1" bindir="$2"
    [ -x "${bindir}/pg_ctl" ] &&
        "${bindir}/pg_ctl" -m fast stop -D "${jobdir}/pgdata" >/dev/null 2>&1 \
        || true
    rm -rf -- "${jobdir}"
    [ ! -d "${jobdir}" ] || _die "POSTGRESQL_TEARDOWN_FAILED job dir ${jobdir##*/} still present after failed-start cleanup" >&2
}

fie_test_pg_start() {  # [--force-portable]; sets FIE_TEST_PG_JOB_DIR + globals
    local force_portable=0 bindir mode port user db jobdir ver_str arg
    for arg in "$@"; do
        case "${arg}" in
            --force-portable) force_portable=1 ;;
            *) _die "unknown fie_test_pg_start option '${arg}'"; return 2 ;;
        esac
    done
    _fie_test_pg_identity_guard || return $?
    bindir=""
    if [ "${force_portable}" -eq 0 ]; then
        if bindir="$(_fie_test_pg_discover)"; then
            mode="discovered"
        else
            echo "provision-test-postgres: no preinstalled server tooling found; portable path ($(_portable_pg_arch))"
            local rc_cap capf
            capf="$(mktemp "${TMPDIR:-/tmp}/fie-pgbindircap.XXXXXX")" || {
                _die "POSTGRESQL_START_FAILED cannot create bin-dir capture file"; return "${PG_EXIT_START}"; }
            if _fie_test_pg_ensure_cache > "${capf}"; then
                # NOT command substitution: the resolver contract variables
                # (PG_CACHE_DIR/PG_CACHE_TEMP_CREATED) must survive into this
                # scope — the temp-mode cache cleanup depends on them.
                bindir="$(<"${capf}")"
                rm -f -- "${capf}"
                mode="portable"
            else
                rc_cap=$?
                rm -f -- "${capf}"
                return "${rc_cap}"
            fi
        fi
    else
        echo "provision-test-postgres: --force-portable (system discovery skipped; test-only contract)"
        local rc_cap capf
        capf="$(mktemp "${TMPDIR:-/tmp}/fie-pgbindircap.XXXXXX")" || {
            _die "POSTGRESQL_START_FAILED cannot create bin-dir capture file"; return "${PG_EXIT_START}"; }
        if _fie_test_pg_ensure_cache > "${capf}"; then
            # NOT command substitution: the resolver contract variables
            # (PG_CACHE_DIR/PG_CACHE_TEMP_CREATED) must survive into this
            # scope — the temp-mode cache cleanup depends on them.
            bindir="$(<"${capf}")"
            rm -f -- "${capf}"
            mode="portable"
        else
            rc_cap=$?
            rm -f -- "${capf}"
            return "${rc_cap}"
        fi
    fi
    # The portable path is an INSTALL ROOT (bin/ beneath it); discovery is a
    # DIR with initdb+pg_ctl directly. Normalize: everything downstream is a
    # BIN DIR.
    if [ "${mode}" = "portable" ]; then
        bindir="${bindir}/bin"
    fi
    ver_str="$("${bindir}/initdb" --version 2>/dev/null | head -1)"
    echo "provision-test-postgres: ${mode} server tooling: ${ver_str}"
    expected_major="$(printf '%s' "${ver_str}" | sed -n 's/.*PostgreSQL) \([0-9][0-9]*\).*/\1/p')"
    [ -n "${expected_major}" ] || {
        _pg_cleanup_failed_job "${jobdir}" "${bindir}" || true
        _die "POSTGRESQL_TOOLING_DISCOVERY_FAILED initdb --version output unusable; owned job state removed"
        return "${PG_EXIT_INTEGRITY}"
    }
    jobdir="$(mktemp -d "${TMPDIR:-/tmp}/fie-pgtest.XXXXXX")" || {
        _die "POSTGRESQL_START_FAILED cannot create job dir"
        return "${PG_EXIT_START}"
    }
    user="fie_$( _random_hex 6)"
    db="fie_test_$( _random_hex 6)"
    mkdir -p "${jobdir}/pgdata"
    (umask 077 && printf '%s\n' "$( _random_hex 24)" > "${jobdir}/auth.password") || {
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_START_FAILED cannot write 0600 credential material (value withheld)"
        return "${PG_EXIT_START}"
    }
    if ! "${bindir}/initdb" -D "${jobdir}/pgdata" -U "${user}" --auth=trust \
            --encoding=UTF8 --locale=C.UTF-8 \
            > "${jobdir}/initdb.log" 2>&1; then
        tail -5 "${jobdir}/initdb.log" >&2 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_INITDB_FAILED job-owned initdb failed (initdb.log tail above); owned state removed"
        return "${PG_EXIT_INITDB}"
    fi
    # Lifecycle ownership marker — created BEFORE start (proof-of-ownership
    # for teardown and for partial-init cleanup).
    printf 'owner_job_id=%s\ndata_dir=%s/\nformat=fie-6-9a-ephemeral-v1\n' \
        "$( _random_hex 12)" "${jobdir}" \
        > "${jobdir}/pgdata/fie_ephemeral_pg.owner"
    # Job-owned pg_hba: local/socket stays trust; host lines pinned to
    # scram-sha-256 so the TCP listener (127.0.0.1) authenticates with the
    # synthetic password only. System PostgreSQL configuration untouched.
    python3 - "${jobdir}/pgdata/pg_hba.conf" <<'PY'
import sys
p = sys.argv[1]
out = []
for line in open(p):
    if line.split()[:1] == ["host"] and line.rstrip().rsplit(None, 1)[-1] == "trust":
        out.append(line.rstrip().rsplit(None, 1)[0] + " scram-sha-256\n")
    else:
        out.append(line)
open(p, "w").write("".join(out))
PY
    port="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
    if ! "${bindir}/pg_ctl" -D "${jobdir}/pgdata" -l "${jobdir}/pg.log" \
            -w -t 60 -o "-p ${port} -k ${jobdir} -c listen_addresses=127.0.0.1" \
            start > "${jobdir}/pg_ctl_start.log" 2>&1; then
        tail -5 "${jobdir}/pg.log" >&2 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_START_FAILED pg_ctl start failed on loopback port ${port}; owned state removed"
        return "${PG_EXIT_START}"
    fi
    if ! _loopback_bind_proof "${port}" > "${jobdir}/bind_proof.txt" 2>&1; then
        "${bindir}/pg_ctl" -m fast stop -D "${jobdir}/pgdata" >/dev/null 2>&1 || true
        cat "${jobdir}/bind_proof.txt" >&2 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_START_FAILED loopback-only bind proof FAILED; job cluster stopped (listener must be 127.0.0.1 only)"
        return "${PG_EXIT_START}"
    fi
    PY_BIN_READY="$(_pg_py)" || {
        # No psycopg interpreter: a running job cluster would leak. Stop
        # and remove owned state; keep _pg_py's classification (95).
        fie_test_pg_stop "${jobdir}" >/dev/null 2>&1 || true
        return "${PG_EXIT_READINESS}"
    }
    if ! PGPASSWORD="$(cat "${jobdir}/auth.password")" _pg_readiness_probe \
            "${bindir}" "${jobdir}" "${port}" "${user}" "${db}" \
            "${expected_major}" \
            > "${jobdir}/readiness.json" 2>&1; then
        "${bindir}/pg_ctl" -m fast stop -D "${jobdir}/pgdata" >/dev/null 2>&1 || true
        tail -5 "${jobdir}/readiness.json" >&2 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_READINESS_FAILED readiness probe failed (job-owned; stopped and removed)"
        return "${PG_EXIT_READINESS}"
    fi
    _pg_write_manifest "${jobdir}" "${mode}" "${port}" "${user}" "${db}" \
        "${bindir}" "${ver_str}" || {
        "${bindir}/pg_ctl" -m fast stop -D "${jobdir}/pgdata" >/dev/null 2>&1 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_START_FAILED manifest write failed; owned state removed"
        return "${PG_EXIT_START}"
    }
    _pg_write_ownership_registry "${jobdir}" "${mode}" "${bindir}" \
        "${PG_CACHE_DIR:+${PG_CACHE_DIR}/.acquire.lock}" \
        "${PG_CACHE_DIR:-}" "${PG_CACHE_TEMP_CREATED:-}" \
        "${PG_CACHE_MODE:-}" "${PG_CACHE_PERSISTENT:-}" || {
        "${bindir}/pg_ctl" -m fast stop -D "${jobdir}/pgdata" >/dev/null 2>&1 || true
        rm -rf -- "${jobdir}"
        _die "POSTGRESQL_START_FAILED ownership registry write failed; owned state removed"
        return "${PG_EXIT_TEARDOWN}"
    }
    FIE_TEST_PG_JOB_DIR="${jobdir}"
    FIE_TEST_PG_MODE="${mode}"
    FIE_TEST_PG_BIN_DIR="${bindir}"
    FIE_TEST_PG_PORT="${port}"
    FIE_TEST_PG_USER="${user}"
    FIE_TEST_PG_DB="${db}"
    FIE_TEST_PG_VERSION_STR="${ver_str}"
    FIE_TEST_PG_DSN="postgresql://${user}@/${db}?host=${jobdir}&port=${port}"
    (umask 077 && printf 'export FIE_TEST_PG_DSN=%q\n' "${FIE_TEST_PG_DSN}" \
        > "${jobdir}/test_pg.env")
    cat "${jobdir}/readiness.json"
    echo "provision-test-postgres: READY (job-dir ${jobdir}; bind 127.0.0.1:${port}; encoding UTF8; identity proven by construction + guard pass)"
    return 0
}

# Stop ONLY the job-owned cluster and remove ONLY its job dir. Idempotent;
# refuses (exit 96) any dir whose ownership cannot be proven.
_pg_stop_verify_stopped() {  # args: <pgdata> <observer_pid>; registry on stdin
    python3 "${_PROVISIONER_LIB_DIR}/resource_ownership.py" verify-stopped \
        --data-dir "$1" --observer "$2"
}

_pg_stop_evidence_emit() {  # args: <jobdir>; registry on stdin
    python3 "${_PROVISIONER_LIB_DIR}/resource_ownership.py" stop-evidence \
        --job-dir "$1" --removed-dir "$1"
}

fie_test_pg_stop() {
    local dir
    if [ -n "${1:-}" ]; then dir="$1"; else dir="${FIE_TEST_PG_JOB_DIR:-}"; fi
    if [ -z "${dir:-}" ]; then
        _die "POSTGRESQL_TEARDOWN_FAILED no job dir known (nothing stopped)"
        return "${PG_EXIT_TEARDOWN}"
    fi
    if [ ! -d "${dir}" ]; then
        echo "provision-test-postgres: teardown idempotent — job ${dir##*/} state already gone"
        return 0
    fi
    local pgdata="${dir}/pgdata" marker_bin
    if [ ! -f "${pgdata}/fie_ephemeral_pg.owner" ]; then
        _die "POSTGRESQL_TEARDOWN_FAILED ownership marker absent; refusing to touch (ownership unprovable; dir left untouched for diagnosis)"
        return "${PG_EXIT_TEARDOWN}"
    fi
    if [ -f "${pgdata}/postmaster.pid" ]; then
        # Ownership: the running server must claim THIS job dir as data_dir.
        if ! grep -q "^$(printf '%s' "${pgdata}")$\|^$(printf '%s' "${pgdata}/")$" \
                "${pgdata}/postmaster.pid" 2>/dev/null; then
            _die "POSTGRESQL_TEARDOWN_FAILED postmaster.pid data_dir identity does not match this job dir; refusing stop (unrelated protected)"
            return "${PG_EXIT_TEARDOWN}"
        fi
    fi
    marker_bin="${FIE_TEST_PG_BIN_DIR:-}"
    [ -n "${marker_bin}" ] && [ -x "${marker_bin}/pg_ctl" ] || {
        marker_bin="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("bin_dir",""))' "${dir}/manifest.json" 2>/dev/null || true)"
    }
    local reg_json=""
    if [ -f "${dir}/ownership_registry.json" ]; then
        reg_json="$(cat "${dir}/ownership_registry.json")"
    fi
    if [ -n "${marker_bin}" ] && [ -x "${marker_bin}/pg_ctl" ]; then
        "${marker_bin}/pg_ctl" -D "${pgdata}" -m fast stop \
            > "${dir}/pg_ctl_stop.log" 2>&1 || true
    fi
    # Ownership-proven stopped-state verification (R4-R3-R1): the registry's
    # server process (and any child attributable ONLY by this job's data-dir
    # command signature) must be ENDED before the job dir is removed. A
    # still-live job-owned server after 'pg_ctl fast stop' is a failed
    # teardown — fail closed, PRESERVE, never escalate to arbitrary kills
    # (no pkill/killall/wildcard: that would touch foreign processes).
    if [ -n "${reg_json}" ] &&
        ! printf '%s' "${reg_json}" | _pg_stop_verify_stopped "${pgdata}" "$$"; then
        _die "POSTGRESQL_TEARDOWN_FAILED registry-proven postgres processes remain alive after the granted stop mechanism (preserved; no kill outside pg_ctl)"
        return "${PG_EXIT_TEARDOWN}"
    fi
    rm -rf -- "${dir}" || true
    if [ -d "${dir}" ]; then
        _die "POSTGRESQL_TEARDOWN_FAILED job dir still present after cleanup"
        return "${PG_EXIT_TEARDOWN}"
    fi
    if [ -n "${reg_json}" ]; then
        printf '%s' "${reg_json}" | _pg_stop_evidence_emit "${dir}"
    fi
    echo "provision-test-postgres: stopped and removed ONLY job-owned state ($(date -u +%H:%M:%SZ))"
    return 0
}

# ---------------------------------------------------------------- standalone
_provisioner_main() {
    local action="${1:-}"
    case "${action}" in
        --ensure-cache)
            _fie_test_pg_ensure_cache
            local rc=$?
            # Standalone invocation: when the warmed content landed in an
            # isolated job-local FALLBACK cache (unclaimable persistent
            # lock; R4-R5-R1 Task D) it has no cross-invocation durability
            # — leave no orphaned residue; this invocation created it
            # (PG_CACHE_TEMP_CREATED) and its marker proves ownership.
            if [ -n "${PG_CACHE_TEMP_CREATED:-}" ] && [ -d "${PG_CACHE_TEMP_CREATED}" ]; then
                grep -q "^format=fie-6-9a-cache-v1$" \
                    "${PG_CACHE_TEMP_CREATED}/fie_cache.owner" 2>/dev/null &&
                    rm -rf -- "${PG_CACHE_TEMP_CREATED}" 2>/dev/null || true
                PG_CACHE_TEMP_CREATED=""
                echo "provision-test-postgres: ensure-cache warmed a job-local fallback cache (no cross-invocation durability); removed it at exit" >&2
            fi
            return "${rc}" ;;
        --resolve-cache)
            _fie_test_pg_cache_resolve || return $?
            printf 'CACHE_DIR=%s\nCACHE_MODE=%s\nCACHE_PERSISTENT=%s\n' \
                "${PG_CACHE_DIR}" "${PG_CACHE_MODE}" "${PG_CACHE_PERSISTENT}"
            # Standalone standalone resolution is OBSERVATION-ONLY: a
            # temp-mode dir resolved here exists only for this invocation —
            # clean it up instead of leaking it (R4-R2 Task D/C10).
            fie_test_pg_cache_cleanup >/dev/null 2>&1 || true ;;
        --cache-state)
            # Structured cache-state API (R4-R2 Task D3): the one source of
            # truth tests may use to see the ACTIVE cache. Never a second
            # resolution engine in the test helpers.
            _fie_test_pg_cache_resolve || return $?
            local _warm _cls
            if [ -x "${PG_CACHE_DIR}/bin/initdb" ] &&
               [ -x "${PG_CACHE_DIR}/bin/pg_ctl" ] &&
               [ -x "${PG_CACHE_DIR}/bin/postgres" ] &&
               [ -f "${PG_CACHE_DIR}/portable-postgres.jar" ] &&
               [ -f "${PG_CACHE_DIR}/artifact.sha256" ]; then
                _warm=true
            else
                _warm=false
            fi
            printf 'CACHE_DIR=%s\nCACHE_MODE=%s\nCACHE_PERSISTENT=%s\nCACHE_WARM=%s\n' \
                "${PG_CACHE_DIR}" "${PG_CACHE_MODE}" "${PG_CACHE_PERSISTENT}" "${_warm}"
            fie_test_pg_cache_lock_classify "${PG_CACHE_DIR}" 2>/dev/null ||
                printf 'LOCK_CLASS=UNKNOWN\n'
            fie_test_pg_cache_cleanup >/dev/null 2>&1 || true ;;
        --start)
            shift
            fie_test_pg_start "$@" ;;
        --stop)
            shift
            fie_test_pg_stop "${1:-}" ;;
        --run)
            shift
            [ -n "${1:-}" ] || { _die "no child command for --run"; return 2; }
            run_force=""
            [ "${1:-}" = "--force-portable" ] && { run_force="--force-portable"; shift; }
            [ -n "${1:-}" ] || { _die "no child command for --run"; return 2; }
            fie_test_pg_start ${run_force:+${run_force}} || return $?
            trap 'fie_test_pg_stop >/dev/null 2>&1 || true; fie_test_pg_cache_cleanup >/dev/null 2>&1 || true' EXIT
            export FIE_TEST_PG_DSN
            echo "provision-test-postgres: synthetic DSN exported to child scope only (no production fallback possible)"
            "${@}"
            rc=$?
            fie_test_pg_stop >/dev/null 2>&1 || true
            fie_test_pg_cache_cleanup >/dev/null 2>&1 || true
            return "${rc}"
            ;;
        ""|-h|--help|*)
            _die "unknown/missing action '${action}' (fail closed)"
            printf 'USAGE:\n' >&2
            printf '  scripts/provision-test-postgres.sh --ensure-cache\n' >&2
            printf '  scripts/provision-test-postgres.sh --resolve-cache\n' >&2
            printf '  scripts/provision-test-postgres.sh --cache-state\n' >&2
            printf '  scripts/provision-test-postgres.sh --start [--force-portable]\n' >&2
            printf '  scripts/provision-test-postgres.sh --stop <job-dir>\n' >&2
            printf '  scripts/provision-test-postgres.sh --run [--force-portable] <child…>\n' >&2
            return 2
            ;;
    esac
}

if [ "${BASH_SOURCE[0]:-}" = "${0}" ]; then
    _provisioner_main "$@" || exit $?
fi