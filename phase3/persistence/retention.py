"""Explicit retention helpers for Phase 3B.

There is no automatic retention. Callers explicitly request a purge
of old data. This is by design: a runaway cron job that silently
prunes data is a worse failure mode than a database that grows
slowly and gets noticed in a monthly report.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from phase3.persistence.sqlite import SQLiteStore


# ---------------------------------------------------------------------------
# Signal retention
# ---------------------------------------------------------------------------


def purge_signals_before(
    store: SQLiteStore,
    cutoff: str | datetime,
) -> int:
    """Delete signal_log rows whose ``ingested_at`` is older than ``cutoff``.

    Returns the number of rows deleted. Caller is responsible for
    choosing a sensible cutoff. This helper will *not* cascade —
    signal_log has no foreign keys to it, so a hard delete is safe.
    """
    cutoff_iso = _iso(cutoff)
    with store.transaction():
        cur = store.execute(
            "DELETE FROM signal_log WHERE ingested_at < ?", (cutoff_iso,)
        )
        return int(cur.rowcount or 0)


def purge_signals_by_id(store: SQLiteStore, ids: Iterable[str]) -> int:
    """Hard-delete specific signals by id. Returns the row count."""
    id_list = list(ids)
    if not id_list:
        return 0
    placeholders = ",".join("?" for _ in id_list)
    with store.transaction():
        cur = store.execute(
            f"DELETE FROM signal_log WHERE signal_id IN ({placeholders})",
            tuple(id_list),
        )
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Score retention — note the append-only policy
# ---------------------------------------------------------------------------
#
# score_snapshot is append-only; we do NOT provide a delete helper
# here. The trigger from schema_v1 enforces that contract at the
# storage layer. If a caller truly needs to drop score history they
# must drop the table and rerun migrations.


# ---------------------------------------------------------------------------
# Graph retention
# ---------------------------------------------------------------------------


def purge_graph_edges_for_node(store: SQLiteStore, node_id: str) -> int:
    """Delete every edge referencing ``node_id`` (both directions).

    The node itself is left intact. Returns the number of edges
    deleted.
    """
    with store.transaction():
        cur = store.execute(
            "DELETE FROM graph_edges WHERE from_node_id = ? OR to_node_id = ?",
            (node_id, node_id),
        )
        return int(cur.rowcount or 0)


def purge_graph_node(store: SQLiteStore, node_id: str) -> tuple[int, int]:
    """Delete ``node_id`` plus any edges referencing it.

    Returns ``(edges_deleted, node_deleted)`` where ``node_deleted``
    is 0 or 1.
    """
    with store.transaction():
        edge_cur = store.execute(
            "DELETE FROM graph_edges WHERE from_node_id = ? OR to_node_id = ?",
            (node_id, node_id),
        )
        node_cur = store.execute(
            "DELETE FROM graph_nodes WHERE node_id = ?", (node_id,)
        )
        return (int(edge_cur.rowcount or 0), int(node_cur.rowcount or 0))


def purge_ingestion_errors_before(
    store: SQLiteStore,
    cutoff: str | datetime,
) -> int:
    """Delete ingestion_errors rows older than ``cutoff``."""
    cutoff_iso = _iso(cutoff)
    with store.transaction():
        cur = store.execute(
            "DELETE FROM ingestion_errors WHERE occurred_at < ?", (cutoff_iso,)
        )
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _iso(value: str | datetime) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = [
    "purge_signals_before",
    "purge_signals_by_id",
    "purge_graph_edges_for_node",
    "purge_graph_node",
    "purge_ingestion_errors_before",
]
