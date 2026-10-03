"""PostgreSQL-backed graph store for Phase 3B (Phase 6.3 parity).

Same behaviour and public surface as
:class:`phase3.graph.sqlite_store.SQLiteGraphStore` — the base class
:class:`SqlGraphStore` is backend-agnostic — but opened on
PostgreSQL instead. Exists to prove the graph persistence contract
ported to the disposable/synthetic PostgreSQL backend (Phase 6.2
ADR-C03, Phase 6.3 work order §3); not a production target in this
phase.
"""
from __future__ import annotations

from phase3.graph.sqlite_store import SqlGraphStore
from phase3.persistence.postgres import PostgresStore

__all__ = ["PostgresGraphStore"]


class PostgresGraphStore(SqlGraphStore):
    """PostgreSQL-backed :class:`GraphStore`.

    Parameters
    ----------
    dsn:
        libpq-style DSN (``postgres://user@host:port/db``). May embed
        a password; never print raw (use
        :func:`phase3.persistence.backend.sanitize_db_url`).
    auto_migrate:
        If True (default), :meth:`ensure_schema` runs at
        construction so first use Just Works. Set False in tests
        that drive migration explicitly.
    """

    def __init__(self, dsn: str, *, auto_migrate: bool = True) -> None:
        super().__init__(PostgresStore(dsn), auto_migrate=auto_migrate)

    @property
    def dsn_masked(self) -> str:
        """Sanitized DSN of the underlying store (log-safe)."""
        return self._store.dsn_masked