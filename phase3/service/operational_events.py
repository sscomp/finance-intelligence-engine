"""Structured operational-logging contract (Phase 6.9B-R4, WO §8–§9).

One repository-owned taxonomy for machine-distinguishable operational
events, plus the redaction helper every emitter shares. Events go to
the ``fie.operations`` logger as one JSON line per event — structured,
sortable, and free of secrets by construction (the emitter refuses
fields whose values carry DSNs; callers pass identifiers,
fingerprints and stable codes, never connection material).

Taxonomy (work order §9; BACKUP_*/RESTORE_* are emitted by
:mod:`phase3.persistence.pg_backup`, STARTUP/SHUTDOWN semantics are the
http transport's ``server_started``/``shutdown_signal``/``server_stopped``
events mapped here, readiness failures are emitted by the service
boundary's health op):

    STARTUP SHUTDOWN READINESS_FAILURE DATABASE_UNAVAILABLE
    SCHEMA_INCOMPATIBLE AUTH_FAILURE CONFIGURATION_INVALID
    BACKUP_STARTED BACKUP_COMPLETED BACKUP_FAILED
    RESTORE_STARTED RESTORE_COMPLETED RESTORE_FAILED

Secret requirements (§9): no plaintext password, full credential-bearing
DSN, bearer token, API secret, private key or real credential fixture
may appear in any emitted event. :func:`redact` is the shared mask
(``postgres://user:***@host``, ``password=***``); the redaction
regression test pins it (``tests/phase3/test_69b_r4_operations.py``).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

__all__ = [
    "OPERATIONS_LOGGER_NAME",
    "STARTUP",
    "SHUTDOWN",
    "READINESS_FAILURE",
    "DATABASE_UNAVAILABLE",
    "SCHEMA_INCOMPATIBLE",
    "AUTH_FAILURE",
    "CONFIGURATION_INVALID",
    "BACKUP_STARTED",
    "BACKUP_COMPLETED",
    "BACKUP_FAILED",
    "RESTORE_STARTED",
    "RESTORE_COMPLETED",
    "RESTORE_FAILED",
    "TAXONOMY",
    "emit",
    "redact",
]

OPERATIONS_LOGGER_NAME = "fie.operations"

def _operations_logger() -> logging.Logger:
    """The operations logger, ALWAYS at INFO.

    Events are operational verdicts (failures and transitions), not
    debug chatter — they must never be silently swallowed by an
    unset/WARNING effective level on the bare logger.
    """
    logger = logging.getLogger(OPERATIONS_LOGGER_NAME)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    return logger

STARTUP = "STARTUP"
SHUTDOWN = "SHUTDOWN"
READINESS_FAILURE = "READINESS_FAILURE"
DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
SCHEMA_INCOMPATIBLE = "SCHEMA_INCOMPATIBLE"
AUTH_FAILURE = "AUTH_FAILURE"
CONFIGURATION_INVALID = "CONFIGURATION_INVALID"
BACKUP_STARTED = "BACKUP_STARTED"
BACKUP_COMPLETED = "BACKUP_COMPLETED"
BACKUP_FAILED = "BACKUP_FAILED"
RESTORE_STARTED = "RESTORE_STARTED"
RESTORE_COMPLETED = "RESTORE_COMPLETED"
RESTORE_FAILED = "RESTORE_FAILED"

#: The full work-order §9 set — one place, machine-checkable.
TAXONOMY = frozenset({
    STARTUP, SHUTDOWN, READINESS_FAILURE, DATABASE_UNAVAILABLE,
    SCHEMA_INCOMPATIBLE, AUTH_FAILURE, CONFIGURATION_INVALID,
    BACKUP_STARTED, BACKUP_COMPLETED, BACKUP_FAILED,
    RESTORE_STARTED, RESTORE_COMPLETED, RESTORE_FAILED,
})

_CRED_BEARING_FIELDS = frozenset({
    "password", "token", "bearer", "secret", "dsn", "credential",
    "authorization", "api_key", "private_key", "pgpassword",
})


def emit(
    event: str,
    logger: logging.Logger | None = None,
    **fields: Any,
) -> str:
    """Emit one structured operational event line; returns the JSON.

    Fields whose NAME suggests credential content (``password``,
    ``token``, ``dsn``, …) are DROPPED, not trusted to the caller; every
    remaining string value is additionally passed through
    :func:`redact`. The contract does not rely on emitters remembering
    the rules.
    """
    safe: dict[str, Any] = {
        key: redact(value) if isinstance(value, str) else value
        for key, value in fields.items()
        if key.lower() not in _CRED_BEARING_FIELDS
    }
    line = json.dumps({"event": event, **safe}, sort_keys=True)
    (logger or _operations_logger()).info(line)
    return line


_URL_CRED_RE = re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s/]+(@)")
_KW_PASSWORD_RE = re.compile(r"(password\s*[=:]\s*)(\S+)", re.IGNORECASE)


def redact(text: str) -> str:
    """The shared credential mask for any operational log/detail text."""
    text = _URL_CRED_RE.sub(r"\1***\2", text)
    return _KW_PASSWORD_RE.sub(r"\1***", text)