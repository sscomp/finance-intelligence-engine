"""Authentication boundary (Phase 6.6 §6).

A real architectural boundary without production identity
infrastructure:

* :class:`Authenticator` — the transport-wide interface. Identity
  provider details live HERE, never inside ``IntelligenceService``
  (which keeps consuming the Phase 6.5 opaque
  :class:`~phase3.service.contracts.RequestContext`);
* :class:`NoopAuthenticator` — safe test/dev anonymous principal;
* :class:`BearerTokenAuthenticator` — dev/test token auth from the
  environment (``FIE_AUTH_TOKEN``); never a default, never logged.

Production identity integration (OAuth/OIDC etc.) is replaceable via
``Authenticator`` — Phase 6.6 deliberately does NOT provision any
production identity infrastructure (work order §6).

Security shape (§6.5/§6.6): credentials arrive from the environment
only; a token never appears in telemetry, errors or the config's
dict view; missing or invalid credentials are rejected with
``UNAUTHENTICATED``; the returned principal is opaque
(synthetic-safe for tests).
"""
from __future__ import annotations

import hmac
import os
import re
from typing import Any, Protocol

from phase3.transport.errors import TransportError, TransportErrorCode

__all__ = [
    "Authenticator",
    "NoopAuthenticator",
    "BearerTokenAuthenticator",
    "FIE_AUTH_MODE",
    "FIE_AUTH_TOKEN",
    "FIE_AUTH_PRINCIPAL",
]

FIE_AUTH_MODE = "FIE_AUTH_MODE"
FIE_AUTH_TOKEN = "FIE_AUTH_TOKEN"
FIE_AUTH_PRINCIPAL = "FIE_AUTH_PRINCIPAL"

AUTH_MODES = ("none", "token")
#: Every Phase 6.5/6.6 interactive operation is a shared-intelligence
#: read with the same scope; the name exists so a future restrictive
#: authenticator can deny without changing the transport contract.
READ_SCOPE = "intelligence:read"

_HEADER_TOKEN_RE = re.compile(r"Bearer[ ]+(\S+)", re.IGNORECASE)
_REQUEST_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
# collapse dot runs and trim them: a sanitized id must never look like a
# relative path fragment (".." survives the character-class above otherwise)
_REQUEST_ID_DOT_RUNS = re.compile(r"\.{2,}")


class Authenticator(Protocol):
    """Extracts an opaque authenticated principal from request headers."""

    def authenticate(self, headers: Any) -> "Any":
        """Return a ``RequestContext`` or raise ``TransportError``."""
        ...


class NoopAuthenticator:
    """mode=none — anonymous test/dev principal (safe default)."""

    mode = "none"
    principal_id = "anonymous"

    def authenticate(self, headers: Any) -> Any:
        from phase3.service.contracts import RequestContext

        request_id = _request_id_from_headers(headers)
        return RequestContext(
            principal_id=self.principal_id,
            request_id=request_id,
            scopes=frozenset({READ_SCOPE}),
        )


class BearerTokenAuthenticator:
    """mode=token — one configured bearer token from the environment.

    Construction requires the token to be resolvable; the reference
    runtime refuses to start with ``auth_mode=token`` but no token
    (fail-closed config, not per-request guessing).
    """

    mode = "token"

    def __init__(self, token: str, principal_id: str = "service-consumer") -> None:
        if not token or not token.strip():
            raise TransportError(
                TransportErrorCode.FORBIDDEN,  # config error surfaced at startup
                "auth mode 'token' requires FIE_AUTH_TOKEN to be set",
            )
        self._token = token.strip().encode("utf-8")
        self.principal_id = principal_id or "service-consumer"

    def authenticate(self, headers: Any) -> Any:
        from phase3.service.contracts import RequestContext

        header = headers.get("Authorization", "") if headers is not None else ""
        match = _HEADER_TOKEN_RE.fullmatch(header.strip())
        if not match:
            raise TransportError(
                TransportErrorCode.UNAUTHENTICATED,
                "missing bearer credentials",
            )
        supplied = match.group(1)
        if not hmac.compare_digest(supplied.encode("utf-8"), self._token):
            raise TransportError(
                TransportErrorCode.UNAUTHENTICATED,
                "invalid credentials",
            )
        # The credential never becomes identity and never re-enters
        # logs/errors: only the configured opaque principal appears.
        return RequestContext(
            principal_id=self.principal_id,
            request_id=_request_id_from_headers(headers),
            scopes=frozenset({READ_SCOPE}),
        )


def build_authenticator(
    auth_mode: str, *, token: str | None = None, principal_id: str | None = None
) -> Any:
    """Construct the authenticator for a configured mode (fail-closed)."""
    if auth_mode == "none":
        return NoopAuthenticator()
    if auth_mode == "token":
        return BearerTokenAuthenticator(
            token or os.environ.get(FIE_AUTH_TOKEN, ""),
            principal_id=principal_id
            or os.environ.get(FIE_AUTH_PRINCIPAL, "service-consumer"),
        )
    raise TransportError(
        TransportErrorCode.FORBIDDEN, f"unknown auth mode {auth_mode!r}"
    )


def _request_id_from_headers(headers: Any) -> str:
    raw = headers.get("X-Request-Id", "") if headers is not None else ""
    if raw:
        clean = _REQUEST_ID_RE.sub("", raw.strip())
        clean = _REQUEST_ID_DOT_RUNS.sub(".", clean).strip(".")[:64]
        if clean:
            return clean
    import uuid

    return uuid.uuid4().hex