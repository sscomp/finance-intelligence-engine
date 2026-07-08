"""Phase 3B sources config loader.

Reads ``config/phase3/sources.yaml`` and returns a typed
:class:`SourcesConfig` object. The loader is deliberately minimal —
it does NOT merge with ``source_weights.yaml`` (which is scoring
metadata, not routing metadata). Its job is "given a source name,
what adapter class produces Signals, and what default input path
should the CLI use?".

If the file is missing, the loader returns an empty SourcesConfig
so callers can fall back to a hard-coded default. This matches the
existing ``ConfigLoader._read`` behavior on missing files.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from phase3.config.loader import DEFAULT_CONFIG_ROOT, _read_yaml


@dataclass(frozen=True)
class SourceEntry:
    """One source's routing metadata.

    ``adapter`` is the key registered in
    :mod:`phase3.signals.adapters.registry`. ``source_type`` is the
    string written on each produced :class:`Signal`. ``default_input``
    is the default JSON path the CLI uses when the operator does
    not pass ``--input``. ``extra`` holds the optional
    ``entity_match`` / ``direction`` / ``description`` / ``enabled``
    hints.
    """

    name: str
    adapter: str
    source_type: str
    default_input: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def is_enabled(self) -> bool:
        return bool(self.extra.get("enabled", True))


@dataclass(frozen=True)
class SourcesConfig:
    """Top-level sources config object."""

    sources: dict[str, SourceEntry] = field(default_factory=dict)
    config_path: Path | None = None
    config_hash: str = ""

    def get(self, name: str) -> SourceEntry | None:
        return self.sources.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.sources))

    def enabled_names(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, s in self.sources.items() if s.is_enabled()))


def load_sources(root: Path | str | None = None) -> SourcesConfig:
    """Load ``sources.yaml`` from the Phase 3 config root.

    Returns an empty :class:`SourcesConfig` if the file is missing.
    Raises :class:`ValueError` if the file exists but is malformed.
    """
    root_path = Path(root) if root is not None else DEFAULT_CONFIG_ROOT
    path = root_path / "sources.yaml"
    if not path.exists():
        return SourcesConfig()
    data = _read_yaml(path)
    raw_sources = data.get("sources", data)
    sources: dict[str, SourceEntry] = {}
    for name, cfg in raw_sources.items():
        if not isinstance(cfg, dict):
            continue
        adapter = str(cfg.get("adapter", "")).strip()
        if not adapter:
            raise ValueError(
                f"sources.yaml: source {name!r} is missing required 'adapter' key"
            )
        # Known top-level routing fields; everything else goes into extra
        known = {"adapter", "source_type", "default_input"}
        extra = {k: v for k, v in cfg.items() if k not in known}
        sources[name] = SourceEntry(
            name=name,
            adapter=adapter,
            source_type=str(cfg.get("source_type", name)),
            default_input=(str(cfg["default_input"])
                           if cfg.get("default_input") is not None
                           else None),
            extra=extra,
        )
    # File-content hash for config-version tracking
    h = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return SourcesConfig(sources=sources, config_path=path, config_hash=h)


__all__ = [
    "SourceEntry",
    "SourcesConfig",
    "load_sources",
]
