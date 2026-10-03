"""Backend-neutral UTC timestamp helpers for the persistence layer.

Why here
--------
The Phase 3B schema and repos previously relied on
``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` — a SQLite-only dialect
construct — to stamp ``ingested_at`` / ``computed_at`` /
``created_at`` / ``applied_at`` rows. Phase 6.3 moves *all* runtime
timestamp decisions to the application layer so the same Python code
can target SQLite and PostgreSQL without any backend-specific SQL.

The produced format is **byte-identical** to the old SQLite
``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` output (UTC, ISO-8601,
exactly 3 fractional digits, ``Z`` suffix), so existing SQLite rows
and rows written after this change sort and compare identically —
verified by the Phase 6.3 contract tests.
"""
from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utc_now_iso"]


def utc_now_iso() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SS.mmmZ``.

    Matches the historical SQLite ``strftime('%Y-%m-%dT%H:%M:%fZ','now')``
    format exactly: 3-digit millisecond precision, uppercase ``Z``.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S") + f".{now.microsecond // 1000:03d}Z"