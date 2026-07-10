from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from phase3.datamodel.signals import Signal, SignalSource
from phase3.persistence.signal_repo import SignalRecord, SignalRepository
from phase3.persistence.sqlite import SQLiteStore
from phase3.pipeline.signal_loader import SignalLoader, SignalLoaderFilters


REPO_ROOT = Path("/home/ubuntu/macro-report")


def _new_store() -> tuple[str, SQLiteStore]:
    fd, path = tempfile.mkstemp(prefix="signal_loader_test", suffix=".db")
    os.close(fd)
    from phase3.persistence.migrations import MigrationManager
    from phase3.persistence.schema_v1 import build as build_v1

    store = SQLiteStore(path)
    MigrationManager(store, [build_v1()]).apply()
    return path, store


def _make_signal_record(
    *,
    signal_id: str,
    entity_type: str,
    entity_id: str,
    signal_type: str,
    value: float,
    timestamp: datetime,
    source_type: str,
    date_bucket: str,
) -> SignalRecord:
    ts_iso = timestamp.isoformat()
    return SignalRecord(
        signal_id=signal_id,
        entity_type=entity_type,
        entity_id=entity_id,
        signal_type=signal_type,
        value=value,
        unit="ratio",
        direction="bullish",
        timestamp=ts_iso,
        date_bucket=date_bucket,
        source_id=f"{source_type}-{entity_id}-{date_bucket}",
        source_type=source_type,
        ref="fixture",
        fetched_at=ts_iso,
        fetch_id="",
        schema_version="3.0",
        metadata={"raw": value},
        raw_payload={},
        ingested_at=ts_iso,
    )


def _seed_records(repo: SignalRepository, records: list[SignalRecord]) -> None:
    repo.upsert_many(records)


class SignalLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path, self.store = _new_store()
        self.repo = SignalRepository(self.store)

    def tearDown(self) -> None:
        self.store.close()
        os.unlink(self.db_path)

    def test_loads_all_signals_and_preserves_order(self) -> None:
        ts = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)
        records = [
            _make_signal_record(
                signal_id=f"sig-{i}",
                entity_type="company",
                entity_id="2330",
                signal_type="pe_ratio" if i % 2 == 0 else "roe",
                value=float(i),
                timestamp=ts + timedelta(minutes=i),
                source_type="yfinance",
                date_bucket="2026-07-08",
            )
            for i in range(5)
        ]
        _seed_records(self.repo, records)

        loader = SignalLoader(self.repo, source_hint=self.db_path)
        loaded = loader.load()
        self.assertEqual(len(loaded.signals), 5)
        self.assertEqual([s.signal_id for s in loaded.signals], [f"sig-{i}" for i in range(5)])
        self.assertEqual(loaded.source, self.db_path)

    def test_filters_by_entity_and_signal_type(self) -> None:
        ts = datetime(2026, 7, 8, tzinfo=timezone.utc)
        records = [
            _make_signal_record(
                signal_id="sig-1",
                entity_type="company",
                entity_id="2330",
                signal_type="pe_ratio",
                value=20.0,
                timestamp=ts,
                source_type="yfinance",
                date_bucket="2026-07-08",
            ),
            _make_signal_record(
                signal_id="sig-2",
                entity_type="company",
                entity_id="2317",
                signal_type="roe",
                value=0.2,
                timestamp=ts,
                source_type="yfinance",
                date_bucket="2026-07-08",
            ),
        ]
        _seed_records(self.repo, records)

        loader = SignalLoader(self.repo)
        filters = SignalLoaderFilters(entity_id="2330", signal_types=["pe_ratio"])
        loaded = loader.load(filters)
        self.assertEqual(len(loaded.signals), 1)
        self.assertEqual(loaded.signals[0].signal_id, "sig-1")

    def test_filters_by_source_type_and_date_range(self) -> None:
        ts = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)
        records = [
            _make_signal_record(
                signal_id="sig-old",
                entity_type="company",
                entity_id="2330",
                signal_type="pe_ratio",
                value=15.0,
                timestamp=ts - timedelta(days=5),
                source_type="yfinance",
                date_bucket="2026-07-03",
            ),
            _make_signal_record(
                signal_id="sig-new",
                entity_type="company",
                entity_id="2330",
                signal_type="pe_ratio",
                value=18.0,
                timestamp=ts,
                source_type="yfinance",
                date_bucket="2026-07-08",
            ),
            _make_signal_record(
                signal_id="sig-rss",
                entity_type="company",
                entity_id="2330",
                signal_type="news_headline",
                value=1.0,
                timestamp=ts,
                source_type="rss",
                date_bucket="2026-07-08",
            ),
        ]
        _seed_records(self.repo, records)
        loader = SignalLoader(self.repo)
        filters = SignalLoaderFilters(
            entity_id="2330",
            source_types=["yfinance"],
            since=ts - timedelta(days=1),
            until=ts + timedelta(hours=1),
        )
        loaded = loader.load(filters)
        self.assertEqual([sig.signal_id for sig in loaded.signals], ["sig-new"])

    def test_respects_limit(self) -> None:
        ts = datetime(2026, 7, 8, tzinfo=timezone.utc)
        records = [
            _make_signal_record(
                signal_id=f"sig-{i}",
                entity_type="company",
                entity_id="2330",
                signal_type="pe_ratio",
                value=float(i),
                timestamp=ts + timedelta(minutes=i),
                source_type="yfinance",
                date_bucket="2026-07-08",
            )
            for i in range(20)
        ]
        _seed_records(self.repo, records)
        loader = SignalLoader(self.repo)
        loaded = loader.load(SignalLoaderFilters(limit=5))
        self.assertEqual(len(loaded.signals), 5)


if __name__ == "__main__":
    unittest.main()
