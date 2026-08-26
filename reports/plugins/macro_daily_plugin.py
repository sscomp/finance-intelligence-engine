#!/usr/bin/env python3
"""
MacroDailyReport — thin BaseReport wrapper around macro_daily.py.

Phase 2B Step 3. This module does NOT reimplement macro_daily.py.
It imports the existing functions and delegates:

    fetch   -> macro_daily.fetch_indicators
    analyze -> macro_daily.assess_liquidity
    render  -> macro_daily.format_report

The three underlying functions are pure (or pure-ish — fetch_indicators
makes yfinance HTTP calls, but the rest is in-memory). Importing
macro_daily at module load time is safe: it has no top-level side
effects, and its `if __name__ == "__main__":` block is guarded.

When the framework calls `.run()` on this plugin, it will produce
output byte-identical to running `python3 macro_daily.py` directly —
the same fetch, the same analyze, the same render. The only thing
the wrapper adds is structured timing and error capture, which the
existing main() does not surface.

The plugin is registered in `config/reports/macro_daily.json` with
entrypoint `reports.plugins.macro_daily_plugin:MacroDailyReport`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from reports.base import BaseReport, ReportResult

# Make sure the macro-report project root is on sys.path so the bare
# `import macro_daily` works whether the framework is launched from
# the project root OR from elsewhere. This is the same sys.path
# bootstrap the existing run.sh does via the venv.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class MacroDailyReport(BaseReport):
    """
    BaseReport plugin that wraps the existing macro_daily.py module.
    Zero behavior change: fetch / analyze / render delegate to the
    same functions the cron-driven run.sh calls.
    """

    def fetch(self) -> dict:
        import macro_daily  # local import — module has no top-level I/O
        return macro_daily.fetch_indicators()

    def analyze(self, data: dict) -> tuple[Any, list, int]:
        import macro_daily
        verdict, signals, score = macro_daily.assess_liquidity(data)
        # The existing function returns (verdict, signals, score) as a
        # tuple of (str, list, int). Preserve that contract.
        return verdict, list(signals), int(score)

    def render(self, data: dict, verdict: Any, signals: list, score: int) -> str:
        import macro_daily
        return macro_daily.format_report(data, verdict, signals, score)

    def archive(self, result: ReportResult) -> None:
        """
        Best-effort archive: call db.save_macro_daily if the SQLite
        module is importable. We catch all exceptions because the
        existing main() does the same (DB failures are non-fatal —
        the report still goes to Telegram).

        NOTE: This is a no-op when the framework is being used in
        dry-run / list / test mode, because run() never reaches the
        archive stage unless fetch / analyze / render succeed.
        """
        try:
            from db import save_macro_daily  # type: ignore
            save_macro_daily(
                result.raw_data,
                result.verdict,
                result.score,
                result.signals,
            )
            result.artifacts["sqlite"] = "macro_daily"
        except Exception as exc:  # noqa: BLE001
            result.artifacts["sqlite_error"] = f"{type(exc).__name__}: {exc}"


__all__ = ["MacroDailyReport"]
