"""SQLite connection helper for Phase 3B.

Provides:
- :class:`SQLiteStore` — owns a sqlite3 connection, applies the Phase 3B
  PRAGMA profile (WAL, foreign_keys, synchronous=NORMAL, busy_timeout),
  exposes :meth:`transaction` as a ``BEGIN IMMEDIATE`` / ``COMMIT`` /
  ``ROLLBACK`` context manager, and rejects any path that resolves to
  ``macro_history.db``.
- :func:`integrity_check` / :func:`quick_check` — small helpers that
  work on any sqlite file (used by retention + backup tests).
- :data:`FORBIDDEN_DB_NAME` — the protected production DB filename.
  Used as the sole tripwire: if any resolved path's basename matches
  this value, opening the store raises :class:`PathGuardError`.

Design notes
------------
* The store is a thin wrapper over stdlib ``sqlite3`` — no ORM, no
  third-party deps. This keeps Phase 3B tests network-free and
  hermetic.
* The store is single-connection by design. If callers need concurrent
  access they should open a second store. The WAL pragma is set so
  reads do not block a single writer, but a second writer will
  ``SQLITE_BUSY`` until the first commits (caller's responsibility).
* Path resolution uses :func:`os.path.realpath` so symlinks cannot
  bypass the guard.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Hard guard
# ---------------------------------------------------------------------------

#: Basename of the production database that Phase 3B is forbidden from
#: touching. Any resolved DB path whose real basename equals this value
#: is rejected by :class:`SQLiteStore`.
FORBIDDEN_DB_NAME: str = "macro_history.db"


class PathGuardError(RuntimeError):
    """Raised when a path resolves to a protected database name."""


class TransactionError(RuntimeError):
    """Raised when a managed transaction fails to commit cleanly."""


def _check_path(db_path: str | os.PathLike[str]) -> str:
    """Resolve ``db_path`` to an absolute path and guard against the
    forbidden production DB.

    Returns the realpath as a string. Raises :class:`PathGuardError` if
    the resolved path's basename equals :data:`FORBIDDEN_DB_NAME`.
    """
    if not db_path:
        raise PathGuardError("db_path must be a non-empty string")
    # os.path.realpath resolves symlinks and `..` traversal, both of
    # which are the obvious ways a caller might try to bypass the guard.
    resolved = os.path.realpath(str(db_path))
    if os.path.basename(resolved) == FORBIDDEN_DB_NAME:
        raise PathGuardError(
            f"refusing to open {resolved!r}: basename matches "
            f"{FORBIDDEN_DB_NAME!r}. Phase 3B is not allowed to touch "
            f"the production database. Pick a different path "
            f"(default: phase3/data/intelligence.db)."
        )
    return resolved


# ---------------------------------------------------------------------------
# PRAGMA profile
# ---------------------------------------------------------------------------

#: Default PRAGMA settings applied to every connection opened by
#: :class:`SQLiteStore`. ``WAL`` lets readers proceed while a writer
#: holds the lock; ``synchronous=NORMAL`` is the recommended pairing for
#: WAL (full durability is preserved at checkpoint); ``busy_timeout``
#: is a small grace period for short concurrent transactions.
_DEFAULT_PRAGMAS: dict[str, Any] = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "foreign_keys": "ON",
    "busy_timeout": 5000,
}


def _apply_pragma(conn: sqlite3.Connection, pragmas: dict[str, Any] | None = None) -> None:
    """Apply the Phase 3B PRAGMA profile to ``conn``.

    If ``pragmas`` is provided, it overrides the module-level
    default on a per-key basis. This is the hook
    :class:`SQLiteStore` uses to honour caller-supplied overrides.
    """
    profile: dict[str, Any] = dict(_DEFAULT_PRAGMAS)
    if pragmas:
        profile.update(pragmas)
    cur = conn.cursor()
    try:
        for key, value in profile.items():
            cur.execute(f"PRAGMA {key}={value}")
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SQLiteStore:
    """Lightweight SQLite connection wrapper for Phase 3B.

    Parameters
    ----------
    db_path:
        Path to the database file. Created if it does not exist. The
        resolved basename is checked against :data:`FORBIDDEN_DB_NAME`
        before any file is opened.
    pragmas:
        Optional PRAGMA overrides. Keys not in this dict fall back to
        the module-level default. Pass an empty dict to keep the
        default profile unchanged.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        pragmas: dict[str, Any] | None = None,
    ) -> None:
        self._resolved_path: str = _check_path(db_path)
        self._pragmas: dict[str, Any] = dict(_DEFAULT_PRAGMAS)
        if pragmas:
            self._pragmas.update(pragmas)
        # ``isolation_level=None`` means we control transactions
        # explicitly (we want BEGIN IMMEDIATE, not the default
        # deferred). ``detect_types`` left at default — we serialize
        # datetimes as ISO strings ourselves.
        self._conn: sqlite3.Connection = sqlite3.connect(
            self._resolved_path,
            isolation_level=None,
            timeout=30.0,
        )
        # Row factory gives us dict-like access; keeps repo code
        # readable.
        self._conn.row_factory = sqlite3.Row
        _apply_pragma(self._conn, self._pragmas)

    # ----- introspection ---------------------------------------------------

    @property
    def path(self) -> str:
        """The realpath of the opened database (post-guard)."""
        return self._resolved_path

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying ``sqlite3.Connection``.

        Callers should prefer :meth:`execute` and :meth:`transaction`
        over touching the connection directly — that keeps the API
        surface auditable.
        """
        return self._conn

    def pragma(self, name: str) -> Any:
        """Return the live value of a single PRAGMA (for assertions)."""
        cur = self._conn.execute(f"PRAGMA {name}")
        row = cur.fetchone()
        return row[0] if row is not None else None

    # ----- query / mutate --------------------------------------------------

    def execute(self, sql: str, params: tuple | dict | list | None = None) -> sqlite3.Cursor:
        """Run a single statement. Caller manages transactions."""
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq: list[tuple] | list[dict]) -> sqlite3.Cursor:
        """Run a parameterised batch. Caller manages transactions."""
        return self._conn.executemany(sql, seq)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside ``BEGIN IMMEDIATE`` / ``COMMIT`` /
        ``ROLLBACK``.

        ``BEGIN IMMEDIATE`` acquires a RESERVED lock at the start, so
        the rest of the body can do writes without ``SQLITE_BUSY`` from
        a parallel writer. If the body raises, the transaction is
        rolled back and the exception re-raised. If ``COMMIT`` itself
        fails, the partial transaction is rolled back and the original
        sqlite error is wrapped in :class:`TransactionError`.

        Robust to :meth:`sqlite3.Connection.executescript` in the
        body: ``executescript`` first issues a ``COMMIT`` on any open
        transaction, then runs the script in autocommit mode. We
        therefore check :attr:`Connection.in_transaction` before
        issuing our final ``COMMIT`` / ``ROLLBACK`` so a body that
        called ``executescript`` does not trip
        ``"cannot commit - no transaction is active"``. Callers that
        use ``executescript`` for migration DDL are correct on the
        DB side — the schema changes land; we just don't try to
        double-commit.
        """
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                # ROLLBACK after a failure is best-effort. The original
                # exception is the actionable signal.
                pass
            raise
        try:
            if conn.in_transaction:
                conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise TransactionError(f"COMMIT failed: {exc}") from exc

    # ----- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"SQLiteStore(path={self._resolved_path!r})"


