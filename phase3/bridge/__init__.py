"""Bridge module: map production data tables to Phase 3 signals.

Public API:
    seed_from_macro_history  — seed signal_log from macro_history.db
    resolve_as_of_dates      — deterministic freshness date resolution
"""
from phase3.bridge.seed_signals import seed_from_macro_history, resolve_as_of_dates

__all__ = ["seed_from_macro_history", "resolve_as_of_dates"]