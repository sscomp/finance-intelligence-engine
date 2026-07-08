"""Adapters package — Phase 3B Task 2.

This package turns raw source payloads into normalized
:class:`phase3.datamodel.signals.Signal` objects. The available
adapters are:

  - :class:`NoOpAdapter`     — round-trip an already-built Signal
                              (Phase 3A scaffold, still used by tests)
  - :class:`FixtureAdapter`  — read JSON fixtures into Signals
  - :class:`YFinanceAdapter` — yfinance .info payload → multiple Signals
  - :class:`RSSAdapter`      — RSS / Atom items → news_headline / sentiment
  - :class:`T86Adapter`      — TWSE T86 rows → foreign_net / prop_net / dealer_net
  - :class:`MacroAdapter`    — daily macro dict → macro Signals

The :mod:`phase3.signals.adapters.registry` module provides a
plain-dict registry. ``register(...)`` is called below so that
:func:`registry.get` resolves a name to a class without doing any
import acrobatics.

Importing this package must NOT trigger network calls — the
adapters are pure normalizers and the registry is built from
eager imports of in-process classes only.
"""
from __future__ import annotations

from phase3.signals.adapters import registry
from phase3.signals.adapters.base import NoOpAdapter, SourceAdapter
from phase3.signals.adapters.fixture import FixtureAdaptResult, FixtureAdapter
from phase3.signals.adapters.macro import MacroAdaptResult, MacroAdapter
from phase3.signals.adapters.rss import RSSAdaptResult, RSSAdapter
from phase3.signals.adapters.t86 import T86AdaptResult, T86Adapter
from phase3.signals.adapters.yfinance import YFinanceAdaptResult, YFinanceAdapter


# Eager registration. Adding a new adapter = add it here + add it
# to the registry via the import above. The order is documented; do
# not depend on the dict iteration order in production code (use
# :func:`registry.names` if you need a sorted view).
registry.register("fixture", FixtureAdapter)
registry.register("yfinance", YFinanceAdapter)
registry.register("rss", RSSAdapter)
registry.register("t86", T86Adapter)
registry.register("macro", MacroAdapter)


__all__ = [
    "SourceAdapter",
    "NoOpAdapter",
    "FixtureAdapter",
    "FixtureAdaptResult",
    "YFinanceAdapter",
    "YFinanceAdaptResult",
    "RSSAdapter",
    "RSSAdaptResult",
    "T86Adapter",
    "T86AdaptResult",
    "MacroAdapter",
    "MacroAdaptResult",
    "registry",
]