# ---------------------------------------------------------------------------
# Health helpers
# ---------------------------------------------------------------------------


def quick_check(db_path: str | os.PathLike[str]) -> str:
    """Run ``PRAGMA quick_check`` on ``db_path`` and return the result.

    Returns the single-row value (typically ``"ok"``). Raises
    :class:`PathGuardError` if the path is forbidden. The store is
    closed before returning.
    """
    with SQLiteStore(db_path) as store:
        cur = store.connection.execute("PRAGMA quick_check")
        row = cur.fetchone()
        return str(row[0]) if row is not None else ""


def integrity_check(db_path: str | os.PathLike[str]) -> str:
    """Run ``PRAGMA integrity_check`` on ``db_path`` and return the result.

    The full integrity check is more expensive than ``quick_check``
    but is what ``backup.py`` uses to verify a copy. Returns ``"ok"``
    for a healthy database. Raises :class:`PathGuardError` for a
    forbidden path.
    """
    with SQLiteStore(db_path) as store:
        cur = store.connection.execute("PRAGMA integrity_check")
        row = cur.fetchone()
        return str(row[0]) if row is not None else ""


__all__ = [
    "FORBIDDEN_DB_NAME",
    "SQLiteStore",
    "PathGuardError",
    "TransactionError",
    "quick_check",
    "integrity_check",
]
