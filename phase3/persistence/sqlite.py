"""SQLite connection helper for Phase 3B.

Provides:
- :class:`SQLiteStore` — owns a sqlite3 connection, applies the Phase 3B
  PRAGMA profile (WAL, foreign_keys, synchronous=NORMAL, busy_timeout),
  exposes :meth:`transaction` as a ``BEGIN IMMEDIATE`` / ``COMMIT`` /
  ``ROLLBACK`` context manager, and rejects any path that resolves to
  ``macro_history.db``.
- **access modes (Phase 6.6R4)** — :data:`ACCESS_WRITABLE` (default;
  the historical read-write open with the full PRAGMA profile),
  :data:`ACCESS_READONLY` (``file:...?mode=ro`` URI — read-only
  connection; SQLite may still create ``-shm``/``-wal`` sidecars when
  the database is in WAL journal mode and the directory is writable),
  and :data:`ACCESS_IMMUTABLE` (``file:...?mode=ro&immutable=1`` —
  the *published snapshot* open for the declared Phase 6.6 read-only
  deployment: SQLite assumes the artifact will never change while the
  connection lives, so no sidecar state is created or required, and
  the artifact **must not** be modified while such a reader is open).
  The ``journal_mode`` PRAGMA — a database-header write — is skipped
  for both read-only modes.
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
* Deployment semantics (Phase 6.6R4, why read-only access modes exist):
  WAL journal mode is *persistent* in the database header. Any open of
  a WAL-mode database requires the wal-index shared-memory file
  (``-shm``) — created on demand — so a read-only *open* of a
  WAL-mode artifact inside a read-only directory/mount cannot even
  start querying (``OperationalError``) unless the deployment supplied
  sidecar state. The Phase 6.6 reference deployment (documented
  ``-v <host-data-dir>:/data:ro``) ships the main ``.db`` only, so its
  consumer must open the artifact with the
  :data:`ACCESS_IMMUTABLE` semantics instead. Write-path workflows
  (ingest/scoring/CLI) keep the default :data:`ACCESS_WRITABLE`
  behaviour unchanged.
* Path resolution uses :func:`os.path.realpath` so symlinks cannot
  bypass the guard.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import quote as _uri_quote

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
    # Compare on lowercase to close the case-bypass gap (F2 finding).
    # On a case-sensitive filesystem, ``Macro_History.db`` and
    # ``macro_history.db`` resolve to different files, so the bypass
    # was previously silent. Treating the basename as case-insensitive
    # matches the production-DB contract: the path guard is the sole
    # tripwire, and the only safe assumption is that the production
    # name is canonical regardless of caller-supplied case.
    if os.path.basename(resolved).lower() == FORBIDDEN_DB_NAME.lower():
        raise PathGuardError(
            f"refusing to open {resolved!r}: basename matches "
            f"{FORBIDDEN_DB_NAME!r} (case-insensitive). Phase 3B is "
            f"not allowed to touch the production database. Pick a "
            f"different path (default: phase3/data/intelligence.db)."
        )
    return resolved


# ---------------------------------------------------------------------------
# PRAGMA profile
# ---------------------------------------------------------------------------

#: Access modes for :class:`SQLiteStore` (Phase 6.6R4 contract).
#:
#: - ``writable`` — default; the historical read-write open. This is
#:   the producer/batch/CLI profile (ingest, scoring, retention,
#:   backup): reads and writes, full PRAGMA profile with WAL.
#: - ``readonly`` — read-only connection (``mode=ro``). No writes of
#:   database content ever occur, but the reader still participates in
#:   live SQLite locking: if the artifact is a WAL-mode database the
#:   reader needs writable sidecar space (``-shm``/``-wal`` created in
#:   the database's directory) — an explicit, portable requirement.
#: - ``immutable_snapshot`` — read-only open of a *published snapshot*:
#:   SQLite is told the file will never change while this connection
#:   lives (``immutable=1``), so locking and WAL/shm state are skipped
#:   entirely and a clean DB-only artifact opens inside a strictly
#:   read-only directory/mount. The artifact **must not** be modified
#:   while an immutable-snapshot reader is open — that is the declared
#:   Phase 6.6 read-only deployment contract (ADR-013).
ACCESS_WRITABLE = "writable"
ACCESS_READONLY = "readonly"
ACCESS_IMMUTABLE = "immutable_snapshot"

#: All valid access modes, in declaration order (used by validation).
ACCESS_MODES: tuple[str, ...] = (
    ACCESS_WRITABLE,
    ACCESS_READONLY,
    ACCESS_IMMUTABLE,
)

#: Default PRAGMA settings applied to every connection opened by
#: :class:`SQLiteStore`. ``WAL`` lets readers proceed while a writer
#: holds the lock; ``synchronous=NORMAL`` is the recommended pairing for
#: WAL (full durability is preserved at checkpoint); ``busy_timeout``
#: is a small grace period for short concurrent transactions.
#:
#: Note (Phase 6.6R4): ``journal_mode`` is a header-write operation —
#: :class:`SQLiteStore` applies it in the default :data:`ACCESS_WRITABLE`
#: mode and *skips* it for the two read-only access modes.
_DEFAULT_PRAGMAS: dict[str, Any] = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "foreign_keys": "ON",
    "busy_timeout": 5000,
}


def _read_only_open_uri(resolved_path: str, *, immutable: bool) -> str:
    """Build the SQLite ``file:`` URI that opens ``resolved_path`` read-only.

    The path is percent-encoded so the URI keeps filesystem semantics for
    names containing URI-reserved characters. ``immutable`` appends the
    ``immutable=1`` flag (:data:`ACCESS_IMMUTABLE`).
    """
    encoded = _uri_quote(resolved_path, safe="/")
    uri = f"file:{encoded}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return uri


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

    Phase 6.3: instances of this class satisfy the
    :class:`~phase3.persistence.contracts.DatabaseStore` protocol, so
    repositories written against the contract run unchanged on both
    this store and :class:`~phase3.persistence.postgres.PostgresStore`.

    Parameters
    ----------
    db_path:
        Path to the database file. Created if it does not exist (in
        the default writable access mode — a read-only access mode
        never creates the file). The resolved basename is checked
        against :data:`FORBIDDEN_DB_NAME` before any file is opened.
    pragmas:
        Optional PRAGMA overrides. Keys not in this dict fall back to
        the module-level default. Pass an empty dict to keep the
        default profile unchanged. In a read-only access mode the
        ``journal_mode`` key is skipped entirely (it is a
        database-header write; see the deployment-semantics notes in
        the module docstring).
    access_mode:
        One of :data:`ACCESS_MODES` (Phase 6.6R4). Defaults to
        :data:`ACCESS_WRITABLE` — fully backward compatible. An
        unknown value raises :class:`ValueError` before any file is
        opened (validated at construction).

    Dialect notes (Phase 6.3)
    -------------------------
    * SQL crossing this seam uses ``%s`` placeholders. :meth:`execute`
      and :meth:`executemany` translate ``%s`` → ``?`` just before the
      driver call. ``%s`` is a reserved token: a literal ``%`` (e.g.
      in a ``LIKE`` pattern) must never appear in SQL passed through
      this seam.
    * Callers may still pass legacy ``?``-parametrised SQL — it
      contains no ``%s`` and passes through untouched.
    * Timestamps are application-supplied
      (:func:`~phase3.persistence.timeutil.utc_now_iso`); no runtime
      SQL in the Phase 3B seam relies on ``strftime`` anymore.
    """

    backend: str = "sqlite"

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        pragmas: dict[str, Any] | None = None,
        *,
        access_mode: str = ACCESS_WRITABLE,
    ) -> None:
        if access_mode not in ACCESS_MODES:
            raise ValueError(
                f"unknown SQLite access mode {access_mode!r}; "
                f"expected one of {list(ACCESS_MODES)}"
            )
        self._access_mode: str = access_mode
        self._resolved_path: str = _check_path(db_path)
        if access_mode == ACCESS_WRITABLE:
            # 2026-10-04 incident closure: fail-closed rehearsal DB-target
            # guard. A rehearsal/test process (FIE_SERVICE_ENV test|staging)
            # must never open a WRITABLE store that resolves to the live
            # Production intelligence store (canonical path / same-inode
            # match). This seam is the single funnel every Phase 3B write
            # path goes through (seed, persist, init-db, graph, migration),
            # so the guard fires BEFORE any DB write — including the
            # 2026-10-04 incident class (rehearsal harness fallback to the
            # production seed DB). See phase3/persistence/rehearsal_guard.py.
            from phase3.persistence.rehearsal_guard import assert_writable_target

            assert_writable_target(self._resolved_path, component="SQLiteStore")
        self._pragmas: dict[str, Any] = dict(_DEFAULT_PRAGMAS)
        if pragmas:
            self._pragmas.update(pragmas)
        if access_mode != ACCESS_WRITABLE:
            # journal_mode is a database-header WRITE: a read-only
            # open must not attempt it (and a WAL-header artifact must
            # stay exactly as the producer finalized it). The
            # artifact's header journal mode is left untouched.
            self._pragmas.pop("journal_mode", None)
        # ``isolation_level=None`` means we control transactions
        # explicitly (we want BEGIN IMMEDIATE, not the default
        # deferred). ``detect_types`` left at default — we serialize
        # datetimes as ISO strings ourselves.
        if access_mode == ACCESS_WRITABLE:
            self._conn: sqlite3.Connection = sqlite3.connect(
                self._resolved_path,
                isolation_level=None,
                timeout=30.0,
            )
        else:
            # Read-only deployment open (Phase 6.6R4): a ``file:`` URI
            # with mode=ro (+ immutable=1 for the published-snapshot
            # contract). sqlite3 never creates the file through this
            # URI, and for ``immutable=1`` SQLite skips locking and
            # WAL/shm sidecar state entirely.
            self._conn = sqlite3.connect(
                _read_only_open_uri(
                    self._resolved_path, immutable=(access_mode == ACCESS_IMMUTABLE)
                ),
                uri=True,
                isolation_level=None,
                timeout=30.0,
            )
        # Row factory gives us dict-like access; keeps repo code
        # readable.
        self._conn.row_factory = sqlite3.Row
        _apply_pragma(self._conn, self._pragmas)

    # ----- introspection ---------------------------------------------------

    @property
    def access_mode(self) -> str:
        """The access mode this store was opened with (post-validation)."""
        return self._access_mode

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

    def describe(self) -> dict[str, Any]:
        """Cheap diagnostics for logs/health checks (never raises)."""
        try:
            return {
                "backend": "sqlite",
                "path": self._resolved_path,
                "access_mode": self._access_mode,
                "journal_mode": str(self.pragma("journal_mode")),
            }
        except sqlite3.Error:  # pragma: no cover - diagnostics only
            return {
                "backend": "sqlite",
                "path": self._resolved_path,
                "access_mode": self._access_mode,
            }

    # ----- query / mutate --------------------------------------------------

    def execute(self, sql: str, params: tuple | dict | list | None = None) -> sqlite3.Cursor:
        """Run a single statement. Caller manages transactions.

        ``%s`` placeholders are translated to the sqlite driver's ``?``
        positional form. Named-parameter SQL (dict params) passes
        through unchanged.
        """
        if params is None:
            return self._conn.execute(sql)
        if isinstance(params, dict):
            return self._conn.execute(sql, params)
        return self._conn.execute(sql.replace("%s", "?"), params)

    def executemany(self, sql: str, seq: list[tuple] | list[dict]) -> sqlite3.Cursor:
        """Run a parameterised batch. Caller manages transactions.

        Positional sequences get the same ``%s`` → ``?`` translation
        as :meth:`execute`.
        """
        if seq and isinstance(seq[0], dict):
            return self._conn.executemany(sql, seq)  # type: ignore[arg-type]
        translated = sql.replace("%s", "?")
        return self._conn.executemany(
            translated, seq  # type: ignore[arg-type]
        )

    def executescript(self, sql: str) -> sqlite3.Cursor | None:
        """Run a multi-statement script (no parameters). DDL path.

        ``executescript`` implicitly COMMITs any open transaction,
        then runs the script in autocommit mode — the
        :meth:`transaction` context manager is aware of this quirk
        (it checks ``in_transaction`` before COMMIT), so DDL
        migrations work both inside and outside a managed
        transaction.
        """
        return self._conn.executescript(sql)

    def set_query_only(self) -> None:
        """Flip the connection to read-only (``PRAGMA query_only = 1``).

        Hard guarantee for read-only CLI paths: even a buggy future
        write is rejected by SQLite itself instead of silently
        mutating the database. Idempotent.
        """
        self._conn.execute("PRAGMA query_only = 1")

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
    "ACCESS_WRITABLE",
    "ACCESS_READONLY",
    "ACCESS_IMMUTABLE",
    "ACCESS_MODES",
    "SQLiteStore",
    "PathGuardError",
    "TransactionError",
    "quick_check",
    "integrity_check",
]
