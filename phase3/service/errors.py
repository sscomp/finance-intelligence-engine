"""Typed error taxonomy for the FIE service boundary (Phase 6.5).

The service boundary (``phase3.service``) exposes FIE intelligence to
external clients (ChatGPT today in design, other authorized clients
later). Transport adapters (HTTP or otherwise) map these codes onto
their own response shapes *outside* the domain contract — the work
order §5.2 explicitly forbids HTTP status codes as the domain error
model.

Taxonomy
--------
INVALID_REQUEST        request schema/arguments failed validation
NOT_FOUND              a well-formed request named something FIE has
                       no record of
STALE_DATA             a valid record exists but is past its hard
                       freshness window (governance ``EXPIRED``)
DATA_UNAVAILABLE       no data at all exists for the requested
                       domain object (further than NOT_FOUND: the
                       entity class itself has nothing to offer)
DEPENDENCY_UNAVAILABLE the persistence backend (or another
                       dependency) is unreachable/unusable — the
                       request might succeed later
INTERNAL_ERROR         an unexpected failure inside FIE (never
                       leaks stack traces or environment data)
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any

__all__ = ["ServiceErrorCode", "ServiceError", "sanitize_for_error"]

_DB_URL_RE = re.compile(
    r"\S*://\S+",  # any scheme://... string (credentials live in DSNs/URLs)
)
_PATH_RE = re.compile(r"(?:[\w@.-]*/)?[\w@-]*(?:/(?:[\w./@-]+))+", re.ASCII)
# Phase 6.6R1 (DEFECT-A): internal implementation detail that external
# consumers must never see — persistence exception reprs, SQL engine
# diagnostics and raw traceback banners. Applied in order; everything
# maps to the same neutral marker as DSNs/paths.
_INTERNAL_DETAIL_RES = (
    # dotted module/exception reprs ("sqlite3.OperationalError",
    # "psycopg.errors.UndefinedTable")
    re.compile(r"\b(?:sqlite3?|psycopg\d?|pg8000)\b(?:\.\w+)+"),
    # exception class names ("OperationalError", "ValueError", …)
    re.compile(r"\b[A-Za-z_]\w*(?:Error|Exception)\b"),
    # SQL engine diagnostics ("no such table: score_snapshot", …)
    re.compile(r"(?i)\bno such (?:table|column|function)\b[^\n]*"),
    # raw traceback banners
    re.compile(r"Traceback \(most recent call last\)"),
)


class ServiceErrorCode(str, Enum):
    """Stable, transport-neutral error classification (work order §5.2)."""

    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    STALE_DATA = "STALE_DATA"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    #: Phase 6.7B-R3 (HP-07/06 / OI-08): the persistence layer is
    #: reachable but its schema metadata is absent/stale/incompatible —
    #: readiness fails CLOSED with this stable, transport-neutral code
    #: (never a raw driver exception).
    SCHEMA_INCOMPATIBLE = "SCHEMA_INCOMPATIBLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def sanitize_for_error(text: str) -> str:
    """Strip transport/secret-bearing fragments from error text.

    Error responses must never carry DSNs/URLs (which may embed
    credentials), host filesystem paths (work order §14), or internal
    persistence/exception detail (Phase 6.6R1 DEFECT-A). All patterns
    are replaced by a neutral marker regardless of content —
    sanitization errs on the side of removing a benign string rather
    than leaking a secret-bearing one.
    """
    text = _DB_URL_RE.sub("<redacted>", text)
    for pattern in _INTERNAL_DETAIL_RES:
        text = pattern.sub("<redacted>", text)
    return _PATH_RE.sub("<redacted>", text)


class ServiceError(Exception):
    """A boundary-level error carrying the stable taxonomy code.

    ``details`` is an optional JSON-safe dict (no objects). The
    message and details are already expected to be secret-free;
    :meth:`to_dict` applies :func:`sanitize_for_error` again so a
    careless caller cannot defeat the contract.
    """

    def __init__(
        self,
        code: ServiceErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(sanitize_for_error(message))
        self.code = code
        self.details = {
            k: sanitize_for_error(v) if isinstance(v, str) else v
            for k, v in (details or {}).items()
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe error body for response envelopes."""
        out: dict[str, Any] = {"code": self.code.value, "message": str(self)}
        if self.details:
            out["details"] = self.details
        return out


__all__ += ["ServiceError"]