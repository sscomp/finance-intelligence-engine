"""Backend selection for the Phase 3B persistence layer (Phase 6.3).

One entry point — :func:`resolve_spec` — turns a caller-supplied
specifier into a :class:`DatabaseSpec`, and :func:`open_store`
instantiates the right :class:`~phase3.persistence.contracts.DatabaseStore`.

Precedence (Phase 6.8A runtime contract; supersedes the Phase 6.3 §13
default)
------------
1. explicit API/CLI argument (``--db-path`` value or a Python
   argument), when non-empty;
2. the canonical role variable ``FIE_DB_TARGET_INTELLIGENCE``;
3. legacy aliases ``FIE_DATABASE_URL`` / ``FIE_INTELLIGENCE_DB``
   (contradictory values fail closed);
4. nothing — a missing target raises :class:`FailClosedTarget`
   (no implicit CWD SQLite selection remains).

Selection rule
--------------
A specifier that starts with ``postgres://`` or ``postgresql://``
(ignoring leading whitespace, case-insensitive scheme) selects the
PostgreSQL backend; anything else is a SQLite file path. A SQLite
path goes through the existing ``macro_history.db`` path guard
(:func:`~phase3.persistence.sqlite._check_path`) at *open* time, so
opening a forbidden path still fails closed exactly as before.

Security
--------
PostgreSQL DSNs may embed a password. :func:`sanitize_db_url` masks
it; log lines and diagnostics built in this module only ever render
the sanitized form. No secrets are written by this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from phase3.runtime_contract import (
    FailClosedTarget,
    resolve_intelligence_target,
)
from phase3.persistence.sqlite import (
    ACCESS_IMMUTABLE,
    ACCESS_MODES,
    ACCESS_READONLY,
    ACCESS_WRITABLE,
)

__all__ = [
    "DatabaseSpec",
    "BACKEND_SQLITE",
    "BACKEND_POSTGRES",
    "PG_URL_PREFIXES",
    "ACCESS_WRITABLE",
    "ACCESS_READONLY",
    "ACCESS_IMMUTABLE",
    "ACCESS_MODES",
    "sanitize_db_url",
    "resolve_spec",
    "open_store",
]

BACKEND_SQLITE = "sqlite"
BACKEND_POSTGRES = "postgres"

PG_URL_PREFIXES = ("postgres://", "postgresql://")


def is_pg_dsn(specifier: str) -> bool:
    """True when ``specifier`` selects the PostgreSQL backend."""
    return bool(specifier) and specifier.strip().lower().startswith(PG_URL_PREFIXES)

_MASK = re.compile(r"(?::)([^:@/\s]+)(?=@)")


@dataclass(frozen=True)
class DatabaseSpec:
    """Resolved backend specifier.

    Attributes
    ----------
    backend:
        ``"sqlite"`` or ``"postgres"``.
    dsn:
        SQLite: the database file path. PostgreSQL: a libpq-style
        DSN string. Treat as secret-bearing for PostgreSQL.
    source:
        ``"explicit"``, ``"env"`` or ``"default"`` — where the
        specifier came from (precedence evidence for logs/reports).
    original:
        The raw specifier as given (may contain a password for
        PostgreSQL; never log).
    """

    backend: str
    dsn: str
    source: str
    original: str

    def sanitized(self) -> str:
        """The raw specifier with any password masked (log-safe)."""
        return sanitize_db_url(self.original)


def sanitize_db_url(url: str) -> str:
    """Mask the password component of a DSN/URL, if present.

    Handles both URL form (``postgres://user:secret@host/db``) and
    libpq keyword form (``host=x password=secret``?). Non-matching
    strings are returned unchanged.
    """
    masked = _MASK.sub(":***", url)
    masked = re.sub(r"(password\s*=\s*)(\S+)", r"\1***", masked, flags=re.I)
    return masked


def resolve_spec(explicit: str | None = None) -> DatabaseSpec:
    """Resolve the backend spec through the canonical runtime contract.

    Phase 6.8A precedence: explicit > ``FIE_DB_TARGET_INTELLIGENCE`` >
    legacy aliases (``FIE_DATABASE_URL`` / ``FIE_INTELLIGENCE_DB``,
    contradiction = fail closed). The historical portable default
    (CWD-relative ``phase3/data/intelligence.db``) is ABOLISHED: a
    missing target raises :class:`FailClosedTarget` before any store can
    be opened (WO C-3 / §3.5 — no implicit CWD SQLite selection).
    """
    try:
        resolved = resolve_intelligence_target(explicit)
    except FailClosedTarget as exc:
        if explicit:
            # An explicit-but-unparseable target retains the caller's
            # context; re-raise unchanged (fail closed).
            raise
        raise FailClosedTarget(
            exc.reason,
            exc.detail or "no intelligence/store target contract",
        ) from None
    return DatabaseSpec(
        backend=resolved.backend,
        dsn=resolved.value,
        source="env" if resolved.source in ("canonical_env", "legacy_alias")
        else "explicit",
        original=resolved.value,
    )


def _spec_from(value: str, source: str) -> DatabaseSpec:
    candidate = value.strip()
    lowered = candidate.lower()
    if lowered.startswith(PG_URL_PREFIXES):
        return DatabaseSpec(
            backend=BACKEND_POSTGRES, dsn=candidate, source=source, original=value
        )
    if lowered.startswith("sqlite://") or lowered.startswith("sqlite:"):
        # Accept sqlite:// URLs by stripping the scheme prefix for the
        # path (sqlite:///abs/path -> abs/path; sqlite:relative -> relative).
        # (Phase 6.7B-R1 defect FIE-R1-001 repair: the former regex
        # consumed one slash too many and silently rewrote the
        # documented absolute form — "sqlite:///abs/db" resolved to
        # "abs/db"-without-leading-slash, i.e. a CWD-relative path.
        # Scheme stripping must never invent a different path; the
        # production DSN gate (ADR-017) classifies by absolute-ness,
        # so a silent leading-slash mutation is a fail-open hazard.)
        if lowered.startswith("sqlite://"):
            path = candidate[len("sqlite://"):]
        else:
            path = candidate[len("sqlite:"):]
        return DatabaseSpec(
            backend=BACKEND_SQLITE, dsn=path, source=source, original=value
        )
    return DatabaseSpec(
        backend=BACKEND_SQLITE, dsn=candidate, source=source, original=value
    )


def open_store(spec: DatabaseSpec, *, access_mode: str | None = None) -> Any:
    """Open the :class:`~phase3.persistence.contracts.DatabaseStore` for ``spec``.

    Returns an open (unclosed) store; the caller owns its lifecycle.
    PostgreSQL requires the optional ``psycopg`` dependency — a clear
    ImportError is raised when it is missing so operators can
    ``pip install 'finance-intelligence-engine[postgres]'``.

    ``access_mode`` (Phase 6.6R4) selects the SQLite deployment open
    semantics (:data:`~phase3.persistence.backend.ACCESS_WRITABLE` /
    :data:`ACCESS_READONLY` / :data:`ACCESS_IMMUTABLE`, declared in the
    SQLite seam and re-exported here); ``None`` keeps
    the historical writable default. A non-default access mode against a
    PostgreSQL DSN is rejected — its read-only counterpart is
    ``set_query_only()`` (``SET default_transaction_read_only = on``),
    applied by the service boundary independently of deployment open
    semantics.
    """
    if spec.backend == BACKEND_POSTGRES:
        if access_mode is not None and access_mode != ACCESS_WRITABLE:
            raise ValueError(
                "open_store: access_mode is a SQLite deployment contract; "
                f"{spec.backend!r} specs only accept the writable default "
                "(read-only enforcement is set_query_only())"
            )
        from phase3.persistence.postgres import PostgresStore

        return PostgresStore(spec.dsn)
    from phase3.persistence.sqlite import SQLiteStore

    return SQLiteStore(spec.dsn, access_mode=access_mode or ACCESS_WRITABLE)