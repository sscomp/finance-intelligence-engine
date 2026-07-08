"""SourceAdapter ABC + normalization primitives.

Phase 3A ships the abstract base class and a single illustrative
adapter (NoOpAdapter) that round-trips an already-built Signal —
sufficient for the scaffold + tests. Phase 3B will add YFinance,
RSS, T86, and MacroDaily adapters here, each owning the
SourceAdapter contract.

Importing from phase3.signals.adapters.* must NOT trigger network
calls; if an adapter needs config, it gets it at construction time.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from phase3.datamodel.signals import Signal


class SourceAdapter(ABC):
    """Abstract base class for raw-source → list[Signal] conversion.

    Implementations are stateless after construction; calling adapt()
    twice with the same input must return equal output.
    """

    source_type: str = "abstract"

    @abstractmethod
    def adapt(self, raw: Any) -> list[Signal]:
        """Convert a single source-native payload into one or more Signals.

        `raw` is intentionally typed as `Any` so a single adapter can
        accept dicts, dataclasses, or list-of-dicts depending on the
        source. Concrete adapters should narrow this with a `Union[...]`
        in their signature.
        """


class NoOpAdapter(SourceAdapter):
    """Adapter that wraps an already-built Signal.

    Useful for:
      - Tests (avoid needing real yfinance data)
      - Phase 3A scaffold (the engine is meant to be fed Signals anyway)

    The `raw` argument MUST be a Signal or a list of Signals.
    """

    source_type = "custom"

    def adapt(self, raw: Any) -> list[Signal]:
        if isinstance(raw, Signal):
            return [raw]
        if isinstance(raw, list) and all(isinstance(s, Signal) for s in raw):
            return list(raw)
        raise TypeError(
            f"NoOpAdapter expects Signal or list[Signal]; got {type(raw).__name__}"
        )


__all__ = ["SourceAdapter", "NoOpAdapter"]
