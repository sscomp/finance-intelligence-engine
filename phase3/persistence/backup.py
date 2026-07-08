"""Backup and integrity helpers for Phase 3B SQLite stores.

The simplest correct path is ``sqlite3.Connection.backup`` (the
online backup API). It is safe to run while the source connection
is in use because it takes an internal SHARED lock and copies
page-by-page. The alternative ``VACUUM INTO`` rewrites the entire
file and requires an exclusive lock — we do not use it for the
default backup path because it serialises writes.

Both paths still flow through :func:`_check_path` so a backup
destination that resolves to ``macro_history.db`` is rejected
before the copy starts.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from typing import Callable

from phase3.persistence.sqlite import (
    FORBIDDEN_DB_NAME,
    SQLiteStore,
    PathGuardError,
    _check_path,
    integrity_check as _integrity_check,
    quick_check as _quick_check,
)


def backup_db(
    src: str | os.PathLike[str],
    dest: str | os.PathLike[str],
) -> int:
    """Copy ``src`` to ``dest`` using the SQLite online backup API.

    Returns the number of bytes written to ``dest``.

    Raises :class:`PathGuardError` if either path resolves to
    :data:`~phase3.persistence.sqlite.FORBIDDEN_DB_NAME`.
    """
    src_resolved = _check_path(src)
    dest_resolved = _check_path(dest)
    if os.path.abspath(src_resolved) == os.path.abspath(dest_resolved):
        raise ValueError("backup_db: src and dest resolve to the same path")
    # Ensure parent directory exists for the destination.
    os.makedirs(os.path.dirname(dest_resolved) or ".", exist_ok=True)
    src_conn = sqlite3.connect(src_resolved)
    try:
        dest_conn = sqlite3.connect(dest_resolved)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    return os.path.getsize(dest_resolved)


def restore_db(
    src: str | os.PathLike[str],
    dest: str | os.PathLike[str],
) -> int:
    """Copy ``src`` to ``dest`` (same engine as :func:`backup_db`).

    The function name matches user expectation that "restore" is
    the inverse of "backup". Internally both are just a sqlite
    backup, so we delegate.
    """
    return backup_db(src, dest)


def integrity_check(db_path: str | os.PathLike[str]) -> str:
    """Run ``PRAGMA integrity_check`` on ``db_path``.

    This is a thin re-export of
    :func:`~phase3.persistence.sqlite.integrity_check` placed in
    the backup module because that is where Phase 3B tests expect
    to find it. The check is what we use to verify a backup was
    complete.
    """
    return _integrity_check(db_path)


def quick_check(db_path: str | os.PathLike[str]) -> str:
    """Run ``PRAGMA quick_check`` on ``db_path`` (cheap)."""
    return _quick_check(db_path)


__all__ = ["backup_db", "restore_db", "integrity_check", "quick_check"]
