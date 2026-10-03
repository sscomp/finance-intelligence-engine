"""Batch worker boundary (Phase 6.5 §12) — scheduled computation path.

The **only** supported execution arc for scheduled ingestion/scoring:
a future cloud scheduler calls these application entry points; it
never calls interactive service read handlers and — symmetrically —
interactive reads (``phase3.service.boundary``) never invoke batch
orchestration. The invariant the work order requires:

    interactive read path != scheduler/batch orchestration path

The functions wrap the *existing* Phase 4 application entry points
(:func:`phase3.api.run_pipeline`) and the existing ingest path used
by the CLI (:class:`~phase3.persistence.signal_repo.SignalRepository`
fed by registered adapters) — no pipeline redesign, no scheduler
provisioning in this phase.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["BatchWorker", "BatchResult"]


@dataclass(frozen=True)
class BatchResult:
    """What a batch orchestration step returned (JSON-safe)."""

    stage: str  # ingestion | scoring
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "payload": self.payload}


class BatchWorker:
    """Stable entry points for the future cloud scheduler.

    Construction takes the same portable persistence specifier the
    interactive boundary resolves (``FIE_DATABASE_URL`` / explicit
    DSN / SQLite path) so batch and interactive planes share the
    persistence abstraction — while keeping their process
    lifecycles independent.
    """

    def __init__(self, db_spec: str | None = None) -> None:
        from phase3.persistence.backend import open_store, resolve_spec
        from phase3.persistence.migrations import (
            MigrationManager,
            default_migrations_for,
        )

        spec = resolve_spec(db_spec) if db_spec else resolve_spec()
        self._spec = spec
        self._store = open_store(spec)
        # Schema ownership (§13): the *migration layer* defines schema;
        # the worker merely triggers the declared migrations. No
        # ad-hoc/ dialect-specific DDL is ever issued from here.
        MigrationManager(self._store, default_migrations_for(self._store)).apply()

    # -- ingestion --------------------------------------------------------

    def run_ingestion(
        self,
        adapter_name: str,
        input_path: str,
        *,
        limit: int = 1000,
    ) -> BatchResult:
        """Ingest signals through the production conversion path."""
        from phase3.cli import _signal_to_record
        from phase3.persistence.signal_repo import SignalRepository
        from phase3.signals.adapters import registry as adapter_registry

        adapter = adapter_registry.get(adapter_name)()
        result = adapter.adapt_with_stats(input_path)
        records = [_signal_to_record(s) for s in result.signals][:limit]
        repo = SignalRepository(self._store)
        n_new = repo.upsert_many(records)
        return BatchResult("ingestion", {"signals_ingested": n_new})

    # -- scoring / persistence ----------------------------------------------

    def run_scoring(
        self,
        *,
        date_bucket: str,
        industry_ids: tuple[str, ...] = (),
        company_specs: tuple[dict[str, Any], ...] = (),
        run_macro: bool = True,
    ) -> BatchResult:
        """Ingest→score→persist through the production orchestrator.

        ``persist_results`` is not a separate callable: the
        orchestrator's persist path writes snapshots and evidence
        graph atomically as part of one production-verified flow.
        """
        from phase3.api import run_pipeline

        api_result = run_pipeline(
            date_bucket=date_bucket,
            industry_ids=tuple(industry_ids),
            company_specs=tuple(company_specs),
            run_macro=run_macro,
            persist=True,
            db_path=self._spec.original,
        )
        return BatchResult("scoring", api_result.payload)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "BatchWorker":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()