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
                            (cosmetic/diagnostic only in 6.5; safe default = "local")
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


def load_runtime_config(explicit_db: str | None = None) -> ServiceRuntimeConfig:
    """Resolve the runtime contract (precedence: arg > env > safe default)."""
    from phase3.persistence.backend import resolve_spec

    spec = resolve_spec(explicit_db)
    service_env = os.environ.get(FIE_SERVICE_ENV, "local").strip().lower()
    if service_env not in _VALID_PROFILES:
        service_env = "local"
    log_format = os.environ.get(FIE_LOG_FORMAT, "structured").strip().lower()
    if log_format not in _VALID_LOG_FORMATS:
        log_format = "structured"
    return ServiceRuntimeConfig(
        # database_spec: only the sanitized form is ever materialized
        database_spec=spec.sanitized(),
        database_source=spec.source,
        data_dir=str(data_dir()),
        config_dir=os.environ.get("FIE_CONFIG_DIR", str(data_dir())),
        artifact_dir=str(artifact_dir()),
        service_env=service_env,
        log_format=log_format,
    )