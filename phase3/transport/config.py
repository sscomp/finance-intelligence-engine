"""Reference-runtime HTTP configuration (Phase 6.6 §9).

Environment/config driven, provider-neutral, safe defaults:

=================== ================================ ================================
variable            meaning                          default
=================== ================================ ================================
FIE_HTTP_HOST       bind host                        127.0.0.1 (loopback — never
                                                     an implicit public bind)
FIE_HTTP_PORT       bind port                        8787
FIE_AUTH_MODE       "none" (dev/test) | "token"      "none"
FIE_AUTH_TOKEN      bearer token (env-only; never
                    has a default, never logged)
FIE_AUTH_PRINCIPAL  opaque principal label for a
                    token-authenticated caller       "service-consumer"
FIE_LOG_LEVEL       Python logging level name        "INFO"
FIE_REQUEST_TIMEOUT seconds per HTTP connection       "60"
FIE_DATABASE_URL    persistence target (Phase 6.3)   portable SQLite default
FIE_SERVICE_ENV     runtime profile (diagnostic)     "local"
FIE_SQLITE_ACCESS_MODE  SQLite deployment open mode  "writable"
                    (Phase 6.6R4): "writable" (historical
                    read-write producer/batch profile),
                    "readonly" (mode=ro reader; needs writable
                    sidecar space for WAL artifacts) or
                    "immutable_snapshot" (declared read-only
                    container deployment: DB-only artifact,
                    never modified while the reader lives —
                    ADR-013)
=================== ================================ ================================

Precedence (documented + tested): explicit argument > environment > safe
default — the same shape as the Phase 6.5/6.3 contracts
(``load_runtime_config`` / ``resolve_spec``).

Security: ``auth_token`` is deliberately NOT part of the log-safe
``to_dict()`` view; the persistence DSN appears only in masked form
(:func:`phase3.persistence.backend.sanitize_db_url`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from phase3.service.runtime_config import load_runtime_config

__all__ = [
    "TransportConfig",
    "load_transport_config",
    "FIE_HTTP_HOST",
    "FIE_HTTP_PORT",
    "FIE_AUTH_MODE",
    "FIE_LOG_LEVEL",
    "FIE_REQUEST_TIMEOUT",
    "FIE_SQLITE_ACCESS_MODE",
    "VALID_ACCESS_MODES",
]

FIE_HTTP_HOST = "FIE_HTTP_HOST"
FIE_HTTP_PORT = "FIE_HTTP_PORT"
FIE_AUTH_MODE = "FIE_AUTH_MODE"
FIE_LOG_LEVEL = "FIE_LOG_LEVEL"
FIE_REQUEST_TIMEOUT = "FIE_REQUEST_TIMEOUT"

#: SQLite deployment access mode (Phase 6.6R4): writable | readonly |
#: immutable_snapshot. ``writable`` (default) is the historical
#: behavior; ``immutable_snapshot`` is the declared read-only
#: container deployment contract (``-v <dir>:/data:ro``; ADR-013).
FIE_SQLITE_ACCESS_MODE = "FIE_SQLITE_ACCESS_MODE"

#: The SQLite deployment access-mode vocabulary (Phase 6.6R4) as accepted
#: by this transport contract. Must stay identical to
#: ``phase3.persistence.sqlite.ACCESS_MODES`` — the identity is pinned by
#: ``tests/phase3/transport/test_66r4_readonly_sqlite.py`` (the transport
#: layer imports no persistence seam beyond ``backend``, so the
#: vocabulary is mirrored here and cross-pinned by test).
VALID_ACCESS_MODES = ("writable", "readonly", "immutable_snapshot")

#: the complete Phase 6.6 transport env surface (documentation/tests)
TRANSPORT_ENV_VARS = (
    FIE_HTTP_HOST,
    FIE_HTTP_PORT,
    FIE_AUTH_MODE,
    "FIE_AUTH_TOKEN",
    "FIE_AUTH_PRINCIPAL",
    FIE_LOG_LEVEL,
    FIE_REQUEST_TIMEOUT,
    "FIE_DATABASE_URL",
    "FIE_SERVICE_ENV",
    FIE_SQLITE_ACCESS_MODE,
)

_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class TransportConfig:
    """Resolved HTTP runtime configuration (log-safe)."""

    host: str = "127.0.0.1"
    port: int = 8787
    auth_mode: str = "none"          # none | token
    auth_token: str = field(default="", repr=False)  # env-only; NEVER serialised
    auth_principal: str = "service-consumer"
    log_level: str = "INFO"
    request_timeout: float = 60.0
    sqlite_access_mode: str = "writable"  # writable | readonly | immutable_snapshot
    runtime: dict[str, Any] = field(default_factory=dict)
    db_spec: str | None = field(default=None, repr=False)  # raw DSN, never serialised

    def to_dict(self) -> dict[str, Any]:
        """Log/report-safe view — auth_token is never included."""
        return {
            "host": self.host,
            "port": self.port,
            "auth_mode": self.auth_mode,
            "log_level": self.log_level,
            "request_timeout": self.request_timeout,
            "sqlite_access_mode": self.sqlite_access_mode,
            "runtime": self.runtime,
        }


def load_transport_config(
    explicit_db: str | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    auth_mode: str | None = None,
    auth_token: str | None = None,
    log_level: str | None = None,
    request_timeout: float | None = None,
    sqlite_access_mode: str | None = None,
) -> TransportConfig:
    """Resolve the transport contract (arg > env > safe default)."""
    host = (host or os.environ.get(FIE_HTTP_HOST, "127.0.0.1")).strip()
    try:
        # an explicit 0 (ephemeral bind, used by tests/clean-room probes)
        # is a deliberate value, so only None defers to the environment
        port = int(port) if port is not None else int(
            os.environ.get(FIE_HTTP_PORT, "8787")
        )
        if port < 0:
            port = 8787
    except (ValueError, TypeError):
        port = 8787  # safe default on invalid env (tested §9)
    auth_mode = (auth_mode or os.environ.get(FIE_AUTH_MODE, "none")).strip().lower()
    if auth_mode not in ("none", "token"):
        auth_mode = "none"
    token = auth_token if auth_token is not None else os.environ.get("FIE_AUTH_TOKEN", "")
    log_level = (log_level or os.environ.get(FIE_LOG_LEVEL, "INFO")).strip().upper()
    if log_level not in _VALID_LOG_LEVELS:
        log_level = "INFO"
    request_timeout = request_timeout if request_timeout is not None else float(
        os.environ.get(FIE_REQUEST_TIMEOUT, "60")
    )
    if request_timeout <= 0:
        request_timeout = 60.0
    sqlite_access_mode = (
        sqlite_access_mode if sqlite_access_mode is not None
        else os.environ.get(FIE_SQLITE_ACCESS_MODE, "writable")
    )
    sqlite_access_mode = (sqlite_access_mode or "writable").strip().lower()
    if sqlite_access_mode not in VALID_ACCESS_MODES:
        # safe default on invalid env (same fallback style as the
        # other Phase 6.6 knobs — tested in the transport suite)
        sqlite_access_mode = "writable"
    return TransportConfig(
        host=host,
        port=port,
        auth_mode=auth_mode,
        auth_token=token or "",
        log_level=log_level,
        request_timeout=request_timeout,
        sqlite_access_mode=sqlite_access_mode,
        runtime=load_runtime_config(explicit_db).to_dict(),
        db_spec=explicit_db,
    )