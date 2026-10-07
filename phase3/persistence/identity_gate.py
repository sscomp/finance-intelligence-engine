"""Deterministic runtime database-identity gate (Phase 6.9B-R3).

Fail-closed companion to :mod:`phase3.persistence.schema_gate`. The
schema gate proves WHAT schema the database carries; the identity gate
proves WHO the connected runtime identity is and WHAT it is allowed to
do. A successful TCP connect (or `SELECT 1`) proves neither — R3 is
the work order that adds this gate.

Gate semantics (fail closed, deterministic, PostgreSQL-scoped)
--------------------------------------------------------------
``check_runtime_identity`` raises :class:`IdentityIncompatible` — a
stable, transport-neutral classification — for every negative case:

1. connected role is a superuser           → RUNTIME_ROLE_SUPERUSER
2. connected role has CREATEROLE attribute → RUNTIME_ROLE_CREATEROLE
3. connected role has CREATEDB attribute   → RUNTIME_ROLE_CREATEDB
4. connected role has replication attribute→ RUNTIME_ROLE_REPLICATION
5. connected role has BYPASSRLS attribute  → RUNTIME_ROLE_BYPASSRLS
6. connected role can CREATE the current   → RUNTIME_OVERPRIVILEGED_SCHEMA
   schema (would allow schema mutation)
7. connected role OWNS relations/objects   → RUNTIME_ROLE_OWNS_OBJECTS
   in the current schema
8. connected role inherits other database  → RUNTIME_INHERITED_ROLE
   roles (membership escalation path)
9. connected database/schema/role does not → WRONG_DATABASE / WRONG_SCHEMA /
   match the operator-pinned expectations    WRONG_ROLE
   (``FIE_EXPECTED_RUNTIME_DB`` /
   ``FIE_EXPECTED_RUNTIME_SCHEMA`` /
   ``FIE_EXPECTED_RUNTIME_ROLE``, when set)

Attribute prohibitions are ALWAYS enforced for a runtime connection;
expectation comparisons are enforced only when the operator pins the
expectation (an unset expectation is deliberately not guessed).

Database-level failures during the gate's own reads raise
:class:`IdentityGateUnavailable` (the readiness layer maps it to
``DEPENDENCY_UNAVAILABLE``, not an identity verdict).

Like :mod:`schema_gate`, this module sits in the persistence layer and
imports nothing from the service/transport layers; readiness
integration lives in :mod:`phase3.service.boundary`.

Non-PostgreSQL backends: the identity gate returns
``{"applicable": false}`` — a SQLite file's identity is the resolved
path itself, owned by the R1 target-resolution contract (fail-closed
target resolution, no dial). Nothing is proved absent; the caller must
not treat the gate as skipped-silently — the report states it.

Credential boundary: no DSN, password, or connection material is ever
returned or logged by this module; the report carries catalog-derived
identity names (``current_user``/database/schema) only. Names printed
here are database catalogs the caller already has access to.
"""
from __future__ import annotations

import os
from typing import Any

__all__ = [
    "IdentityIncompatible",
    "IdentityGateUnavailable",
    "check_runtime_identity",
    "RUNTIME_ROLE_SUPERUSER",
    "RUNTIME_ROLE_CREATEROLE",
    "RUNTIME_ROLE_CREATEDB",
    "RUNTIME_ROLE_REPLICATION",
    "RUNTIME_ROLE_BYPASSRLS",
    "RUNTIME_OVERPRIVILEGED_SCHEMA",
    "RUNTIME_ROLE_OWNS_OBJECTS",
    "RUNTIME_INHERITED_ROLE",
    "WRONG_DATABASE",
    "WRONG_SCHEMA",
    "WRONG_ROLE",
    "expected_runtime_expectations",
]

RUNTIME_ROLE_SUPERUSER = "RUNTIME_ROLE_SUPERUSER"
RUNTIME_ROLE_CREATEROLE = "RUNTIME_ROLE_CREATEROLE"
RUNTIME_ROLE_CREATEDB = "RUNTIME_ROLE_CREATEDB"
RUNTIME_ROLE_REPLICATION = "RUNTIME_ROLE_REPLICATION"
RUNTIME_ROLE_BYPASSRLS = "RUNTIME_ROLE_BYPASSRLS"
RUNTIME_OVERPRIVILEGED_SCHEMA = "RUNTIME_OVERPRIVILEGED_SCHEMA"
RUNTIME_ROLE_OWNS_OBJECTS = "RUNTIME_ROLE_OWNS_OBJECTS"
RUNTIME_INHERITED_ROLE = "RUNTIME_INHERITED_ROLE"
WRONG_DATABASE = "WRONG_DATABASE"
WRONG_SCHEMA = "WRONG_SCHEMA"
WRONG_ROLE = "WRONG_ROLE"

