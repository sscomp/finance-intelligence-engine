"""Shared bootstrap helpers for the Phase 6.5 service contract tests.

Builds a scored disposable SQLite database through the *production*
paths only (fixture adapter → ``_signal_to_record`` → repositories →
``run_pipeline(persist=True)``), so every service operation is tested
against real persisted domain state. PostgreSQL variants reuse
``FIE_TEST_PG_DSN`` (disposable local cluster; SKIP with reason when
unreachable — never a production database).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

FIXTURE = (
    REPO_ROOT / "tests" / "phase3" / "fixtures"
    / "fixture_company_industry_2026-07-08.json"
)

PG_DSN = os.environ.get(
    "FIE_TEST_PG_DSN",
    "postgresql://fie@/fie_contract?host=/tmp/fie-pg&port=54329",
)

DATE_BUCKET = "2026-07-08"

COMPANY_POLICY_FRESH_AS_OF = "2026-07-09"     # 1 day old → FRESH
COMPANY_POLICY_DEGRADED_AS_OF = "2026-08-20"  # ~43 days → DEGRADED
COMPANY_POLICY_STALE_AS_OF = "2026-10-03"     # ~87 days → STALE

PG_DROP_SCRIPT = """
DROP TABLE IF EXISTS schema_migrations CASCADE;
DROP TABLE IF EXISTS score_snapshot CASCADE;
DROP TABLE IF EXISTS signal_log CASCADE;
DROP TABLE IF EXISTS graph_edges CASCADE;
DROP TABLE IF EXISTS graph_nodes CASCADE;
DROP TABLE IF EXISTS adapter_run_log CASCADE;
DROP TABLE IF EXISTS ingestion_errors CASCADE;
DROP FUNCTION IF EXISTS fie_score_snapshot_append_only();
"""


def bootstrap_state(db_spec: str | None = None, *, persist: bool = True) -> dict:
    """Create a disposable store, ingest the fixture, run the pipeline.

    Returns a bundle::

        {
            "store", "path", "service",
            "entity_ids": {"company": "2330", "industry": "半導體", ...},
        }

    ``service`` is a :class:`DefaultIntelligenceService` opened read-only
    over the same database.
    """
    from phase3 import api as _api
    from phase3.cli import _signal_to_record
    from phase3.persistence.backend import open_store, resolve_spec
    from phase3.persistence.migrations import (
        MigrationManager,
        default_migrations_for,
    )
    from phase3.persistence.signal_repo import SignalRepository
    from phase3.service.boundary import DefaultIntelligenceService
    from phase3.signals.adapters import registry as adapter_registry

    if db_spec is None:
        handle, path = tempfile.mkstemp(prefix="fie_service_", suffix=".db")
        os.close(handle)
        os.unlink(path)
        target = path
    else:
        target = db_spec

    spec = resolve_spec(target)
    store = open_store(spec)
    if spec.backend == "postgres":
        store.executescript(PG_DROP_SCRIPT)
    MigrationManager(store, default_migrations_for(store)).apply()

    adapter = adapter_registry.get("fixture")()
    result = adapter.adapt_with_stats(str(FIXTURE))
    SignalRepository(store).upsert_many(
        [_signal_to_record(s) for s in result.signals]
    )

    if persist:
        _api.run_pipeline(
            date_bucket=DATE_BUCKET,
            industry_ids=("半導體",),
            company_specs=({"code": "1101"}, {"code": "2330"}),
            run_macro=True,
            persist=True,
            db_path=spec.original,
        )

    service = DefaultIntelligenceService(store)
    return {
        "store": store,
        "path": spec.original,
        "service": service,
        "spec": spec,
    }


def bootstrap_pg_state() -> dict | None:
    """The same bootstrap on the disposable PostgreSQL cluster, or None."""
    try:
        return bootstrap_state(PG_DSN)
    except Exception:  # noqa: BLE001 - no disposable cluster here → caller skips
        return None