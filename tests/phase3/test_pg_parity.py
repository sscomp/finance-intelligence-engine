"""Deterministic pipeline parity: SQLite vs PostgreSQL (Phase 6.3).

Same fixture input → same persisted domain content → same pipeline
output on both backends. This is the Phase 6.3 §35/§37 parity gate
in automated form:

* ingest: the canonical Phase 3B fixture adapter drives the SAME
  ``Signal`` → ``SignalRecord`` conversion the production CLI uses
  (``phase3.cli._signal_to_record``), persisted through
  ``SignalRepository`` on each backend.
* pipeline: ``phase3.api.run_pipeline(persist=True, db_path=<spec>)``
  — exercised on SQLite first, then on the disposable PostgreSQL
  cluster; backend selection flows through the production dispatch
  (``--db-path`` accepts a ``postgres://`` DSN exactly as the CLI
  would pass it).
* comparison: every deterministic field must be byte-equal across
  backends. Run-scoped volatility is stripped by a recursive
  normalizer (not just top-level keys): the pipeline stamps
  ``run_id`` per run (``uuid4().hex[:12]`` in
  ``phase3.pipeline.intelligence_pipeline``), wall-clock
  timestamps/``valid_until`` into score breakdowns and evidence rows,
  and embeds the run id in score ``notes`` — none of that is domain
  content. Scores, dimension factors, signals, graph nodes and edges
  are compared in full.

PostgreSQL target: disposable local cluster only
(``FIE_TEST_PG_DSN`` env override; trust-auth /tmp cluster per the
Phase 6.3 report). SKIP with reason when unreachable. No cloud
provisioning happens in this test (Phase 6.2 ADR-C03).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from phase3.persistence.backend import open_store, resolve_spec  # noqa: E402
from phase3.persistence.migrations import (  # noqa: E402
    MigrationManager,
    default_migrations_for,
)
from phase3.persistence.signal_repo import SignalRepository  # noqa: E402
from phase3.persistence.score_repo import ScoreRepository  # noqa: E402
from phase3.persistence.graph_repo import GraphRepository  # noqa: E402
from phase3.signals.adapters import registry as adapter_registry  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "phase3" / "fixtures" / "fixture_company_industry_2026-07-08.json"

PG_DSN = os.environ.get(
    "FIE_TEST_PG_DSN",
    "postgresql://fie@/fie_parity?host=/tmp/fie-pg&port=54329",
)

DATE_BUCKET = "2026-07-08"

#: Keys that encode run metadata (per-run id / wall clock) rather
#: than domain results; dropped recursively from the comparison at
#: any nesting depth. ``_run_id`` appears inside score breakdowns;
#: ``timestamp``/``valid_until`` appear on evidence items.
VOLATILE_KEYS = frozenset({
    "run_id", "_run_id",
    "started_at", "finished_at", "duration_seconds",
    "timestamp", "valid_until",
    "ingested_at", "created_at", "computed_at", "fetched_at",
})

#: Score ``notes`` embed the per-run pipeline id
#: (``"intelligence_pipeline run_id=ipr-…"``) — mask the id so the
#: human-readable text itself still gets compared.
_RUN_ID_IN_TEXT = re.compile(r"run_id=ipr-[0-9a-f]{12}")


def _normalize(obj: Any) -> Any:
    """Recursively strip run-scoped volatility from a structure."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in VOLATILE_KEYS:
                continue
            if key == "notes" and isinstance(value, str):
                out[key] = _RUN_ID_IN_TEXT.sub("run_id=<run>", value)
            else:
                out[key] = _normalize(value)
        return out
    if isinstance(obj, list):
        return [_normalize(item) for item in obj]
    return obj


