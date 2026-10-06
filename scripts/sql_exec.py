#!/usr/bin/env python3
"""Canonical portable SQL execution boundary (6.9A-R4-R4-R1 Task 1).

Repo validation used to shell out to host ``psql`` for SQL-only needs
(create/drop test objects, query values, assert zero-write invariants,
fixture preparation). A fresh Codex Cloud job has NO host psql and the
pinned portable PostgreSQL artifact ships only the server binaries
(initdb/pg_ctl/postgres) — the R4-R4 mandatory focused acceptance failed
with ``FileNotFoundError: 'psql'`` during setUpClass.

Contract (WO §5):

  * ONE canonical execution boundary for SQL used only to prepare/
    assert repository validation state: the interpreter's PostgreSQL
    driver, psycopg (already a pyproject-declared dependency and the
    same client the canonical provisioner's readiness check requires).
  * The target is ALWAYS an explicit DSN argument. There is NO
    environment fallback, NO provider default, NO Production DSN
    selection: a missing/ambiguous DSN argument fails closed here,
    before any connection attempt.
  * Deterministic error semantics: every failed statement raises
    :class:`SqlExecError` carrying the original server message; a
    missing driver raises :class:`SqlClientUnavailable` naming the
    contract (never a bare ImportError).
  * UTF-8 is preserved end-to-end (psycopg text I/O is UTF-8; the
    hermetic provisioner pins server_encoding=UTF8).
  * No secrets are ever emitted: DSNs printed in diagnostics are
    redacted of their password component; passwords are passed out of
    band as a function argument, never logged.
  * A CLI client (host psql) is deliberately NOT part of this
    contract. Host psql remains exclusively for modules whose subject
    IS host-tool behavior (e.g. phase3/service/pg_runtime_preflight.py),
    and never for hermetic validation.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

__all__ = [
    "SqlClientUnavailable", "SqlExecError", "redact_dsn",
    "resolve_client", "connect", "execute_statements", "fetch_rows",
    "query_scalar",
]

_DRIVER = "psycopg"


class SqlClientUnavailable(RuntimeError):
    """The contract-declared portable SQL client is not importable."""


class SqlExecError(RuntimeError):
    """A statement failed against the explicitly-addressed target."""


_PASSWORD_RE = re.compile(r"(password=)[^&\s]*", re.IGNORECASE)


def redact_dsn(dsn: str) -> str:
    """Return the DSN with its password component masked (diagnostics
    only; the value is never needed anywhere)."""
    return _PASSWORD_RE.sub(r"\1***", dsn or "")


def resolve_client() -> str:
    """Import and return the canonical SQL client module name.

    Fail closed with a precise contract error — never a generic
    FileNotFoundError-style resolution failure, never a silent skip.
    """
    try:
        __import__(_DRIVER)
    except ImportError as exc:
        raise SqlClientUnavailable(
            "POSTGRESQL_SQL_CLIENT_UNAVAILABLE the repository's canonical "
            f"portable SQL client ({_DRIVER}) is not importable in this "
            "interpreter; hermetic validation cannot shell out to a host "
            "psql by contract. Run scripts/bootstrap.sh --all first "
            f"(underlying import error: {exc})") from exc
    return _DRIVER


def connect(dsn: str, *, password: str = None, autocommit: bool = True):
    """Open a psycopg connection to the EXPLICIT dsn.

    The password, when the DSN does not embed one, is passed as a
    connection keyword out of band. No other argument source exists:
    this function reads no environment variables for target selection.
    """
    if not dsn or not isinstance(dsn, str) or not dsn.startswith(
            ("postgres://", "postgresql://")):
        # Fail closed on unparseable/ambiguous provenance BEFORE dialing:
        # there is deliberately no default DSN and no environment lookup
        # to fall back to.
        raise SqlExecError(
            "POSTGRESQL_SQL_TARGET_UNRESOLVED no explicit PostgreSQL DSN "
            f"argument was provided (got: {redact_dsn(str(dsn))!r}); the "
            "canonical SQL client never selects a target from the "
            "environment and never falls back to Production")
    psycopg = __import__(_DRIVER)
    try:
        conn = psycopg.connect(dsn, password=password,
                               autocommit=autocommit)
    except Exception as exc:
        raise SqlExecError(
            "POSTGRESQL_SQL_CONNECT_FAILED to the explicit target "
            f"{redact_dsn(dsn)}: "
            f"{type(exc).__name__}: {exc}") from exc
    return conn


def execute_statements(dsn: str, statements: Sequence[str], *,
                       password: str = None) -> None:
    """Execute each statement in its own implicit transaction (autocommit
    connection), so DDL such as CREATE ROLE/CREATE DATABASE is legal.

    Raises :class:`SqlExecError` carrying the original server message on
    the first failure; earlier statements are already applied (documented
    deterministic semantics for fixture preparation, which the fixture
    teardown reclaims by ownership).
    """
    if not isinstance(statements, (list, tuple)) or not statements:
        # A single un-split SQL script is accepted as one statement only
        # when the caller is explicit; otherwise refuse (no hidden split).
        statements = list(statements) if isinstance(statements, Iterable) \
            else None
        if not statements:
            raise SqlExecError(
                "POSTGRESQL_SQL_NO_STATEMENTS empty or non-sequence "
                "statement argument")
    conn = connect(dsn, password=password, autocommit=True)
    try:
        for sql in statements:
            try:
                conn.execute(sql)
            except Exception as exc:
                raise SqlExecError(
                    "POSTGRESQL_SQL_EXEC_FAILED statement rejected by the "
                    f"target: {type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()


def fetch_rows(dsn: str, sql: str, *, password: str = None) -> list:
    """Run one SELECT statement; return rows as a list of tuples
    (server-side values, UTF-8 text preserved)."""
    conn = connect(dsn, password=password)
    try:
        try:
            cur = conn.execute(sql)
            rows = cur.fetchall()
        except Exception as exc:
            raise SqlExecError(
                "POSTGRESQL_SQL_QUERY_FAILED query rejected by the "
                f"target: {type(exc).__name__}: {exc}") from exc
        return rows
    finally:
        conn.close()


def query_scalar(dsn: str, sql: str, *, password: str = None) -> Any:
    """Run one single-row/single-column SELECT; return the scalar value
    (None when the result set is empty)."""
    rows = fetch_rows(dsn, sql, password=password)
    if not rows:
        return None
    return rows[0][0]