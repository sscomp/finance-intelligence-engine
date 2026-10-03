"""Integration tests for the adapter → SignalRepository path.

This is a Python-level (not subprocess) integration test. It
verifies that:
  * each adapter's output is well-formed for persistence;
  * ``_signal_to_record`` produces a valid ``SignalRecord``;
  * ``SignalRepository.upsert_many`` correctly stores signals;
  * running the same adapt() twice yields updated (not new) on the
    second call (idempotency).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from phase3.cli import _signal_to_record
from phase3.persistence.signal_repo import SignalRecord, SignalRepository
from phase3.persistence.sqlite import SQLiteStore
from phase3.signals.adapters import (
    FixtureAdapter, MacroAdapter, RSSAdapter, T86Adapter, YFinanceAdapter,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "phase3" / "fixtures"


def _new_db() -> tuple[str, SQLiteStore]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Schema must be initialized via init-db. For a Python-only test we
    # use the migrations API directly (matches cmd_init_db).
    from phase3.persistence.migrations import MigrationManager
    from phase3.persistence.schema_v1 import build as build_v1
    store = SQLiteStore(path)
    MigrationManager(store, [build_v1()]).apply()
    return path, store


class SignalToRecordTests(unittest.TestCase):
    def test_basic_signal_to_record(self) -> None:
        adapter = YFinanceAdapter()
        sigs = adapter.adapt(str(FIXTURES / "yfinance_2330_2026-07-08.json"))
        rec = _signal_to_record(sigs[0])
        self.assertIsInstance(rec, SignalRecord)
        self.assertEqual(rec.signal_id, sigs[0].signal_id)
        self.assertEqual(rec.entity_id, sigs[0].entity_id)
        self.assertEqual(rec.value, sigs[0].value)


class AdapterPersistenceTests(unittest.TestCase):
    def test_yfinance_persists_correct_row_count(self) -> None:
        path, store = _new_db()
        try:
            adapter = YFinanceAdapter()
            sigs = adapter.adapt(str(FIXTURES / "yfinance_2330_2026-07-08.json"))
            repo = SignalRepository(store)
            n_new = repo.upsert_many([_signal_to_record(s) for s in sigs])
            self.assertEqual(n_new, 10)
            con = sqlite3.connect(path)
            count = con.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            con.close()
            self.assertEqual(count, 10)
        finally:
            store.close()
            os.unlink(path)

    def test_macro_persists_with_global_entity(self) -> None:
        path, store = _new_db()
        try:
            adapter = MacroAdapter()
            sigs = adapter.adapt(str(FIXTURES / "macro_daily_2026-07-08.json"))
            repo = SignalRepository(store)
            n_new = repo.upsert_many([_signal_to_record(s) for s in sigs])
            self.assertEqual(n_new, len(sigs))
            con = sqlite3.connect(path)
            # All signals should have entity_id='global'
            rows = con.execute(
                "SELECT DISTINCT entity_id FROM signal_log"
            ).fetchall()
            con.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "global")
        finally:
            store.close()
            os.unlink(path)

    def test_t86_persists_with_company_and_industry(self) -> None:
        path, store = _new_db()
        try:
            adapter = T86Adapter()
            sigs = adapter.adapt(str(FIXTURES / "t86_institutional_2026-07-08.json"))
            repo = SignalRepository(store)
            n_new = repo.upsert_many([_signal_to_record(s) for s in sigs])
            # SignalRepository.upsert_many de-duplicates by signal_id; the T86
            # fixture intentionally emits 16 signal objects that share 13 unique
            # ids, so n_new must equal the count of unique ids, not the raw
            # signal list length.
            unique_ids = {s.signal_id for s in sigs}
            self.assertEqual(n_new, len(unique_ids))
            con = sqlite3.connect(path)
            types = {row[0] for row in
                     con.execute("SELECT DISTINCT entity_type FROM signal_log")}
            con.close()
            self.assertIn("company", types)
            self.assertIn("industry", types)
        finally:
            store.close()
            os.unlink(path)

    def test_idempotent_rerun_upserts(self) -> None:
        path, store = _new_db()
        try:
            adapter = FixtureAdapter()
            sigs = adapter.adapt(str(FIXTURES / "fixture_company_industry_2026-07-08.json"))
            records = [_signal_to_record(s) for s in sigs]
            repo = SignalRepository(store)
            n_new1 = repo.upsert_many(records)
            n_new2 = repo.upsert_many(records)
            self.assertEqual(n_new1, len(records))
            self.assertEqual(n_new2, 0)  # all updates
            con = sqlite3.connect(path)
            count = con.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            con.close()
            self.assertEqual(count, len(records))
        finally:
            store.close()
            os.unlink(path)

    def test_rss_persists_with_company_industry_and_news(self) -> None:
        path, store = _new_db()
        try:
            adapter = RSSAdapter()
            sigs = adapter.adapt(str(FIXTURES / "cnyes_rss_headlines_2026-07-08.json"))
            self.assertGreater(len(sigs), 0)
            repo = SignalRepository(store)
            n_new = repo.upsert_many([_signal_to_record(s) for s in sigs])
            self.assertEqual(n_new, len(sigs))
            con = sqlite3.connect(path)
            types = {row[0] for row in
                     con.execute("SELECT DISTINCT entity_type FROM signal_log")}
            con.close()
            self.assertIn("company", types)
            self.assertIn("news", types)
        finally:
            store.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
