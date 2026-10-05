"""Cloud-safe runtime configuration contract (Phase 6.5 §10).

Everything a service/batch runtime may need arrives through the
environment with a documented precedence and zero
developer/host-specific defaults:

=========== ============================ ============================
variable    meaning                      default
=========== ============================ ============================
FIE_DATABASE_URL                            Phase 3B store target   unset → portable SQLite default
FIE_DATA_DIR                filesystem data root (only where FS data is explicitly
                            supported, e.g. macro_history layer)     repo root (paths.py)
FIE_ARTIFACT_DIR            report artifact output                   <root>/metadata/reports/artifacts
FIE_SERVICE_ENV             runtime profile: "local"|"test"|"staging"|"production"
                            (Phase 6.7B/ADR-017: unsafe in staging/production —
                            see :mod:`phase3.transport.config`; absent →
                            "local"; an explicitly INVALID value raises
                            ConfigurationError — it never silently becomes
                            "local")
FIE_LOG_FORMAT              "structured"|"plain" log lines           "structured"
=========== ============================ ============================

Precedence (documented + tested):
    explicit function argument > environment > safe local default

Security shape (work order §14): values are never logged raw — any
database URL travels through
:func:`phase3.persistence.backend.sanitize_db_url` (masking), and
:func:`redact` scrubs URL/path fragments from diagnostics generally.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from phase3.paths import (
    artifact_dir,
    data_dir,
    database_url,
)
from phase3.persistence.backend import sanitize_db_url

__all__ = [
    "ConfigurationError",
    "ServiceRuntimeConfig",
    "load_runtime_config",
    "FIE_SERVICE_ENV",
    "FIE_LOG_FORMAT",
    "RUNTIME_ENV_VARS",
]

FIE_SERVICE_ENV = "FIE_SERVICE_ENV"
FIE_LOG_FORMAT = "FIE_LOG_FORMAT"

#: The complete runtime-environment surface of Phase 6.5 (for docs/tests).
RUNTIME_ENV_VARS = (
    "FIE_DATABASE_URL",
    "FIE_DATA_DIR",
    "FIE_CONFIG_DIR",
    "FIE_ARTIFACT_DIR",
    "FIE_SERVICE_ENV",
    "FIE_LOG_FORMAT",
)

_VALID_PROFILES = ("local", "test", "staging", "production")
_VALID_LOG_FORMATS = ("structured", "plain")


class ConfigurationError(Exception):
    """Deterministic configuration refusal (Phase 6.7B / ADR-017).

    Raised when an explicitly supplied configuration value is invalid
    or a required production-like configuration is missing. Fail-closed
    semantics: the caller must refuse startup — there is no silent
    fallback, degraded mode or partial service.

    Attributes
    ----------
    code:
        Stable machine-testable refusal category (e.g.
        ``"UNKNOWN_SERVICE_ENV"``). Values are configuration
        categories, never message fragments that could carry secrets.
    message:
        Sanitized single-line diagnostic. Configuration values are
        deliberately withheld — the message names the environment
        variable and the reason only, so no token, DSN, password or
        injected marker can leak through it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

_URL_RE = re.compile(r"\S*://\S+")
_PATH_RE = re.compile(r"(?:[\w@.-]*/)?[\w@-]*(?:/(?:[\w./@-]+))+", re.ASCII)


def redact(text: str) -> str:
    """Scrub URL/path fragments from any diagnostic line (§14)."""
    text = _URL_RE.sub("<redacted>", text)
    return _PATH_RE.sub("<redacted>", text)


@dataclass(frozen=True)
class ServiceRuntimeConfig:
    """Resolved runtime configuration (values may be empty strings)."""

    database_spec: str = ""  # sanitized (never raw)
    database_source: str = ""  # explicit | env | default
    data_dir: str = ""
    config_dir: str = ""
    artifact_dir: str = ""
    service_env: str = "local"
    log_format: str = "structured"
    extras: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Log/report-safe view — the DSN is only ever the masked form."""
        return {
            "database_spec": self.database_spec,
            "database_source": self.database_source,
            "data_dir": self.data_dir,
            "config_dir": self.config_dir,
            "artifact_dir": self.artifact_dir,
            "service_env": self.service_env,
            "log_format": self.log_format,
        }


def load_runtime_config(
    explicit_db: str | None = None, *, service_env: str | None = None
) -> ServiceRuntimeConfig:
    """Resolve the runtime contract (precedence: arg > env > safe default).

    ``service_env`` (Phase 6.7B) is an explicit-argument override with
    the same precedence as every other explicit argument; both the
    argument and the ``FIE_SERVICE_ENV`` environment variable are
    validated against the profile vocabulary, and an explicitly
    supplied invalid value raises :class:`ConfigurationError` — it
    never silently becomes ``local`` (ADR-017). An *absent* value
    keeps the accepted ``local`` development default.
    """
    from phase3.persistence.backend import resolve_spec
    from phase3.runtime_contract import FailClosedTarget

    if service_env is not None:
        profile = service_env.strip().lower()
    else:
        profile = os.environ.get(FIE_SERVICE_ENV, "").strip().lower()
        if not profile:
            profile = "local"
    if profile not in _VALID_PROFILES:
        raise ConfigurationError(
            "UNKNOWN_SERVICE_ENV",
            "FIE_SERVICE_ENV has an invalid value; startup refused "
            "(value withheld)",
        )
    log_format = os.environ.get(FIE_LOG_FORMAT, "structured").strip().lower()
    if log_format not in _VALID_LOG_FORMATS:
        # Phase 6.7B-R4 (ADR-017 §7 ownership): the last silent-config
        # fallback is closed. An explicitly supplied invalid
        # FIE_LOG_FORMAT refuses deterministically like every other
        # knob — it never silently becomes "structured".
        raise ConfigurationError(
            "INVALID_LOG_FORMAT",
            "FIE_LOG_FORMAT invalid; expected one of structured|plain; "
            "value withheld",
        )
    # Phase 6.8A: DB-target resolution fails closed at the canonical
    # resolver (no implicit default exists anymore) — resolved AFTER the
    # profile/log-format vocabulary so each refusal class stays
    # independently testable (order: profile → log-format → DB target).
    # The contract refusal is translated into this layer's closed
    # refusal vocabulary so the composed refusal surface stays
    # ConfigurationError-typed; the fail-closed class is preserved in
    # the message.
    try:
        spec = resolve_spec(explicit_db)
    except FailClosedTarget as exc:
        code = {
            "FAIL_CLOSED_DB_TARGET_REQUIRED": "DATABASE_URL_MISSING",
            "FAIL_CLOSED_MALFORMED_DB_TARGET": "DATABASE_URL_INVALID",
            "FAIL_CLOSED_CONTRADICTORY_DB_TARGETS":
                "CONTRADICTORY_DB_TARGETS",
        }.get(exc.reason, "DATABASE_URL_MISSING")
        raise ConfigurationError(
            code,
            f"database-target contract refused ({type(exc).__name__}; "
            "values withheld)",
        ) from None
    return ServiceRuntimeConfig(
        # database_spec: only the sanitized form is ever materialized
        database_spec=spec.sanitized(),
        database_source=spec.source,
        data_dir=str(data_dir()),
        config_dir=os.environ.get("FIE_CONFIG_DIR", str(data_dir())),
        artifact_dir=str(artifact_dir()),
        service_env=profile,
        log_format=log_format,
    )