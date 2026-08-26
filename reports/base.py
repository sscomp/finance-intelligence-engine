#!/usr/bin/env python3
"""
BaseReport — abstract base class for all report plugins.

Phase 2B Step 3. Defines the minimal interface that every report must
implement, plus a default `run()` orchestrator and a default `archive()`
helper. The four existing production entrypoints (macro_daily.py,
industry_weekly.py, company_monthly.py, institutional.py) do NOT inherit
from this class — they are wrapped by thin plugin modules in
`reports/plugins/`. This is intentional: we never touch existing
main() functions.

Interface contract (4 abstract methods + 1 concrete `run`):

    fetch(self) -> dict
        Pull external data. Return whatever shape the analyzer expects.
        Pure: no I/O to logs/DB unless the report itself wants it.

    analyze(self, data: dict) -> tuple[Any, list, int]
        Return (verdict, signals, score). Domain-specific — the framework
        doesn't care what verdict means.

    render(self, data, verdict, signals, score) -> str
        Return the final text (the string that would go to Telegram).
        Pure: no side effects.

    manifest(self) -> dict
        Return the manifest dict (loaded from YAML at construction time).
        Exposed as a method so callers can introspect schedule, version,
        etc. without re-reading the YAML.

    run(self) -> int
        Default orchestrator. Calls fetch → analyze → render → archive
        in order, always catches exceptions, returns 0 on success / 1 on
        failure. This is the same exit-code contract the existing
        main() functions use, so the new framework does not introduce
        a new failure mode for the cron pipeline.

Design notes:

- No I/O imports at module level (no `import yaml`, no `import json`).
  yaml is imported lazily in `from_manifest` so unit tests that don't
  have PyYAML installed still pass.

- The class is intentionally lightweight. It does NOT call
  `print(report)` itself — that is the Publisher's job (Phase 2C will
  make that an injected dependency). Right now `run()` returns the text
  in `result.report_text` and the CLI is responsible for delivery.

- Subclasses MAY override `archive` if they need custom persistence.
  The default is a no-op because persistence is report-specific (macro
  goes to macro_daily table, monthly goes to stock_monthly, etc.).

This file does NOT change any existing production behavior. The
existing .py scripts in the project root continue to work unchanged.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReportResult:
    """Standard return shape from BaseReport.run()."""
    report_text: str
    verdict: Any
    score: int
    signals: list
    raw_data: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    # Diagnostic fields useful for the Dispatcher / observability layer.
    started_at: str = ""
    finished_at: str = ""
    error: str = ""


class BaseReport(ABC):
    """
    Abstract report plugin. Subclasses MUST implement fetch / analyze /
    render. The default `run()` orchestrates them in order.

    Manifest fields expected at construction time (set by from_manifest
    classmethod OR by direct __init__ call in tests):
        name: str
        display_name: str
        schedule: str            # cron expr
        timezone: str
        version: str
        entrypoint: str          # "module.path:ClassName"
        timeout_seconds: int
        retry_policy: dict
        publisher: dict
        archive: dict
    """

    def __init__(self, manifest: dict | None = None):
        self._manifest = manifest or self._default_manifest()
        # Convenience accessors so subclasses / callers don't have to
        # always go through self._manifest["..."].
        self.name = self._manifest.get("name", self.__class__.__name__)
        self.display_name = self._manifest.get("display_name", self.name)
        self.schedule = self._manifest.get("schedule", "")
        self.timezone = self._manifest.get("timezone", "Asia/Taipei")
        self.version = self._manifest.get("version", "0.0.0")

    # --- abstract methods (subclasses MUST implement) ---

    @abstractmethod
    def fetch(self) -> dict:
        """Pull external data. Return shape is report-specific."""

    @abstractmethod
    def analyze(self, data: dict) -> tuple[Any, list, int]:
        """Compute verdict / signals / score from fetched data."""

    @abstractmethod
    def render(self, data: dict, verdict: Any, signals: list, score: int) -> str:
        """Return the final text. Telegram-friendly is the convention."""

    # --- concrete helpers ---

    def manifest(self) -> dict:
        """Return the manifest dict (read-only contract)."""
        return dict(self._manifest)

    def archive(self, result: ReportResult) -> None:
        """
        Default archive is a no-op. Subclasses override to write to
        logs/<name>-<date>.json or to call db.save_* helpers. The
        default behavior is intentional: the framework should not
        impose a persistence schema on reports that don't want it.
        """
        return None

    def run(self, publish: bool = False) -> ReportResult:
        """
        Orchestrate fetch → analyze → render → archive. Always catches
        exceptions and returns a ReportResult with `error` populated on
        failure. The caller decides what to do with the result (the
        Dispatcher layer in `reports/hermes/dispatcher.py` is the
        intended primary caller; the CLI is the secondary caller).

        The `publish` flag is currently a hook for future Phase 2C
        work — when False (default), the report text is returned in
        the ReportResult but NOT printed. When True, it is also
        printed to stdout (matching the existing cron agent
        contract: stdout → telegram). The framework default is
        False so unit tests don't accidentally print to stdout.
        """
        import datetime as _dt
        started = _dt.datetime.now(_dt.timezone.utc).isoformat()
        try:
            data = self.fetch()
            verdict, signals, score = self.analyze(data)
            text = self.render(data, verdict, signals, score)
            result = ReportResult(
                report_text=text,
                verdict=verdict,
                score=score,
                signals=list(signals) if signals else [],
                raw_data=data,
                artifacts={},
                started_at=started,
                finished_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            )
            self.archive(result)
            if publish:
                print(text)
            return result
        except Exception as exc:  # noqa: BLE001 — top-level boundary
            return ReportResult(
                report_text="",
                verdict=None,
                score=0,
                signals=[],
                raw_data={},
                artifacts={},
                started_at=started,
                finished_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                error=f"{type(exc).__name__}: {exc}",
            )

    # --- manifest loader (classmethod, lazy yaml import) ---

    @classmethod
    def from_manifest(cls, manifest: dict) -> "BaseReport":
        """
        Construct an instance from a manifest dict. Does NOT import
        yaml — that is the caller's responsibility (or the loaders
        in `reports/registry.py`). This split keeps the base class
        usable in environments without PyYAML.
        """
        return cls(manifest=manifest)

    @staticmethod
    def _default_manifest() -> dict:
        return {
            "name": "unnamed",
            "display_name": "Unnamed Report",
            "schedule": "",
            "timezone": "Asia/Taipei",
            "version": "0.0.0",
            "entrypoint": "",
            "timeout_seconds": 120,
            "retry_policy": {"max_attempts": 1, "backoff_seconds": 0},
            "publisher": {"type": "stdout"},
            "archive": {"enabled": False},
        }


__all__ = ["BaseReport", "ReportResult"]
