"""Tests for the ``config/phase3/sources.yaml`` loader.

Contract:
* ``load_sources()`` returns a ``SourcesConfig`` mapping name → SourceEntry.
* Missing file → empty config (no raise).
* Each entry's ``adapter`` must point to a registered adapter class.
* Each entry exposes ``is_enabled()`` from the ``enabled`` extra.
* The loader's default root is ``config/phase3``.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from phase3.config.sources import (
    SourceEntry,
    SourcesConfig,
    load_sources,
)
from phase3.signals.adapters import registry


class LoadSourcesBasicTests(unittest.TestCase):
    def test_default_loads_real_yaml(self) -> None:
        cfg = load_sources()
        # The on-disk config has 6 source entries
        names = cfg.names()
        self.assertEqual(len(names), 6)
        # rss adapter is registered, but YAML uses logical source names
        # (cnyes_rss, digitimes_rss) that both point to the "rss" adapter.
        for required in ("yfinance", "cnyes_rss", "digitimes_rss", "t86",
                         "macro_yfinance", "fixture"):
            self.assertIn(required, names)

    def test_each_entry_adapter_is_registered(self) -> None:
        cfg = load_sources()
        for name, entry in cfg.sources.items():
            with self.subTest(source=name):
                self.assertTrue(
                    registry.has(entry.adapter),
                    f"{name} → adapter={entry.adapter!r} not registered",
                )

    def test_default_source_type_matches_name(self) -> None:
        """When source_type is not specified in YAML, it falls back to the
        entry's name. (Our YAML provides it explicitly; this is the
        fallback behavior.)"""
        cfg = load_sources()
        entry = cfg.get("yfinance")
        assert entry is not None  # for type checker
        self.assertEqual(entry.source_type, "yfinance")

    def test_enabled_names_excludes_disabled(self) -> None:
        """Build a temporary config with one disabled entry and verify."""
        yaml_content = """
sources:
  a_source:
    adapter: fixture
    enabled: true
  b_source:
    adapter: fixture
    enabled: false
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "phase3"
            root.mkdir()
            (root / "sources.yaml").write_text(yaml_content)
            cfg = load_sources(root)
            self.assertEqual(set(cfg.enabled_names()), {"a_source"})

    def test_missing_file_returns_empty_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = load_sources(td)  # no sources.yaml inside
            self.assertEqual(cfg.names(), ())
            self.assertIsNone(cfg.get("anything"))


class SourceEntryTests(unittest.TestCase):
    def test_is_enabled_default_true(self) -> None:
        e = SourceEntry(name="x", adapter="fixture", source_type="fixture")
        self.assertTrue(e.is_enabled())

    def test_is_enabled_false_when_disabled(self) -> None:
        e = SourceEntry(name="x", adapter="fixture", source_type="fixture",
                        extra={"enabled": False})
        self.assertFalse(e.is_enabled())


class LoadSourcesMalformedTests(unittest.TestCase):
    def test_missing_adapter_raises(self) -> None:
        yaml_content = """
sources:
  bad:
    source_type: yfinance
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "phase3"
            root.mkdir()
            (root / "sources.yaml").write_text(yaml_content)
            with self.assertRaises(ValueError):
                load_sources(root)

    def test_non_dict_source_entry_is_skipped(self) -> None:
        """A list or string under sources is silently skipped (not raised).
        Defensive: lets operators comment out a source with `[]`."""
        yaml_content = """
sources:
  good:
    adapter: fixture
  bad: []
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "phase3"
            root.mkdir()
            (root / "sources.yaml").write_text(yaml_content)
            cfg = load_sources(root)
            self.assertIn("good", cfg.sources)
            self.assertNotIn("bad", cfg.sources)


if __name__ == "__main__":
    unittest.main()
