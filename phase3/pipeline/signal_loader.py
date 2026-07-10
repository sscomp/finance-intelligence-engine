"""SignalLoader  Phase 3B Task 3 data-access helper.

Loads ``Signal`` datamodel objects from the ``signal_log`` table through the
existing :class:`phase3.persistence.signal_repo.SignalRepository`. The loader is
read-only and keeps all filtering logic in Python so tests can point it at a
temporary SQLite database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from phase3.datamodel.signals import Signal, SignalSource
from phase3.persistence.signal_repo import SignalRecord, SignalRepository
from phase3.pipeline import LoadedSignals


@dataclass(frozen=True)
class SignalLoaderFilters:
    """User-friendly filtering options for the loader."""

    entity_type: str | None = None
    entity_id: str | None = None
    signal_types: Sequence[str] | None = None
    source_types: Sequence[str] | None = None
    date_bucket: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int | None = None


class SignalLoader:
    """Hydrates Signal datamodels from SignalRepository rows."""

    def __init__(self, repo: SignalRepository, source_hint: str = "") -> None:
        self._repo = repo
        self._source_hint = source_hint or "signal_log"

    def load(self, filters: SignalLoaderFilters | None = None) -> LoadedSignals:
        filters = filters or SignalLoaderFilters()
        records = self._query(filters)
        signals = [self._record_to_signal(r) for r in records]
        return LoadedSignals(
            signals=signals,
            source=self._source_hint,
            filters=self._filters_dict(filters),
        )

    # ------------------------------------------------------------------
    def _query(self, filters: SignalLoaderFilters) -> Iterable[SignalRecord]:
        # SignalRepository.query supports a subset of filters; we apply what it
        # knows about first and post-filter the rest.
        limit = filters.limit or 1000
        repo_records = self._repo.query(
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            signal_type=self._single_value(filters.signal_types),
            source_type=self._single_value(filters.source_types),
            date_bucket=filters.date_bucket,
            limit=limit,
        )
        filtered = [
            r for r in repo_records
            if self._record_matches(r, filters)
        ]
        filtered.sort(key=lambda r: (self._parse_dt(r.timestamp), r.signal_id))
        return filtered[:limit]

    @staticmethod
    def _single_value(values: Sequence[str] | None) -> str | None:
        if values and len(values) == 1:
            return values[0]
        return None

    def _record_matches(self, record: SignalRecord, filters: SignalLoaderFilters) -> bool:
        if filters.signal_types and record.signal_type not in filters.signal_types:
            return False
        if filters.source_types and record.source_type not in filters.source_types:
            return False
        ts = self._parse_dt(record.timestamp)
        if filters.since and ts < filters.since:
            return False
        if filters.until and ts > filters.until:
            return False
        return True

    @staticmethod
    def _parse_dt(value: str | None) -> datetime:
        if not value:
            return datetime.min
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.min

    @staticmethod
    def _record_to_signal(record: SignalRecord) -> Signal:
        return Signal(
            signal_id=record.signal_id,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            signal_type=record.signal_type,
            value=record.value,
            unit=record.unit,
            direction=record.direction,  # type: ignore[arg-type]
            timestamp=datetime.fromisoformat(record.timestamp),
            source=SignalSource(
                source_id=record.source_id,
                source_type=record.source_type,
                ref=record.ref,
                fetched_at=datetime.fromisoformat(record.fetched_at)
                if record.fetched_at
                else datetime.min,
                metadata=dict(record.metadata or {}),
            ),
            date_bucket=record.date_bucket,
            schema_version=record.schema_version,
            metadata=dict(record.metadata or {}),
        )

    @staticmethod
    def _filters_dict(filters: SignalLoaderFilters) -> dict[str, object]:
        return {
            "entity_type": filters.entity_type,
            "entity_id": filters.entity_id,
            "signal_types": list(filters.signal_types or []),
            "source_types": list(filters.source_types or []),
            "date_bucket": filters.date_bucket,
            "since": filters.since.isoformat() if filters.since else None,
            "until": filters.until.isoformat() if filters.until else None,
            "limit": filters.limit,
        }


__all__ = ["SignalLoader", "SignalLoaderFilters"]
