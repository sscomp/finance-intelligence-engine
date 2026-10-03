"""Deterministic schema compatibility gate (Phase 6.7B-R3, HP-07/06 / OI-08).

Single authoritative source
---------------------------
The authoritative schema identity is the SAME one migrations use:
:meth:`~phase3.persistence.migrations.MigrationManager` over
:func:`~phase3.persistence.migrations.default_migrations_for`. The gate
does NOT invent a parallel version mechanism — it derives, at runtime,
from the registered migration list:

* the expected applied-version set (each registered :class:`~Migration`),
* the expected registry checksum per version,
* the expected application objects (every ``CREATE TABLE`` target named
  in the registered migration SQL).

Gate semantics (fail closed, deterministic)
-------------------------------------------
``check_schema_compatibility`` raises :class:`SchemaIncompatible` — a
stable, transport-neutral classification — for every negative case:

1. ``schema_migrations`` table absent        → REGISTRY_TABLE_MISSING
2. registry present, no successful row       → VERSION_RECORD_ABSENT
3. applied schema older than the registered  → SCHEMA_STALE
4. applied versions the registry does not    → SCHEMA_FUTURE_VERSION
   know (newer/foreign schema)
5. malformed metadata (non-integer version,  → SCHEMA_MALFORMED
   NULL checksum)
6. a migration failed on last apply          → SCHEMA_MIGRATION_FAILED
7. checksum of an applied version disagrees  → SCHEMA_CHECKSUM_MISMATCH
   with the registered migration (tampered/
   divergent schema)
8. connection works but required application → REQUIRED_OBJECTS_MISSING
   objects (tables) are missing

Database-level failures during the gate's own reads raise
:class:`SchemaGateUnavailable` (the readiness layer maps it to
``DEPENDENCY_UNAVAILABLE``, not to a schema verdict).

This module sits in the persistence layer and imports nothing from the
service/transport layers; the readiness integration lives in
:mod:`phase3.service.boundary`.
"""
from __future__ import annotations

import re
from typing import Any

from phase3.persistence.migrations import MigrationManager, default_migrations_for

__all__ = [
    "SchemaIncompatible",
    "SchemaGateUnavailable",
    "REGISTRY_TABLE_MISSING",
    "VERSION_RECORD_ABSENT",
    "SCHEMA_STALE",
    "SCHEMA_FUTURE_VERSION",
    "SCHEMA_MALFORMED",
    "SCHEMA_CHECKSUM_MISMATCH",
    "SCHEMA_MIGRATION_FAILED",
    "REQUIRED_OBJECTS_MISSING",
    "SCHEMA_GATE_CODES",
    "check_schema_compatibility",
]

# ----- stable gate codes (machine-readable readiness classification) ------

REGISTRY_TABLE_MISSING = "REGISTRY_TABLE_MISSING"
VERSION_RECORD_ABSENT = "VERSION_RECORD_ABSENT"
SCHEMA_STALE = "SCHEMA_STALE"
SCHEMA_FUTURE_VERSION = "SCHEMA_FUTURE_VERSION"
SCHEMA_MALFORMED = "SCHEMA_MALFORMED"
SCHEMA_CHECKSUM_MISMATCH = "SCHEMA_CHECKSUM_MISMATCH"
SCHEMA_MIGRATION_FAILED = "SCHEMA_MIGRATION_FAILED"
REQUIRED_OBJECTS_MISSING = "REQUIRED_OBJECTS_MISSING"

#: Closed, stable set of failure codes — pinned by the R3 suite.
SCHEMA_GATE_CODES = frozenset(
    {
        REGISTRY_TABLE_MISSING,
        VERSION_RECORD_ABSENT,
        SCHEMA_STALE,
        SCHEMA_FUTURE_VERSION,
        SCHEMA_MALFORMED,
        SCHEMA_CHECKSUM_MISMATCH,
        SCHEMA_MIGRATION_FAILED,
        REQUIRED_OBJECTS_MISSING,
    }
)

_CREATE_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.IGNORECASE,
)


