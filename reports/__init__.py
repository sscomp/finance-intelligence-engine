"""macro-report package marker.

Phase 2B Step 3: This package is the namespace for the new report
framework. Existing root-level modules (macro_daily.py, etc.) are
unaffected — they are imported by their own scripts. New code lives
under `reports/`:

  reports.base        — BaseReport + ReportResult
  reports.pipeline    — ReportPipeline orchestrator
  reports.registry    — Manifest Registry
  reports.hermes      — Dispatcher / Task framework skeleton
  reports.plugins     — Thin wrappers around existing .py entrypoints

Public surface (re-exported here for ergonomics):

    from reports import BaseReport, ReportResult, ReportPipeline, Registry
"""
from reports.base import BaseReport, ReportResult
from reports.pipeline import PipelineResult, ReportPipeline, StageRecord
from reports.registry import Registry, RegistryEntry

__all__ = [
    "BaseReport",
    "ReportResult",
    "ReportPipeline",
    "PipelineResult",
    "StageRecord",
    "Registry",
    "RegistryEntry",
]
