"""Persistence contract layer (Phase 6.3).

Structural types declaring what the repositories, migration manager,
and retention helpers require from a backend store. Two concrete
backends satisfy these Protocols:

* :class:`~phase3.persistence.sqlite.SQLiteStore` — stdlib ``sqlite3``,
  the local/dev/test fallback (SQLite is retained per the Phase 6.2
  architecture decision).
* :class:`~phase3.persistence.postgres.PostgresStore` — ``psycopg3``
  against a disposable/synthetic PostgreSQL instance (Phase 6.2 ADR
  ADR-003: PostgreSQL is a *disposable parity* backend for Phase 6.3,
  not a production target yet).

Dialect rule
------------
Every SQL string crossing this seam uses ``%s`` placeholders and
positional parameter tuples only — never named ``:param`` placeholders
and never backend functions (``strftime``, ``now()``, ...). Timestamps
are supplied by the application via
:func:`~phase3.persistence.timeutil.utc_now_iso`.

``%s`` is therefore a *reserved* token in the SQL surface: a literal
``%s`` (or any percent sign) inside a string literal will be
misinterpreted by the SQLite translation shim. A grep for ``%`` in
the persistence SQL surface is part of the Phase 6.3 report evidence.

The Protocols here are runtime-inert (structural typing only); no
backend imports psycopg at module import time — the PostgreSQL
dependency stays optional (``pip install 'finance-intelligence-engine[postgres]'``).
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Iterator, Protocol, Sequence, runtime_checkable

__all__ = [
    "DatabaseStore",
    "SignalRepositoryProtocol",
    "ScoreRepositoryProtocol",
    "GraphRepositoryProtocol",
]


@runtime_checkable
class DatabaseStore(Protocol):
    """Minimal connection-manager surface required by every repo.

    Both :class:`SQLiteStore` and :class:`PostgresStore` satisfy this.

    Notes
    -----
    * ``backend``: ``"sqlite"`` or ``"postgres"`` — used by factories
      and diagnostics, never for dialect branching inside repo SQL.
    * ``execute(sql, params)``: one statement; ``params`` is a
      positional tuple (or ``None``). All SQL crossing the seam uses
      ``%s`` placeholders.
    * ``executescript(sql)``: run a multi-statement script with no
      parameters (DDL). On SQLite this is ``executescript``; on
      PostgreSQL the driver's simple-query protocol plays the same
      role. Migrations go through here.
    * ``transaction()``: context manager wrapping the body in an
      explicit transaction (``BEGIN IMMEDIATE`` on SQLite,
      ``BEGIN`` on PostgreSQL), rolling back on error.
    * ``set_query_only()``: flip the connection to a hard read-only
      mode (SQLite ``PRAGMA query_only``; PostgreSQL
      ``default_transaction_read_only``). Used by read-only CLI paths
      so accidental writes fail closed.
    * ``describe()``: cheap diagnostics for logs/health checks.
    """

    backend: str

    def execute(
        self, sql: str, params: tuple | list | None = None
    ) -> Any: ...

    def executemany(self, sql: str, seq: Sequence[tuple]) -> Any: ...

    def executescript(self, sql: str) -> Any: ...

    def transaction(self) -> AbstractContextManager[Any]: ...

    def set_query_only(self) -> None: ...

    def close(self) -> None: ...

    def describe(self) -> dict[str, Any]: ...


@runtime_checkable
class SignalRepositoryProtocol(Protocol):
    """Surface of :class:`~phase3.persistence.signal_repo.SignalRepository`."""

    def upsert(self, record: Any) -> bool: ...

    def upsert_many(self, records: Any) -> int: ...

    def get(self, signal_id: str) -> Any: ...

    def query(self, **filters: Any) -> list[Any]: ...

    def count(self) -> int: ...


@runtime_checkable
class ScoreRepositoryProtocol(Protocol):
    """Surface of :class:`~phase3.persistence.score_repo.ScoreRepository`."""

    def append(self, record: Any) -> int: ...

    def latest(self, scorer: str, entity_type: str, entity_id: str) -> Any: ...

    def history(self, **filters: Any) -> list[Any]: ...

    def count(self) -> int: ...


@runtime_checkable
class GraphRepositoryProtocol(Protocol):
    """Surface of :class:`~phase3.persistence.graph_repo.GraphRepository`."""

    def upsert_node(self, node: Any) -> bool: ...

    def upsert_edge(self, edge: Any) -> bool: ...

    def get_node(self, node_id: str) -> Any: ...

    def get_edge(self, edge_id: str) -> Any: ...

    def list_nodes(self, **filters: Any) -> list[Any]: ...

    def list_edges(self, **filters: Any) -> list[Any]: ...

    def node_count(self) -> int: ...

    def edge_count(self) -> int: ...

    def fetch_edges_by_nodes(
        self,
        node_ids: Sequence[str],
        *,
        direction: str = "both",
        edge_types: Sequence[str] | None = None,
    ) -> list[Any]: ...

    def fetch_nodes_by_ids(self, node_ids: Sequence[str]) -> list[Any]: ...

    def clear(self) -> None: ...