"""Repository for the ``signal_log`` table.

This is intentionally a thin wrapper over SQL: every method maps 1:1
to a query the caller could write by hand. The wrapper exists to
centralize:

* JSON serialization for ``metadata`` and ``raw_payload``,
* timestamp normalization (ISO-8601 strings in the DB),
* idempotent upsert keyed on ``signal_id``.

Phase 3B Task 1 only ships the foundation. Production wiring
(connecting a real :class:`Signal` from a live adapter) is Task 2+.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from phase3.persistence.sqlite import SQLiteStore
from phase3.persistence.timeutil import utc_now_iso


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalRecord:
    """A row from ``signal_log``, hydrous form.

    Mirrors the table columns plus the JSON metadata payload. Callers
    that want the full :class:`~phase3.datamodel.signals.Signal`
    object can use :func:`to_signal` once Phase 3B wires the live
    adapter; for now this DTO is the public contract.
    """

    signal_id: str
    entity_type: str
    entity_id: str
    signal_type: str
    value: float
    unit: str
    direction: str
    timestamp: str
    date_bucket: str = ""
    source_id: str = ""
    source_type: str = ""
    ref: str = ""
    fetched_at: str | None = None
    fetch_id: str = ""
    schema_version: str = "3.0"
    metadata: dict[str, Any] | None = None
    raw_payload: dict[str, Any] | None = None
    ingested_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "signal_type": self.signal_type,
            "value": self.value,
            "unit": self.unit,
            "direction": self.direction,
            "timestamp": self.timestamp,
            "date_bucket": self.date_bucket,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "ref": self.ref,
            "fetched_at": self.fetched_at,
            "fetch_id": self.fetch_id,
            "schema_version": self.schema_version,
            "metadata": dict(self.metadata or {}),
            "raw_payload": dict(self.raw_payload or {}),
            "ingested_at": self.ingested_at,
        }


def _to_iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class SignalRepository:
    """CRUD-ish repository for ``signal_log``.

    Parameters
    ----------
    store:
        Open :class:`SQLiteStore`. The repo does not own its
        lifecycle.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    # ----- writes ----------------------------------------------------------

    def upsert(self, record: SignalRecord) -> bool:
        """Idempotent insert/update on ``signal_id``.

        Returns True if a new row was created, False if an existing
        row was updated.

        ``ingested_at`` is stamped here in the application layer (same
        format the SQLite DDL default produced), so the statement is
        dialect-neutral and runs unchanged on SQLite and PostgreSQL.
        """
        sql = """
        INSERT INTO signal_log (
            signal_id, entity_type, entity_id, signal_type, value, unit,
            direction, timestamp, date_bucket, source_id, source_type,
            ref, fetched_at, fetch_id, schema_version,
            metadata_json, raw_payload, ingested_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT(signal_id) DO UPDATE SET
            entity_type    = excluded.entity_type,
            entity_id      = excluded.entity_id,
            signal_type    = excluded.signal_type,
            value          = excluded.value,
            unit           = excluded.unit,
            direction      = excluded.direction,
            timestamp      = excluded.timestamp,
            date_bucket    = excluded.date_bucket,
            source_id      = excluded.source_id,
            source_type    = excluded.source_type,
            ref            = excluded.ref,
            fetched_at     = excluded.fetched_at,
            fetch_id       = excluded.fetch_id,
            schema_version = excluded.schema_version,
            metadata_json  = excluded.metadata_json,
            raw_payload    = excluded.raw_payload,
            ingested_at    = excluded.ingested_at
        """
        params = (
            record.signal_id,
            record.entity_type,
            record.entity_id,
            record.signal_type,
            record.value,
            record.unit,
            record.direction,
            _to_iso(record.timestamp) or "",
            record.date_bucket,
            record.source_id,
            record.source_type,
            record.ref,
            _to_iso(record.fetched_at),
            record.fetch_id,
            record.schema_version,
            json.dumps(record.metadata or {}, sort_keys=True),
            json.dumps(record.raw_payload or {}, sort_keys=True),
            utc_now_iso(),
        )
        with self._store.transaction():
            cur = self._store.execute(
                "SELECT 1 FROM signal_log WHERE signal_id = %s", (record.signal_id,)
            )
            existed = cur.fetchone() is not None
            self._store.execute(sql, params)
        return not existed

    def upsert_many(self, records: Iterable[SignalRecord]) -> int:
        """Bulk idempotent insert/update. Returns the count of new rows."""
        new_count = 0
        for r in records:
            if self.upsert(r):
                new_count += 1
        return new_count

    # ----- reads -----------------------------------------------------------

    def get(self, signal_id: str) -> SignalRecord | None:
        cur = self._store.execute(
            "SELECT * FROM signal_log WHERE signal_id = %s", (signal_id,)
        )
        row = cur.fetchone()
        return _row_to_record(row) if row is not None else None

    def query(
        self,
        entity_type: str | None = None,
        entity_id: str | None = None,
        signal_type: str | None = None,
        source_type: str | None = None,
        date_bucket: str | None = None,
        limit: int = 1000,
    ) -> list[SignalRecord]:
        """Filter by any combination of the indexed columns.

        All filters are AND. ``None`` means "any".
        """
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            clauses.append("entity_type = %s")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id = %s")
            params.append(entity_id)
        if signal_type is not None:
            clauses.append("signal_type = %s")
            params.append(signal_type)
        if source_type is not None:
            clauses.append("source_type = %s")
            params.append(source_type)
        if date_bucket is not None:
            clauses.append("date_bucket = %s")
            params.append(date_bucket)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM signal_log {where} ORDER BY timestamp DESC LIMIT %s"
        params.append(int(limit))
        rows = self._store.execute(sql, tuple(params)).fetchall()
        return [_row_to_record(r) for r in rows]

    def count(self) -> int:
        cur = self._store.execute("SELECT COUNT(*) FROM signal_log").fetchone()
        return int(cur[0]) if cur is not None else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_record(row: Any) -> SignalRecord:
    """Map a sqlite3.Row from ``signal_log`` to a :class:`SignalRecord`."""
    return SignalRecord(
        signal_id=str(row["signal_id"]),
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        signal_type=str(row["signal_type"]),
        value=float(row["value"]),
        unit=str(row["unit"]),
        direction=str(row["direction"]),
        timestamp=str(row["timestamp"]),
        date_bucket=str(row["date_bucket"]),
        source_id=str(row["source_id"]),
        source_type=str(row["source_type"]),
        ref=str(row["ref"]),
        fetched_at=str(row["fetched_at"]) if row["fetched_at"] is not None else None,
        fetch_id=str(row["fetch_id"]),
        schema_version=str(row["schema_version"]),
        metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        raw_payload=json.loads(row["raw_payload"]) if row["raw_payload"] else {},
        ingested_at=str(row["ingested_at"]) if row["ingested_at"] is not None else None,
    )


__all__ = ["SignalRecord", "SignalRepository"]
