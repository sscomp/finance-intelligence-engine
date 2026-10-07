"""Transport-level error semantics (Phase 6.6 §7).

The Phase 6.5 service taxonomy (``phase3.service.errors.ServiceErrorCode``)
stays the *domain* error model untouched. The transport adds exactly two
transport-caused codes — ``UNAUTHENTICATED`` and ``FORBIDDEN`` — that the
service layer can never raise, and owns the HTTP status mapping table.

Do not use HTTP status codes in place of the domain taxonomy: the JSON
``error.code`` value is the contract; the status code is transport sugar
(Phase 6.5 ADR-005/ADR-010 discipline).
"""
from __future__ import annotations

import enum

from phase3.service.errors import ServiceError, sanitize_for_error

__all__ = [
    "TransportErrorCode",
    "TransportError",
    "HTTP_STATUS_BY_CODE",
]


class TransportErrorCode(str, enum.Enum):
    """Codes a *transport* may raise (service taxonomy is separate)."""

    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"


# Transport + service codes → HTTP status. One table, documented and
# tested; the JSON error envelope always carries the *code*, the HTTP
# status is derivable from it deterministically.
HTTP_STATUS_BY_CODE: dict[str, int] = {
    "INVALID_REQUEST": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "STALE_DATA": 409,
    "DATA_UNAVAILABLE": 503,
    "DEPENDENCY_UNAVAILABLE": 503,
    # Phase 6.7B-R3 (HP-07/06 / OI-08): deterministic fail-closed schema
    # compatibility verdict — readiness is not ready, and 503 (like the
    # other unavailability codes) never exposes a driver exception.
    "SCHEMA_INCOMPATIBLE": 503,
    # Phase 6.9B-R3: deterministic fail-closed runtime-identity verdict —
    # same unavailability family (the layer is reachable, its identity is
    # unacceptable); 503 never exposes a driver exception or catalog ids.
    "IDENTITY_INCOMPATIBLE": 503,
    # Phase 6.7B-R4: the request operation did not finish inside its
    # bounded dispatch window. Same unavailability family (a retrying
    # GET may succeed later); the abandoned read-only operation is
    # never re-executed.
    "OPERATION_TIMEOUT": 503,
    "INTERNAL_ERROR": 500,
}

#: DEGRADED is a freshness *state*, never an error: a DEGRADED
#: intelligence read is HTTP 200 with ``freshness.state = "DEGRADED"``
#: and the matching warning (work order §7/§8).


class TransportError(ServiceError):
    """Auth-plane error; same envelope shape, transport-level code.

    Passes the ``TransportErrorCode`` member itself (a str-Enum with
    ``.value``) so ``ServiceError.to_dict()``'s ``code.value`` keeps
    working; the type is distinct from the service taxonomy on purpose.
    """

    def __init__(
        self,
        code: TransportErrorCode,
        message: str,
        details: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code, message, details or {})  # type: ignore[arg-type]

    @property
    def transport_code(self) -> TransportErrorCode:
        return TransportErrorCode(self.code)


def http_status_for(code: str) -> int:
    """Deterministic HTTP status for an error code (default 500)."""
    return HTTP_STATUS_BY_CODE.get(code, 500)


def _sanitize(message: str) -> str:
    return sanitize_for_error(message)