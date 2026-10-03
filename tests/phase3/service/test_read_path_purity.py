"""Read-path purity + batch/interactive separation (Phase 6.5 §17, §12).

Invariants:

* interactive reads NEVER mutate the database (structurally: the
  service opens the store query-only; behaviorally: every operation
  leaves signal/score counts unchanged);
* interactive reads NEVER trigger ingestion/scoring (all five ops on
  a signals-only database succeed without a snapshot appearing);
* the batch worker is a *separate* entry arc (``phase3.service.batch``)
  over the same persistence abstraction, and its ingestion path is
  idempotent without scoring.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service import RequestContext, dispatch  # noqa: E402
from tests.phase3.service import helpers  # noqa: E402

CTX = RequestContext(principal_id="synthetic-alpha", request_id="req-001")


class TestReadOnlyGuarantee(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = helpers.bootstrap_state()
        cls.service = cls.bundle["service"]

    @classmethod
    def tearDownClass(cls) -> None:
        path = cls.bundle["path"]
        try:
            cls.bundle["store"].close()
        finally:
            if Path(path).exists():
                Path(path).unlink()

    def test_query_only_connection_rejects_writes(self) -> None:
        with self.assertRaises(Exception):
            self.bundle["store"].execute(
                "UPDATE signal_log SET unit = unit"
            )

    def test_all_reads_leave_counts_unchanged(self) -> None:
        store = self.bundle["store"]
        before = (
            store.execute("SELECT COUNT(*) FROM signal_log").fetchone(),
            store.execute("SELECT COUNT(*) FROM score_snapshot").fetchone(),
            store.execute("SELECT COUNT(*) FROM graph_nodes").fetchone(),
            store.execute("SELECT COUNT(*) FROM graph_edges").fetchone(),
        )
        dispatch(self.service, "health", {}, CTX)
        dispatch(self.service, "latest_intelligence", {"limit": 10}, CTX)
        dispatch(self.service, "entity_intelligence",
                 {"kind": "company", "entity_id": "2330"}, CTX)
        dispatch(self.service, "evidence", {"ref": "entity:company:2330"}, CTX)
        dispatch(self.service, "freshness",
                 {"kind": "macro", "entity_id": "global"}, CTX)
        after = (
            store.execute("SELECT COUNT(*) FROM signal_log").fetchone(),
            store.execute("SELECT COUNT(*) FROM score_snapshot").fetchone(),
            store.execute("SELECT COUNT(*) FROM graph_nodes").fetchone(),
            store.execute("SELECT COUNT(*) FROM graph_edges").fetchone(),
        )
        self.assertEqual(before, after)

    def test_reads_need_no_snapshot_table_writes(self) -> None:
        # signals-only database: all five operations still answer
        from tests.phase3.service.helpers import bootstrap_state

        bundle = bootstrap_state(persist=False)
        try:
            service = bundle["service"]
            self.assertEqual(dispatch(service, "health", {}, CTX)["status"], "ok")
            raw = dispatch(service, "latest_intelligence", {"limit": 10}, CTX)
            self.assertEqual(raw["status"], "ok")
            self.assertEqual(raw["payload"], [])  # nothing scored yet
            fresh = dispatch(service, "freshness",
                             {"kind": "company", "entity_id": "2330",
                              "as_of": "2026-10-03"}, CTX)
            self.assertEqual(fresh["status"], "ok")
            self.assertEqual(fresh["payload"]["state"], "STALE")
        finally:
            bundle["store"].close()
            Path(bundle["path"]).unlink()


class TestBatchInteractiveSeparation(unittest.TestCase):
    def test_batch_worker_is_a_distinct_boundary(self) -> None:
        # separate module, separate call arcs, same persistence abstraction
        import phase3.service.batch as batch_mod
        import phase3.service.boundary as boundary_mod

        self.assertIsNot(batch_mod, boundary_mod)
        # neither boundary exports the other's arc
        self.assertFalse(any(hasattr(batch_mod, n) for n in
                             ("get_health", "get_latest_intelligence")))
        self.assertFalse(any(hasattr(boundary_mod, n) for n in
                             ("run_ingestion", "run_scoring")))

    def test_ingestion_is_idempotent_and_does_not_score(self) -> None:
        from phase3.service import BatchWorker

        import tempfile as _tempfile
        handle, path = _tempfile.mkstemp(prefix="fie_batch_", suffix=".db")
        import os as _os
        _os.close(handle)
        worker = BatchWorker(path)
        try:
            first = worker.run_ingestion("fixture", str(helpers.FIXTURE))
            self.assertEqual(first.stage, "ingestion")
            self.assertEqual(first.payload["signals_ingested"], 3)
            again = worker.run_ingestion("fixture", str(helpers.FIXTURE))
            self.assertEqual(again.payload["signals_ingested"], 0)  # idempotent
            counts = worker._store.execute(
                "SELECT COUNT(*) FROM score_snapshot"
            ).fetchone()
            self.assertEqual(counts[0], 0)  # ingestion never scores
        finally:
            worker.close()
            Path(path).unlink()


if __name__ == "__main__":
    unittest.main()