"""Adapter registry — maps adapter name to SourceAdapter class.

The registry is a plain dict (no import side-effects). It exists so:
  - the `ingest-signals` CLI can resolve a name like ``"yfinance"`` to
    the right adapter class without doing string-based ``importlib``
    acrobatics;
  - tests can list all registered adapters and verify coverage;
  - Phase 3B can grow new adapters without modifying the CLI.

Adapter classes are registered at import time via :func:`register`.
The :func:`get` function returns the class (callers instantiate it).
The :func:`build` convenience function instantiates with ``**kwargs``.

The registry is **explicit** — there is no auto-discovery. Adding a
new adapter means (1) writing the adapter module, (2) exporting it
from :mod:`phase3.signals.adapters.__init__`, (3) calling
:func:`register` once there. This keeps the import graph obvious.
"""
from __future__ import annotations

from typing import Type

from phase3.signals.adapters.base import NoOpAdapter, SourceAdapter


_REGISTRY: dict[str, Type[SourceAdapter]] = {
    # ``noop`` is the canonical "round-trip" adapter; useful for tests
    # and for the existing engine scaffold that already feeds Signals.
    "noop": NoOpAdapter,
}


def register(name: str, adapter_cls: Type[SourceAdapter]) -> None:
    """Register ``adapter_cls`` under ``name``.

    Overwrites any previous entry — useful for tests that want to
    substitute a fake. Names are case-insensitive on lookup, but
    stored lower-case for canonical comparison.
    """
    if not name or not isinstance(name, str):
        raise ValueError(f"adapter name must be a non-empty string, got {name!r}")
    if not (isinstance(adapter_cls, type) and issubclass(adapter_cls, SourceAdapter)):
        raise TypeError(
            f"adapter_cls must be a SourceAdapter subclass, got {adapter_cls!r}"
        )
    _REGISTRY[name.lower()] = adapter_cls


def unregister(name: str) -> None:
    """Remove ``name`` from the registry. No-op if absent.

    Intended for tests; production code should not call this.
    """
    _REGISTRY.pop(name.lower(), None)


def get(name: str) -> Type[SourceAdapter]:
    """Return the adapter class registered under ``name``.

    Raises :class:`KeyError` with a helpful message (lists known
    names) if the name is not registered.
    """
    key = name.lower()
    if key not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"unknown adapter {name!r}; known: {known}")
    return _REGISTRY[key]


def has(name: str) -> bool:
    """Return True if ``name`` is registered (case-insensitive)."""
    return name.lower() in _REGISTRY


def names() -> tuple[str, ...]:
    """Return a sorted tuple of all registered adapter names."""
    return tuple(sorted(_REGISTRY))


def build(name: str, **kwargs: object) -> SourceAdapter:
    """Instantiate the adapter registered under ``name``.

    Forwards ``kwargs`` to the adapter constructor. All four Phase 3B
    adapters take no required constructor args, so most callers pass
    nothing.
    """
    return get(name)(**kwargs)


def clear() -> None:
    """Reset the registry to its module-default state.

    Only used by tests; production code must not call this. The
    re-import of the adapters package repopulates the default
    "noop" entry.
    """
    _REGISTRY.clear()
    _REGISTRY["noop"] = NoOpAdapter


__all__ = ["register", "unregister", "get", "has", "names", "build", "clear"]
