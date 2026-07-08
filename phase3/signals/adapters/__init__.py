"""Adapters package. Phase 3A ships only the ABC + NoOp adapter.

Adding more adapters in Phase 3B:
  yfinance.py  — yfinance .info dict → multiple Signals (PE, ROE, ...)
  rss.py       — RSS feed item → sentiment Signal
  t86.py       — TWSE T86 → institutional_flow Signal
  macro.py     — macro_daily output → macro signals
"""
from __future__ import annotations

from phase3.signals.adapters.base import NoOpAdapter, SourceAdapter

__all__ = ["SourceAdapter", "NoOpAdapter"]
