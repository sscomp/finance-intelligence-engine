"""Fail-closed rehearsal/test DB-target guard (Phase 6.7B incident closure).

2026-10-04 incident: a rehearsal harness supplied the seed-target override
under the wrong variable name (``FIE_DATABASE_URL`` where the wrapper
``run.sh`` consumes ``FIE_INTELLIGENCE_DB``). The unset wrapper variable
fell back to the hard-coded production default and the rehearsal write
landed on the live Production intelligence store
(``metadata/intelligence_store.db``). Content-preserving (zero deletions,
zero canonical value changes, append-only held) but an unplanned
production mutation.

This module closes the whole class: a rehearsal/test process can never
silently open (and therefore write) the live Production intelligence
store. It is invoked from the ``SQLiteStore`` writable-open seam, the
single funnel every Phase 3B write path goes through (seed, persist,
init-db, graph, migration).

Execution-mode contract (reused, not a second mode system)
--------------------------------------------------------
The guard keys on the project's existing ``FIE_SERVICE_ENV`` profile
vocabulary (``phase3/service/runtime_config.py``, ADR-017):

- ``test`` / ``staging``            → rehearsal-class: the writable target
  must NOT resolve (canonical path or same-inode) to the live Production
  intelligence store; otherwise refused with
  ``FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED``.
- ``local`` / ``production`` / unset → accepted historical behavior
  (production-class; production runs legitimately open the live store).
- any other, explicitly supplied value → refused (``UNKNOWN_SERVICE_ENV``):
  an unknown execution mode is never silently granted production
  privileges (fail closed, mirroring ADR-017).

PostgreSQL DSN targets have no path semantics; the guard is a no-op for
them (a DSN cannot be resolved to a filesystem inode here). The wrapper
layer (``scripts/rehearsal_db_guard.sh``) enforces the same classes at the
shell boundary, before any Python process starts.

Secrets: refusal messages name the configuration class only — values are
withheld (same contract as :class:`phase3.service.runtime_config.ConfigurationError`).
"""
from __future__ import annotations

import os
from pathlib import Path

from phase3.paths import project_root
from phase3.persistence.backend import PG_URL_PREFIXES

__all__ = [
    "FIE_INTELLIGENCE_DB",
    "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED",
    "UNKNOWN_SERVICE_ENV",
    "REHEARSAL_SERVICE_ENVS",
    "FIE_SERVICE_ENV",
    "RehearsalTargetRefused",
    "is_rehearsal_service_env",
    "production_store_candidates",
    "is_production_target",
    "assert_writable_target",
]

#: The wrapper-layer intelligence-store variable (run.sh / run_weekly.sh /
#: run_monthly.sh seed-target contract). The HTTP service layer instead
#: uses ``FIE_DATABASE_URL`` — the mismatch of these two names is the
#: incident's root cause, so both are recognized here only as DISTINCT
#: variables; one never substitutes for the other.
FIE_INTELLIGENCE_DB = "FIE_INTELLIGENCE_DB"
FIE_SERVICE_ENV = "FIE_SERVICE_ENV"

#: Machine-readable refusal classification.
FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED = "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED"
UNKNOWN_SERVICE_ENV = "UNKNOWN_SERVICE_ENV"

#: Profiles treated as rehearsal/test mode (subset of the ADR-017
#: vocabulary). ``staging`` is rehearsal-class for store writes: only a
#: genuinely production-declared profile may open the live store.
REHEARSAL_SERVICE_ENVS = ("test", "staging")
_HISTORICAL_NON_REHEARSAL = ("", "local", "production")


