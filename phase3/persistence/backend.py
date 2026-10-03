"""Backend selection for the Phase 3B persistence layer (Phase 6.3).

One entry point — :func:`resolve_spec` — turns a caller-supplied
specifier into a :class:`DatabaseSpec`, and :func:`open_store`
instantiates the right :class:`~phase3.persistence.contracts.DatabaseStore`.

Precedence (Phase 6.3 work order §13)
-------------------------------------
1. explicit API/CLI argument (``--db-path`` value or a Python
   argument), when non-empty;
2. ``FIE_DATABASE_URL`` environment variable
   (:func:`~phase3.paths.database_url`);
3. portable default — SQLite at the caller's default path.

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

from phase3.paths import database_url

__all__ = [
    "DatabaseSpec",
    "BACKEND_SQLITE",
    "BACKEND_POSTGRES",
    "PG_URL_PREFIXES",
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
    """Resolve the backend spec: explicit > ``FIE_DATABASE_URL`` > default."""
    if explicit:
        return _spec_from(explicit, "explicit")
    from_env = database_url()
    if from_env:
        return _spec_from(from_env, "env")
    # Default: SQLite at the caller-provided portable default path.
    return DatabaseSpec(
        backend=BACKEND_SQLITE,
        dsn="phase3/data/intelligence.db",
        source="default",
        original="phase3/data/intelligence.db",
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
        path = re.sub(r"^sqlite:(?:///)?", "", candidate, count=1, flags=re.I)
        return DatabaseSpec(
            backend=BACKEND_SQLITE, dsn=path, source=source, original=value
        )
    return DatabaseSpec(
        backend=BACKEND_SQLITE, dsn=candidate, source=source, original=value
    )


def open_store(spec: DatabaseSpec) -> Any:
    """Open the :class:`~phase3.persistence.contracts.DatabaseStore` for ``spec``.

    Returns an open (unclosed) store; the caller owns its lifecycle.
    PostgreSQL requires the optional ``psycopg`` dependency — a clear
    ImportError is raised when it is missing so operators can
    ``pip install 'finance-intelligence-engine[postgres]'``.
    """
    if spec.backend == BACKEND_POSTGRES:
        from phase3.persistence.postgres import PostgresStore

        return PostgresStore(spec.dsn)
    from phase3.persistence.sqlite import SQLiteStore

    return SQLiteStore(spec.dsn)  # type: ignore[return-value]