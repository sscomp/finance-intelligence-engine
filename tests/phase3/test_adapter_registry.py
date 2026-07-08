"""Tests for the Phase 3B adapter registry.

Contract:
* ``register`` adds an adapter class under a lowercased name.
* ``unregister`` removes it; ``clear`` resets to the module default.
* ``get`` returns the class; ``has`` is a bool; ``names`` is sorted.
* ``build`` instantiates with ``**kwargs``.
* All Phase 3B adapters are registered on package import.
* The registry is a plain dict (no auto-discovery) — names not in
  the table raise KeyError with a helpful message.
"""
from __future__ import annotations

import unittest

from phase3.signals.adapters import registry
from phase3.signals.adapters.base import SourceAdapter


class RegistryShapeTests(unittest.TestCase):
    def test_names_is_sorted_tuple(self) -> None:
        names = registry.names()
        self.assertIsInstance(names, tuple)
        self.assertEqual(names, tuple(sorted(names)))

    def test_default_includes_noop(self) -> None:
        # The Phase 3A scaffold adapter must always be present.
        self.assertTrue(registry.has("noop"))

    def test_phase3b_adapters_registered_on_import(self) -> None:
        for name in ("fixture", "yfinance", "rss", "t86", "macro"):
            with self.subTest(name=name):
                self.assertTrue(registry.has(name), f"{name} not registered")

    def test_get_returns_class(self) -> None:
        cls = registry.get("yfinance")
        self.assertTrue(issubclass(cls, SourceAdapter))

    def test_get_unknown_raises_keyerror_with_help(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            registry.get("not-a-real-adapter")
        msg = str(ctx.exception)
        self.assertIn("not-a-real-adapter", msg)
        # Should mention known names
        self.assertIn("yfinance", msg)
        self.assertIn("macro", msg)

    def test_has_is_case_insensitive(self) -> None:
        self.assertTrue(registry.has("YFinance"))
        self.assertTrue(registry.has("YFINANCE"))
        # Register under canonical lowercase internally:
        cls = registry.get("YFINANCE")
        self.assertTrue(issubclass(cls, SourceAdapter))

    def test_build_instantiates_with_kwargs(self) -> None:
        # T86Adapter accepts emit_industry_rollup kwarg
        from phase3.signals.adapters.t86 import T86Adapter
        adapter = registry.build("t86", emit_industry_rollup=False)
        self.assertIsInstance(adapter, T86Adapter)

    def test_register_rejects_non_subclass(self) -> None:
        with self.assertRaises(TypeError):
            registry.register("bogus", dict)  # type: ignore[arg-type]

    def test_register_rejects_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            registry.register("", SourceAdapter)

    def test_unregister_removes(self) -> None:
        registry.register("temp_test_adapter", SourceAdapter)
        self.assertTrue(registry.has("temp_test_adapter"))
        registry.unregister("temp_test_adapter")
        self.assertFalse(registry.has("temp_test_adapter"))

    def test_unregister_missing_is_noop(self) -> None:
        # Should not raise
        registry.unregister("never_existed_xyz")
        self.assertFalse(registry.has("never_existed_xyz"))

    def test_register_overwrites(self) -> None:
        class FakeAdapter(SourceAdapter):
            def adapt(self, raw):  # type: ignore[override]
                return []

        registry.register("overwrite_test", SourceAdapter)
        registry.register("overwrite_test", FakeAdapter)
        self.assertIs(registry.get("overwrite_test"), FakeAdapter)
        # Cleanup
        registry.unregister("overwrite_test")


if __name__ == "__main__":
    unittest.main()
