"""Migration vs runtime authority separation (Phase 6.9B-R3, WO §3.3).

Two authority classes share one cluster/database in the production
shape:

* **Migration authority** — may perform explicitly authorized schema
  migration/bootstrap (operator-driven: ``python -m phase3.cli init-db``,
  repository bootstrap, seeding/repair tools).
* **Runtime authority** — the FIE application identity; consumes an
  ALREADY-migrated schema. It must not auto-DDL and must not silently
  repair Production schema.

R3 closes the implicit-DDL arcs that today's production-shaped code
paths can reach (inventory: phase3/api.py ``_open_store`` /
``_build_graph_store``, raw-layer ``db.py`` ``save_*`` auto-``init_db``,
``backfill._ensure_table``): on a target that resolves to the
authoritative production identity, implicit DDL runs ONLY when the
process holds explicit migration authority via the environment
contract::

    FIE_MIGRATION_AUTHORITY=1    # this process is a migration/bootstrap context

Unset or any other value means runtime authority: the call site does
NOT auto-apply any migration on a production-shaped target. The
strongest guarantee remains structural — the production runtime role
carries no schema-CREATE privilege at all (identity gate +
privilege-failure tests) — this module is the code-path guard layer on
top.

A NON-production-shaped target (test/disposable/default-development
targets whose identity fingerprint is not the production one, or an
environment where production identity is unprovable) keeps the
historical behavior — fail-closed tests never silently flip their own
stack.

Secrets: refusals never carry target values (same contract as
:class:`phase3.persistence.rehearsal_guard.RehearsalTargetRefused`).
"""
from __future__ import annotations

import os

from phase3.runtime_contract import (
    FailClosedTarget,
    TargetSpec,
    Unparseable,
    load_production_identities,
    parse_target,
    target_identity,
)

__all__ = [
    "FIE_MIGRATION_AUTHORITY",
    "MIGRATION_AUTHORITY_REQUIRED",
    "MigrationAuthorityRequired",
    "migration_authority_granted",
    "is_production_shaped",
    "auto_ddl_allowed",
    "assert_auto_ddl_allowed",
]

FIE_MIGRATION_AUTHORITY = "FIE_MIGRATION_AUTHORITY"
MIGRATION_AUTHORITY_REQUIRED = "MIGRATION_AUTHORITY_REQUIRED"

_TRUTHY = ("1", "true", "yes", "on")


class MigrationAuthorityRequired(RuntimeError):
    """Implicit-DDL refusal on a production-shaped target.

    ``code`` is the stable machine-testable classification; the message
    names the operation class only — never the target value.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def migration_authority_granted(
    env: dict[str, str] | None = None,
) -> bool:
    """True when THIS process was explicitly launched as a migration context."""
    e = os.environ if env is None else env
    return (e.get(FIE_MIGRATION_AUTHORITY, "") or "").strip().lower() in _TRUTHY


def is_production_shaped(
    spec: TargetSpec,
    env: dict[str, str] | None = None,
) -> bool:
    """True when the target resolves to the authoritative production identity.

    The comparison uses the same fingerprint machinery the rehearsal
    guards use (:data:`phase3.runtime_contract.DEFAULT_CONTRACT_FILES`):
    a test/disposable target never matches a production fingerprint.
    When production identity is unprovable on this environment (no
    readable contract files — e.g. a clean-room clone without the
    production layout), there is no production to protect and the
    target is treated as NOT production-shaped (the R1 rehearsal
    guards, which fail closed for unprovable identity, remain the
    safety net for the write funnels).
    """
    try:
        prod = load_production_identities(env)
    except FailClosedTarget:
        return False
    try:
        return target_identity(spec).fingerprint in prod.fingerprints
    except Exception:  # noqa: BLE001 - unparseable identity is not a match
        return False


def _as_target_spec(target) -> TargetSpec | None:
    """Normalize the guard input to a :class:`TargetSpec` (or None).

    Accepts a :class:`~phase3.runtime_contract.TargetSpec`, a
    :class:`~phase3.persistence.backend.DatabaseSpec` (``resolve_spec``
    shape, carries ``dsn``), or a raw spec string. Unparseable input is
    not a verifiable match (None).
    """
    if isinstance(target, TargetSpec):
        return target
    if isinstance(target, str):
        value: str | None = target
    else:
        value = getattr(target, "dsn", None)
    if not value:
        return None
    try:
        return parse_target(str(value))
    except (FailClosedTarget, Unparseable):
        return None


def auto_ddl_allowed(
    target: str | TargetSpec,
    env: dict[str, str] | None = None,
) -> bool:
    """Deterministic verdict: may this call site run implicit schema DDL?

    True unless the target is production-shaped AND the process lacks
    explicit migration authority.
    """
    if migration_authority_granted(env):
        return True
    spec = _as_target_spec(target)
    if spec is None:
        return False
    return not is_production_shaped(spec, env)


def assert_auto_ddl_allowed(
    target: str | TargetSpec,
    *,
    operation: str = "implicit schema migration",
    env: dict[str, str] | None = None,
) -> bool:
    """Fail-closed guard: raise :class:`MigrationAuthorityRequired` when
    implicit DDL is not allowed on this target; return the allowed
    verdict otherwise."""
    if auto_ddl_allowed(target, env):
        return True
    raise MigrationAuthorityRequired(
        MIGRATION_AUTHORITY_REQUIRED,
        f"{operation} is forbidden on a production-shaped database target "
        "without FIE_MIGRATION_AUTHORITY=1 (runtime consumes an "
        "already-migrated schema; migration authority is an explicit "
        "operator context)",
    )