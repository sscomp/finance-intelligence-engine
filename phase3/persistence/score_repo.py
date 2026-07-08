"""Repository for the ``score_snapshot`` table (append-only).

Why append-only
---------------
Scores are an audit trail. A re-score with new inputs must produce a
*new* row, not overwrite history. The schema enforces this with
``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers; this repo exposes
the same contract at the API level by raising
:class:`ScoreSnapshotMutationError` on UPDATE/DELETE.

The trigger is the actual safety net. The repo's own check is a
nicer error for callers that try to call :meth:`update` (which
does not exist) or :meth:`delete` (which raises).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from phase3.persistence.sqlite import SQLiteStore, TransactionError


class ScoreSnapshotMutationError(RuntimeError):
    """Raised when a caller tries to mutate a frozen score row."""


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreSnapshotRecord:
    snapshot_id: int | None
    scorer: str
    entity_type: str
    entity_id: str
    score: float
    breakdown: dict[str, Any]
    inputs: dict[str, Any]
    notes: str = ""
    schema_version: str = "3.0"
    computed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "scorer": self.scorer,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "score": self.score,
            "breakdown": dict(self.breakdown),
            "inputs": dict(self.inputs),
            "notes": self.notes,
            "schema_version": self.schema_version,
            "computed_at": self.computed_at,
        }


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ScoreRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    # ----- writes ----------------------------------------------------------

    def append(self, record: ScoreSnapshotRecord) -> int:
        """Insert a new snapshot row. Returns the assigned ``snapshot_id``.

        ``snapshot_id`` on the input record is ignored — the DB
        assigns it.
        """
        sql = """
        INSERT INTO score_snapshot (
            scorer, entity_type, entity_id, score,
            breakdown_json, inputs_json, notes, schema_version, computed_at
        ) VALUES (
            :scorer, :entity_type, :entity_id, :score,
            :breakdown_json, :inputs_json, :notes, :schema_version,
            COALESCE(:computed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
        params = {
            "scorer": record.scorer,
            "entity_type": record.entity_type,
            "entity_id": record.entity_id,
            "score": record.score,
            "breakdown_json": json.dumps(record.breakdown, sort_keys=True),
            "inputs_json": json.dumps(record.inputs, sort_keys=True),
            "notes": record.notes,
            "schema_version": record.schema_version,
            "computed_at": record.computed_at,
        }
        with self._store.transaction():
            cur = self._store.execute(sql, params)
            last_id = cur.lastrowid
            assert last_id is not None  # always set for INSERT on INTEGER PK
            return int(last_id)

    # ----- forbidden mutations --------------------------------------------

    def delete(self, snapshot_id: int) -> None:  # pragma: no cover - tests cover path
        """Always raises :class:`ScoreSnapshotMutationError`.

        Exists so callers that reach for ``delete()`` get a
        domain-level error rather than a sqlite trigger error. The
        trigger is still the real safety net — this is a friendly
        refusal.
        """
        raise ScoreSnapshotMutationError(
            "score_snapshot is append-only: delete is not allowed. "
            "Append a new row instead."
        )

    def update(self, snapshot_id: int, **_: Any) -> None:  # pragma: no cover
        raise ScoreSnapshotMutationError(
            "score_snapshot is append-only: update is not allowed. "
            "Append a new row instead."
        )

    # ----- reads -----------------------------------------------------------

    def latest(
        self,
        scorer: str,
        entity_type: str,
        entity_id: str,
    ) -> ScoreSnapshotRecord | None:
        cur = self._store.execute(
            """
            SELECT * FROM score_snapshot
            WHERE scorer = ? AND entity_type = ? AND entity_id = ?
            ORDER BY computed_at DESC, snapshot_id DESC
            LIMIT 1
            """,
            (scorer, entity_type, entity_id),
        )
        row = cur.fetchone()
        return _row_to_record(row) if row is not None else None

    def history(
        self,
        scorer: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        since: str | datetime | None = None,
        limit: int = 1000,
    ) -> list[ScoreSnapshotRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if scorer is not None:
            clauses.append("scorer = ?")
            params.append(scorer)
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if since is not None:
            clauses.append("computed_at >= ?")
            params.append(_iso(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT * FROM score_snapshot {where}
        ORDER BY computed_at DESC, snapshot_id DESC
        LIMIT ?
        """
        params.append(int(limit))
        rows = self._store.execute(sql, tuple(params)).fetchall()
        return [_row_to_record(r) for r in rows]

    def count(self) -> int:
        cur = self._store.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()
        return int(cur[0]) if cur is not None else 0


# ---------------------------------------------------------------------------
# Trigger-probe helper (used by tests)
# ---------------------------------------------------------------------------


def attempt_raw_update(
    store: SQLiteStore,
    snapshot_id: int,
    new_score: float,
) -> None:
    """Try to UPDATE a score_snapshot row directly. Re-raises
    :class:`ScoreSnapshotMutationError` (or a TransactionError) so tests
    can assert that the trigger fires.

    Kept module-level (not a method) so it does not pollute the
    public repo API. Phase 3B only triggers this from the test
    suite; production code must go through :meth:`ScoreRepository.append`.
    """
    try:
        with store.transaction():
            store.execute(
                "UPDATE score_snapshot SET score = ? WHERE snapshot_id = ?",
                (new_score, snapshot_id),
            )
    except (sqlite3.IntegrityError, TransactionError) as exc:
        # sqlite3.IntegrityError("score_snapshot is append-only: UPDATE
        # rejected") is what the trigger raises. Wrap so the
        # boundary is consistent.
        raise ScoreSnapshotMutationError(str(exc)) from exc


def attempt_raw_delete(store: SQLiteStore, snapshot_id: int) -> None:
    try:
        with store.transaction():
            store.execute(
                "DELETE FROM score_snapshot WHERE snapshot_id = ?",
                (snapshot_id,),
            )
    except (sqlite3.IntegrityError, TransactionError) as exc:
        raise ScoreSnapshotMutationError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(value: str | datetime) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_record(row: Any) -> ScoreSnapshotRecord:
    return ScoreSnapshotRecord(
        snapshot_id=int(row["snapshot_id"]),
        scorer=str(row["scorer"]),
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        score=float(row["score"]),
        breakdown=json.loads(row["breakdown_json"]) if row["breakdown_json"] else {},
        inputs=json.loads(row["inputs_json"]) if row["inputs_json"] else {},
        notes=str(row["notes"]),
        schema_version=str(row["schema_version"]),
        computed_at=str(row["computed_at"]) if row["computed_at"] is not None else None,
    )


__all__ = [
    "ScoreRepository",
    "ScoreSnapshotRecord",
    "ScoreSnapshotMutationError",
    "attempt_raw_update",
    "attempt_raw_delete",
]
