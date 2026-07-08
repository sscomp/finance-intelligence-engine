"""Signal engine subpackage. See docs/phase3/02_signal_engine.md.

Layer hierarchy (top-down):
  signals/engine.py      → SignalEngine (orchestrator; pure functions below)
  signals/aggregator.py  → aggregate multiple WeightedSignals
  signals/confidence.py  → confidence scoring
  signals/weighting.py   → combine source/type/recency weights
  signals/decay.py       → time-decay functions
  signals/adapters/      → source-specific RawSignal → Signal conversion (Phase 3B+)

Phase 3A: signal engine works in-memory from a list of pre-built Signal
objects (no live adapter, no I/O). The SourceAdapter ABC is shipped as
the contract for Phase 3B.
"""
from __future__ import annotations

from phase3.signals.aggregator import SignalAggregator
from phase3.signals.confidence import compute_confidence
from phase3.signals.decay import DECAY_FUNCTIONS, apply_decay
from phase3.signals.engine import SignalEngine
from phase3.signals.weighting import combine_weights

__all__ = [
    "DECAY_FUNCTIONS",
    "apply_decay",
    "combine_weights",
    "compute_confidence",
    "SignalAggregator",
    "SignalEngine",
]
