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


def _record_from_timestamp_string(
    *,
    signal_id: str,
    timestamp_iso: str,
    entity_type: str = "company",
    entity_id: str = "2330",
) -> SignalRecord:
    """Build a SignalRecord from a raw timestamp string.

    Unlike :func:`_make_signal_record` (which formats an aware datetime),
    this accepts arbitrary ISO strings so tests can seed the legacy
    shapes the store actually contains — including NAIVE stamps written
    before the Phase 6.1 timezone policy. ``fetched_at`` stays valid so
    only the value timestamp under test varies.
    """
    base = _make_signal_record(
        signal_id=signal_id,
        entity_type=entity_type,
        entity_id=entity_id,
        signal_type="pe_ratio",
        value=1.0,
        timestamp=datetime(2026, 7, 8, tzinfo=timezone.utc),
        source_type="yfinance",
        date_bucket="2026-07-08",
    )
    return SignalRecord(
        signal_id=signal_id,
        entity_type=base.entity_type,
        entity_id=base.entity_id,
        signal_type=base.signal_type,
        value=base.value,
        unit=base.unit,
        direction=base.direction,
        timestamp=timestamp_iso,
        date_bucket=base.date_bucket,
        source_id=base.source_id,
        source_type=base.source_type,
        ref=base.ref,
        fetched_at=base.fetched_at,
        fetch_id="",
        schema_version=base.schema_version,
        metadata=base.metadata,
        raw_payload={},
        ingested_at=base.ingested_at,
    )


class SignalLoaderTimezonePolicyTests(unittest.TestCase):
    """Phase 6.1 Workstream E — canonical timestamp policy tests.

    Policy: timestamps compare as timezone-aware UTC internally. Naive
    inputs (legacy signal_log rows, bare date strings) are interpreted
    as UTC. These tests pin the loader boundary behavior that fixed the
    ``can't compare offset-naive and offset-aware datetimes`` crash in
    the mixed-era store sort (signal_loader.py ``_query``).
    """

    def setUp(self) -> None:
        self.db_path, self.store = _new_store()
        self.repo = SignalRepository(self.store)

    def tearDown(self) -> None:
        self.store.close()
        os.unlink(self.db_path)

    def _load(self) -> list[Signal]:
        return list(SignalLoader(self.repo).load().signals)

    def test_mixed_naive_and_aware_rows_sort_without_crash(self) -> None:
        """Regression: naive 'T00:00:00' rows + aware '+00:00' rows used
        to raise ``can't compare offset-naive and offset-aware datetimes``
        during the loader sort (the industry/company pipeline crash)."""
        _seed_records(self.repo, [
            # aware row (written by seed_signals.py convention)
            _record_from_timestamp_string(
                signal_id="sig-aware", timestamp_iso="2026-07-08T00:00:00+00:00"),
            # naive row (legacy t86 _parse_date output)
            _record_from_timestamp_string(
                signal_id="sig-naive", timestamp_iso="2026-07-08T00:00:00"),
        ])
        loaded = self._load()
        self.assertEqual(
            [s.signal_id for s in loaded], ["sig-aware", "sig-naive"])

    def test_naive_row_hydrated_as_aware_utc(self) -> None:
        _seed_records(self.repo, [
            _record_from_timestamp_string(
                signal_id="sig-naive", timestamp_iso="2026-07-08T00:00:00"),
        ])
        sig = self._load()[0]
        self.assertIsNotNone(sig.timestamp.tzinfo)
        self.assertEqual(sig.timestamp.utcoffset(), timedelta(0))
        self.assertEqual(sig.timestamp, datetime(2026, 7, 8, tzinfo=timezone.utc))

    def test_aware_utc_row_hydrated_as_aware_utc(self) -> None:
        _seed_records(self.repo, [
            _record_from_timestamp_string(
                signal_id="sig-z", timestamp_iso="2026-07-08T00:00:00+00:00"),
        ])
        sig = self._load()[0]
        self.assertIsNotNone(sig.timestamp.tzinfo)
        self.assertEqual(sig.timestamp, datetime(2026, 7, 8, tzinfo=timezone.utc))

    def test_aware_non_utc_row_normalized_to_utc_instant(self) -> None:
        _seed_records(self.repo, [
            # 2026-07-08 08:00 +08:00 == 2026-07-08 00:00 UTC
            _record_from_timestamp_string(
                signal_id="sig-tpe", timestamp_iso="2026-07-08T08:00:00+08:00"),
        ])
        sig = self._load()[0]
        self.assertIsNotNone(sig.timestamp.tzinfo)
        self.assertEqual(sig.timestamp, datetime(2026, 7, 8, tzinfo=timezone.utc))

    def test_equivalent_instants_in_different_zones_compare_equal(self) -> None:
        _seed_records(self.repo, [
            _record_from_timestamp_string(
                signal_id="sig-utc", timestamp_iso="2026-07-08T00:00:00+00:00"),
            _record_from_timestamp_string(
                signal_id="sig-tpe", timestamp_iso="2026-07-08T08:00:00+08:00"),
        ])
        a, b = self._load()
        self.assertEqual(a.timestamp, b.timestamp)  # same instant
        # Ties fall back to signal_id ordering — the sort must be total.
        self.assertEqual([a.signal_id, b.signal_id], ["sig-tpe", "sig-utc"])

    def test_since_until_filters_accept_naive_bounds_as_utc(self) -> None:
        _seed_records(self.repo, [
            _record_from_timestamp_string(
                signal_id="sig-in", timestamp_iso="2026-07-08T00:00:00+00:00"),
        ])
        loader = SignalLoader(self.repo)
        # Naive bound equal to the aware row's instant must be inclusive.
        loaded = loader.load(SignalLoaderFilters(
            since=datetime(2026, 7, 8)))
        self.assertEqual([s.signal_id for s in loaded.signals], ["sig-in"])
        loaded = loader.load(SignalLoaderFilters(
            until=datetime(2026, 7, 8)))
        self.assertEqual([s.signal_id for s in loaded.signals], ["sig-in"])
        # Aware bound one second later must exclude.
        loaded = loader.load(SignalLoaderFilters(
            since=datetime(2026, 7, 8, 0, 0, 1, tzinfo=timezone.utc)))
        self.assertEqual(loaded.signals, [])

    def test_unparsable_and_missing_timestamps_sort_as_aware_min(self) -> None:
        _seed_records(self.repo, [
            _record_from_timestamp_string(
                signal_id="sig-good", timestamp_iso="2026-07-08T00:00:00+00:00"),
            _record_from_timestamp_string(
                signal_id="sig-bad", timestamp_iso="not-a-date"),
        ])
        loaded = self._load()
        self.assertEqual(loaded[0].signal_id, "sig-bad")  # unparsable sorts first
        self.assertEqual(loaded[0].timestamp, datetime.min.replace(
            tzinfo=timezone.utc))

    def test_serialization_round_trip_preserves_instant(self) -> None:
        _seed_records(self.repo, [
            _record_from_timestamp_string(
                signal_id="sig-tpe", timestamp_iso="2026-07-08T08:00:00+08:00"),
        ])
        sig = self._load()[0]
        data = sig.to_dict()
        again = Signal.from_dict(data)
        self.assertEqual(again.timestamp, sig.timestamp)
        self.assertEqual(again.timestamp.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