class SchemaIncompatible(RuntimeError):
    """The database schema state is deterministically incompatible.

    ``code`` is a stable ``SCHEMA_*``/``*_MISSING`` classification from
    :data:`SCHEMA_GATE_CODES`; ``detail`` carries non-sensitive counts
    (version numbers and names only — never DSNs, hosts or SQL).
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"schema compatibility gate failed: {code}" + (
            f" ({detail})" if detail else ""
        ))
        self.code = code
        self.detail = detail


class SchemaGateUnavailable(RuntimeError):
    """The gate could not read schema metadata at all (DB layer error).

    Mapped by the readiness integration to DEPENDENCY_UNAVAILABLE —
    this is an *unavailability* verdict, not an incompatibility one.
    """

    def __init__(self, detail: str = "schema metadata is not readable") -> None:
        import re as _re

        from phase3.persistence.backend import sanitize_db_url

        super().__init__(_re.sub(r"postgresql?://\S+", "<redacted>", str(
            sanitize_db_url(detail)
        )))
        self.detail = str(detail)


def _is_missing_table_error(exc: BaseException) -> bool:
    """True when ``exc`` means 'relation/table does not exist'."""
    # PostgreSQL: psycopg marks it with sqlstate 42P01.
    if getattr(exc, "sqlstate", None) == "42P01":
        return True
    # SQLite: sqlite3.OperationalError('no such table: schema_migrations').
    text = str(exc).lower()
    return "no such table" in text or "does not exist" in text


def _expected_tables(manager: MigrationManager) -> tuple[str, ...]:
    """Parse the registered migration SQL for CREATE TABLE targets.

    The migration SQL itself stays the single authoritative source; no
    separate table list is maintained here to drift out of sync.
    """
    names: list[str] = []
    for mig in manager.migrations:
        for name in _CREATE_TABLE_RE.findall(mig.sql):
            if name.lower() not in {n.lower() for n in names}:
                names.append(name)
    return tuple(names)


def _existing_table_names(store: Any) -> frozenset[str]:
    """Names of the tables that actually exist in the backend."""
    if getattr(store, "backend", "sqlite") == "postgres":
        rows = store.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        ).fetchall()
    else:
        rows = store.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return frozenset(str(row[0]).lower() for row in rows)


def check_schema_compatibility(store: Any) -> dict[str, Any]:
    """Prove the store's schema state is compatible; fail closed otherwise.

    Returns a small non-sensitive dict (``current_version``,
    ``expected_version``, ``applied``) on success; raises
    :class:`SchemaIncompatible` (stable code) for every incompatible
    metadata state and :class:`SchemaGateUnavailable` when the metadata
    itself cannot be read.
    """
    manager = MigrationManager(store, default_migrations_for(store))
    registered = manager.migrations
    if not registered:
        # A repository with no registered migrations has no schema
        # identity to prove — treat as malformed configuration.
        raise SchemaIncompatible(SCHEMA_MALFORMED, "no migrations registered")
    expected_max = max(m.version for m in registered)
    expected_checksum = {m.version: m.checksum for m in registered}

    try:
        rows = store.execute(
            "SELECT version, name, checksum, applied_at, error "
            "FROM schema_migrations"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - classified below
        if _is_missing_table_error(exc):
            raise SchemaIncompatible(
                REGISTRY_TABLE_MISSING,
                "schema_migrations table is absent — schema was never applied",
            ) from exc
        raise SchemaGateUnavailable(str(exc)) from exc

    # --- validate row shape (deterministic metadata contract) ------------
    successful: dict[int, str] = {}
    failed: list[int] = []
    for row in rows:
        version_raw, checksum = row[0], row[2]
        error = row[4] if len(row) > 4 else None
        try:
            version = int(version_raw)
        except (TypeError, ValueError):
            # Non-integer version values are corrupt metadata.
            if error is None:
                raise SchemaIncompatible(
                    SCHEMA_MALFORMED, f"non-integer schema_migrations.version {version_raw!r}"
                ) from None
            raise SchemaIncompatible(SCHEMA_MALFORMED, "malformed registry row") from None
        if error is None:
            if checksum is None:
                raise SchemaIncompatible(SCHEMA_MALFORMED, "NULL checksum")
            successful[version] = str(checksum)
        else:
            failed.append(version)

    if failed:
        raise SchemaIncompatible(
            SCHEMA_MIGRATION_FAILED,
            f"failed migrations would be retried: {sorted(failed)}",
        )
    if not successful:
        raise SchemaIncompatible(
            VERSION_RECORD_ABSENT,
            "schema_migrations exists but records no successful migration",
        )

    unknown = [v for v in successful if v not in expected_checksum]
    if unknown:
        raise SchemaIncompatible(
            SCHEMA_FUTURE_VERSION,
            f"database schema is newer/unknown to this application: {sorted(unknown)}",
        )
    current = max(successful)
    if current < expected_max:
        raise SchemaIncompatible(
            SCHEMA_STALE,
            f"applied schema version {current} is older than the "
            f"application expects ({expected_max}); missing "
            f"{sorted(set(expected_checksum) - set(successful))}",
        )
    mismatched = [
        v for v, checksum in successful.items()
        if expected_checksum[v] != checksum
    ]
    if mismatched:
        raise SchemaIncompatible(
            SCHEMA_CHECKSUM_MISMATCH,
            f"applied checksum diverges from the registered migration: {sorted(mismatched)}",
        )

    try:
        existing = _existing_table_names(store)
    except Exception as exc:  # noqa: BLE001 - classified
        if _is_missing_table_error(exc):
            raise SchemaIncompatible(
                REQUIRED_OBJECTS_MISSING,
                "required schema objects are absent",
            ) from exc
        raise SchemaGateUnavailable(str(exc)) from exc
    missing = [
        name for name in _expected_tables(manager)
        if name.lower() not in existing
    ]
    if missing:
        raise SchemaIncompatible(
            REQUIRED_OBJECTS_MISSING,
            f"required tables missing from schema: {sorted(name.lower() for name in missing)}",
        )

    return {
        "current_version": current,
        "expected_version": expected_max,
        "applied": sorted(successful),
    }