"""PostgreSQL backend for the Phase 3B persistence layer (Phase 6.3).

Status: the original Phase 6.2 ADR-003 "disposable/synthetic parity
backend only" qualification is SUPERSEDED by owner decision AD-1 of
the PostgreSQL Production Readiness Remediation work order
(ABACUS_FIE_6_7B_POSTGRESQL_PRODUCTION_READINESS_REMEDIATION_AND_CUTOVER_GATE,
2026-10-04): TARGET_BACKEND=POSTGRESQL,
ARCHITECTURE=H2_FULL_POSTGRESQL — this store is the production
Phase 3B backend once the controlled cutover executes (cutover itself
requires separate authorization). SQLite remains supported for
rollback/testing.

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

Connection shape / lifecycle (Phase 6.7B-R3, HP-07/06 / OI-08)
--------------------------------------------------------------
One connection, ``autocommit=True`` so reads outside :meth:`transaction`
self-commit like SQLite autocommit semantics; managed transactions
issue explicit ``BEGIN``/``COMMIT``. Rows come back through a mapping-
with-positional-access factory, so repo code written against
``sqlite3.Row`` (both ``row["col"]`` and ``row[0]``) runs unchanged.

The runtime request model already bounds concurrency (the transport
serves every domain operation on ONE dedicated worker thread), so a
pool would be an architectural rewrite with no benefit — the bounded
liveness contract is instead enforced on the single connection:

* **finite acquisition**: ``connect()`` passes libpq ``connect_timeout``
  (default 10 s, never unbounded);
* **finite operations**: the session sets ``statement_timeout``
  (default 60 s, int ms, overrideable) so an outage/stuck statement
  cannot hang a request indefinitely;
* **deterministic recovery**: a connection-level failure (server
  restart, connection drop, admin shutdown) marks the connection
  broken; the NEXT operation reconnects once with the same bounded
  timeout. A failed statement is NEVER implicitly re-executed —
  recovery happens strictly between caller operations, so a mutating
  operation cannot be double-applied;
* **transactions**: body error → ``ROLLBACK`` + re-raise; COMMIT
  failure → rollback and :class:`PostgresTransactionError`; a
  connection failure mid-transaction marks the connection broken (any
  in-flight work is rolled back server-side at restart);
* **release/close**: deterministic ``close()`` / context-manager
  exit; no leaked checked-out connection, no unbounded connection
  creation (at most one bounded reconnect per operation call);
* **no SQLite fallback**: connect/operation failures raise the
  driver error (sanitized) — the caller decides the readiness
  verdict; nothing silently re-targets another backend;
* **observability**: :data:`reconnect_count` and the module logger
  (``fie.persistence.postgres``) record connect/reconnect failures at
  WARNING with the password-masked DSN only.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Any, Iterator

from phase3.persistence.backend import sanitize_db_url
from phase3.persistence.sqlite import TransactionError

__all__ = [
    "PostgresStore",
    "PG_REGISTRY_DDL",
    "PostgresTransactionError",
    "DEFAULT_PG_CONNECT_TIMEOUT",
    "DEFAULT_PG_STATEMENT_TIMEOUT_MS",
]

_TRANSPORT_LOGGER = logging.getLogger("fie.persistence.postgres")

#: Finite default network/acquisition timeout (seconds) for
#: ``psycopg.connect`` — libpq takes integer seconds.
DEFAULT_PG_CONNECT_TIMEOUT = 10

#: Finite default per-statement session timeout (milliseconds) — guards
#: the runtime against an outage-induced indefinite hang.
DEFAULT_PG_STATEMENT_TIMEOUT_MS = 60_000

_DSN_URL_RE = re.compile(r"postgresql?://\S+")


def _is_connection_level(exc: BaseException, psycopg: Any) -> bool:
    """True when ``exc`` indicates the SESSION is dead (not a query bug).

    Server restart/crash, connection drop, admin shutdown. Statement
    errors (query canceled by timeout, syntax, integrity) are NOT
    connection-level — they must not tear down a healthy session.
    """
    # libpq connection exception classes (connection_exception = 08*, plus
    # operational shutdown codes issued when PostgreSQL disappears).
    # An sqlstate-carrying driver error is classified BY ITS SQLSTATE:
    # psycopg's Python class names overlap (57014 query_canceled — a
    # statement timeout — subclasses OperationalError), so the class
    # fallback must only decide for sqlstate-less transport faults.
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        return sqlstate.startswith("08") or sqlstate in (
            "57P01", "57P02", "57P03",
        )
    cls: str = type(exc).__name__
    if cls in ("OperationalError", "InterfaceError", "AdminShutdown",
               "ConnectionFailure", "ConnectionException"):
        return True
    return False


def _conn_error_text(exc: BaseException, dsn: str) -> str:
    """Log-safe rendering of a connection error: DSN/password redacted."""
    text = str(exc)
    if dsn:
        text = text.replace(dsn, "<redacted-dsn>")
    return _DSN_URL_RE.sub("<redacted-url>", text)


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

    def __init__(
        self,
        dsn: str,
        *,
        application_name: str = "fie-phase3b",
        connect_timeout: int = DEFAULT_PG_CONNECT_TIMEOUT,
        statement_timeout_ms: int = DEFAULT_PG_STATEMENT_TIMEOUT_MS,
    ) -> None:
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
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be a positive number of seconds")
        if statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be a positive number of milliseconds")
        self._dsn: str = dsn
        self._psycopg = psycopg
        self._application_name: str = application_name
        self._connect_timeout: int = int(connect_timeout)
        self._statement_timeout_ms: int = int(statement_timeout_ms)
        #: Observability: how many broken-connection recoveries happened.
        self.reconnect_count: int = 0
        # autocommit=True: statements outside transaction() commit
        # immediately, matching the SQLite store's autocommit-style
        # contract. transaction() issues explicit BEGIN/COMMIT below.
        self._conn = self._connect()

    # ----- bounded connection lifecycle (Phase 6.7B-R3) --------------------

    def _connect(self) -> Any:
        """Open a session with FINITE acquisition and statement timeouts."""
        try:
            conn = self._psycopg.connect(
                self._dsn,
                autocommit=True,
                row_factory=_row_factory,
                application_name=self._application_name,
                connect_timeout=self._connect_timeout,
            )
            # Session-level guard against outage-induced indefinite hangs.
            conn.execute(f"SET statement_timeout = {int(self._statement_timeout_ms)}")
            return conn
        except Exception as exc:  # noqa: BLE001 - sanitized below
            _TRANSPORT_LOGGER.warning(
                "postgres connect failed (timeout=%ss): %s",
                self._connect_timeout,
                _conn_error_text(exc, self._dsn),
            )
            raise

    def _connection_ok(self) -> bool:
        """True while the session is usable (not closed, not broken)."""
        conn = self._conn
        return conn is not None and not conn.closed and not conn.broken

    def _mark_broken(self, exc: BaseException) -> None:
        """Flag the session unusable after a connection-level failure.

        Server restart / dropped connection / admin shutdown. Nothing is
        implicitly retried here — the NEXT store operation reconnects
        with the same bounded acquisition timeout; a failed statement is
        never re-executed by the store itself.
        """
        if _is_connection_level(exc, self._psycopg):
            _TRANSPORT_LOGGER.warning(
                "postgres connection broken — next operation reconnects "
                "(reconnects so far: %d)",
                self.reconnect_count,
            )
            try:
                self.close()
            except Exception:  # noqa: BLE001 - close is best-effort here
                pass

    def _ensure_ready(self) -> None:
        """Guarantee a usable session before an operation runs.

        One bounded reconnect at most; never an unbounded wait, never a
        SQLite fallback. Connect failure raises the sanitized driver
        error so the readiness layer classifies it.
        """
        if self._connection_ok():
            return
        self.reconnect_count += 1
        _TRANSPORT_LOGGER.warning(
            "postgres reconnecting (attempt %d, timeout=%ss, dsn=%s)",
            self.reconnect_count,
            self._connect_timeout,
            sanitize_db_url(self._dsn),
        )
        self._conn = self._connect()

    # ----- introspection ---------------------------------------------------

    @property
    def dsn_masked(self) -> str:
        """The sanitized DSN (password masked) — safe for logs."""
        return sanitize_db_url(self._dsn)

    # ----- query / mutate --------------------------------------------------

    def execute(self, sql: str, params: tuple | list | None = None) -> Any:
        """Run a single statement. Caller manages transactions.

        Bounded recovery (Phase 6.7B-R3): a dead session is reconnected
        once (bounded connect timeout) BEFORE the statement runs; a
        failed statement is never re-executed.
        """
        self._ensure_ready()
        try:
            if params is None:
                return self._conn.execute(sql)
            return self._conn.execute(sql, params)
        except Exception as exc:  # noqa: BLE001 - classified below
            self._mark_broken(exc)
            raise

    def executemany(self, sql: str, seq: list[tuple]) -> Any:
        """Run a parameterised batch. Caller manages transactions.

        (psycopg's ``executemany`` forbids ``RETURNING`` — callers of
        this method must not select it.)
        """
        self._ensure_ready()
        try:
            with self._conn.cursor() as cur:
                rc = cur.executemany(sql, seq)
        except Exception as exc:  # noqa: BLE001 - classified below
            self._mark_broken(exc)
            raise
        return rc

    def executescript(self, sql: str) -> Any:
        """Run a multi-statement script with no parameters.

        With no bound parameters psycopg uses the simple-query
        protocol, so the whole script applies in one round trip.
        """
        self._ensure_ready()
        try:
            return self._conn.execute(sql)
        except Exception as exc:  # noqa: BLE001 - classified below
            self._mark_broken(exc)
            raise

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Run a block inside an explicit transaction.

        Mirrors :meth:`SQLiteStore.transaction`: body error →
        ``ROLLBACK`` + re-raise; COMMIT failure → rollback and raise
        :class:`PostgresTransactionError` (subclass of the shared
        :class:`TransactionError`). A connection failure mid-transaction
        marks the session broken + re-raises (in-flight work rolls back
        with the dead session; the next operation reconnects bounded).
        """
        self._ensure_ready()
        try:
            self._conn.execute("BEGIN")
        except Exception as exc:  # noqa: BLE001 - classified below
            self._mark_broken(exc)
            raise
        conn = self._conn
        # The session must remain the same object for BEGIN..COMMIT —
        # a reconnect is only considered BEFORE an operation, never
        # inside the caller's transaction body.
        try:
            yield conn
        except BaseException as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - rollback is best-effort
                pass
            self._mark_broken(exc)
            raise
        try:
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            self._mark_broken(exc)
            raise PostgresTransactionError(_conn_error_text(exc, self._dsn)) from exc

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

        DSN is rendered password-masked only; no exception text leaks.
        """
        info: dict[str, Any] = {
            "backend": "postgres",
            "dsn": self.dsn_masked,
            "reconnect_count": self.reconnect_count,
            "session_open": self._connection_ok(),
        }
        try:
            row = self._conn.execute("SELECT version()").fetchone()
            if row is not None:
                info["server"] = str(row[0]).split(" on ")[0]
        except Exception:  # noqa: BLE001 - diagnostics only
            info["server"] = "unknown"
        return info

    def __repr__(self) -> str:
        return f"PostgresStore(dsn={self.dsn_masked!r})"