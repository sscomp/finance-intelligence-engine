"""Service-boundary freshness semantics (Phase 6.5 §8).

Wraps the *existing* governance freshness policy
(:mod:`phase3.freshness`, itself unchanged) and exposes the Phase 6.5
service vocabulary:

=============== =================== ============================
service state   governance state    client meaning
=============== =================== ============================
FRESH           FRESH               current known data
DEGRADED        STALE               inside warning window
STALE           EXPIRED             past hard freshness window
UNAVAILABLE     UNAVAILABLE / none  no observation at all
=============== =================== ============================

The mapping is deliberate: the governance ``STALE`` window (data
usable, upstream aging) is more honest at the ChatGPT boundary as
*DEGRADED*, reserving ``STALE`` for data past its hard-expiry —
never silently converted into a current-data claim.

Pure mapping + a single repo call to locate the latest observation
timestamp for an entity. No network fetches happen here, ever
(work order §8).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from phase3.freshness import (
    DATA_CLASS_COMPANY,
    DATA_CLASS_INDUSTRY,
    DATA_CLASS_MACRO,
    classify_freshness,
)
from phase3.service.errors import (
    ServiceError,
    ServiceErrorCode,
    sanitize_for_error,
)
from phase3.service.timeutil import today_utc, utc_now_iso

__all__ = [
    "SERVICE_FRESHNESS_STATES",
    "ENTITY_DATA_CLASS",
    "to_service_state",
    "freshness_for",
]

SERVICE_FRESHNESS_STATES = ("FRESH", "DEGRADED", "STALE", "UNAVAILABLE")

#: Server-side freshness-failure diagnostics (Phase 6.7B-R4) — the
#: EXTERNAL envelope never carries the driver detail.
_DEPENDENCY_LOGGER = logging.getLogger("fie.service")

#: Existing FIE entity kinds → governance data classes (§5.2 policy).
ENTITY_DATA_CLASS = {
    "macro": DATA_CLASS_MACRO,        # trading-day policy
    "company": DATA_CLASS_COMPANY,    # calendar-day policy
    "industry": DATA_CLASS_INDUSTRY,  # calendar-day policy
}


def to_service_state(governance_state: str) -> str:
    """Governance vocabulary → service vocabulary (total mapping)."""
    mapping = {
        "FRESH": "FRESH",
        "STALE": "DEGRADED",
        "EXPIRED": "STALE",
        "UNAVAILABLE": "UNAVAILABLE",
    }
    return mapping.get(governance_state, "UNAVAILABLE")


class _SignalQueryLike(Protocol):
    """The one repository call this module needs (nothing more)."""

    def query(
        self,
        entity_type: str | None = None,
        entity_id: str | None = None,
        signal_type: str | None = None,
        source_type: str | None = None,
        date_bucket: str | None = None,
        limit: int = 1000,
    ) -> list[Any]: ...


def freshness_for(
    signals: _SignalQueryLike,
    kind: str,
    entity_id: str,
    as_of: str | None,
) -> Any:
    """Build a service :class:`~phase3.service.contracts.Freshness`.

    ``as_of`` defaults to today (UTC); the classification is then the
    governance policy applied to the *latest observed signal
    timestamp* for the entity. Unparseable/absent timestamps map to
    UNAVAILABLE rather than guessing freshness.
    """
    from phase3.service.contracts import Freshness

    try:
        rows = signals.query(entity_type=kind, entity_id=entity_id, limit=100)
    except Exception as exc:  # noqa: BLE001
        # Phase 6.7B-R4 (Architecture ruling §4.3): stable external
        # classification + dependency KIND only — sanitized driver text
        # no longer reaches the envelope (it can name internal
        # topology); the sanitized detail stays in server-side logs.
        _DEPENDENCY_LOGGER.warning(json.dumps(
            {
                "event": "freshness_dependency_failure",
                "reason": sanitize_for_error(str(exc)),
            },
            sort_keys=True,
        ))
        raise ServiceError(
            ServiceErrorCode.DEPENDENCY_UNAVAILABLE,
            "signal observations are not readable",
        ) from exc

    as_of_date = as_of or today_utc()
    latest = _latest_timestamp(rows)
    data_class = ENTITY_DATA_CLASS.get(kind)
    if latest is None:
        return Freshness(
            state="UNAVAILABLE",
            as_of=str(as_of_date),
            checked_at=utc_now_iso(),
            source_date=None,
            age=None,
        )
    try:
        gov_status, age = classify_freshness(latest, as_of_date, data_class)
    except Exception:  # noqa: BLE001 - malformed timestamps are data, not crashes
        # fall through to UNAVAILABLE classification
        gov_status, age = "UNAVAILABLE", None
    return Freshness(
        state=to_service_state(getattr(gov_status, "value", str(gov_status))),
        as_of=str(as_of_date),
        checked_at=utc_now_iso(),
        source_date=str(latest),
        age=age,
    )


def _latest_timestamp(rows: list[Any]) -> str | None:
    stamps = []
    for r in rows:
        value = getattr(r, "timestamp", None) or getattr(r, "fetched_at", None)
        if isinstance(value, str) and value.strip():
            # Governance policy consumes *dates*; the stamps are
            # ISO-8601 UTC (possibly with time-of-day), so the date
            # part is the observation date. Latest wins lexicographically
            # — ISO order == time order.
            stamps.append(value.strip()[:10])
    return max(stamps) if stamps else None