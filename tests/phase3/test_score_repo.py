"""Tests for phase3.persistence.score_repo — score_snapshot (append-only).

The append-only contract is enforced two ways:

* At the storage layer by ``BEFORE UPDATE`` and ``BEFORE DELETE``
  triggers on ``score_snapshot`` (schema_v1).
* At the repo layer by ``update()`` and ``delete()`` raising
  :class:`ScoreSnapshotMutationError` immediately.

Both layers are tested. We also confirm the in-process append path
works for two snapshots with the same ``(scorer, entity_type, entity_id)``
— the audit trail grows; nothing is overwritten.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from phase3.persistence import schema_v1
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.score_repo import (
    ScoreRepository,
    ScoreSnapshotMutationError,
    ScoreSnapshotRecord,
    attempt_raw_delete,
    attempt_raw_update,
)
from phase3.persistence.sqlite import SQLiteStore


def _make_record(**overrides) -> ScoreSnapshotRecord:
    base: dict = dict(
        snapshot_id=None,
        scorer="macro",
        entity_type="global",
        entity_id="macro",
        score=10.0,
        breakdown={"economic": 1.0, "rates": 2.0},
        inputs={"gdp_yoy": 0.03, "cpi_yoy": 0.02},
        notes="test",
        schema_version="3.0",
        computed_at="2026-07-08T12:00:00+00:00",
    )
    base.update(overrides)
    return ScoreSnapshotRecord(**base)


class _SchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db_path = os.path.join(self._td.name, "score.db")
        self.store = SQLiteStore(self.db_path)
        self.addCleanup(self.store.close)
        MigrationManager(self.store, [schema_v1.build()]).apply()
        self.repo = ScoreRepository(self.store)


class AppendTests(_SchemaTestCase):
    def test_append_returns_id(self) -> None:
        new_id = self.repo.append(_make_record())
        self.assertIsInstance(new_id, int)
        self.assertGreater(new_id, 0)

    def test_append_assigns_id_ignoring_input(self) -> None:
        new_id = self.repo.append(_make_record(snapshot_id=9999))
        # Input snapshot_id is ignored — DB-assigned id is what comes back.
        self.assertNotEqual(new_id, 9999)

    def test_two_appends_both_persist(self) -> None:
        id1 = self.repo.append(
            _make_record(score=5.0, computed_at="2026-07-08T10:00:00+00:00")
        )
        id2 = self.repo.append(
            _make_record(score=7.0, computed_at="2026-07-08T11:00:00+00:00")
        )
        self.assertNotEqual(id1, id2)
        self.assertEqual(self.repo.count(), 2)

    def test_breakdown_and_inputs_round_trip(self) -> None:
        bd = {"a": [1, 2, 3], "b": {"nested": True}}
        ins = {"x": 1, "y": "字"}
        self.repo.append(_make_record(breakdown=bd, inputs=ins))
        latest = self.repo.latest("macro", "global", "macro")
        self.assertIsNotNone(latest)
        assert latest is not None  # for type checkers
        self.assertEqual(latest.breakdown, bd)
        self.assertEqual(latest.inputs, ins)

    def test_empty_breakdown_inputs_default_to_empty_dict(self) -> None:
        self.repo.append(
            _make_record(breakdown={}, inputs={})
        )
        latest = self.repo.latest("macro", "global", "macro")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.breakdown, {})
        self.assertEqual(latest.inputs, {})


class LatestTests(_SchemaTestCase):
    def test_latest_picks_most_recent(self) -> None:
        self.repo.append(
            _make_record(score=1.0, computed_at="2026-07-08T10:00:00+00:00")
        )
        self.repo.append(
            _make_record(score=2.0, computed_at="2026-07-08T11:00:00+00:00")
        )
        latest = self.repo.latest("macro", "global", "macro")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.score, 2.0)

    def test_latest_missing_returns_none(self) -> None:
        self.assertIsNone(self.repo.latest("macro", "global", "macro"))

    def test_latest_filters_by_scorer(self) -> None:
        self.repo.append(_make_record(scorer="macro", score=1.0))
        self.repo.append(_make_record(scorer="industry", score=2.0))
        self.assertEqual(self.repo.latest("macro", "global", "macro").score, 1.0)
        self.assertEqual(self.repo.latest("industry", "global", "macro").score, 2.0)


class HistoryTests(_SchemaTestCase):
    def test_history_returns_all_for_triple(self) -> None:
        for i in range(3):
            self.repo.append(
                _make_record(score=float(i),
                             computed_at=f"2026-07-08T1{i}:00:00+00:00")
            )
        history = self.repo.history(
            scorer="macro", entity_type="global", entity_id="macro"
        )
        self.assertEqual(len(history), 3)

    def test_history_limit(self) -> None:
        for i in range(5):
            self.repo.append(
                _make_record(score=float(i),
                             computed_at=f"2026-07-08T1{i}:00:00+00:00")
            )
        self.assertEqual(len(self.repo.history(limit=2)), 2)

    def test_history_since(self) -> None:
        for i in range(5):
            self.repo.append(
                _make_record(score=float(i),
                             computed_at=f"2026-07-08T1{i}:00:00+00:00")
            )
        out = self.repo.history(since="2026-07-08T13:00:00+00:00")
        # Should only include the last two (T13 and T14).
        self.assertEqual(len(out), 2)


class AppendOnlyEnforcementTests(_SchemaTestCase):
    def test_repo_update_raises(self) -> None:
        with self.assertRaises(ScoreSnapshotMutationError):
            self.repo.update(1, score=99.0)

    def test_repo_delete_raises(self) -> None:
        with self.assertRaises(ScoreSnapshotMutationError):
            self.repo.delete(1)

    def test_trigger_blocks_raw_update(self) -> None:
        new_id = self.repo.append(_make_record())
        with self.assertRaises(ScoreSnapshotMutationError):
            attempt_raw_update(self.store, new_id, 99.0)
        # Row is unchanged.
        latest = self.repo.latest("macro", "global", "macro")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.score, 10.0)

    def test_trigger_blocks_raw_delete(self) -> None:
        new_id = self.repo.append(_make_record())
        with self.assertRaises(ScoreSnapshotMutationError):
            attempt_raw_delete(self.store, new_id)
        # Row is still present.
        self.assertEqual(self.repo.count(), 1)

    def test_failed_update_does_not_affect_existing_data(self) -> None:
        """Sanity: an attempt to UPDATE a score must not modify or
        delete any other row. The trigger raises *before* the UPDATE
        so no rows are affected."""
        first_id = self.repo.append(
            _make_record(score=10.0,
                         computed_at="2026-07-08T10:00:00+00:00")
        )
        second_id = self.repo.append(
            _make_record(score=20.0,
                         computed_at="2026-07-08T11:00:00+00:00")
        )
        # Try the forbidden UPDATE — it must raise and leave the row intact.
        with self.assertRaises(ScoreSnapshotMutationError):
            attempt_raw_update(self.store, first_id, 99.0)
        # Both rows still present, both unchanged.
        self.assertEqual(self.repo.count(), 2)
        latest = self.repo.latest("macro", "global", "macro")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.score, 20.0)
        # Direct SQL on the older row: still 10.0.
        cur = self.store.execute(
            "SELECT score FROM score_snapshot WHERE snapshot_id=?",
            (first_id,),
        )
        self.assertEqual(cur.fetchone()["score"], 10.0)


if __name__ == "__main__":
    unittest.main()
