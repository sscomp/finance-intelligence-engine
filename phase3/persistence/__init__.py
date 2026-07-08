"""Phase 3 persistence layer.

This package is the SQLite foundation for Phase 3B. It is intentionally
isolated from Phase 2 production code (which owns `db.py` and
`macro_history.db`).

Hard rule: a write path here MUST NEVER target `macro_history.db`.
``SQLiteStore`` rejects any resolved path whose absolute name equals
``macro_history.db`` regardless of working directory, symlink depth, or
relative-path tricks. This guard is the only line of defense that keeps
the new Phase 3B schema from contaminating the production database, so
do not weaken it without explicit orchestrator approval.
"""
from phase3.persistence.sqlite import (
    FORBIDDEN_DB_NAME,
    SQLiteStore,
    PathGuardError,
    TransactionError,
    integrity_check,
    quick_check,
)
from phase3.persistence.migrations import (
    Migration,
    MigrationError,
    MigrationManager,
)
from phase3.persistence import schema_v1

__all__ = [
    "FORBIDDEN_DB_NAME",
    "SQLiteStore",
    "PathGuardError",
    "TransactionError",
    "integrity_check",
    "quick_check",
    "Migration",
    "MigrationError",
    "MigrationManager",
    "schema_v1",
]