class RehearsalTargetRefused(RuntimeError):
    """Deterministic, fail-closed rehearsal DB-target refusal.

    Attributes are the same contract as
    :class:`phase3.service.runtime_config.ConfigurationError`: ``code`` is
    a stable machine-testable category; ``message`` names the class only
    and never carries the offending target value (paths/DSNs withheld).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def is_rehearsal_service_env(service_env: str | None = None) -> bool:
    """True when the current (or given) service env is rehearsal-class.

    Unset/empty and the historical ``local``/``production`` profiles are
    NOT rehearsal (accepted behavior preserved). An explicitly supplied
    INVALID profile raises :class:`RehearsalTargetRefused` — an unknown
    execution mode must not silently pass as production (ADR-017 shape).
    """
    raw = (os.environ.get(FIE_SERVICE_ENV, "") if service_env is None
           else service_env)
    profile = raw.strip().lower()
    if profile in _HISTORICAL_NON_REHEARSAL:
        return False
    if profile in REHEARSAL_SERVICE_ENVS:
        return True
    raise RehearsalTargetRefused(
        UNKNOWN_SERVICE_ENV,
        "FIE_SERVICE_ENV has an invalid value; refusing to resolve the "
        "writable-store target (value withheld)",
    )


def production_store_candidates() -> list[str]:
    """The candidate live Production intelligence-store specifiers.

    Portable first (the accepted production layout derived from the
    repository root). ``FIE_INTELLIGENCE_DB`` is deliberately NOT a
    candidate: in rehearsal mode that variable names the declared
    rehearsal target (falsely listing it here would refuse every legal
    rehearsal write — caught by the G9 end-to-end re-test on 2026-10-04).
    A declared target that actually resolves to production is instead
    caught by comparing against the canonical layout candidate.
    DSNs are excluded — they carry no path semantics.
    """
    candidates = [str(project_root() / "metadata" / "intelligence_store.db")]
    # De-duplicate while keeping order.
    seen: set[str] = set()
    unique = []
    for cand in candidates:
        resolved = _canonical(cand)
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _canonical(spec: str) -> str:
    """Normalize a specifier to its canonical filesystem form (or DSN)."""
    path = spec
    lowered = spec.lower()
    if lowered.startswith("sqlite://"):
        path = spec[len("sqlite://"):]
    elif lowered.startswith("sqlite:"):
        path = spec[len("sqlite:"):]
    if not path:
        return spec
    if path.lower().startswith(PG_URL_PREFIXES):
        return path
    return os.path.realpath(path)


def is_production_target(specifier: str) -> bool:
    """True when a SQLite target equals the live Production intelligence store.

    Comparison is canonical-path equality (``os.path.realpath``, so
    symlinks and ``..`` traversal cannot bypass it) plus same-device/
    inode equivalence (hard links) for existing files. PostgreSQL DSNs
    are never path-resolvable here and are not classified.
    """
    path = specifier
    lowered = (specifier or "").lower()
    if lowered.startswith(PG_URL_PREFIXES):
        return False  # no filesystem semantics for a DSN
    if lowered.startswith("sqlite://"):
        path = specifier[len("sqlite://"):]
    elif lowered.startswith("sqlite:"):
        path = specifier[len("sqlite:"):]
    if not path:
        return False
    resolved = os.path.realpath(path)
    for cand in production_store_candidates():
        cand_resolved = os.path.realpath(cand)
        if resolved == cand_resolved:
            return True
        if os.path.exists(resolved) and os.path.exists(cand_resolved):
            try:
                if os.path.samefile(resolved, cand_resolved):
                    return True
            except OSError:
                pass
    return False


def assert_writable_target(specifier: str, *, component: str = "SQLiteStore") -> None:
    """Fail-closed contract: refuse a REHEARSAL writable open of Production.

    No-op unless the current service env is rehearsal-class (see
    :func:`is_rehearsal_service_env`). Never returns the offending value.
    """
    if not is_rehearsal_service_env():
        return
    if is_production_target(specifier):
        raise RehearsalTargetRefused(
            FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED,
            f"{component}: refusing to open a writable Phase 3B store in "
            "rehearsal/test mode: the requested DB target resolves to the "
            "live Production intelligence store (target value withheld)",
        )