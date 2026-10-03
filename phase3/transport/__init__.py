"""FIE transport layer (Phase 6.6) — thin, dependency-free reference runtime.

Public surface:

* error semantics (:mod:`phase3.transport.errors`) — transport codes
  (``UNAUTHENTICATED`` / ``FORBIDDEN``) + the deterministic
  error→HTTP-status table;
* authentication boundary (:mod:`phase3.transport.auth`) — replaceable
  ``Authenticator``; anonymous (dev/test) and bearer-token modes;
* runtime configuration (:mod:`phase3.transport.config`) — bind
  host/port, auth mode, log level, request timeout, persistence target;
* HTTP server (:mod:`phase3.transport.http`) — stdlib
  ``ThreadingHTTPServer`` wrapping the Phase 6.5 read-only boundary;
  console entry point ``fie-http-server``.

The transport adds no scoring/persistence/scheduler logic: it maps
versioned HTTP routes 1:1 onto the frozen Phase 6.5
``IntelligenceService`` operations and re-emits the dispatch envelope
verbatim.
"""
from __future__ import annotations

from phase3.transport.config import TRANSPORT_ENV_VARS, TransportConfig, load_transport_config
from phase3.transport.errors import (
    HTTP_STATUS_BY_CODE,
    TransportError,
    TransportErrorCode,
    http_status_for,
)
from phase3.transport.http import (
    API_VERSION,
    FIEReferenceRuntime,
    make_http_server,
    run_server,
)

__all__ = [
    "TRANSPORT_ENV_VARS",
    "TransportConfig",
    "load_transport_config",
    "TransportError",
    "TransportErrorCode",
    "HTTP_STATUS_BY_CODE",
    "http_status_for",
    "API_VERSION",
    "FIEReferenceRuntime",
    "make_http_server",
    "run_server",
]