def _run_backend(target: str) -> dict:
    """Ingest the fixture and run the persist pipeline on one backend.

    Returns a comparison bundle: API payload plus raw table dumps.
    """
    from phase3.api import run_pipeline

    # --- bootstrap -------------------------------------------------------
    if resolve_spec(target).backend == "postgres":
        spec = resolve_spec(target)
        store = open_store(spec)
        # Disposable reset (score_snapshot is append-only → DROP, not DELETE)
        store.executescript(
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
    else:
        handle, path = tempfile.mkstemp(prefix="fie_parity_", suffix=".db")
        os.close(handle)
        os.unlink(path)
        target = path  # explicit sqlite file replaces the caller's spec
        store = open_store(resolve_spec(target))

    try:
        MigrationManager(store, default_migrations_for(store)).apply()

        # --- ingest (production conversion path) --------------------------
        from phase3.cli import _signal_to_record

        adapter = adapter_registry.get("fixture")()
        result = adapter.adapt_with_stats(str(FIXTURE))
        signals = result.signals
        self_repo = SignalRepository(store)
        n_new = self_repo.upsert_many([_signal_to_record(s) for s in signals])
        assert n_new == 3, f"fixture ingest must produce 3 new signals, got {n_new}"

        # --- pipeline (production API, backend dispatched) ----------------
        api_result = run_pipeline(
            date_bucket=DATE_BUCKET,
            industry_ids=("半導體",),
            company_specs=({"code": "1101"}, {"code": "2330"}),
            run_macro=True,
            persist=True,
            db_path=target,
        )
        payload = _normalize(api_result.payload)

        # --- dump domain rows ----------------------------------------------
        signal_repo = SignalRepository(store)
        score_repo = ScoreRepository(store)
        graph_repo = GraphRepository(store)
        return {
            "payload": payload,
            "signals": _strip([r.to_dict() for r in signal_repo.query(limit=1000)]),
            "scores": _strip([
                {**r.to_dict()} for r in score_repo.history(limit=1000)
            ]),
            "nodes": _strip([
                {
                    "node_id": n.node_id, "node_type": n.node_type,
                    "label": n.label, "metadata": n.metadata,
                    "tags": n.tags, "schema_version": n.schema_version,
                }
                for n in graph_repo.list_nodes(limit=10000)
            ]),
            "edges": _strip([
                {
                    "edge_id": e.edge_id, "edge_type": e.edge_type,
                    "from_node_id": e.from_node_id, "to_node_id": e.to_node_id,
                    "weight": e.weight, "metadata": e.metadata,
                    "schema_version": e.schema_version,
                }
                for e in graph_repo.list_edges(limit=10000)
            ]),
        }
    finally:
        store.close()


def _strip(rows: list[dict]) -> list[dict]:
    """Normalize run-scoped fields and sort rows deterministically."""
    out = [
        json.loads(json.dumps(_normalize(row), sort_keys=True, default=str))
        for row in rows
    ]
    return sorted(out, key=lambda r: json.dumps(r, sort_keys=True))


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
            f"disposable PostgreSQL cluster for {PG_DSN.split('?')[0]} not "
            f"reachable ({type(exc).__name__}); skip per Phase 6.3 §18 "
            "(no cloud provisioning in this phase)"
        )
    return True, ""


PG_OK, PG_SKIP_REASON = _pg_available()


class PostgresParityTests(unittest.TestCase):
    """SQLite vs disposable-PostgreSQL deterministic parity gate."""

    @classmethod
    def setUpClass(cls) -> None:
        if not PG_OK:
            raise unittest.SkipTest(PG_SKIP_REASON)

    def test_double_run_normalization_is_stable(self) -> None:
        """The volatility rule is complete: two SQLite runs at different
        wall clocks normalize to identical bundles. If this ever fails,
        a field was misclassified as domain content (or volatility crept
        in somewhere new) — fix the classification before trusting the
        cross-backend comparison.
        """
        first = _run_backend('/tmp/fie_parity_sqlite_dummy.db')
        second = _run_backend('/tmp/fie_parity_sqlite_dummy.db')
        for bundle_key in ("payload", "signals", "scores", "nodes", "edges"):
            self.assertEqual(first[bundle_key], second[bundle_key], bundle_key)

    def test_full_pipeline_parity(self) -> None:
        sqlite_res = _run_backend('/tmp/fie_parity_sqlite_dummy.db')
        pg_res = _run_backend(PG_DSN)

        # Payload parity (counts, scores structure, graph summaries).
        self.assertEqual(sqlite_res["payload"], pg_res["payload"])
        # Signal identity + values parity.
        self.assertEqual(sqlite_res["signals"], pg_res["signals"])
        # Score snapshot parity (incl. deterministic snapshot_ids).
        self.assertEqual(sqlite_res["scores"], pg_res["scores"])
        # Evidence graph parity.
        self.assertEqual(sqlite_res["nodes"], pg_res["nodes"])
        self.assertEqual(sqlite_res["edges"], pg_res["edges"])

        # Sanity: the parity is non-trivial — fixture signals really
        # landed and the pipeline really produced snapshots.
        self.assertEqual(len(sqlite_res["signals"]), 3)
        self.assertGreater(len(sqlite_res["scores"]), 0)
        self.assertGreater(len(pg_res["payload"]["snapshot_ids"]), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()