"""Schema version registry and migration runner.

Design
------
* A :class:`Migration` is a named, ordered, forward-only SQL change.
  The class records the SQL text and a stable ``checksum`` that the
  manager verifies before applying — protects against a careless edit
  silently changing already-applied migrations.
* :class:`MigrationManager` keeps a single ``schema_migrations`` table
  tracking which migrations have run. The table itself is created by
  :meth:`ensure_registry` so :meth:`apply` is safe on a fresh DB.
* :meth:`apply` is idempotent: re-running on an already-applied
  version is a no-op (returns the prior ``applied_at`` timestamp).
* If a migration raises, the manager rolls the transaction back and
  records the error in ``schema_migrations.error``. The next ``apply``
  call will retry the failed migration (not skip it).
* The manager is intentionally not generic — it knows about a list of
  migration modules passed in at construction. This keeps the wire
  shape explicit and the test surface small.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from phase3.persistence.sqlite import SQLiteStore, TransactionError
from phase3.persistence.timeutil import utc_now_iso


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied."""


# ---------------------------------------------------------------------------
# Migration dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    """A single forward-only schema change.

    Attributes
    ----------
    version:
        Integer version number. Must be unique within a registry and
        strictly increasing in apply order. The manager will refuse to
        apply a version it has already seen with a different checksum.
    name:
        Short identifier (e.g. ``"initial_schema"``). Stored in
        ``schema_migrations.name`` for human inspection.
    sql:
        The forward SQL — typically a single ``CREATE TABLE`` /
        ``CREATE INDEX`` / ``CREATE TRIGGER`` batch. Multiple
        statements separated by ``;`` are fine; the manager passes the
        string to ``executescript`` inside the migration's transaction.
    checksum:
        Optional pre-computed SHA-256 of the SQL. If omitted, the
        manager computes one at construction. Stored at apply time and
        re-verified on every subsequent apply.
    """

    version: int
    name: str
    sql: str
    checksum: str = field(default="")

    def __post_init__(self) -> None:
        if not self.sql.strip():
            raise ValueError("Migration.sql must be non-empty")
        if self.version <= 0:
            raise ValueError(f"Migration.version must be positive, got {self.version}")
        if not self.checksum:
            # Compute once, store as the canonical checksum.
            object.__setattr__(self, "checksum", _sha256(self.sql))

    def fingerprint(self) -> str:
        """Return the canonical ``<version>:<name>:<checksum>`` triple.

        Used to detect a tampered migration: if a migration that was
        already applied is later modified, the fingerprint changes and
        the manager raises :class:`MigrationError` rather than
        silently re-applying.
        """
        return f"{self.version}:{self.name}:{self.checksum}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


_REGISTRY_DDL: str = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    checksum    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    error       TEXT
);
"""


