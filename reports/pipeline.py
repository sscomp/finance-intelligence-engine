#!/usr/bin/env python3
"""
ReportPipeline — orchestrator that runs a report through its stages
in order, with timing and stage-level error capture. This is a
deliberately thin wrapper around BaseReport.run() that adds:

  - Per-stage timing (fetch / analyze / render / archive durations)
  - A stage list (so callers can introspect what happened)
  - A "skip_publish" mode that returns the result without printing
  - Strict error reporting (each stage failure is captured, not just
    the top-level exception)

Design rationale: the architecture review (ARCHITECTURE_REVIEW_PHASE2.md
§3) calls out the seven-stage pipeline (Scheduler / Task / Fetcher /
Analyzer / Renderer / Publisher / Archive). This module materializes
Fetcher → Analyzer → Renderer → Archive, leaving Scheduler (Hermes
cronjob) and Publisher (stdout→telegram via cron agent) at the
boundary unchanged. Task lifecycle is the Dispatcher's job (see
reports/hermes/dispatcher.py), not this module's.

The pipeline is intentionally synchronous and single-report. Multi-
worker parallelism is Phase 2D scope and is explicitly out of scope
here.
"""
from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Any

from reports.base import BaseReport, ReportResult


@dataclass
class StageRecord:
    """One row in the pipeline execution log."""
    stage: str
    started_at: str
    finished_at: str = ""
    duration_ms: int = 0
    status: str = "ok"  # ok | failed | skipped
    error: str = ""


@dataclass
class PipelineResult:
    """Output of a pipeline run. `stages` is the audit trail."""
    result: ReportResult
    stages: list = field(default_factory=list)  # list[StageRecord]
    total_duration_ms: int = 0


class ReportPipeline:
    """
    Orchestrate a BaseReport instance through its stages. Usage:

        pipeline = ReportPipeline(report)
        outcome = pipeline.run()
        if outcome.result.error:
            ...handle failure...
        else:
            print(outcome.result.report_text)

    The pipeline does NOT itself publish. The caller decides whether
    to print, send to Telegram, or archive. This keeps the test layer
    simple (no stdout noise during unit tests).
    """

    def __init__(self, report: BaseReport):
        self.report = report

    def _now(self) -> str:
        return _dt.datetime.now(_dt.timezone.utc).isoformat()

    def _record(self, stage: str, started: str, status: str = "ok",
                error: str = "") -> StageRecord:
        finished = self._now()
        return StageRecord(
            stage=stage,
            started_at=started,
            finished_at=finished,
            duration_ms=0,
            status=status,
            error=error,
        )

    def run(self, publish: bool = False) -> PipelineResult:
        """
        Run the report synchronously. Returns a PipelineResult with
        timing data even on failure. `publish` is forwarded to
        `BaseReport.run()` — if True, the final text is also printed
        to stdout (matching the existing cron agent contract).
        """
        overall_start = time.monotonic()
        stages: list[StageRecord] = []

        # Stage 1: fetch
        s = self._now()
        t0 = time.monotonic()
        try:
            data = self.report.fetch()
            rec = self._record("fetch", s, status="ok")
            rec.duration_ms = int((time.monotonic() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            rec = self._record("fetch", s, status="failed",
                               error=f"{type(exc).__name__}: {exc}")
            rec.duration_ms = int((time.monotonic() - t0) * 1000)
            stages.append(rec)
            return PipelineResult(
                result=ReportResult(
                    report_text="",
                    verdict=None,
                    score=0,
                    signals=[],
                    raw_data={},
                    artifacts={"stages": [r.__dict__ for r in stages]},
                    started_at=s,
                    finished_at=self._now(),
                    error=rec.error,
                ),
                stages=stages,
                total_duration_ms=int((time.monotonic() - overall_start) * 1000),
            )
        stages.append(rec)

        # Stage 2: analyze
        s = self._now()
        t0 = time.monotonic()
        try:
            verdict, signals, score = self.report.analyze(data)
            rec = self._record("analyze", s, status="ok")
            rec.duration_ms = int((time.monotonic() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            rec = self._record("analyze", s, status="failed",
                               error=f"{type(exc).__name__}: {exc}")
            rec.duration_ms = int((time.monotonic() - t0) * 1000)
            stages.append(rec)
            return PipelineResult(
                result=ReportResult(
                    report_text="",
                    verdict=None,
                    score=0,
                    signals=[],
                    raw_data=data,
                    artifacts={"stages": [r.__dict__ for r in stages]},
                    started_at=s,
                    finished_at=self._now(),
                    error=rec.error,
                ),
                stages=stages,
                total_duration_ms=int((time.monotonic() - overall_start) * 1000),
            )
        stages.append(rec)

        # Stage 3: render
        s = self._now()
        t0 = time.monotonic()
        try:
            text = self.report.render(data, verdict, signals, score)
            rec = self._record("render", s, status="ok")
            rec.duration_ms = int((time.monotonic() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            rec = self._record("render", s, status="failed",
                               error=f"{type(exc).__name__}: {exc}")
            rec.duration_ms = int((time.monotonic() - t0) * 1000)
            stages.append(rec)
            return PipelineResult(
                result=ReportResult(
                    report_text="",
                    verdict=verdict,
                    score=score,
                    signals=list(signals) if signals else [],
                    raw_data=data,
                    artifacts={"stages": [r.__dict__ for r in stages]},
                    started_at=s,
                    finished_at=self._now(),
                    error=rec.error,
                ),
                stages=stages,
                total_duration_ms=int((time.monotonic() - overall_start) * 1000),
            )
        stages.append(rec)

        # Stage 4: archive (best-effort; failure does NOT mark the
        # whole pipeline as failed, because archive errors should
        # not block Telegram delivery — same policy as the existing
        # main() functions).
        archive_started = self._now()
        t0 = time.monotonic()
        artifacts: dict[str, Any] = {}
        archive_status = "ok"
        archive_error = ""
        try:
            archive_input = ReportResult(
                report_text=text,
                verdict=verdict,
                score=score,
                signals=list(signals) if signals else [],
                raw_data=data,
                artifacts=artifacts,
                started_at=archive_started,
                finished_at="",
            )
            self.report.archive(archive_input)
            artifacts = archive_input.artifacts
        except Exception as exc:  # noqa: BLE001
            archive_status = "failed"
            archive_error = f"{type(exc).__name__}: {exc}"
        rec = self._record("archive", archive_started, status=archive_status, error=archive_error)
        rec.duration_ms = int((time.monotonic() - t0) * 1000)
        stages.append(rec)

        # Optional publish (matches existing cron agent contract:
        # stdout → telegram).
        if publish:
            print(text)

        final = ReportResult(
            report_text=text,
            verdict=verdict,
            score=score,
            signals=list(signals) if signals else [],
            raw_data=data,
            artifacts=artifacts,
            started_at=archive_started,
            finished_at=self._now(),
            error="",
        )
        return PipelineResult(
            result=final,
            stages=stages,
            total_duration_ms=int((time.monotonic() - overall_start) * 1000),
        )


__all__ = ["ReportPipeline", "PipelineResult", "StageRecord"]
