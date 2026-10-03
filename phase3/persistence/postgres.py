"""PostgreSQL backend for the Phase 3B persistence layer (Phase 6.3).

Status per Phase 6.2 ADR-003 / work order §3: **disposable/synthetic
parity backend only**. Nothing in Phase 6.3 provisions a cloud
database or migrates production data; this store exists so the same
repositories, migrations and pipeline code can be exercised against
PostgreSQL (development parity, contract tests, golden parity runs).

Surface parity with :class:`~phase3.persistence.sqlite.SQLiteStore`
(both satisfy :class:`~phase3.persistence.contracts.DatabaseStore`):

* ``execute(sql, params)`` — ``%s`` placeholders, positional params.
* ``executemany(sql, seq)``.
* ``executescript(sql)`` — multi-statement script, no params (DDL).
  psycopg's simple-query protocol plays the ``executescript`` role:
  with no parameters the whole string is sent as one PQsendQuery and
  PostgreSQL applies the statements in sequence (inside the current
  transaction, if any) — the property the migration manager relies on.
* ``transaction()`` — explicit ``BEGIN`` / ``COMMIT`` / ``ROLLBACK``
  wrapper with the same failure semantics as the SQLite store
  (rollback on body error; ``TransactionError`` when COMMIT fails).
* ``set_query_only()`` — ``SET default_transaction_read_only = on``.
* ``registry_ddl()`` — backend-correct ``schema_migrations`` DDL
  (the SQLite one cannot be used: it carries a ``strftime`` default).
* ``describe()`` — diagnostics; the DSN is only ever rendered
  sanitized (via :func:`~phase3.persistence.backend.sanitize_db_url`)
  in returned diagnostics.

Connection shape: one connection, ``autocommit=True`` so reads
outside :meth:`transaction` self-commit like SQLite autocommit
semantics; managed transactions issue explicit ``BEGIN``/``COMMIT``.
Rows come back through a mapping-with-positional-access factory, so
repo code written against ``sqlite3.Row`` (both ``row["col"]`` and
``row[0]``) runs unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from phase3.persistence.backend import sanitize_db_url
from phase3.persistence.sqlite import TransactionError

__all__ = ["PostgresStore", "PG_REGISTRY_DDL", "PostgresTransactionError"]


class PostgresTransactionError(TransactionError):
    """Raised when a managed PG transaction fails to commit cleanly.

    Subclasses :class:`phase3.persistence.sqlite.TransactionError` so
    callers that catch the existing error class keep working.
    """


#: Backend-correct ``schema_migrations`` registry DDL. Identical logical
#: shape to the SQLite registry; timestamps are application-supplied
#: (no server-side default), so a replay writes ``applied_at`` explicitly.
PG_REGISTRY_DDL: str = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    checksum    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT '',
    error       TEXT
);
"""


class _Row(dict):
    """Mapping row that also accepts integer (positional) access.

    Lets repo code written against ``sqlite3.Row`` (``row["col"]``
    *and* ``row[0]``) run unchanged on PostgreSQL rows.
    """

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int) and not isinstance(key, bool):
            return super().__getitem__(self._keys()[key])
        return super().__getitem__(key)

    def _keys(self) -> list[str]:
        return list(super().keys())


def _row_factory(cursor: Any) -> Any:
    """psycopg row-factory: return a row *maker* bound to the column names."""
    if cursor.description is None:
        return lambda values: None
    names = [d.name for d in cursor.description]

    def _make(values: tuple) -> _Row:
        return _Row(zip(names, values))

    return _make


class PostgresStore:
    """psycopg-backed :class:`DatabaseStore` (see module docstring)."""

    backend: str = "postgres"

    def __init__(self, dsn: str, *, application_name: str = "fie-phase3b") -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ImportError(
                "PostgreSQL backend requires the optional dependency "
                "psycopg. Install it with: pip install "
                "'finance-intelligence-engine[postgres]' (or "
                "'pip install \"psycopg[binary]>=3.2\"'). "
                "The SQLite backend remains the default and works "
                "with stdlib only."
            ) from exc
        if not dsn:
            raise ValueError("PostgresStore requires a non-empty DSN")
        self._dsn: str = dsn
        self._psycopg = psycopg
        # autocommit=True: statements outside transaction() commit
        # immediately, matching the SQLite store's autocommit-style
        # contract. transaction() issues explicit BEGIN/COMMIT below.
        self._conn = psycopg.connect(
            dsn,
            autocommit=True,
            row_factory=_row_factory,
            application_name=application_name,
        )

    # ----- introspection ---------------------------------------------------

    @property
    def dsn_masked(self) -> str:
        """The sanitized DSN (password masked) — safe for logs."""
        return sanitize_db_url(self._dsn)

    # ----- query / mutate --------------------------------------------------

    def execute(self, sql: str, params: tuple | list | None = None) -> Any:
        """Run a single statement. Caller manages transactions."""
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq: list[tuple]) -> Any:
        """Run a parameterised batch. Caller manages transactions.

        (psycopg's ``executemany`` forbids ``RETURNING`` — callers of
        this method must not select it.)
        """
        with self._conn.cursor() as cur:
            rc = cur.executemany(sql, seq)
        return rc

    def executescript(self, sql: str) -> Any:
        """Run a multi-statement script with no parameters.

        With no bound parameters psycopg uses the simple-query
        protocol, so the whole script applies in one round trip.
        """
        return self._conn.execute(sql)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Run a block inside an explicit transaction.

        Mirrors :meth:`SQLiteStore.transaction`: body error →
        ``ROLLBACK`` + re-raise; COMMIT failure → rollback and raise
        :class:`PostgresTransactionError` (subclass of the shared
        :class:`TransactionError`).
        """
        conn = self._conn
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - rollback is best-effort
                pass
            raise
        try:
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise PostgresTransactionError(f"COMMIT failed: {exc}") from exc

    def set_query_only(self) -> None:
        """Flip the session to read-only (PG equivalent of PRAGMA
        ``query_only=1``). Idempotent."""
        self._conn.execute("SET default_transaction_read_only = on")

    def registry_ddl(self) -> str:
        """Backend-correct ``schema_migrations`` DDL."""
        return PG_REGISTRY_DDL

    # ----- lifecycle -------------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "PostgresStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def describe(self) -> dict[str, Any]:
        """Cheap diagnostics for logs/health checks (never raises).

        DSN is rendered password-masked only.
        """
        info: dict[str, Any] = {"backend": "postgres", "dsn": self.dsn_masked}
        try:
            row = self._conn.execute("SELECT version()").fetchone()
            if row is not None:
                info["server"] = str(row[0]).split(" on ")[0]
        except Exception:  # noqa: BLE001 - diagnostics only
            info["server"] = "unknown"
        return info

    def __repr__(self) -> str:
        return f"PostgresStore(dsn={self.dsn_masked!r})"