#: Relation kinds the ownership check covers: ordinary tables, partitioned
#: tables, sequences, functions-in-table-space (none), views, matviews.
_OWNED_REL_KINDS = ("r", "p", "S", "v", "m")


class IdentityIncompatible(Exception):
    """The connected identity violates the runtime identity contract.

    ``code`` is a stable, transport-neutral classification; ``detail``
    names configuration classes and catalog values the connected role
    can already see — never credentials.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class IdentityGateUnavailable(Exception):
    """The identity metadata itself cannot be read (DB outage/context)."""


def expected_runtime_expectations(
    env: dict[str, str] | None = None,
) -> dict[str, str | None]:
    """Read the optional operator-pinned runtime expectations.

    Values are read only — the caller (readiness/CLI) compares them
    against catalog-derived identity. An unset variable means "no
    expectation" (never a default guess).
    """
    e = os.environ if env is None else env
    return {
        "database": e.get("FIE_EXPECTED_RUNTIME_DB") or None,
        "schema": e.get("FIE_EXPECTED_RUNTIME_SCHEMA") or None,
        "role": e.get("FIE_EXPECTED_RUNTIME_ROLE") or None,
    }


def _classify_role_attributes(attrs: dict[str, bool]) -> None:
    prohibitions = (
        ("rolsuper", RUNTIME_ROLE_SUPERUSER, "superuser"),
        ("rolcreaterole", RUNTIME_ROLE_CREATEROLE, "CREATEROLE"),
        ("rolcreatedb", RUNTIME_ROLE_CREATEDB, "CREATEDB"),
        ("rolreplication", RUNTIME_ROLE_REPLICATION, "REPLICATION"),
        ("rolbypassrls", RUNTIME_ROLE_BYPASSRLS, "BYPASSRLS"),
    )
    for key, code, human in prohibitions:
        if attrs.get(key):
            raise IdentityIncompatible(
                code, f"connected runtime role carries prohibited attribute {human}"
            )


def check_runtime_identity(
    store: Any,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Prove the connected identity satisfies the runtime role contract.

    Raises :class:`IdentityIncompatible` on every prohibited
    attribute/schema privilege/object-ownership/inherited-membership
    state and on a pinned-expectation mismatch. Raises
    :class:`IdentityGateUnavailable` when the catalog reads themselves
    fail. Returns a small non-sensitive dict on success.
    """
    backend = getattr(store, "backend", "sqlite")
    if backend != "postgres":
        # SQLite identity is the resolved target path (R1 contract);
        # PostgreSQL catalog identity concepts do not apply.
        return {"applicable": False, "backend": backend}

    # --- who/where is this connection (one catalog round-trip) ----------
    try:
        row = store.execute(
            "SELECT current_user, session_user, current_database(), "
            "current_schema()"
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 - classified below
        raise IdentityGateUnavailable(
            "identity context is not readable (backend unavailable)"
        ) from exc
    if row is None:
        raise IdentityGateUnavailable("identity context query returned no row")
    role, session_role, database, schema = str(row[0]), str(row[1]), row[2], row[3]
    schema = "public" if schema is None else str(schema)

    try:
        attrs_row = store.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
            "rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 - classified below
        raise IdentityGateUnavailable(
            "role attributes are not readable (backend unavailable)"
        ) from exc
    if attrs_row is None:
        raise IdentityIncompatible(
            WRONG_ROLE,
            "connected role has no pg_roles entry (auth context vanished)",
        )
    attrs = {
        "rolsuper": bool(attrs_row[0]),
        "rolcreaterole": bool(attrs_row[1]),
        "rolcreatedb": bool(attrs_row[2]),
        "rolreplication": bool(attrs_row[3]),
        "rolbypassrls": bool(attrs_row[4]),
    }
    _classify_role_attributes(attrs)

    # --- schema privilege + ownership + inherited roles ------------------
    try:
        create_row = store.execute(
            "SELECT has_schema_privilege(current_user, current_schema(), "
            "'CREATE')"
        ).fetchone()
        owned_row = store.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = current_schema() "
            "AND c.relkind = ANY(%s) AND c.relowner = "
            "(SELECT oid FROM pg_roles WHERE rolname = current_user)",
            (list(_OWNED_REL_KINDS),),
        ).fetchone()
        member_rows = store.execute(
            "SELECT count(*) FROM pg_auth_members WHERE member = "
            "(SELECT oid FROM pg_roles WHERE rolname = current_user)"
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 - classified below
        raise IdentityGateUnavailable(
            "catalog privilege reads failed (backend unavailable)"
        ) from exc
    if create_row is not None and bool(create_row[0]):
        raise IdentityIncompatible(
            RUNTIME_OVERPRIVILEGED_SCHEMA,
            f"runtime role holds CREATE privilege on schema {schema!r}",
        )
    if owned_row is not None and int(owned_row[0]) > 0:
        raise IdentityIncompatible(
            RUNTIME_ROLE_OWNS_OBJECTS,
            f"runtime role owns {int(owned_row[0])} relation(s) in schema {schema!r}",
        )
    if member_rows is not None and int(member_rows[0]) > 0:
        raise IdentityIncompatible(
            RUNTIME_INHERITED_ROLE,
            f"runtime role inherits {int(member_rows[0])} other role(s)",
        )

    # --- operator-pinned expectations (optional) -------------------------
    expectations = expected_runtime_expectations(env)
    enforced: dict[str, bool] = {}
    if expectations["database"] is not None:
        got_db = str(database)
        enforced["database"] = got_db == expectations["database"]
        if not enforced["database"]:
            raise IdentityIncompatible(
                WRONG_DATABASE,
                f"expected database {expectations['database']!r}, "
                f"connected to {got_db!r}",
            )
    if expectations["schema"] is not None:
        enforced["schema"] = schema == expectations["schema"]
        if not enforced["schema"]:
            raise IdentityIncompatible(
                WRONG_SCHEMA,
                f"expected schema {expectations['schema']!r}, "
                f"current schema is {schema!r}",
            )
    if expectations["role"] is not None:
        enforced["role"] = role == expectations["role"] \
            and session_role == expectations["role"]
        if not enforced["role"]:
            raise IdentityIncompatible(
                WRONG_ROLE,
                "expected runtime role "
                f"{expectations['role']!r}; connected as {role!r}"
                + (f" (session_user {session_role!r})" if session_role != role
                   else ""),
            )

    return {
        "applicable": True,
        "role": role,
        "session_role": session_role,
        "database": str(database) if database is not None else None,
        "schema": schema,
        "role_attributes": attrs,
        "schema_create_granted": bool(create_row[0]) if create_row else False,
        "owned_relations": int(owned_row[0]) if owned_row else 0,
        "inherited_roles": int(member_rows[0]) if member_rows else 0,
        "expectations_enforced": enforced,
    }


def _cli() -> int:
    """Deterministic standalone runner (``python -m
    phase3.persistence.identity_gate``).

    Resolves the intelligence target through the R1 contract
    (explicit > canonical env > aliases; no implicit default), opens
    the store, runs :func:`check_runtime_identity`, prints a masked
    JSON verdict, and exits 0 (PASS) / 2 (incompatible) / exit 78 for
    missing/malformed targets (R1 fail-closed exit class).
    """
    import json
    import sys

    from phase3.persistence.backend import open_store, resolve_spec
    from phase3.runtime_contract import FailClosedTarget

    try:
        spec = resolve_spec()
    except FailClosedTarget as exc:
        print(
            json.dumps({"result": "REFUSED", "reason": getattr(exc, "reason", None) or str(exc)}),
            file=sys.stderr,
        )
        return getattr(exc, "exit_code", 78)
    try:
        store = open_store(spec)
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps({"result": "REFUSED", "reason": "STORE_OPEN_FAILED",
             "detail": "the resolved target is not openable (detail withheld)"}),
            file=sys.stderr,
        )
        return 2
    try:
        verdict = check_runtime_identity(store)
    except IdentityIncompatible as exc:
        print(
            json.dumps({"result": "REJECTED", "code": exc.code, "detail": exc.detail}),
            file=sys.stderr,
        )
        return 2
    except IdentityGateUnavailable as exc:
        print(
            json.dumps({"result": "UNAVAILABLE", "reason": "identity metadata unreadable"}),
            file=sys.stderr,
        )
        return 2
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - best effort teardown
            pass
    print(json.dumps({"result": "PASS", **verdict}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())