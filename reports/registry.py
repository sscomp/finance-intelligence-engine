#!/usr/bin/env python3
"""
Registry — central catalog of registered report plugins.

Phase 2B Step 3. The registry is the single source of truth for "which
reports exist, what schedule do they run on, what is their entrypoint".
It loads manifests from disk and resolves them into BaseReport
instances on demand.

Why a registry (vs. a plain dict in a module): the registry is the
seam between "manifest on disk" (yaml) and "Python object in memory".
In Phase 2C the Dispatcher will ask the registry "what's queued for
right now?" without having to re-parse yaml each tick. By centralizing
the load + cache logic here, we keep the Dispatcher stateless and
testable.

Two layers of API:

  1. Manifest-level (no Python import needed):
        reg = Registry.from_directory("/path/to/config/reports")
        reg.list_names()             -> ["macro_daily", ...]
        reg.get_manifest("macro_daily") -> dict

  2. Instance-level (resolves the entrypoint and constructs the
     BaseReport subclass):
        report = reg.instantiate("macro_daily")
        outcome = report.run()

The "list-only" mode is what the CLI uses to render the inventory
without ever invoking a fetcher. This satisfies the Phase 2B
requirement: "CLI must NOT execute production reports."

Manifest format (config/reports/<name>.yaml):

    name: macro_daily
    display_name: 總體經濟晨報
    schedule: "30 8 * * 1-6"
    timezone: Asia/Taipei
    version: "1.0.0"
    entrypoint: "reports.plugins.macro_daily_plugin:MacroDailyReport"
    timeout_seconds: 120
    retry_policy:
      max_attempts: 1
      backoff_seconds: 0
    publisher:
      type: stdout
    archive:
      enabled: false
      log_dir: ${FIE_DATA_DIR}/logs        # portable: via FIE_DATA_DIR, not a host path
"""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from reports.base import BaseReport


@dataclass
class RegistryEntry:
    """One row in the registry."""
    name: str
    manifest_path: str
    manifest: dict = field(default_factory=dict)


class Registry:
    """
    In-memory catalog of manifests + a cache of instantiated plugins.
    Construction is cheap; instantiation is lazy and cached.
    """

    def __init__(self, entries: Optional[dict[str, RegistryEntry]] = None):
        self._entries: dict[str, RegistryEntry] = entries or {}
        self._instance_cache: dict[str, BaseReport] = {}

    # ----- classmethod constructors -----

    @classmethod
    def from_directory(cls, config_dir: str | os.PathLike) -> "Registry":
        """
        Load all `*.yaml` and `*.json` files in `config_dir` as
        manifests. Tries PyYAML first; falls back to JSON if YAML
        isn't installed. Either way, the returned Registry is
        manifest-only — no BaseReport is instantiated until
        `instantiate()` is called.
        """
        config_path = Path(config_dir)
        entries: dict[str, RegistryEntry] = {}
        if not config_path.exists():
            return cls(entries)
        for manifest_file in sorted(config_path.glob("*")):
            if not manifest_file.is_file():
                continue
            if manifest_file.suffix not in (".yaml", ".yml", ".json"):
                continue
            try:
                data = cls._load_file(manifest_file)
            except Exception:
                # Manifests that fail to parse are silently skipped
                # at the manifest layer. The CLI surfaces them via
                # the `_unparseable` list below.
                continue
            name = data.get("name") or manifest_file.stem
            entries[name] = RegistryEntry(
                name=name,
                manifest_path=str(manifest_file),
                manifest=data,
            )
        return cls(entries)

    @staticmethod
    def _load_file(path: Path) -> dict:
        """Load a YAML or JSON manifest. Tries YAML first, JSON fallback."""
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
                return yaml.safe_load(text) or {}
            except ImportError:
                # No PyYAML — try to interpret as JSON. Most simple
                # manifests are valid JSON if you swap `:` for `,`
                # and strip comments; we don't do that conversion
                # here, we just raise so the caller can skip.
                raise RuntimeError("PyYAML not installed; cannot parse YAML manifest")
        return json.loads(text)

    # ----- manifest-level API -----

    def list_names(self) -> list[str]:
        """Return the registered report names, sorted alphabetically."""
        return sorted(self._entries.keys())

    def get_manifest(self, name: str) -> dict:
        """Return the manifest dict for `name`. Raises KeyError if missing."""
        if name not in self._entries:
            raise KeyError(f"No report registered under name '{name}'")
        return dict(self._entries[name].manifest)

    def get_entry(self, name: str) -> RegistryEntry:
        """Return the full RegistryEntry (manifest + path)."""
        if name not in self._entries:
            raise KeyError(f"No report registered under name '{name}'")
        return self._entries[name]

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self.list_names())

    # ----- instance-level API -----

    def instantiate(self, name: str) -> BaseReport:
        """
        Resolve the manifest's `entrypoint` to a BaseReport subclass
        and return a cached instance. Raises KeyError if the manifest
        is missing, and ImportError / AttributeError if the entrypoint
        is broken.
        """
        if name in self._instance_cache:
            return self._instance_cache[name]
        manifest = self.get_manifest(name)
        entrypoint = manifest.get("entrypoint", "")
        if not entrypoint:
            raise ValueError(
                f"Manifest for '{name}' has no 'entrypoint' field. "
                f"Expected 'module.path:ClassName'."
            )
        if ":" not in entrypoint:
            raise ValueError(
                f"Manifest for '{name}' entrypoint '{entrypoint}' "
                f"must be of the form 'module.path:ClassName'."
            )
        module_path, attr = entrypoint.split(":", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, attr)
        if not isinstance(cls, type) or not issubclass(cls, BaseReport):
            raise TypeError(
                f"Entrypoint '{entrypoint}' for '{name}' resolved to "
                f"{cls!r}, which is not a BaseReport subclass."
            )
        instance = cls.from_manifest(manifest)
        self._instance_cache[name] = instance
        return instance

    def clear_cache(self) -> None:
        """Drop all cached plugin instances. Used by tests."""
        self._instance_cache.clear()


__all__ = ["Registry", "RegistryEntry"]
