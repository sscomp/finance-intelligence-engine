"""Tests for phase3.persistence.signal_repo — signal_log CRUD.

Contract:
* ``upsert()`` is idempotent on ``signal_id`` and returns True the
  first time, False on conflict (matches the production
  dispatcher-side "new vs update" semantics).
* ``query()`` filters by the indexed columns and respects ``limit``.
* ``metadata`` and ``raw_payload`` round-trip through JSON.
* ``count()`` tracks the row count correctly across upserts.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from phase3.persistence import schema_v1
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.signal_repo import SignalRecord, SignalRepository
from phase3.persistence.sqlite import SQLiteStore


def _make_record(signal_id: str = "sig-1", **overrides) -> SignalRecord:
    base: dict = dict(
        signal_id=signal_id,
        entity_type="company",
        entity_id="2330",
        signal_type="institutional_flow",
        value=1500.0,
        unit="zhang",
        direction="bullish",
        timestamp="2026-07-08T00:00:00+00:00",
        date_bucket="2026-07-08",
        source_id="t86-2330-foreign",
        source_type="t86",
        ref="t86",
        fetched_at="2026-07-08T01:00:00+00:00",
        fetch_id="f-1",
        schema_version="3.0",
        metadata={"evidence_id": "ev-1", "note": "buy"},
        raw_payload={"raw": 42, "nested": {"k": "v"}},
    )
    base.update(overrides)
    return SignalRecord(**base)


class _SchemaTestCase(unittest.TestCase):
    """Base class that opens a temp DB with schema_v1 applied."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db_path = os.path.join(self._td.name, "test.db")
        self.store = SQLiteStore(self.db_path)
        self.addCleanup(self.store.close)
        MigrationManager(self.store, [schema_v1.build()]).apply()
        self.repo = SignalRepository(self.store)


class UpsertTests(_SchemaTestCase):
    def test_first_upsert_creates(self) -> None:
        created = self.repo.upsert(_make_record())
        self.assertTrue(created)
        self.assertEqual(self.repo.count(), 1)

    def test_second_upsert_updates(self) -> None:
        self.repo.upsert(_make_record())
        created_again = self.repo.upsert(
            _make_record(value=2500.0, metadata={"note": "buy more"}),
        )
        self.assertFalse(created_again)
        self.assertEqual(self.repo.count(), 1)
        fetched = self.repo.get("sig-1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.value, 2500.0)
        self.assertEqual(fetched.metadata["note"], "buy more")

    def test_upsert_many_returns_new_count(self) -> None:
        created = self.repo.upsert_many([
            _make_record(signal_id="a"),
            _make_record(signal_id="b"),
            # Re-upsert of a — should NOT count.
            _make_record(signal_id="a", value=99.0),
        ])
        self.assertEqual(created, 2)
        self.assertEqual(self.repo.count(), 2)

    def test_metadata_json_round_trips(self) -> None:
        md = {"list": [1, 2, 3], "nested": {"k": "v"}, "u": "台"}
        self.repo.upsert(_make_record(metadata=md))
        fetched = self.repo.get("sig-1")
        self.assertEqual(fetched.metadata, md)

    def test_raw_payload_json_round_trips(self) -> None:
        rp = {"a": 1, "b": [True, False, None], "台": True}
        self.repo.upsert(_make_record(raw_payload=rp))
        fetched = self.repo.get("sig-1")
        self.assertEqual(fetched.raw_payload, rp)

    def test_empty_metadata_is_serialised_as_empty_dict(self) -> None:
        rec = SignalRecord(
            signal_id="empty",
            entity_type="company",
            entity_id="2330",
            signal_type="institutional_flow",
            value=1.0,
            unit="x",
            direction="bullish",
            timestamp="2026-07-08T00:00:00+00:00",
            metadata=None,
            raw_payload=None,
        )
        self.repo.upsert(rec)
        fetched = self.repo.get("empty")
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.metadata, {})
        self.assertEqual(fetched.raw_payload, {})

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(self.repo.get("nope"))


class QueryTests(_SchemaTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Seed three rows with different filter values.
        for sid, et, eid, st, val in [
            ("s1", "company", "2330", "institutional_flow", 1.0),
            ("s2", "company", "2330", "news_headline", 0.5),
            ("s3", "industry", "半導體", "institutional_flow", 2.0),
        ]:
            self.repo.upsert(
                _make_record(
                    signal_id=sid, entity_type=et, entity_id=eid,
                    signal_type=st, value=val,
                )
            )

    def test_query_by_entity(self) -> None:
        out = self.repo.query(entity_type="company", entity_id="2330")
        ids = {r.signal_id for r in out}
        self.assertEqual(ids, {"s1", "s2"})

    def test_query_by_signal_type(self) -> None:
        out = self.repo.query(signal_type="institutional_flow")
        ids = {r.signal_id for r in out}
        self.assertEqual(ids, {"s1", "s3"})

    def test_query_combined_filters(self) -> None:
        out = self.repo.query(
            entity_type="company",
            signal_type="institutional_flow",
        )
        self.assertEqual([r.signal_id for r in out], ["s1"])

    def test_query_no_match_returns_empty(self) -> None:
        self.assertEqual(self.repo.query(entity_id="9999"), [])

    def test_query_limit(self) -> None:
        # 3 rows total; limit=2 should return 2.
        out = self.repo.query(limit=2)
        self.assertEqual(len(out), 2)

    def test_query_returns_ordered_by_timestamp_desc(self) -> None:
        # Re-insert s2 with a future timestamp — should come first.
        self.repo.upsert(
            _make_record(
                signal_id="s2", entity_type="company", entity_id="2330",
                signal_type="news_headline", value=0.5,
                timestamp="2027-01-01T00:00:00+00:00",
            )
        )
        out = self.repo.query()
        self.assertEqual(out[0].signal_id, "s2")

    def test_count(self) -> None:
        self.assertEqual(self.repo.count(), 3)


class _RecordPersistenceTests(_SchemaTestCase):
    """Round-trip every column of SignalRecord to ensure no field is
    silently dropped.

    Note: ``ingested_at`` is auto-set by the DB on first insert; we
    verify the *shape* of the round-trip (no field is dropped) by
    comparing everything except the auto-generated timestamp.
    """

    _AUTO_FIELDS: tuple[str, ...] = ("ingested_at",)

    def test_all_fields_round_trip(self) -> None:
        rec = _make_record(
            signal_id="full",
            date_bucket="2026-07-08",
            source_id="s-1",
            source_type="t86",
            ref="ref-x",
            fetched_at="2026-07-08T01:00:00+00:00",
            fetch_id="f-1",
            schema_version="3.0",
            metadata={"k": "v"},
            raw_payload={"p": 1},
        )
        self.repo.upsert(rec)
        got = self.repo.get("full")
        self.assertIsNotNone(got)
        assert got is not None
        d = got.to_dict()
        expected = rec.to_dict()
        for k, v in expected.items():
            if k in self._AUTO_FIELDS:
                # Field is auto-populated by the DB on insert.
                self.assertIsNotNone(d[k], msg=f"field {k} was not set")
                continue
            self.assertEqual(d[k], v, msg=f"field {k} mismatch")

if __name__ == "__main__":
    unittest.main()
