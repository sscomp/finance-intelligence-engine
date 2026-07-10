"""Tests for phase3.persistence.sqlite — path guard, PRAGMA, transaction.

These cover the Phase 3B Task 1 acceptance contract for the SQLite
foundation:

* The path guard rejects ``macro_history.db`` (basename match) and
  symlink / ``..`` traversal attempts.
* The default PRAGMA profile is applied.
* ``transaction()`` commits on success, rolls back on exception, and
  survives an inner ``executescript()`` (the migration case that
  triggered the original bug — see migrations.py for the
  ExecuteScriptBreaksCustomTransaction regression test).
* ``transaction()`` is robust to ``executescript()``-induced
  autocommit: it does not raise
  ``"cannot commit - no transaction is active"`` afterwards.
* quick_check / integrity_check return ``"ok"`` for a healthy DB.

All tests use a temp directory; no production paths are touched.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from phase3.persistence.sqlite import (
    FORBIDDEN_DB_NAME,
    PathGuardError,
    SQLiteStore,
    TransactionError,
    _check_path,
    integrity_check,
    quick_check,
)


class PathGuardTests(unittest.TestCase):
    """The path guard is the only tripwire against prod-DB access."""

    def test_basename_match_is_rejected(self) -> None:
        with self.assertRaises(PathGuardError):
            SQLiteStore("macro_history.db")

    def test_absolute_basename_match_is_rejected(self) -> None:
        with self.assertRaises(PathGuardError):
            SQLiteStore("/some/path/macro_history.db")

    def test_parent_traversal_is_rejected(self) -> None:
        # A relative path that traverses into a folder whose basename
        # is ``macro_history.db`` must still be rejected.
        with self.assertRaises(PathGuardError):
            SQLiteStore("phase3/data/../macro_history.db")

    def test_symlink_to_forbidden_db_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            forbidden = os.path.join(td, FORBIDDEN_DB_NAME)
            # Create the forbidden file so symlink resolves to it.
            open(forbidden, "wb").close()
            link = os.path.join(td, "link_to_forbidden.db")
            os.symlink(forbidden, link)
            with self.assertRaises(PathGuardError):
                SQLiteStore(link)

    def test_empty_path_is_rejected(self) -> None:
        with self.assertRaises(PathGuardError):
            SQLiteStore("")

    def test_safe_path_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            safe = os.path.join(td, "intelligence.db")
            with SQLiteStore(safe) as store:
                self.assertEqual(store.path, os.path.realpath(safe))

    def test_check_path_helper_returns_realpath(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            safe = os.path.join(td, "x.db")
            resolved = _check_path(safe)
            self.assertEqual(resolved, os.path.realpath(safe))

    def test_check_path_helper_rejects_forbidden(self) -> None:
        with self.assertRaises(PathGuardError):
            _check_path("macro_history.db")

    def test_uppercase_basename_match_is_rejected(self) -> None:
        # F2: a case-bypass (``Macro_History.db``) must still trip the
        # guard. On a case-sensitive filesystem the bypass was previously
        # silent because the basename comparison was case-sensitive.
        with self.assertRaises(PathGuardError):
            SQLiteStore("Macro_History.db")

    def test_check_path_helper_rejects_uppercase_forbidden(self) -> None:
        with self.assertRaises(PathGuardError):
            _check_path("MACRO_HISTORY.DB")

    def test_case_insensitive_safe_path_still_accepted(self) -> None:
        # Sanity check: a different-cased SAFE name (e.g. ``Macro.db``)
        # is not the production DB and must be accepted.
        with tempfile.TemporaryDirectory() as td:
            safe = os.path.join(td, "IntelliGence.db")
            with SQLiteStore(safe) as store:
                self.assertEqual(store.path, os.path.realpath(safe))


class PragmaProfileTests(unittest.TestCase):
    """The default PRAGMA profile is applied to every store."""

    def test_journal_mode_is_wal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            with SQLiteStore(db) as store:
                self.assertEqual(store.pragma("journal_mode").lower(), "wal")

    def test_synchronous_is_normal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            with SQLiteStore(db) as store:
                # SQLite reports this as integer 1 (NORMAL).
                self.assertEqual(store.pragma("synchronous"), 1)

    def test_foreign_keys_on(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            with SQLiteStore(db) as store:
                self.assertEqual(store.pragma("foreign_keys"), 1)

    def test_pragma_override_takes_effect(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            # We can't override synchronous once WAL is on (SQLite
            # silently keeps it at NORMAL) — instead override
            # busy_timeout, which is honoured regardless of mode.
            with SQLiteStore(db, pragmas={"busy_timeout": 1234}) as store:
                self.assertEqual(store.pragma("busy_timeout"), 1234)


class TransactionTests(unittest.TestCase):
    """``transaction()`` commits on success, rolls back on exception."""

    def test_commit_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            with SQLiteStore(db) as store:
                with store.transaction():
                    store.execute(
                        "CREATE TABLE t1 (id INTEGER PRIMARY KEY, v INTEGER)"
                    )
                    store.execute("INSERT INTO t1(v) VALUES (1)")
                # Visible after commit.
                self.assertEqual(
                    store.execute("SELECT COUNT(*) FROM t1").fetchone()[0],
                    1,
                )

    def test_rollback_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            with SQLiteStore(db) as store:
                store.execute(
                    "CREATE TABLE t1 (id INTEGER PRIMARY KEY, v INTEGER)"
                )
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.execute("INSERT INTO t1(v) VALUES (1)")
                        raise RuntimeError("boom")
                # The insert was rolled back.
                self.assertEqual(
                    store.execute("SELECT COUNT(*) FROM t1").fetchone()[0],
                    0,
                )

    def test_inner_raises_sqlite_error_also_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            with SQLiteStore(db) as store:
                store.execute(
                    "CREATE TABLE t1 (id INTEGER PRIMARY KEY, v INTEGER)"
                )
                try:
                    with store.transaction():
                        store.execute("INSERT INTO t1(v) VALUES (1)")
                        # Force a sqlite error (unique violation on rowid 1).
                        store.execute(
                            "INSERT INTO t1(id, v) VALUES (1, 2)"
                        )
                except sqlite3.IntegrityError:
                    pass
                self.assertEqual(
                    store.execute("SELECT COUNT(*) FROM t1").fetchone()[0],
                    0,
                )

    def test_executescript_does_not_break_commit(self) -> None:
        """The original Phase 3B bug: ``executescript`` implicitly
        COMMITs the open transaction, then the context manager tries
        to COMMIT again and gets
        ``"cannot commit - no transaction is active"``.

        The fix: ``transaction()`` checks ``conn.in_transaction``
        before issuing COMMIT/ROLLBACK. The body still runs the
        migration SQL via executescript; the schema changes land; the
        final COMMIT is skipped because the connection is no longer
        in a transaction.
        """
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            with SQLiteStore(db) as store:
                with store.transaction():
                    store.connection.executescript(
                        "CREATE TABLE m (id INTEGER PRIMARY KEY);"
                        "INSERT INTO m(id) VALUES (10), (20);"
                    )
                # Schema is in place and rows persisted.
                self.assertEqual(
                    store.execute("SELECT COUNT(*) FROM m").fetchone()[0],
                    2,
                )

    def test_executescript_followed_by_normal_statement_works(self) -> None:
        """After executescript closes the implicit transaction, a
        subsequent normal statement inside the same ``with`` block
        implicitly reopens an autocommit transaction. The
        context-manager's final COMMIT must not fire.
        """
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            with SQLiteStore(db) as store:
                with store.transaction():
                    store.connection.executescript(
                        "CREATE TABLE m (id INTEGER PRIMARY KEY);"
                    )
                    # Normal statement — autocommit, not our BEGIN IMMEDIATE.
                    store.execute("INSERT INTO m(id) VALUES (1)")
                self.assertEqual(
                    store.execute("SELECT COUNT(*) FROM m").fetchone()[0],
                    1,
                )

    def test_failed_executescript_does_not_leak_lock(self) -> None:
        """An exception inside an executescript-using body must leave
        the connection usable for the next transaction. We test this
        by running a subsequent transaction after the failure.
        """
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "t.db")
            with SQLiteStore(db) as store:
                with self.assertRaises(sqlite3.OperationalError):
                    with store.transaction():
                        # Invalid SQL → OperationalError.
                        store.connection.executescript(
                            "THIS IS NOT VALID SQL;"
                        )
                # Connection is still healthy — next txn commits cleanly.
                with store.transaction():
                    store.connection.executescript(
                        "CREATE TABLE n (id INTEGER PRIMARY KEY);"
                    )
                self.assertEqual(
                    store.execute("SELECT COUNT(*) FROM n").fetchone()[0],
                    0,
                )


class IntegrityHelperTests(unittest.TestCase):
    def test_quick_check_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "i.db")
            with SQLiteStore(db) as store:
                store.execute("CREATE TABLE x (id INTEGER)")
            self.assertEqual(quick_check(db), "ok")

    def test_integrity_check_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "i.db")
            with SQLiteStore(db) as store:
                store.execute("CREATE TABLE x (id INTEGER)")
            self.assertEqual(integrity_check(db), "ok")

    def test_quick_check_rejects_forbidden_path(self) -> None:
        with self.assertRaises(PathGuardError):
            quick_check("macro_history.db")

    def test_integrity_check_rejects_forbidden_path(self) -> None:
        with self.assertRaises(PathGuardError):
            integrity_check("/tmp/macro_history.db")


if __name__ == "__main__":
    unittest.main()
