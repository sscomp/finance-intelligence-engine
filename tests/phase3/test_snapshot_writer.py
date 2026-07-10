"""Tests for Phase 3B Task 3 Run 2A SnapshotWriter.

Scope is intentionally tight — eight targeted tests covering the
contract the writer must satisfy:

1. deterministic score_id
2. dry-run no rows
3. persist one row
4. repeated persist obeys append-only behavior
5. config_hash round-trip
6. evidence_signal_ids round-trip
7. notes/run_mode preserved
8. SnapshotWriter satisfies SnapshotSink Protocol

Design constraints (per Phase 3 master doc):
  - Reuse ScoreRepository / ScoreSnapshotRecord (no new persistence
    layer).
  - Append-only snapshots; no update / no delete.
  - Deterministic output (same inputs → same score_id).
  - No network, temp SQLite only.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any

from phase3.datamodel.scores import DimensionResult, ScoreBreakdown, WeightedFactor
from phase3.datamodel.scores_macro import (
    MACRO_ENTITY_ID,
    MACRO_ENTITY_TYPE,
    MACRO_SCORER_TYPE,
    MacroScore,
)
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.score_repo import ScoreRepository
from phase3.persistence.schema_v1 import build as build_v1
from phase3.persistence.sqlite import SQLiteStore
from phase3.pipeline.scoring_pipeline import SnapshotSink
from phase3.pipeline.snapshot_writer import SnapshotWriter, SnapshotWriterConfig


TS = datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
DATE_BUCKET = "2026-07-09"
CONFIG_HASH = "cfg-v1"
EVIDENCE = ("m-gdp", "m-cpi", "m-pmi")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_store() -> tuple[str, SQLiteStore]:
    fd, path = tempfile.mkstemp(prefix="snapshot_writer_test", suffix=".db")
    os.close(fd)
    store = SQLiteStore(path)
    MigrationManager(store, [build_v1()]).apply()
    return path, store


def _make_breakdown(
    *,
    score: float = 10.0,
    config_hash: str = CONFIG_HASH,
) -> ScoreBreakdown:
    """A real ScoreBreakdown with one populated dimension.

    We need at least one factor in a dimension so the inputs payload
    has something to round-trip; an empty dimension works too, but
    having data makes the tests stronger.
    """
    wf = WeightedFactor(
        name="gdp_yoy",
        raw_value=0.025,
        raw_unit="ratio",
        sub_score=15.0,
        sub_weight=1.0,
        signed_score=15.0,
        transformation="threshold",
        source="yfinance",
        source_ref="macro.gdp_yoy",
        evidence=[],
    )
    dim = DimensionResult(
        name="economic",
        score=15.0,
        sub_indicators=[],
        weight=0.2,
        confidence=0.8,
        factors=[wf],
        evidence=[],
    )
    return ScoreBreakdown(
        scorer_type=MACRO_SCORER_TYPE,
        entity_type=MACRO_ENTITY_TYPE,
        entity_id=MACRO_ENTITY_ID,
        score=score,
        confidence=0.8,
        dimensions=[dim],
        overall_evidence=[],
        timestamp=TS,
        config_hash=config_hash,
        valid_until=TS,
    )


def _make_score(
    *,
    score: float = 10.0,
    config_hash: str = CONFIG_HASH,
) -> MacroScore:
    return MacroScore(breakdown=_make_breakdown(score=score, config_hash=config_hash))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class DeterministicScoreIdTests(unittest.TestCase):
    """Test 1: deterministic score_id.

    Same scorer/entity/date/config/evidence/run_mode → same id.
    Different evidence or different config → different id.
    Different run_mode → different id (this is part of the contract;
    the run_mode is a column the writer must distinguish).
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.repo = ScoreRepository(self.store)
        self.writer = SnapshotWriter(self.repo, SnapshotWriterConfig(run_mode="live"))

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_deterministic_score_id(self) -> None:
        id_a = self.writer.compute_score_id(
            scorer_type="macro",
            entity_type="macro",
            entity_id="global",
            date_bucket=DATE_BUCKET,
            config_hash=CONFIG_HASH,
            evidence_signal_ids=EVIDENCE,
        )
        id_b = self.writer.compute_score_id(
            scorer_type="macro",
            entity_type="macro",
            entity_id="global",
            date_bucket=DATE_BUCKET,
            config_hash=CONFIG_HASH,
            evidence_signal_ids=EVIDENCE,
        )
        self.assertEqual(id_a, id_b)
        # 16-char hex (sha256 truncated)
        self.assertEqual(len(id_a), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in id_a))

    def test_deterministic_score_id_order_invariant(self) -> None:
        """Same set of evidence in different order → same id."""
        id_a = self.writer.compute_score_id(
            scorer_type="macro", entity_type="macro", entity_id="global",
            date_bucket=DATE_BUCKET, config_hash=CONFIG_HASH,
            evidence_signal_ids=("a", "b", "c"),
        )
        id_b = self.writer.compute_score_id(
            scorer_type="macro", entity_type="macro", entity_id="global",
            date_bucket=DATE_BUCKET, config_hash=CONFIG_HASH,
            evidence_signal_ids=("c", "a", "b"),
        )
        self.assertEqual(id_a, id_b)

    def test_deterministic_score_id_different_evidence(self) -> None:
        id_a = self.writer.compute_score_id(
            scorer_type="macro", entity_type="macro", entity_id="global",
            date_bucket=DATE_BUCKET, config_hash=CONFIG_HASH,
            evidence_signal_ids=("m-gdp", "m-cpi"),
        )
        id_b = self.writer.compute_score_id(
            scorer_type="macro", entity_type="macro", entity_id="global",
            date_bucket=DATE_BUCKET, config_hash=CONFIG_HASH,
            evidence_signal_ids=("m-gdp", "m-cpi", "m-pmi"),
        )
        self.assertNotEqual(id_a, id_b)

    def test_deterministic_score_id_run_mode_distinguishes(self) -> None:
        """Same inputs but different run_mode → different id."""
        writer_shadow = SnapshotWriter(
            self.repo, SnapshotWriterConfig(run_mode="shadow")
        )
        id_live = self.writer.compute_score_id(
            scorer_type="macro", entity_type="macro", entity_id="global",
            date_bucket=DATE_BUCKET, config_hash=CONFIG_HASH,
            evidence_signal_ids=EVIDENCE,
        )
        id_shadow = writer_shadow.compute_score_id(
            scorer_type="macro", entity_type="macro", entity_id="global",
            date_bucket=DATE_BUCKET, config_hash=CONFIG_HASH,
            evidence_signal_ids=EVIDENCE,
        )
        self.assertNotEqual(id_live, id_shadow)


