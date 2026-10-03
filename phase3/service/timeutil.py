"""App-layer UTC stamping for the service boundary (Phase 6.5).

Mirrors :mod:`phase3.persistence.timeutil` semantics (UTC, ISO,
3 fractional digits, ``Z`` suffix) so service envelope timestamps
are byte-compatible with persistence-layer stamps. Kept as a
separate module so the service never imports persistence internals
beyond the documented seam.
"""
from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utc_now_iso", "today_utc"]


def utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return (
        now.strftime("%Y-%m-%dT%H:%M:%S")
        + f".{now.microsecond // 1000:03d}Z"
    )


def today_utc() -> str:
    """Today's ISO date (UTC) — the default ``as_of`` for freshness reads."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")