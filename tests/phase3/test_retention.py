"""Tests for phase3.persistence.retention — explicit purge helpers.

We do not assert automatic retention. Each purge is a deliberate
caller action. score_snapshot has no delete helper by design (the
trigger blocks it).
"""
from __future__ import annotations

import os
import tempfile
import unittest

from phase3.datamodel.graph import EdgeType, GraphEdge, GraphNode, NodeType
from phase3.persistence import schema_v1
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.retention import (
    purge_graph_edges_for_node,
    purge_graph_node,
    purge_ingestion_errors_before,
    purge_signals_before,
    purge_signals_by_id,
)
from phase3.persistence.signal_repo import SignalRecord, SignalRepository
from phase3.persistence.sqlite import SQLiteStore


def _make_signal(signal_id: str, ingested_at: str) -> SignalRecord:
    return SignalRecord(
        signal_id=signal_id,
        entity_type="company",
        entity_id="2330",
        signal_type="institutional_flow",
        value=1.0,
        unit="zhang",
        direction="bullish",
        timestamp=ingested_at,
        date_bucket=ingested_at[:10],
        ingested_at=ingested_at,
    )


class _SchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db_path = os.path.join(self._td.name, "r.db")
        self.store = SQLiteStore(self.db_path)
        self.addCleanup(self.store.close)
        MigrationManager(self.store, [schema_v1.build()]).apply()
        self.sig_repo = SignalRepository(self.store)

    def _seed_signal_at(self, signal_id: str, ingested_at: str) -> None:
        """Insert a signal_log row with a controlled ``ingested_at``.

        The repo layer always overwrites ``ingested_at`` to the
        current time on upsert, so we cannot control it from
        ``SignalRepository.upsert`` — go through raw SQL.
        """
        self.sig_repo.upsert(
            SignalRecord(
                signal_id=signal_id,
                entity_type="company",
                entity_id="2330",
                signal_type="institutional_flow",
                value=1.0,
                unit="zhang",
                direction="bullish",
                timestamp=ingested_at,
                date_bucket=ingested_at[:10],
            )
        )
        # Backdate ingested_at.
        self.store.execute(
            "UPDATE signal_log SET ingested_at = ? WHERE signal_id = ?",
            (ingested_at, signal_id),
        )


class PurgeSignalsBeforeTests(_SchemaTestCase):
    def test_purges_older_rows(self) -> None:
        for sid, t in [
            ("old-1", "2026-01-01T00:00:00+00:00"),
            ("old-2", "2026-02-01T00:00:00+00:00"),
            ("new-1", "2026-07-01T00:00:00+00:00"),
            ("new-2", "2026-07-15T00:00:00+00:00"),
        ]:
            self._seed_signal_at(sid, t)
        n = purge_signals_before(self.store, "2026-06-01T00:00:00+00:00")
        self.assertEqual(n, 2)
        kept = {r.signal_id for r in self.sig_repo.query(limit=1000)}
        self.assertEqual(kept, {"new-1", "new-2"})

    def test_returns_zero_when_none_match(self) -> None:
        self._seed_signal_at("a", "2026-07-01T00:00:00+00:00")
        self.assertEqual(
            purge_signals_before(self.store, "2026-01-01T00:00:00+00:00"),
            0,
        )

    def test_accepts_datetime_object(self) -> None:
        from datetime import datetime, timezone
        self._seed_signal_at("a", "2026-01-01T00:00:00+00:00")
        n = purge_signals_before(
            self.store, datetime(2026, 6, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(n, 1)


class PurgeSignalsByIdTests(_SchemaTestCase):
    def test_purges_listed_ids(self) -> None:
        for sid in ("a", "b", "c"):
            self._seed_signal_at(sid, "2026-07-01T00:00:00+00:00")
        n = purge_signals_by_id(self.store, ["a", "c"])
        self.assertEqual(n, 2)
        kept = {r.signal_id for r in self.sig_repo.query(limit=1000)}
        self.assertEqual(kept, {"b"})

    def test_empty_list_is_noop(self) -> None:
        self._seed_signal_at("a", "2026-07-01T00:00:00+00:00")
        self.assertEqual(purge_signals_by_id(self.store, []), 0)
        self.assertEqual(self.sig_repo.count(), 1)


class PurgeGraphTests(_SchemaTestCase):
    def setUp(self) -> None:
        super().setUp()
        # We need a graph to operate on; seed nodes/edges directly via
        # the schema's table.
        from datetime import datetime, timezone
        for nid in ("a", "b", "c"):
            self.store.execute(
                "INSERT INTO graph_nodes(node_id, node_type, label, "
                "created_at, metadata_json, tags_json) "
                "VALUES (?, ?, ?, ?, '{}', '[]')",
                (nid, "company", nid,
                 datetime(2026, 7, 8, tzinfo=timezone.utc).isoformat()),
            )
        for f, t in [("a", "b"), ("b", "c"), ("a", "c")]:
            self.store.execute(
                "INSERT INTO graph_edges(edge_id, edge_type, "
                "from_node_id, to_node_id, weight, metadata_json, "
                "created_at) VALUES (?, ?, ?, ?, NULL, '{}', ?)",
                (f"e-{f}-{t}", "member_of", f, t,
                 "2026-07-08T00:00:00+00:00"),
            )

    def test_purge_edges_for_node(self) -> None:
        n = purge_graph_edges_for_node(self.store, "a")
        # Edges involving a: a→b, a→c.
        self.assertEqual(n, 2)
        cur = self.store.execute(
            "SELECT COUNT(*) FROM graph_edges"
        ).fetchone()
        self.assertEqual(cur[0], 1)
        # Node a is still present.
        cur = self.store.execute(
            "SELECT 1 FROM graph_nodes WHERE node_id='a'"
        ).fetchone()
        self.assertIsNotNone(cur)

    def test_purge_node_removes_node_and_edges(self) -> None:
        edges, nodes = purge_graph_node(self.store, "a")
        self.assertEqual(edges, 2)
        self.assertEqual(nodes, 1)
        self.assertIsNone(
            self.store.execute(
                "SELECT 1 FROM graph_nodes WHERE node_id='a'"
            ).fetchone()
        )


class PurgeIngestionErrorsTests(_SchemaTestCase):
    def test_purges_older_errors(self) -> None:
        for eid, t in [
            (1, "2026-01-01T00:00:00+00:00"),
            (2, "2026-02-01T00:00:00+00:00"),
            (3, "2026-07-15T00:00:00+00:00"),
        ]:
            self.store.execute(
                "INSERT INTO ingestion_errors(source, occurred_at, "
                "message) VALUES (?, ?, ?)",
                ("test", t, f"err{eid}"),
            )
        n = purge_ingestion_errors_before(
            self.store, "2026-06-01T00:00:00+00:00"
        )
        self.assertEqual(n, 2)
        cur = self.store.execute(
            "SELECT COUNT(*) FROM ingestion_errors"
        ).fetchone()
        self.assertEqual(cur[0], 1)


if __name__ == "__main__":
    unittest.main()
