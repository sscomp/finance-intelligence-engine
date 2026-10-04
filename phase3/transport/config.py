"""Reference-runtime HTTP configuration (Phase 6.6 §9; 6.7B fail-closed).

Environment/config driven, provider-neutral, safe defaults:

=================== ================================ ================================
variable            meaning                          default (absent value)
=================== ================================ ================================
FIE_HTTP_HOST       bind host                        127.0.0.1 (loopback — never
                                                     an implicit public bind)
FIE_HTTP_PORT       bind port                        8787 (0 = ephemeral bind,
                                                     deliberate — used by tests/
                                                     clean-room probes)
FIE_AUTH_MODE       "none" (dev/test) | "token"      "none" (FORBIDDEN under
                                                     staging/production — ADR-017)
FIE_AUTH_TOKEN      bearer token (env-only; never
                    has a default, never logged)
FIE_AUTH_PRINCIPAL  opaque principal label for a
                    token-authenticated caller       "service-consumer"
FIE_LOG_LEVEL       Python logging level name        "INFO"
FIE_REQUEST_TIMEOUT seconds per HTTP connection       "60"
FIE_DATABASE_URL    persistence target (Phase 6.3)   portable SQLite default
                                                     (local/test ONLY — REQUIRED
                                                     under staging/production)
FIE_SERVICE_ENV     runtime profile: local | test |  "local"
                    staging | production
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

Fail-closed contract (Phase 6.7B-R1 / ADR-017)
----------------------------------------------
The *absent* value of every knob keeps its accepted safe default (the
local/test compatibility boundary is tested, not assumed). An
*explicitly supplied invalid* value — argument or environment — is a
configuration error and raises :class:`ConfigurationError`; it never
silently falls back to the default (HP-00/HP-04/HP-08 repair):

* invalid ``FIE_AUTH_MODE`` raises (it can never silently disable
  authentication by falling back to ``none``);
* ``FIE_AUTH_MODE=none`` is FORBIDDEN under staging/production, and
  ``token`` mode requires a non-empty ``FIE_AUTH_TOKEN``; outside
  production-like profiles, credential presence stays the
  authenticator boundary's own fail-closed contract
  (:class:`~phase3.transport.auth.BearerTokenAuthenticator` raises at
  construction — R2 owns that boundary);
* staging/production require an explicit ``FIE_DATABASE_URL`` (no
  CWD-relative SQLite fallback; SQLite paths must be absolute);
* invalid ``FIE_AUTH_MODE`` / ``FIE_LOG_LEVEL`` /
  ``FIE_REQUEST_TIMEOUT`` / ``FIE_HTTP_PORT`` /
  ``FIE_SQLITE_ACCESS_MODE`` raise with one deterministic,
  store-compatible vocabulary (``ACCESS_MODES`` for the access mode);
* ``FIE_REQUEST_TIMEOUT`` has one vocabulary: a parseable, finite,
  positive number — non-numeric, zero, negative and non-finite values
  all refuse deterministically (no split-brain fallbacks to ``60.0``).

Production-like profiles (``staging``/``production``) are validated at
configuration resolution — BEFORE the server binds — and the console
entry point (:func:`phase3.transport.http.main`) prints a single
sanitized refusal diagnostic and exits non-zero. No fallback service,
no degraded unauthenticated mode, no partial start (work order §5).
Configuration values are NEVER echoed in diagnostics (leak-marker
tested): the message names the variable and category, withholds the
value.

Scope boundary (R1): this hardens configuration semantics only — the
authenticator architecture stays replaceable (R2 owns the
authentication/health-probe redesign).

Security: ``auth_token`` is deliberately NOT part of the log-safe
``to_dict()`` view; the persistence DSN appears only in masked form
(:func:`phase3.persistence.backend.sanitize_db_url`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from phase3.service.runtime_config import ConfigurationError, load_runtime_config

__all__ = [
    "TransportConfig",
    "load_transport_config",
    "ConfigurationError",
    "REFUSAL_CODES",
    "FIE_HTTP_HOST",
    "FIE_HTTP_PORT",
    "FIE_AUTH_MODE",
    "FIE_LOG_LEVEL",
    "FIE_REQUEST_TIMEOUT",
    "FIE_SQLITE_ACCESS_MODE",
    "VALID_ACCESS_MODES",
    "VALID_PROFILES",
    "PRODUCTION_LIKE_PROFILES",
]

from phase3.transport.auth import AUTH_MODES

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

#: Service-profile vocabulary (Phase 6.7B / ADR-017) — validated against
#: ``_VALID_PROFILES`` in :mod:`phase3.service.runtime_config`; mirrored
#: here for documentation/tests and cross-pinned by the R1 suite.
VALID_PROFILES = ("local", "test", "staging", "production")

#: Profiles that fail closed on missing/invalid required configuration.
PRODUCTION_LIKE_PROFILES = ("staging", "production")

#: Stable machine-testable refusal categories raised by the fail-closed
#: configuration contract (ADR-017 §startup-refusal). The console entry
#: point emits these verbatim in its refusal diagnostic.
REFUSAL_CODES = (
    "UNKNOWN_SERVICE_ENV",
    "UNKNOWN_AUTH_MODE",
    "AUTH_MODE_FORBIDDEN_IN_PRODUCTION",
    "AUTH_CREDENTIAL_MISSING",
    "DATABASE_URL_MISSING",
    "DATABASE_URL_INVALID",
    "UNKNOWN_ACCESS_MODE",
    "INVALID_HTTP_PORT",
    "INVALID_LOG_LEVEL",
    "INVALID_REQUEST_TIMEOUT",
    # Phase 6.7B-R4 (ADR-017 §7 ownership): the observability format
    # knob is determinized like every other knob — its last silent
    # fallback is closed.
    "INVALID_LOG_FORMAT",
)

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


def _refuse(code: str, message: str) -> ConfigurationError:
    """Build a sanitized refusal (values are never echoed)."""
    assert code in REFUSAL_CODES  # the category vocabulary is closed
    return ConfigurationError(code, message)


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
    service_env: str = "local"       # local | test | staging | production
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
            "service_env": self.service_env,
            "runtime": self.runtime,
        }


def _resolve_port(explicit: int | None) -> int:
    """Validate the bind port (0 = deliberate ephemeral bind).

    Non-numeric, negative and >65535 values refuse deterministically —
    they never silently become 8787 (ADR-017 §4.6).
    """
    _BAD = (
        "INVALID_HTTP_PORT",
        "FIE_HTTP_PORT invalid; expected an integer in 0..65535 "
        "(0 = ephemeral); value withheld",
    )
    if explicit is not None:
        try:
            port = int(explicit)
        except (TypeError, ValueError):
            raise _refuse(*_BAD) from None
    else:
        raw = os.environ.get(FIE_HTTP_PORT)
        if raw is None:
            return 8787  # absent → accepted safe default
        try:
            port = int(raw.strip())
        except ValueError:
            raise _refuse(*_BAD) from None
    if port < 0 or port > 65535:
        raise _refuse(*_BAD)
    return port


def _resolve_log_level(explicit: str | None) -> str:
    if explicit is not None:
        level = str(explicit).strip().upper()
    else:
        raw = os.environ.get(FIE_LOG_LEVEL)
        if raw is None:
            return "INFO"  # absent → accepted safe default
        level = raw.strip().upper()
    if level not in _VALID_LOG_LEVELS:
        raise _refuse(
            "INVALID_LOG_LEVEL",
            "FIE_LOG_LEVEL invalid; expected one of "
            "DEBUG|INFO|WARNING|ERROR; value withheld",
        )
    return level


def _resolve_request_timeout(explicit: float | None) -> float:
    """One timeout vocabulary: parseable, finite, strictly positive.

    Non-numeric, zero, negative and non-finite values all refuse
    deterministically — no fallback to ``60.0`` (HP-08: the old split
    brain where non-numeric escaped as ValueError but ``<=0`` silently
    fell back is eliminated).
    """
    import math

    if explicit is not None:
        try:
            value = float(explicit)
        except (TypeError, ValueError):
            raise _refuse(
                "INVALID_REQUEST_TIMEOUT",
                "FIE_REQUEST_TIMEOUT invalid; expected a positive finite "
                "number of seconds; value withheld",
            ) from None
    else:
        raw = os.environ.get(FIE_REQUEST_TIMEOUT)
        if raw is None:
            return 60.0  # absent → accepted safe default
        try:
            value = float(raw.strip())
        except (ValueError, TypeError):
            raise _refuse(
                "INVALID_REQUEST_TIMEOUT",
                "FIE_REQUEST_TIMEOUT invalid; expected a positive finite "
                "number of seconds; value withheld",
            ) from None
    if not math.isfinite(value) or value <= 0:
        raise _refuse(
            "INVALID_REQUEST_TIMEOUT",
            "FIE_REQUEST_TIMEOUT invalid; expected a positive finite "
            "number of seconds; value withheld",
        )
    return value


def _resolve_access_mode(explicit: str | None) -> str:
    """One access-mode vocabulary, consistent with the store layer.

    The SQLite store raises on a mode outside ``ACCESS_MODES``; the
    configuration layer now refuses the same way instead of silently
    falling back to ``writable`` (ADR-017 §4.5 — the two-layer
    disagreement is eliminated; the vocabulary stays cross-pinned to
    ``phase3.persistence.sqlite.ACCESS_MODES``).
    """
    if explicit is not None:
        mode = str(explicit).strip().lower()
        supplied = True
    else:
        raw = os.environ.get(FIE_SQLITE_ACCESS_MODE)
        supplied = raw is not None
        mode = (raw or "").strip().lower()
    if not supplied:
        return "writable"  # absent → accepted safe default
    if not mode or mode not in VALID_ACCESS_MODES:
        raise _refuse(
            "UNKNOWN_ACCESS_MODE",
            "FIE_SQLITE_ACCESS_MODE invalid; expected one of "
            "writable|readonly|immutable_snapshot; value withheld",
        )
    return mode


def _resolve_auth_mode(explicit: str | None) -> str:
    """Validate the auth mode — an invalid value can never disable auth.

    HP-00 repair: the old behavior silently fell back to ``none``,
    turning a mistyped ``FIE_AUTH_MODE`` into an unauthenticated
    service. Vocabulary is the authenticator boundary's own
    ``AUTH_MODES`` (single source; R2 keeps it replaceable).
    """
    if explicit is not None:
        mode = str(explicit).strip().lower()
        supplied = True
    else:
        raw = os.environ.get(FIE_AUTH_MODE)
        supplied = raw is not None
        mode = (raw or "").strip().lower()
    if not supplied:
        return "none"  # absent → accepted dev/test default
    if not mode or mode not in AUTH_MODES:
        raise _refuse(
            "UNKNOWN_AUTH_MODE",
            "FIE_AUTH_MODE invalid; expected one of none|token; "
            "value withheld",
        )
    return mode


def _validate_production_like(
    profile: str, auth_mode: str, token: str,
    database_source: str, database_spec_sanitized: str,
) -> None:
    """staging/production fail-closed gates (ADR-017 §4.2–§4.4).

    Runs at configuration resolution — before anything binds — so a
    partially-configured service never accepts traffic. Consumes only
    the service seam (:mod:`phase3.service.runtime_config` views): the
    DSN never appears here in raw form and never in a diagnostic.

    The DSN *shape* gate (absolute path required for SQLite) lives in
    the composition root (:func:`phase3.transport.http.run_server`) —
    the one transport module permitted to resolve a full spec (the
    Phase 6.7A frozen import surface is preserved; see ADR-017).
    """
    if profile not in PRODUCTION_LIKE_PROFILES:
        return
    if auth_mode == "none":
        raise _refuse(
            "AUTH_MODE_FORBIDDEN_IN_PRODUCTION",
            "FIE_AUTH_MODE=none is forbidden under staging/production; "
            "select a configured authentication mode",
        )
    if auth_mode == "token" and not token.strip():
        raise _refuse(
            "AUTH_CREDENTIAL_MISSING",
            "FIE_AUTH_MODE=token requires FIE_AUTH_TOKEN to be set"
            " (staging/production)",
        )
    if database_source == "default" or not database_spec_sanitized.strip():
        raise _refuse(
            "DATABASE_URL_MISSING",
            "FIE_DATABASE_URL is required under staging/production; the "
            "portable CWD-relative SQLite default is not accepted",
        )


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
    service_env: str | None = None,
) -> TransportConfig:
    """Resolve the transport contract (arg > env > safe default).

    Raises :class:`ConfigurationError` on any explicitly supplied
    invalid value (every profile) and on missing required
    configuration under staging/production — fail-closed, before bind
    (ADR-017). An absent value keeps the accepted safe default.
    """
    # runtime/profile resolution first: an invalid FIE_SERVICE_ENV
    # refuses before any other knob is even examined (deterministic).
    runtime = load_runtime_config(explicit_db, service_env=service_env)
    profile = runtime.service_env
    spec = runtime.to_dict()  # sanitized only — never echo the raw DSN

    host = (host if host is not None else os.environ.get(FIE_HTTP_HOST, "127.0.0.1")).strip() or "127.0.0.1"
    port = _resolve_port(port)
    auth_mode = _resolve_auth_mode(auth_mode)
    token = auth_token if auth_token is not None else os.environ.get("FIE_AUTH_TOKEN", "")
    log_level = _resolve_log_level(log_level)
    request_timeout = _resolve_request_timeout(request_timeout)
    sqlite_access_mode = _resolve_access_mode(sqlite_access_mode)
    # runtime.database_spec is the sanitized (masked) DSN — the only
    # form this layer ever touches (raw paths/DSNs stay withheld)
    _validate_production_like(
        profile, auth_mode, token,
        runtime.database_source, runtime.database_spec,
    )
    return TransportConfig(
        host=host,
        port=port,
        auth_mode=auth_mode,
        auth_token=token or "",
        log_level=log_level,
        request_timeout=request_timeout,
        sqlite_access_mode=sqlite_access_mode,
        service_env=profile,
        runtime=spec,
        db_spec=explicit_db,
    )
