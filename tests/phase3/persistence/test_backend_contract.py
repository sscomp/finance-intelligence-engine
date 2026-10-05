"""Backend contract tests — Phase 3B persistence on SQLite AND PostgreSQL.

Phase 6.3: the same contract suite runs against both backends. The
SQLite cases always run (stdlib, hermetic). The PostgreSQL cases run
against a **disposable** local cluster when one is reachable;
otherwise they SKIP with an explicit reason (the PostgreSQL backend
is an optional dependency and no cloud database is provisioned in
this phase — Phase 6.2 ADR-C03).

PG target resolution
--------------------
``FIE_TEST_PG_DSN`` (env) if set, else the disposable local default:

    postgresql://fie@/fie_contract?host=/tmp/fie-pg&port=54329

(created by the Phase 6.3 disposable-cluster script; trust-auth,
user-level, /tmp only, no service installation). Never point these
tests at a production, shared, or abacus-claw database.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.persistence.backend import open_store, resolve_spec  # noqa: E402
from phase3.persistence.graph_repo import EdgeRow, NodeRow  # noqa: E402
from phase3.persistence.migrations import (  # noqa: E402
    MigrationManager,
    default_migrations_for,
)
from phase3.persistence.signal_repo import SignalRecord, SignalRepository  # noqa: E402
from phase3.persistence.score_repo import (  # noqa: E402
    ScoreRepository,
    ScoreSnapshotMutationError,
    ScoreSnapshotRecord,
    attempt_raw_delete,
    attempt_raw_update,
)

PG_DSN = os.environ.get(
    "FIE_TEST_PG_DSN",
    "postgresql://fie@/fie_contract?host=/tmp/fie-pg&port=54329",
)

# ---------------------------------------------------------------------------
# Shared contract mixin (backend-neutral)
# ---------------------------------------------------------------------------


class ContractMixin:
    """Every test here runs unchanged on SQLite and PostgreSQL."""

    # -- helpers ------------------------------------------------------------
    def sig(self, sid: str, **kw) -> SignalRecord:
        defaults = dict(
            signal_id=sid,
            entity_type="macro",
            entity_id="us10y",
            signal_type="us10y",
            value=4.2,
            unit="%",
            direction="up",
            timestamp="2026-07-08T00:00:00Z",
        )
        defaults.update(kw)
        return SignalRecord(**defaults)

    def snap(self, scorer="macro", etype="macro", eid="twn", score=62.5, **kw):
        return ScoreSnapshotRecord(
            snapshot_id=None, scorer=scorer, entity_type=etype,
            entity_id=eid, score=score,
            breakdown=kw.get("breakdown", {"gross": 0.4, "yield": 0.6}),
            inputs=kw.get("inputs", {"us10y": 4.2}),
            notes=kw.get("notes", ""),
            computed_at=kw.get("computed_at"),
        )

    # -- migrations ---------------------------------------------------------
    def test_migrations_fresh_apply_replay_and_version(self):
        mgr = MigrationManager(self.store, default_migrations_for(self.store))
        mgr.apply()
        self.assertEqual(mgr.current_version(), 1)
        # replay is a no-op
        applied2 = mgr.apply()
        self.assertEqual([m.version for m in applied2], [])
        self.assertEqual(mgr.current_version(), 1)
        self.assertEqual(MigrationManager(self.store, default_migrations_for(self.store)).current_version(), 1)

    def test_registry_checksum_tamper_is_rejected(self):
        mgr = MigrationManager(self.store, default_migrations_for(self.store))
        mgr.apply()
        # Simulate tampering: overwrite the stored checksum.
        with self.store.transaction():
            self.store.execute(
                "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = %s",
                (1,),
            )
        mgr2 = MigrationManager(self.store, default_migrations_for(self.store))
        with self.assertRaises(Exception):
            mgr2.apply()

    # -- signals ------------------------------------------------------------
    def test_signal_upsert_insert_then_update_and_roundtrip(self):
        repo = SignalRepository(self.store)
        self.assertTrue(repo.upsert(self.sig("s1", value=4.2)))
        self.assertFalse(repo.upsert(self.sig("s1", value=4.4)))
        self.assertEqual(repo.count(), 1)
        rec = repo.get("s1")
        self.assertEqual(rec.value, 4.4)
        self.assertEqual(rec.metadata, {})
        # JSON payload round-trip (dict + bool keep types)
        self.assertTrue(repo.upsert(self.sig("s2", metadata={"k": True, "n": [1, 2]})))
        rec2 = repo.get("s2")
        self.assertEqual(rec2.metadata, {"k": True, "n": [1, 2]})
        # ingested_at is the app-layer stamp: ISO + 'Z', 3 fractional digits
        self.assertRegex(rec2.ingested_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_signal_query_filters_and_ordering(self):
        repo = SignalRepository(self.store)
        repo.upsert_many([
            self.sig("a", entity_id="x", timestamp="2026-07-01T00:00:00Z"),
            self.sig("b", entity_id="y", timestamp="2026-07-02T00:00:00Z"),
            self.sig("c", entity_id="x", source_type="rss", timestamp="2026-07-03T00:00:00Z"),
        ])
        self.assertEqual({r.signal_id for r in repo.query(entity_id="x")}, {"a", "c"})
        self.assertEqual(len(repo.query(source_type="rss")), 1)
        # ordering: timestamp DESC
        self.assertEqual(
            [r.signal_id for r in repo.query()], ["c", "b", "a"]
        )
        self.assertEqual([r.signal_id for r in repo.query(limit=2)], ["c", "b"])
        self.assertEqual(repo.query(entity_id="missing"), [])

    def test_signal_null_fetched_at_survives_roundtrip(self):
        repo = SignalRepository(self.store)
        repo.upsert(self.sig("s9", fetched_at=None))
        self.assertIsNone(repo.get("s9").fetched_at)

    # -- scores -------------------------------------------------------------
    def test_score_append_returning_monotonic_and_latest(self):
        repo = ScoreRepository(self.store)
        id1 = repo.append(self.snap(score=10.0, computed_at="2026-07-08T05:00:00Z"))
        id2 = repo.append(self.snap(score=11.0, computed_at="2026-07-09T05:00:00Z"))
        id3 = repo.append(self.snap(score=12.0, computed_at="2026-07-10T05:00:00Z"))
        self.assertTrue(0 < id1 < id2 < id3)
        latest = repo.latest("macro", "macro", "twn")
        self.assertEqual(latest.snapshot_id, id3)
        self.assertEqual(latest.score, 12.0)
        # breakdown JSON round-trip
        self.assertEqual(latest.breakdown, {"gross": 0.4, "yield": 0.6})
        hist = repo.history()
        self.assertEqual([r.snapshot_id for r in hist], [id3, id2, id1])
        self.assertEqual([r.score for r in repo.history(since="2026-07-09")], [12.0, 11.0])

    def test_score_computed_at_defaults_to_app_stamp(self):
        repo = ScoreRepository(self.store)
        repo.append(self.snap(computed_at=None))
        rec = repo.history()[0]
        self.assertRegex(rec.computed_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_score_append_only_enforcement(self):
        repo = ScoreRepository(self.store)
        snap_id = repo.append(self.snap())
        with self.assertRaises(ScoreSnapshotMutationError):
            attempt_raw_update(self.store, snap_id, 1.0)
        with self.assertRaises(ScoreSnapshotMutationError):
            attempt_raw_delete(self.store, snap_id)
        # The rejected mutation left the data intact.
        self.assertEqual(repo.latest("macro", "macro", "twn").score, 62.5)

    def test_score_tiebreak_by_snapshot_id(self):
        """Same computed_at: the later snapshot_id wins the tiebreak."""
        repo = ScoreRepository(self.store)
        repo.append(self.snap(score=1.0, computed_at="2026-07-08T05:00:00Z"))
        repo.append(self.snap(score=2.0, computed_at="2026-07-08T05:00:00Z"))
        self.assertEqual(repo.latest("macro", "macro", "twn").score, 2.0)

    # -- graph --------------------------------------------------------------
    def node(self, nid: str, **kw) -> NodeRow:
        return NodeRow(
            node_id=nid,
            node_type=kw.get("node_type", "score"),
            label=kw.get("label", nid),
            created_at=kw.get("created_at", "2026-07-08T04:00:00Z"),
            metadata=kw.get("metadata", {"m": 1}),
            tags=kw.get("tags", ["t"]),
        )

    def edge(self, eid: str, frm: str, to: str, **kw) -> EdgeRow:
        return EdgeRow(
            edge_id=eid,
            edge_type=kw.get("edge_type", "influences"),
            from_node_id=frm,
            to_node_id=to,
            weight=kw.get("weight", 0.5),
            metadata=kw.get("metadata", {}),
            created_at=kw.get("created_at", "2026-07-08T04:00:00Z"),
        )

    def test_graph_node_upsert_and_roundtrip(self):
        repo = self.graph_repo()
        self.assertTrue(repo.upsert_node(self.node("n1")))
        self.assertFalse(repo.upsert_node(self.node("n1", label="new")))
        node = repo.get_node("n1")
        self.assertEqual((node.label, node.metadata, node.tags), ("new", {"m": 1}, ["t"]))
        self.assertEqual(repo.node_count(), 1)
        self.assertEqual([n.node_id for n in repo.list_nodes(node_type="score")], ["n1"])
        self.assertEqual(repo.list_nodes(node_type="nope"), [])

    def test_graph_edge_folds_on_triple_and_updates_by_id(self):
        repo = self.graph_repo()
        self.assertTrue(repo.upsert_edge(self.edge("e1", "n1", "n2", weight=0.5)))
        # same id → update path
        self.assertFalse(repo.upsert_edge(self.edge("e1", "n1", "n2", weight=0.7)))
        e = repo.get_edge("e1")
        self.assertEqual(e.weight, 0.7)
        # different id, same logical triple → folded into the existing row
        self.assertFalse(repo.upsert_edge(self.edge("e2", "n1", "n2", weight=0.9)))
        self.assertEqual(repo.edge_count(), 1)
        folds = repo.list_edges()
        self.assertEqual(len(folds), 1)
        self.assertEqual(folds[0].edge_id, "e1")
        self.assertEqual(folds[0].weight, 0.9)
        self.assertEqual(repo.get_edge("e2"), None)

    def test_graph_batch_reads_match_pointwise_reads(self):
        repo = self.graph_repo()
        for i in range(6):
            repo.upsert_node(self.node(f"n{i}"))
        repo.upsert_edge(self.edge("ea", "n0", "n2"))
        repo.upsert_edge(self.edge("eb", "n1", "n2"))
        repo.upsert_edge(self.edge("ec", "n3", "n5"))
        repo.upsert_edge(self.edge("ed", "n0", "n4"))
        # 'both' returns exactly the edges touching either input id
        # (an edge with both endpoints in the set is returned by both
        # direction passes — same as the pointwise from+to union).
        batch_ids = [e.edge_id for e in repo.fetch_edges_by_nodes(["n0", "n2"], direction="both")]
        self.assertEqual(sorted(batch_ids), ["ea", "ea", "eb", "ed"])
        # ordering inside the batch is edge_id-sorted per direction pass
        from_pass = [e.edge_id for e in repo.fetch_edges_by_nodes(["n0", "n2"], direction="from")]
        self.assertEqual(from_pass, ["ea", "ed"])
        to_pass = [e.edge_id for e in repo.fetch_edges_by_nodes(["n0", "n2"], direction="to")]
        self.assertEqual(to_pass, ["ea", "eb"])
        nodes = repo.fetch_nodes_by_ids(["n0", "missing", "n1"])
        self.assertEqual([n.node_id for n in nodes], ["n0", "n1"])
        self.assertEqual(repo.fetch_nodes_by_ids([]), [])

    def test_graph_clear(self):
        repo = self.graph_repo()
        repo.upsert_node(self.node("n1"))
        repo.upsert_edge(self.edge("e1", "n1", "n1"))
        repo.clear()
        self.assertEqual((repo.node_count(), repo.edge_count()), (0, 0))

    # -- transactions -------------------------------------------------------
    def test_transaction_rolls_back_on_error(self):
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO signal_log (signal_id, entity_type, entity_id, "
                "signal_type, value, unit, direction, timestamp, ingested_at) "
                "VALUES (%s, 'm', 'm', 't', 1.0, 'pct', 'up', 'T', %s)",
                ("rollback_me", "2026-07-08T00:00:00.000Z"),
            )
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                pass  # simulate a caught-but-persisted... no transaction here
        # (body completed normally after catching; row commits)
        from phase3.persistence.signal_repo import SignalRepository as SR
        self.assertEqual(SR(self.store).count(), 1)
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.execute(
                    "INSERT INTO signal_log (signal_id, entity_type, entity_id, "
                    "signal_type, value, unit, direction, timestamp, ingested_at) "
                    "VALUES (%s, 'm', 'm', 't', 1.0, 'pct', 'up', 'T', %s)",
                    ("gone", "2026-07-08T00:00:00.000Z"),
                )
                raise RuntimeError("abort")
        self.assertEqual(SR(self.store).count(), 1)  # 'gone' rolled back

    def test_read_only_mode_blocks_writes(self):
        self.store.set_query_only()
        with self.assertRaises(Exception):
            self.store.execute(
                "INSERT INTO signal_log (signal_id) VALUES (%s)", ("nope",)
            )


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------


class SQLiteContractTests(ContractMixin, unittest.TestCase):
    """Contract on the SQLite backend (stdlib, always runs)."""

    def test_sqlite_scheme_normalization_forms(self) -> None:
        # Phase 6.7B-R1 defect FIE-R1-001: scheme stripping must never
        # invent a different path. Pin the documented forms
        # (sqlite:///abs/path -> abs/path) — the former regex silently
        # rewrote the ABSOLUTE form into a CWD-relative path.
        # Phase 6.8A: relative forms no longer normalize to a CWD walk —
        # they fail closed (absolute path required).
        import os as _os

        from phase3.runtime_contract import FailClosedTarget

        cases = [
            ("sqlite:///abs/db", "/abs/db"),
            ("sqlite:/abs/db", "/abs/db"),
            ("SQLITE:///abs/db", "/abs/db"),  # case-insensitive scheme
            ("/abs/plain", "/abs/plain"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                saved = _os.environ.get("FIE_DATABASE_URL")
                _os.environ["FIE_DATABASE_URL"] = value
                try:
                    spec = resolve_spec()
                    self.assertEqual(spec.backend, "sqlite")
                    self.assertEqual(spec.dsn, expected)
                finally:
                    if saved is None:
                        _os.environ.pop("FIE_DATABASE_URL", None)
                    else:
                        _os.environ["FIE_DATABASE_URL"] = saved
        for relative in ("sqlite:relative/db", "relative/plain.db"):
            with self.subTest(relative=relative):
                saved = _os.environ.get("FIE_DATABASE_URL")
                _os.environ["FIE_DATABASE_URL"] = relative
                try:
                    with self.assertRaises(FailClosedTarget) as cm:
                        resolve_spec()
                    self.assertEqual(
                        cm.exception.reason,
                        "FAIL_CLOSED_MALFORMED_DB_TARGET",
                    )
                finally:
                    if saved is None:
                        _os.environ.pop("FIE_DATABASE_URL", None)
                    else:
                        _os.environ["FIE_DATABASE_URL"] = saved

    def setUp(self) -> None:
        from phase3.persistence.sqlite import SQLiteStore

        fd, path = tempfile.mkstemp(prefix="fie_contract_", suffix=".db")
        os.close(fd)
        os.unlink(path)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        self.store = SQLiteStore(path)
        # Contract tests start at v1 fresh — same shape as the PG setUp.
        MigrationManager(self.store, default_migrations_for(self.store)).apply()

    def graph_repo(self):
        from phase3.persistence.graph_repo import GraphRepository

        return GraphRepository(self.store)

    def tearDown(self) -> None:
        self.store.close()


def _pg_available() -> tuple[bool, str]:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False, (
            "psycopg not installed (pip install 'psycopg[binary]>=3.2'); "
            "PostgreSQL parity backend is optional per Phase 6.3"
        )
    try:
        store = open_store(resolve_spec(PG_DSN))
        store.close()
    except Exception as exc:
        return False, (
            f"disposable PostgreSQL cluster at {PG_DSN.split('?')[0]} not "
            f"reachable ({type(exc).__name__}); skip per Phase 6.3 §18 "
            "(no cloud provisioning in this phase)"
        )
    return True, ""


PG_OK, PG_SKIP_REASON = _pg_available()


class PostgresContractTests(ContractMixin, unittest.TestCase):
    """The SAME contract on the disposable PostgreSQL backend."""

    @classmethod
    def setUpClass(cls) -> None:
        if not PG_OK:
            raise unittest.SkipTest(PG_SKIP_REASON)

    def setUp(self) -> None:
        import psycopg

        self.store = open_store(resolve_spec(PG_DSN))
        self.addCleanup(self.store.close)
        # Disposable reset: drop everything (score_snapshot is append-only,
        # so DELETE/TRUNCATE are out — dropping the tables is the only wipe)
        # then re-apply the migrations so each test starts at v1 fresh.
        self.store.executescript(
            """
            DROP TABLE IF EXISTS schema_migrations CASCADE;
            DROP TABLE IF EXISTS score_snapshot CASCADE;
            DROP TABLE IF EXISTS signal_log CASCADE;
            DROP TABLE IF EXISTS graph_edges CASCADE;
            DROP TABLE IF EXISTS graph_nodes CASCADE;
            DROP TABLE IF EXISTS adapter_run_log CASCADE;
            DROP TABLE IF EXISTS ingestion_errors CASCADE;
            DROP FUNCTION IF EXISTS fie_score_snapshot_append_only();
            """
        )
        mgr = MigrationManager(self.store, default_migrations_for(self.store))
        mgr.apply()

    def graph_repo(self):
        from phase3.persistence.graph_repo import GraphRepository

        return GraphRepository(self.store)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()