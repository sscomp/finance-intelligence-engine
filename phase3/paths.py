"""Central runtime path configuration for FIE (Phase 6.1 portability).

Before Phase 6.1, runtime paths were hard-coded absolute paths from the
historical Ubuntu deployment (``/home/ubuntu/macro-report/...``). This module
replaces those with one explicit configuration boundary using the precedence:

    explicit override (CLI flag / function argument)
        > environment variable (FIE_*)
        > portable default derived from this repository's location

Environment variables
---------------------

``FIE_PROJECT_ROOT``
    Repository/project root. Default: the directory that contains ``phase3/``
    (auto-discovered from this file's location — never a username-specific
    home directory).
``FIE_DATA_DIR``
    Directory for runtime data: databases, logs. Default:
    ``<project_root>`` (preserves the legacy on-disk layout of
    ``macro_history.db`` and ``logs/`` sitting at the project root).
``FIE_CONFIG_DIR``
    Directory holding the root-level JSON configs (``taiwan50_config.json``,
    ``industry_config.json``) and the ``config/`` manifest tree.
    Default: ``<project_root>``.
``FIE_ARTIFACT_DIR``
    Pipeline export artifact directory. Default:
    ``<project_root>/metadata/reports/artifacts``.
``FIE_DB_PATH``
    Absolute path of the legacy production history database
    (``macro_history.db``). Default: ``<FIE_DATA_DIR>/macro_history.db``.
    Guarded by the existing ``macro_history.db`` path/basename guards — this
    variable is meant for pointing the *guard reference* at the real
    production file when it lies outside the project root, not for overriding
    ad-hoc test DB paths (use ``--db-path`` for that).

``FIE_DATABASE_URL`` (Phase 6.3)
    Persistence backend selector for the Phase 3B store. Unset or
    empty → SQLite at the portable default / ``--db-path`` path
    (unchanged Phase 6.1 behaviour). ``postgres(ql)://user:pass@host:port/db``
    → the PostgreSQL parity backend (psycopg3 DSN). URLs are never
    logged raw: use
    :func:`phase3.persistence.backend.sanitize_db_url` before printing.
    Precedence for the Phase 3B store: explicit API/CLI argument >
    ``FIE_DATABASE_URL`` > portable default (SQLite).
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "ENV_PROJECT_ROOT",
    "ENV_DATA_DIR",
    "ENV_CONFIG_DIR",
    "ENV_ARTIFACT_DIR",
    "ENV_DB_PATH",
    "ENV_DATABASE_URL",
    "project_root",
    "data_dir",
    "config_dir",
    "artifact_dir",
    "macro_history_db_path",
    "database_url",
]

ENV_PROJECT_ROOT = "FIE_PROJECT_ROOT"
ENV_DATA_DIR = "FIE_DATA_DIR"
ENV_CONFIG_DIR = "FIE_CONFIG_DIR"
ENV_ARTIFACT_DIR = "FIE_ARTIFACT_DIR"
ENV_DB_PATH = "FIE_DB_PATH"
ENV_DATABASE_URL = "FIE_DATABASE_URL"

def _discover_repo_root(start: Path) -> Path:
    """Discover the project root containing the ``phase3`` package.

    Walks up from the directory containing this module until a repository
    marker file (``pyproject.toml`` or ``README.md``) is found. In a source
    checkout this resolves to the checkout root; for an installed copy it
    avoids accidentally treating ``site-packages`` as the project root (the
    fallback is the package's parent directory).
    """
    here = start  # .../<project_root>/phase3
    for candidate in here.parents:
        if candidate.joinpath("pyproject.toml").is_file() or candidate.joinpath("README.md").is_file():
            return candidate
    return here.parent


def project_root() -> Path:
    """Return the FIE project root (``FIE_PROJECT_ROOT`` > repo location)."""
    return Path(os.environ.get(ENV_PROJECT_ROOT) or _discover_repo_root(Path(__file__).resolve().parent))


def data_dir() -> Path:
    """Return the runtime data directory (``FIE_DATA_DIR`` > project root)."""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override)
    return project_root()


def config_dir() -> Path:
    """Return the root-level config directory (``FIE_CONFIG_DIR`` > project root).

    Root-level JSON configs (``taiwan50_config.json``, ``industry_config.json``)
    live here, plus the ``config/`` manifest tree (``config/reports``,
    ``config/phase3``, ...).
    """
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override)
    return project_root()


def artifact_dir() -> Path:
    """Return the pipeline artifact directory (``FIE_ARTIFACT_DIR`` > default)."""
    override = os.environ.get(ENV_ARTIFACT_DIR)
    if override:
        return Path(override)
    return project_root() / "metadata" / "reports" / "artifacts"


def macro_history_db_spec() -> str:
    """Return the raw-layer specifier (``FIE_DB_PATH`` > data dir) VERBATIM.

    Unlike :func:`macro_history_db_path`, the override is NOT normalized
    through ``Path``: a ``postgres://`` / ``postgresql://`` override is a
    backend DSN, and ``Path`` would silently mangle it (``postgres://db``
    -> ``postgres:/db`` — a different, broken specifier). Callers that
    treat the value as a backend specifier (raw layer ``db.py``, seed
    bridge, cutover tooling) must use this function.
    """
    override = os.environ.get(ENV_DB_PATH)
    if override:
        return override
    return str(data_dir() / "macro_history.db")


def macro_history_db_path() -> Path:
    """Return the production history DB path (``FIE_DB_PATH`` > data dir).

    This is the *reference* production path used by guards, backfill source
    resolution and CLI ``--source-db`` defaults. It defaults to
    ``<FIE_DATA_DIR>/macro_history.db``, which mirrors the historical
    ``/home/ubuntu/macro-report/macro_history.db`` layout without hard-coding
    any user's home directory.
    """
    override = os.environ.get(ENV_DB_PATH)
    if override:
        return Path(override)
    return data_dir() / "macro_history.db"


def database_url() -> str | None:
    """Return the Phase 3B backend selector URL (``FIE_DATABASE_URL``).

    ``None`` when unset/empty — callers then use the portable SQLite
    default. Never log the returned value directly; PostgreSQL URLs
    may embed a password (mask with
    :func:`phase3.persistence.backend.sanitize_db_url`).
    """
    value = os.environ.get(ENV_DATABASE_URL)
    return value if value else None