class DryRunTests(unittest.TestCase):
    """Test 2: dry-run inserts no rows, returns None."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.repo = ScoreRepository(self.store)
        self.writer = SnapshotWriter(
            self.repo,
            SnapshotWriterConfig(run_mode="live", dry_run=True),
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_dry_run_returns_none_and_inserts_no_rows(self) -> None:
        before = self.repo.count()
        result = self.writer.write(
            score=_make_score(),
            evidence_signal_ids=EVIDENCE,
            notes="dry",
            config_hash=CONFIG_HASH,
        )
        after = self.repo.count()
        self.assertIsNone(result)
        self.assertEqual(after, before, "dry-run must not insert snapshots")


class PersistTests(unittest.TestCase):
    """Test 3: persist mode inserts one row and returns its id."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.repo = ScoreRepository(self.store)
        self.writer = SnapshotWriter(
            self.repo,
            SnapshotWriterConfig(run_mode="live", dry_run=False),
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_persist_one_row(self) -> None:
        before = self.repo.count()
        snap_id = self.writer.write(
            score=_make_score(),
            evidence_signal_ids=EVIDENCE,
            notes="persist",
            config_hash=CONFIG_HASH,
        )
        after = self.repo.count()
        self.assertEqual(after - before, 1)
        self.assertIsInstance(snap_id, int)
        assert snap_id is not None
        self.assertGreater(snap_id, 0)


class AppendOnlyTests(unittest.TestCase):
    """Test 4: repeated persist grows the audit trail (append-only).

    Two writes with the same inputs must produce two distinct rows —
    the writer must never collapse or update.
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.repo = ScoreRepository(self.store)
        self.writer = SnapshotWriter(
            self.repo,
            SnapshotWriterConfig(run_mode="live", dry_run=False),
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_repeated_persist_appends(self) -> None:
        id1 = self.writer.write(
            score=_make_score(score=5.0),
            evidence_signal_ids=EVIDENCE,
            notes="first",
            config_hash=CONFIG_HASH,
        )
        id2 = self.writer.write(
            score=_make_score(score=7.0),
            evidence_signal_ids=EVIDENCE,
            notes="second",
            config_hash=CONFIG_HASH,
        )
        self.assertIsNotNone(id1)
        self.assertIsNotNone(id2)
        assert id1 is not None
        assert id2 is not None
        self.assertNotEqual(id1, id2)
        self.assertEqual(self.repo.count(), 2)
        # Repo enforces append-only: update/delete must raise.
        with self.assertRaises(Exception):
            self.repo.update(id1, score=999.0)
        with self.assertRaises(Exception):
            self.repo.delete(id1)


class ConfigHashRoundTripTests(unittest.TestCase):
    """Test 5: config_hash is preserved on the row."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.repo = ScoreRepository(self.store)
        self.writer = SnapshotWriter(
            self.repo,
            SnapshotWriterConfig(run_mode="live", dry_run=False),
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_config_hash_round_trip(self) -> None:
        snap_id = self.writer.write(
            score=_make_score(),
            evidence_signal_ids=EVIDENCE,
            notes="cfg-test",
            config_hash=CONFIG_HASH,
        )
        record = self.repo.latest("macro", "macro", "global")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.snapshot_id, snap_id)
        # config_hash is stored under breakdown_json["config_hash"]
        self.assertEqual(record.breakdown["config_hash"], CONFIG_HASH)


class EvidenceRoundTripTests(unittest.TestCase):
    """Test 6: evidence_signal_ids are preserved on the row."""

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.repo = ScoreRepository(self.store)
        self.writer = SnapshotWriter(
            self.repo,
            SnapshotWriterConfig(run_mode="live", dry_run=False),
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_evidence_signal_ids_round_trip(self) -> None:
        self.writer.write(
            score=_make_score(),
            evidence_signal_ids=EVIDENCE,
            notes="ev",
            config_hash=CONFIG_HASH,
        )
        record = self.repo.latest("macro", "macro", "global")
        self.assertIsNotNone(record)
        assert record is not None
        # Writer de-duplicates and order-stabilizes; the persisted
        # list is sorted in score_id-compute order. The DTO keeps
        # evidence_signal_ids verbatim in the breakdown payload, so
        # the set is what matters.
        stored = record.breakdown["evidence_signal_ids"]
        self.assertEqual(set(stored), set(EVIDENCE))
        self.assertEqual(len(stored), len(set(stored)), "duplicates must be removed")


class NotesAndRunModeTests(unittest.TestCase):
    """Test 7: notes and run_mode are preserved on the row.

    Three sub-cases:
      * explicit notes → stored verbatim
      * no notes but run_id set → run_mode + run_id synthesized
      * run_mode "shadow" survives round-trip on notes too
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.repo = ScoreRepository(self.store)

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_explicit_notes_preserved(self) -> None:
        writer = SnapshotWriter(
            self.repo,
            SnapshotWriterConfig(run_mode="live", dry_run=False),
        )
        writer.write(
            score=_make_score(),
            evidence_signal_ids=EVIDENCE,
            notes="morning-brief-run",
            config_hash=CONFIG_HASH,
        )
        rec = self.repo.latest("macro", "macro", "global")
        assert rec is not None
        self.assertEqual(rec.notes, "morning-brief-run")

    def test_run_mode_synthesized_into_notes(self) -> None:
        writer = SnapshotWriter(
            self.repo,
            SnapshotWriterConfig(
                run_mode="shadow",
                run_id="2026-07-09T08:00Z",
                dry_run=False,
            ),
        )
        writer.write(
            score=_make_score(),
            evidence_signal_ids=EVIDENCE,
            notes="",
            config_hash=CONFIG_HASH,
        )
        rec = self.repo.latest("macro", "macro", "global")
        assert rec is not None
        # Empty notes ⇒ writer synthesizes "run_mode=… run_id=…"
        self.assertIn("run_mode=shadow", rec.notes)
        self.assertIn("run_id=2026-07-09T08:00Z", rec.notes)


class ProtocolComplianceTests(unittest.TestCase):
    """Test 8: SnapshotWriter satisfies the SnapshotSink Protocol.

    This is a structural check (runtime-checkable Protocol) plus a
    smoke test that the writer behaves correctly when fed through
    the Protocol-shaped call signature.
    """

    def setUp(self) -> None:
        self.path, self.store = _new_store()
        self.repo = ScoreRepository(self.store)
        self.writer = SnapshotWriter(
            self.repo,
            SnapshotWriterConfig(run_mode="live", dry_run=False),
        )

    def tearDown(self) -> None:
        os.unlink(self.path)

    def test_satisfies_snapshot_sink_protocol(self) -> None:
        # Protocol is runtime_checkable — isinstance check is the
        # standard way to assert compliance in our codebase.
        self.assertIsInstance(self.writer, SnapshotSink)

    def test_protocol_shaped_call_works(self) -> None:
        """Call write() through a SnapshotSink-typed reference."""
        sink: SnapshotSink = self.writer
        snap_id = sink.write(
            score=_make_score(),
            evidence_signal_ids=EVIDENCE,
            notes="protocol",
            config_hash=CONFIG_HASH,
        )
        self.assertIsInstance(snap_id, int)
        self.assertEqual(self.repo.count(), 1)


if __name__ == "__main__":
    unittest.main()
