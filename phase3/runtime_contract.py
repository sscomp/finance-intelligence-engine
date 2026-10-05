"""Canonical FIE database-target runtime contract (Phase 6.8A).

One authoritative resolver/validator used by the runtime, CLI, wrappers
(via the shell guard's stdin helper), tests, and operational tooling.
Doc: ABACUS_FIE_6_8A_RUNTIME_CONTRACT.md (repo root of the 6.8A WO).

Contract summary
----------------
* Backend classes are explicit — ``postgres*://`` DSN or an explicit
  SQLite target — and are NEVER inferred from CWD, import failure,
  missing environment variables, or an implicit default file.
* Targets are role-scoped. The four roles are the raw layer, the
  intelligence/store layer, the test/rehearsal target, and the explicit
  SQLite rollback target.
* The authoritative PRODUCTION identity set is resolved ONLY from the
  production contract files (``FIE_PRODUCTION_DB_CONTRACT`` override or
  the two default contract files). Wrappers/tests can never re-label
  their own environment as the production contract.
* Every production-capable resolution fails closed on: missing target,
  malformed target, unsupported backend, contradictory aliases, and
  (in rehearsal/test mode) production-equivalent or indeterminate
  targets.

Architecture decision (WO Task C): the DSN identity engine that used to
live in ``scripts/db_target_identity.py`` is owned here;
``scripts/db_target_identity.py`` is a thin stdin-protocol adapter for
the shell guard (bash wrappers invoke the guard before Python exists,
and re-implementing DSN parsing in shell is the duplication the WO
forbids). No secret/credential value is ever echoed by this module —
targets are rendered through :func:`sanitize_db_url`-style masking and
one-way fingerprints only.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import unquote, urlsplit

__all__ = [
    "BACKEND_SQLITE",
    "BACKEND_POSTGRES",
    "PG_URL_PREFIXES",
    "ROLE_RAW",
    "ROLE_INTELLIGENCE",
    "ROLE_TEST",
    "ROLE_ROLLBACK",
    "CANONICAL_ENV_VARS",
    "LEGACY_ALIAS_ROLES",
    "DEFAULT_CONTRACT_FILES",
    "REHEARSAL_SERVICE_ENVS",
    "PRODUCTION_SERVICE_ENVS",
    "Unparseable",
    "FailClosedTarget",
    "parse_postgres_dsn",
    "parse_target",
    "target_identity",
    "identity_fingerprint",
    "identity_text",
    "targets_equivalent",
    "classify_service_env",
    "canonical_env_var",
    "legacy_alias_env_vars",
    "resolve_role_target",
    "resolve_raw_target",
    "resolve_intelligence_target",
    "ProductionIdentity",
    "load_production_identities",
    "assert_rehearsal_target_safe",
    "sanitize_target",
]

BACKEND_SQLITE = "sqlite"
BACKEND_POSTGRES = "postgres"

PG_URL_PREFIXES = ("postgres://", "postgresql://")

# --- roles ---------------------------------------------------------------
ROLE_RAW = "raw"
ROLE_INTELLIGENCE = "intelligence"
ROLE_TEST = "test"
ROLE_ROLLBACK = "rollback"

CANONICAL_ENV_VARS = {
    ROLE_RAW: "FIE_DB_TARGET_RAW",
    ROLE_INTELLIGENCE: "FIE_DB_TARGET_INTELLIGENCE",
    ROLE_TEST: "FIE_DB_TARGET_TEST",
    ROLE_ROLLBACK: "FIE_DB_TARGET_ROLLBACK",
}

# Legacy aliases retain their 6.7B-era roles. FIE_DB_PATH carries the RAW
# DSN in the current production contract files (C-1 semantics, normalized
# here); FIE_DATABASE_URL / FIE_INTELLIGENCE_DB both alias the
# intelligence slot (C-2 semantics, normalized here) and conflict with
# each other only when their resolved identities differ (B5).
LEGACY_ALIAS_ROLES = {
    "FIE_DB_PATH": (ROLE_RAW,),
    "FIE_DATABASE_URL": (ROLE_INTELLIGENCE,),
    "FIE_INTELLIGENCE_DB": (ROLE_INTELLIGENCE,),
}

# Production identity contract files (authoritative; wrapper-order
# independent). Mirrors scripts/rehearsal_db_guard.sh's default set so the
# shell and Python layers share ONE authoritative set (B2).
DEFAULT_CONTRACT_FILES = (
    "/home/ubuntu/fie-67b-upgrade/fie-service.env",
    "/home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env",
)

REHEARSAL_SERVICE_ENVS = ("test", "staging")
PRODUCTION_SERVICE_ENVS = ("", "local", "production")

EXIT_GUARD = 78  # shared fail-closed exit class for shell boundaries


class Unparseable(Exception):
    """The text does not fit any supported PostgreSQL DSN form."""


class FailClosedTarget(Exception):
    """A target failed contract validation (fail closed).

    ``reason`` is a machine-readable class name; ``detail`` names only
    configuration classes/paths never target values (targets may embed
    credentials). ``exit_code`` is the shell-boundary exit code.
    """

    def __init__(self, reason: str, detail: str = "", exit_code: int = EXIT_GUARD):
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.exit_code = exit_code


# --- PostgreSQL DSN identity engine (moved from scripts/, unchanged) -----
_LOOPBACK_HOSTS = frozenset({
    "localhost", "::1", "0.0.0.0", "[::1]",
    "::ffff:127.0.0.1",
})

DEFAULT_PORT = "5432"


def _normalize_host(host: str) -> str:
    host = unquote(host).strip("[]").strip()
    if not host:
        raise Unparseable("empty host")
    if host in _LOOPBACK_HOSTS or host.startswith("127."):
        return "loopback"
    if host.startswith("/"):
        # unix-socket directory: identity is the canonical directory path
        return "socket:" + (host.rstrip("/") or "/")
    return host.lower()


def _parse_url_form(text: str) -> dict:
    if not text.lower().startswith(("postgres://", "postgresql://")):
        raise Unparseable("not a postgres URL")
    parts = urlsplit(text)
    params: dict = {}
    if parts.query:
        for chunk in parts.query.split("&"):
            if not chunk:
                continue
            key, _, value = chunk.partition("=")
            params[unquote(key).lower()] = unquote(value)
    userinfo, _, hostport = parts.netloc.rpartition("@")
    host = hostport
    port_from_url = ""
    if hostport.startswith("[") and "]" in hostport:  # IPv6 literal
        host, _, rest = hostport.rpartition("]")
        host = "[" + host[1:] + "]"
        port_from_url = rest[1:] if rest.startswith(":") else ""
    elif hostport.count(":") == 1:
        host, _, port_from_url = hostport.partition(":")
    elif hostport.count(":") > 1:
        # An unbracketed bare IPv6 literal does not fit the URL grammar;
        # mis-reading it would corrupt the host identity — fail closed.
        raise Unparseable("bare IPv6 host without brackets")
    dbname = unquote(parts.path)[1:] if parts.path else ""
    dbname = params.get("dbname") or params.get("database") or dbname
    host = params.get("host") or host
    port = params.get("port") or port_from_url or DEFAULT_PORT
    if not dbname:
        raise Unparseable("no database selected")
    if not host:
        raise Unparseable("no host or socket directory given")
    try:
        port = str(int(port))
    except ValueError:
        raise Unparseable("non-numeric port") from None
    return {
        "kind": "postgres",
        "host": _normalize_host(host),
        "port": port,
        "dbname": dbname,
    }


def _parse_keyword_form(text: str) -> dict:
    import shlex

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise Unparseable(f"bad keyword DSN ({exc})") from None
    if not tokens:
        raise Unparseable("empty DSN")
    pairs: dict = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep or not key or not value:
            raise Unparseable("not in key=value form")
        pairs[key.lower()] = value
    if not pairs:
        raise Unparseable("no recognized keys")
    if not any(key in pairs for key in ("host", "dbname", "database",
                                        "port")):
        raise Unparseable("no recognized postgres keys")
    dbname = pairs.get("dbname") or pairs.get("database") or ""
    host = pairs.get("host", "")
    port = pairs.get("port", DEFAULT_PORT)
    if not dbname:
        raise Unparseable("no database selected")
    if not host:
        raise Unparseable("no host or socket directory given")
    try:
        port = str(int(port))
    except ValueError:
        raise Unparseable("non-numeric port") from None
    return {
        "kind": "postgres",
        "host": _normalize_host(host),
        "port": port,
        "dbname": dbname,
    }


def parse_postgres_dsn(text: str) -> dict:
    """Parse a PostgreSQL DSN in URL or keyword form into its identity."""
    text = text.strip()
    if not text:
        raise Unparseable("empty")
    if text.lower().startswith(("postgres://", "postgresql://")):
        return _parse_url_form(text)
    if "=" in text and "://" not in text:
        return _parse_keyword_form(text)
    raise Unparseable("unrecognized DSN form")

def identity_text(identity: dict) -> str:
    return "{kind}@{host}@{port}@{dbname}".format(**identity)


def identity_fingerprint(identity: dict) -> str:
    return hashlib.sha256(identity_text(identity).encode()).hexdigest()[:16]


# --- explicit target parsing (B1: backend explicit, never inferred) ------

@dataclass(frozen=True)
class TargetSpec:
    """A resolved, validated database target."""

    backend: str                 # "postgres" | "sqlite"
    value: str                   # PG DSN verbatim, or SQLite path
    source: str                  # explicit | canonical_env | legacy_alias
    env_var: str                 # contributing variable/argument name

    def fingerprint(self) -> str:
        return target_identity(self).fingerprint

    def _replace_source(self, source: str, env_var: str) -> "TargetSpec":
        return TargetSpec(self.backend, self.value, source, env_var)


def parse_target(value: str, *, allow_relative: bool = False) -> TargetSpec:
    """Parse a target value into an explicit backend class.

    A SQLite path is only explicit when absolute (``:memory:`` is the one
    exception) or when written with the ``sqlite://`` scheme; relative
    paths and unknown schemes are rejected (fail closed) unless the
    caller explicitly opts into relative paths (read-only reference use).
    """
    if value is None or not str(value).strip():
        raise FailClosedTarget("FAIL_CLOSED_DB_TARGET_REQUIRED",
                               "empty target value")
    candidate = str(value).strip()
    lowered = candidate.lower()
    if lowered.startswith(PG_URL_PREFIXES):
        try:
            parse_postgres_dsn(candidate)
        except Unparseable as exc:
            raise FailClosedTarget(
                "FAIL_CLOSED_MALFORMED_DB_TARGET",
                f"unparseable PostgreSQL DSN ({exc}; value withheld)",
            ) from None
        return TargetSpec(BACKEND_POSTGRES, candidate, "explicit", "argument")
    if lowered.startswith("sqlite://"):
        path = candidate[len("sqlite://"):]
    elif lowered.startswith("sqlite:"):
        path = candidate[len("sqlite:"):]
    else:
        path = None
    if path is not None:
        if not path.startswith("/"):
            raise FailClosedTarget(
                "FAIL_CLOSED_MALFORMED_DB_TARGET",
                "sqlite:// target is a relative path; an absolute path is "
                "required (value withheld)",
            )
        return TargetSpec(BACKEND_SQLITE, path, "explicit", "argument")
    if candidate == ":memory:":
        return TargetSpec(BACKEND_SQLITE, ":memory:", "explicit", "argument")
    if lowered.split("://", 1)[0] in ("http", "https", "mysql", "oracle", "file", "duckdb"):
        raise FailClosedTarget("FAIL_CLOSED_MALFORMED_DB_TARGET",
                               "unsupported URL scheme (value withheld)")
    if "://" in lowered:
        raise FailClosedTarget("FAIL_CLOSED_MALFORMED_DB_TARGET",
                               "unsupported URL scheme (value withheld)")
    if candidate.startswith("/"):
        return TargetSpec(BACKEND_SQLITE, candidate, "explicit", "argument")
    if allow_relative:
        return TargetSpec(BACKEND_SQLITE, candidate, "explicit", "argument")
    raise FailClosedTarget(
        "FAIL_CLOSED_MALFORMED_DB_TARGET",
        "relative path; an explicit backend (postgres:// or sqlite://) or "
        "an absolute path is required (value withheld)",
    )


def target_identity(spec: TargetSpec) -> "Identity":
    """Normalized, secret-free identity of a resolved target."""
    if spec.backend == BACKEND_POSTGRES:
        identity = parse_postgres_dsn(spec.value)
    else:
        if spec.value == ":memory:":
            path = ":memory:"
        else:
            path = os.path.realpath(os.path.abspath(spec.value))
        identity = {"kind": "sqlite", "host": "file", "port": "-",
                    "dbname": path}
    return Identity(identity_text(identity),
                    identity_fingerprint(identity))


class Identity:
    """Identity text + one-way fingerprint (never credentials)."""

    __slots__ = ("text", "fingerprint")

    def __init__(self, text: str, fingerprint: str) -> None:
        self.text = text
        self.fingerprint = fingerprint


def targets_equivalent(a: TargetSpec, b: TargetSpec) -> bool:
    """Semantic identity equivalence (host aliases coalesce; files realpath).

    Cross-backend targets are NEVER equivalent (a SQLite path is not a
    PostgreSQL database even in name).
    """
    if a.backend != b.backend:
        return False
    return target_identity(a).text == target_identity(b).text


def sanitize_target(spec: TargetSpec) -> str:
    """Log-safe rendering (password masked; never emitted)."""
    import re as _re

    masked = _re.sub(r"(?::)([^:@/\s]+)(?=@)", ":***", spec.value)
    masked = _re.sub(r"(password\s*=\s*)(\S+)", r"\1***", masked, flags=_re.I)
    return masked


# --- execution mode (ADR-017, fail-closed classification) ---------------

def classify_service_env(value: object) -> str:
    """Classify FIE_SERVICE_ENV into 'rehearsal' | 'production' (B3)."""
    mode = str(value if value is not None else "").strip().lower()
    if mode in REHEARSAL_SERVICE_ENVS:
        return "rehearsal"
    if mode in PRODUCTION_SERVICE_ENVS:
        return "production"
    raise FailClosedTarget("FAIL_CLOSED_PRODUCTION_MODE_DECLARATION_REQUIRED",
                           "FIE_SERVICE_ENV has an invalid value (value "
                           "withheld)")


# --- role resolution with alias contradiction detection (B5/B6/B7) -------

def canonical_env_var(role: str) -> str:
    return CANONICAL_ENV_VARS[role]


def legacy_alias_env_vars(role: str) -> tuple:
    return tuple(v for v, roles in LEGACY_ALIAS_ROLES.items() if role in roles)


def resolve_role_target(
    role: str,
    explicit: str | None = None,
    env: Mapping[str, str] | None = None,
) -> TargetSpec:
    """Resolve one role's target: explicit > canonical var > legacy aliases.

    Multiple legacy aliases for the role are allowed only when their
    resolved targets are identity-equivalent — including against the
    canonical var when both are present: silently letting a canonical
    target override a contradictory legacy alias would hide a
    misnamed/mis-scoped variable exactly like the 2026-10-04 incident
    class (contradictory values fail closed). A missing target fails
    closed (B7: never a silent default).
    """
    env_vars = dict(os.environ if env is None else env)
    if explicit:
        return parse_target(explicit)._replace_source("explicit", "argument")
    canonical_value = env_vars.get(CANONICAL_ENV_VARS[role], "").strip()
    if canonical_value:
        canonical = parse_target(canonical_value)._replace_source(
            "canonical_env", CANONICAL_ENV_VARS[role])
        for alias in legacy_alias_env_vars(role):
            value = env_vars.get(alias, "").strip()
            if not value:
                continue
            alias_spec = _parse_target_for(alias, value)
            if not targets_equivalent(canonical, alias_spec):
                raise FailClosedTarget(
                    "FAIL_CLOSED_CONTRADICTORY_DB_TARGETS",
                    f"{CANONICAL_ENV_VARS[role]} and {alias} resolve to "
                    f"different targets for role {role!r} (fail closed; "
                    f"values withheld)",
                )
        return canonical
    values = []
    for alias in legacy_alias_env_vars(role):
        value = env_vars.get(alias, "").strip()
        if value:
            values.append((alias, value))
    if not values:
        raise FailClosedTarget(
            f"FAIL_CLOSED_DB_TARGET_REQUIRED",
            f"no target contract for role {role!r} "
            f"(canonical {CANONICAL_ENV_VARS[role]}, aliases "
            f"{', '.join(legacy_alias_env_vars(role)) or 'none'}; "
            f"no implicit default)",
        )
    specs = [(_name_var(alias, role), _parse_target_for(alias, value))
             for alias, value in values]
    first = specs[0][1]
    for alias, spec in specs[1:]:
        if not targets_equivalent(first, spec):
            raise FailClosedTarget(
                "FAIL_CLOSED_CONTRADICTORY_DB_TARGETS",
                f"aliases {specs[0][0]} and {alias} resolve to different "
                f"targets for role {role!r} (fail closed; values withheld)",
            )
    return specs[0][1]


def _name_var(alias: str, role: str) -> str:
    return alias


def _parse_target_for(alias: str, value: str) -> TargetSpec:
    spec = parse_target(value)
    return spec._replace_source("legacy_alias", alias)


def resolve_raw_target(explicit: str | None = None,
                       env: Mapping[str, str] | None = None) -> TargetSpec:
    """Raw-layer target resolution (db.py)."""
    return resolve_role_target(ROLE_RAW, explicit, env)


def resolve_intelligence_target(explicit: str | None = None,
                                env: Mapping[str, str] | None = None) -> TargetSpec:
    """Intelligence/store target resolution (backend.resolve_spec)."""
    return resolve_role_target(ROLE_INTELLIGENCE, explicit, env)


# --- authoritative production identity (B2/B3) --------------------------

@dataclass(frozen=True)
class ProductionIdentity:
    """The authoritative production target set from contract files."""

    fingerprints: frozenset
    postgres_identity_texts: frozenset
    sqlite_paths: frozenset          # canonical paths as written in contract
    sources: tuple


def load_production_identities(
    env: Mapping[str, str] | None = None,
    contract_files: Iterable[str] | None = None,
) -> ProductionIdentity:
    """Load production identities from the PRODUCTION contract files only.

    Never consults FIE_WRAPPER_ENV or other candidate-target variables.
    A missing/unreadable/DSN-free contract set is fail-closed for
    callers that must prove a target non-Production (unknown != safe).
    """
    env_vars = dict(os.environ if env is None else env)
    if contract_files is None:
        override = env_vars.get("FIE_PRODUCTION_DB_CONTRACT", "").strip()
        files = override.split() if override else list(DEFAULT_CONTRACT_FILES)
    else:
        files = list(contract_files)
    fingerprints: set = set()
    pg_texts: set = set()
    sqlite_paths: set = set()
    sources: list = []
    for f in files:
        if not os.path.isfile(f):
            continue
        values = _contract_file_values(f)
        sources.append(f)
        for role, value in values:
            try:
                spec = parse_target(value)
            except FailClosedTarget:
                continue  # non-DB variable in the contract file
            identity = target_identity(spec)
            fingerprints.add(identity.fingerprint)
            if spec.backend == BACKEND_POSTGRES:
                pg_texts.add(identity.text)
            else:
                sqlite_paths.add(spec.value if spec.value == ":memory:"
                                 else os.path.realpath(
                                     os.path.abspath(spec.value)))
    if not sources:
        raise FailClosedTarget(
            "FAIL_CLOSED_PRODUCTION_IDENTITY_UNAVAILABLE",
            "no production contract file could be read; cannot prove a "
            "target non-Production (unknown != safe; value withheld)",
        )
    return ProductionIdentity(
        fingerprints=frozenset(fingerprints),
        postgres_identity_texts=frozenset(pg_texts),
        sqlite_paths=frozenset(sqlite_paths),
        sources=tuple(sources),
    )


_ENV_CONTRACT_ROLE_KEYS = (
    ("FIE_DB_TARGET_RAW", ROLE_RAW),
    ("FIE_DB_PATH", ROLE_RAW),
    ("FIE_DB_TARGET_INTELLIGENCE", ROLE_INTELLIGENCE),
    ("FIE_DATABASE_URL", ROLE_INTELLIGENCE),
    ("FIE_INTELLIGENCE_DB", ROLE_INTELLIGENCE),
    ("FIE_DB_TARGET_TEST", ROLE_TEST),
    ("FIE_DB_TARGET_ROLLBACK", ROLE_ROLLBACK),
)


def _contract_file_values(path: str) -> list:
    """Read candidate target values from a contract env file (S3-safe).

    Values are parsed in-process here (the file is a trusted production
    contract source for THIS module); keys with secrets (anything
    matching PASSWORD/TOKEN/SECRET) are never returned.
    """
    values = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lstrip("export ").strip()
            if not value or "PASSWORD" in key.upper() \
                    or "TOKEN" in key.upper() or "SECRET" in key.upper():
                continue
            if not any(name == key for name, _ in _ENV_CONTRACT_ROLE_KEYS):
                continue
            value = value.strip().strip("'").strip('"')
            values.append((key, value))
    return values


def assert_rehearsal_target_safe(
    spec: TargetSpec,
    prod: ProductionIdentity | None = None,
    *,
    env: Mapping[str, str] | None = None,
    extra_sqlite_prod_paths: Iterable[str] = (),
) -> None:
    """Rehearsal boundary: refuse production-equivalent targets (F/E).

    Raises FailClosedTarget when an explicit test/rehearsal target
    resolves to the authoritative production identity (or when the
    production identity cannot be loaded — unknown != safe).
    """
    if prod is None:
        prod = load_production_identities(env)
    identity = target_identity(spec)
    if identity.fingerprint in prod.fingerprints:
        raise FailClosedTarget(
            "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED",
            f"target is Production-equivalent (identity fingerprint "
            f"{identity.fingerprint}; value withheld)",
        )
    if spec.backend == BACKEND_SQLITE:
        resolved = (spec.value if spec.value == ":memory:"
                    else os.path.realpath(os.path.abspath(spec.value)))
        for prod_path in list(prod.sqlite_paths) + list(extra_sqlite_prod_paths):
            if resolved == prod_path:
                raise FailClosedTarget(
                    "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED",
                    "target is Production-equivalent (canonical-path "
                    "equivalent; value withheld)",
                )
        if os.path.exists(resolved) and prod.sqlite_paths:
            cand_ino = os.stat(resolved)
            for prod_path in prod.sqlite_paths:
                if os.path.exists(prod_path):
                    prod_ino = os.stat(prod_path)
                    if (cand_ino.st_dev, cand_ino.st_ino) == \
                            (prod_ino.st_dev, prod_ino.st_ino):
                        raise FailClosedTarget(
                            "FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED",
                            "target is Production-equivalent "
                            "(same-device/inode equivalent; value withheld)",
                        )