class MigrationManager:
    """Apply a fixed list of :class:`Migration` objects idempotently.

    Parameters
    ----------
    store:
        Open :class:`SQLiteStore`. The manager does not own its
        lifecycle.
    migrations:
        Iterable of :class:`Migration`. They are sorted by ``version``
        and applied in order. The order is fixed at construction so a
        later caller cannot accidentally shuffle the sequence.
    """

    def __init__(
        self,
        store: SQLiteStore,
        migrations: Iterable[Migration],
    ) -> None:
        self._store = store
        self._migrations: tuple[Migration, ...] = tuple(
            sorted(migrations, key=lambda m: m.version)
        )
        # Sanity: version uniqueness.
        versions = [m.version for m in self._migrations]
        if len(versions) != len(set(versions)):
            seen: set[int] = set()
            dupes = [v for v in versions if v in seen or seen.add(v)]  # type: ignore[func-returns-value]
            raise ValueError(f"duplicate migration versions: {dupes}")

    # ----- introspection ---------------------------------------------------

    @property
    def migrations(self) -> tuple[Migration, ...]:
        """The registered migrations, in apply order."""
        return self._migrations

    def applied_versions(self) -> list[int]:
        """Versions present in ``schema_migrations`` with no error."""
        rows = self._store.execute(
            "SELECT version FROM schema_migrations WHERE error IS NULL ORDER BY version"
        ).fetchall()
        return [int(r[0]) for r in rows]

    def current_version(self) -> int:
        """The highest successfully applied version (0 if none)."""
        rows = self._store.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations WHERE error IS NULL"
        ).fetchone()
        return int(rows[0]) if rows is not None else 0

    def failed_versions(self) -> list[int]:
        """Versions that errored on last apply and would be retried."""
        rows = self._store.execute(
            "SELECT version FROM schema_migrations WHERE error IS NOT NULL ORDER BY version"
        ).fetchall()
        return [int(r[0]) for r in rows]

    # ----- ensure registry -------------------------------------------------

    def ensure_registry(self) -> None:
        """Create the ``schema_migrations`` table if missing.

        Idempotent. Called automatically by :meth:`apply` so callers
        rarely need this directly.
        """
        with self._store.transaction():
            self._store.execute(_REGISTRY_DDL)

    # ----- apply -----------------------------------------------------------

    def apply(self) -> list[Migration]:
        """Apply any pending or failed migrations.

        Returns the list of migrations that were newly applied in
        this call. Re-running an already-applied version whose stored
        checksum matches the registered one is a no-op. A version
        whose stored checksum differs raises
        :class:`MigrationError` (caller must fix the file or wipe the
        row manually).
        """
        self.ensure_registry()
        applied_now: list[Migration] = []

        for mig in self._migrations:
            existing = self._fetch_row(mig.version)
            if existing is not None and existing["error"] is None:
                # Already applied — verify checksum.
                if existing["checksum"] != mig.checksum:
                    raise MigrationError(
                        f"migration {mig.version} ({mig.name!r}) was already "
                        f"applied with checksum {existing['checksum']}, but the "
                        f"registered checksum is {mig.checksum}. The migration "
                        f"file was modified after apply. Revert the file or drop "
                        f"the schema_migrations row to retry."
                    )
                continue

            # Apply (or retry). The whole migration runs inside one
            # transaction so a partial failure cannot leave half a
            # schema behind.
            try:
                with self._store.transaction():
                    self._store.executescript(mig.sql)
                    # Upsert the registry row so a previous failed
                    # attempt (error != NULL) is overwritten on
                    # success. INSERT ... ON CONFLICT ... DO UPDATE is
                    # portable across both backends (INSERT OR REPLACE
                    # is SQLite-only dialect) — Phase 6.3.
                    self._store.execute(
                        """
                        INSERT INTO schema_migrations
                            (version, name, checksum, applied_at, error)
                        VALUES (%s, %s, %s, %s, NULL)
                        ON CONFLICT(version) DO UPDATE SET
                            name       = excluded.name,
                            checksum   = excluded.checksum,
                            applied_at = excluded.applied_at,
                            error      = NULL
                        """,
                        (mig.version, mig.name, mig.checksum, utc_now_iso()),
                    )
            except (sqlite3.Error, TransactionError) as exc:
                # Record the failure so a future apply() can retry.
                # We open a *new* transaction — the failed one is
                # already rolled back by the context manager.
                self._record_failure(mig, repr(exc))
                raise MigrationError(
                    f"migration {mig.version} ({mig.name!r}) failed: {exc}"
                ) from exc

            applied_now.append(mig)

        return applied_now

    # ----- internals -------------------------------------------------------

    def _fetch_row(self, version: int) -> sqlite3.Row | None:
        cur = self._store.execute(
            "SELECT version, name, checksum, applied_at, error "
            "FROM schema_migrations WHERE version = %s",
            (version,),
        )
        return cur.fetchone()

    def _record_failure(self, mig: Migration, error_repr: str) -> None:
        """Best-effort: stamp the error on the registry row.

        We do not raise from this helper; it runs *after* the original
        exception, so swallowing avoids masking the real error.
        """
        try:
            with self._store.transaction():
                self._store.execute(
                    """
                    INSERT INTO schema_migrations
                        (version, name, checksum, applied_at, error)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(version) DO UPDATE SET
                        name       = excluded.name,
                        checksum   = excluded.checksum,
                        applied_at = excluded.applied_at,
                        error      = excluded.error
                    """,
                    (mig.version, mig.name, mig.checksum, utc_now_iso(), error_repr),
                )
        except sqlite3.Error:
            # If we cannot even write the failure, the registry is
            # probably corrupt — the caller will see the original
            # MigrationError anyway.
            pass


__all__ = ["Migration", "MigrationError", "MigrationManager"]
