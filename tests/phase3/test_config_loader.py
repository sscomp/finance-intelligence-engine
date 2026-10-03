"""Tests for Phase 3A config loader: YAML + stdlib fallback + validation.

Coverage:
  - YAML configs load with sane defaults and types.
  - The stdlib `_mini_yaml_load` fallback parses the same files.
  - `compute_config_hash` is stable for the same files.
  - Missing files fall back to defaults without raising.
  - Invalid config raises ValueError.
  - Weight maps sum to 1.0 within tolerance.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from phase3.config.loader import (
    DEFAULT_CONFIG_ROOT,
    ConfigLoader,
    LoadedConfig,
    _mini_yaml_load,
    compute_config_hash,
    load_config,
)
from phase3.datamodel import (
    COMPANY_DEFAULT_WEIGHTS,
    INDUSTRY_DEFAULT_WEIGHTS,
    MACRO_DEFAULT_WEIGHTS,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG_ROOT = REPO_ROOT / "config" / "phase3"


# ---------- _mini_yaml_load ----------

class TestMiniYamlLoad(unittest.TestCase):
    def test_simple_scalar(self):
        data = _mini_yaml_load("foo: bar\n")
        self.assertEqual(data, {"foo": "bar"})

    def test_int_scalar(self):
        data = _mini_yaml_load("n: 42\n")
        self.assertEqual(data, {"n": 42})
        self.assertIsInstance(data["n"], int)

    def test_float_scalar(self):
        data = _mini_yaml_load("x: 0.5\n")
        self.assertEqual(data, {"x": 0.5})
        self.assertIsInstance(data["x"], float)

    def test_bool_true(self):
        data = _mini_yaml_load("enabled: true\n")
        self.assertEqual(data, {"enabled": True})

    def test_bool_false(self):
        data = _mini_yaml_load("enabled: false\n")
        self.assertEqual(data, {"enabled": False})

    def test_null(self):
        data = _mini_yaml_load("v: null\n")
        self.assertEqual(data, {"v": None})

    def test_nested_block(self):
        text = (
            "outer:\n"
            "  inner1: 1\n"
            "  inner2: 2\n"
        )
        data = _mini_yaml_load(text)
        self.assertEqual(data, {"outer": {"inner1": 1, "inner2": 2}})

    def test_double_nested(self):
        text = (
            "a:\n"
            "  b:\n"
            "    c: 1\n"
        )
        data = _mini_yaml_load(text)
        self.assertEqual(data, {"a": {"b": {"c": 1}}})

    def test_comment_skipped(self):
        text = "foo: bar  # this is a comment\n"
        data = _mini_yaml_load(text)
        self.assertEqual(data, {"foo": "bar"})

    def test_blank_lines_ignored(self):
        text = "foo: 1\n\nbar: 2\n"
        data = _mini_yaml_load(text)
        self.assertEqual(data, {"foo": 1, "bar": 2})

    def test_quoted_string(self):
        data = _mini_yaml_load('foo: "bar baz"\n')
        self.assertEqual(data, {"foo": "bar baz"})

    def test_empty(self):
        data = _mini_yaml_load("")
        self.assertEqual(data, {})

    def test_tabs_in_indent_strict_2space(self):
        # The parser enforces 2-space indentation. A 1-space indent
        # is rejected.
        with self.assertRaises(ValueError):
            _mini_yaml_load(" foo: 1\n")  # 1-space indent

    def test_3space_indent_raises(self):
        # 3-space indent is not 2-space → rejected.
        with self.assertRaises(ValueError):
            _mini_yaml_load("   foo: 1\n")  # 3-space indent

    def test_missing_colon_raises(self):
        with self.assertRaises(ValueError):
            _mini_yaml_load("this is not yaml\n")


# ---------- compute_config_hash ----------

class TestComputeConfigHash(unittest.TestCase):
    def test_hash_is_stable_for_same_files(self):
        a = compute_config_hash(REAL_CONFIG_ROOT / "decay.yaml",
                                 REAL_CONFIG_ROOT / "enabled.yaml")
        b = compute_config_hash(REAL_CONFIG_ROOT / "decay.yaml",
                                 REAL_CONFIG_ROOT / "enabled.yaml")
        self.assertEqual(a, b)
        self.assertIsInstance(a, str)
        # Hash is sha256[:16] = 16 hex chars
        self.assertEqual(len(a), 16)

    def test_hash_changes_with_file_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f1 = root / "a.yaml"
            f2 = root / "b.yaml"
            f1.write_text("foo: 1\n", encoding="utf-8")
            f2.write_text("bar: 2\n", encoding="utf-8")
            h1 = compute_config_hash(f1, f2)
            f1.write_text("foo: 999\n", encoding="utf-8")
            h2 = compute_config_hash(f1, f2)
            self.assertNotEqual(h1, h2)

    def test_hash_handles_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "a.yaml"
            f.write_text("foo: 1\n", encoding="utf-8")
            ghost = root / "missing.yaml"
            # Should not raise
            h = compute_config_hash(f, ghost)
            self.assertIsInstance(h, str)
            self.assertEqual(len(h), 16)


# ---------- ConfigLoader on real configs ----------

class TestConfigLoaderReal(unittest.TestCase):
    def setUp(self):
        self.loader = ConfigLoader(REAL_CONFIG_ROOT)
        self.cfg: LoadedConfig = self.loader.load()

    def test_load_returns_loaded_config(self):
        self.assertIsInstance(self.cfg, LoadedConfig)

    def test_enabled_field(self):
        # enabled.yaml exists and has a value
        self.assertIsInstance(self.cfg.enabled, bool)

    def test_macro_weights_sum_to_one(self):
        s = sum(self.cfg.macro.weights.weights.values())
        self.assertAlmostEqual(s, 1.0, places=6)

    def test_industry_weights_sum_to_one(self):
        s = sum(self.cfg.industry.weights.weights.values())
        self.assertAlmostEqual(s, 1.0, places=6)

    def test_company_weights_sum_to_one(self):
        s = sum(self.cfg.company.weights.weights.values())
        self.assertAlmostEqual(s, 1.0, places=6)

    def test_macro_weights_have_six_dims(self):
        self.assertEqual(len(self.cfg.macro.weights.weights), 6)
        self.assertEqual(
            len(self.cfg.macro.weights.dimension_order), 6
        )

    def test_industry_weights_have_six_dims(self):
        self.assertEqual(len(self.cfg.industry.weights.weights), 6)

    def test_company_weights_have_seven_dims(self):
        self.assertEqual(len(self.cfg.company.weights.weights), 7)

    def test_decay_rules_loaded(self):
        # decay.yaml has rules; at least a few should be present
        self.assertGreater(len(self.cfg.decay_rules), 0)
        # Each rule has a known function
        for k, v in self.cfg.decay_rules.items():
            self.assertIn(v.function, {"linear", "exponential", "step", "none"})

    def test_source_weights_loaded(self):
        # source_weights.yaml has entries
        self.assertGreater(len(self.cfg.source_weights), 0)
        for k, v in self.cfg.source_weights.items():
            self.assertGreaterEqual(v.source_weight, 0.0)
            self.assertLessEqual(v.source_weight, 1.0)

    def test_config_hash_present(self):
        self.assertIsInstance(self.cfg.config_hash, str)
        self.assertEqual(len(self.cfg.config_hash), 16)

    def test_config_hash_stable_across_loads(self):
        a = self.loader.load().config_hash
        b = self.loader.load().config_hash
        self.assertEqual(a, b)

    def test_ttl_hours_loaded(self):
        # macro/industry default 24, company default 168
        self.assertIsInstance(self.cfg.macro.ttl_hours, int)
        self.assertIsInstance(self.cfg.industry.ttl_hours, int)
        self.assertIsInstance(self.cfg.company.ttl_hours, int)

    def test_source_files_populated(self):
        # At least one config file exists
        self.assertGreater(len(self.cfg.source_files), 0)
        for p in self.cfg.source_files:
            self.assertTrue(p.exists())

    def test_default_config_root(self):
        self.assertEqual(DEFAULT_CONFIG_ROOT, REAL_CONFIG_ROOT)

    def test_load_config_helper(self):
        cfg = load_config(REAL_CONFIG_ROOT)
        self.assertIsInstance(cfg, LoadedConfig)
        self.assertEqual(cfg.config_hash, self.cfg.config_hash)


# ---------- ConfigLoader: defaults / missing files ----------

class TestConfigLoaderMissing(unittest.TestCase):
    def test_missing_root_uses_defaults(self):
        # Empty tempdir — all config files "missing" → uses defaults.
        with tempfile.TemporaryDirectory() as td:
            loader = ConfigLoader(td)
            cfg = loader.load()
            self.assertIsInstance(cfg, LoadedConfig)
            # Defaults should be applied
            self.assertEqual(
                cfg.macro.weights.weights, MACRO_DEFAULT_WEIGHTS
            )
            self.assertEqual(
                cfg.industry.weights.weights, INDUSTRY_DEFAULT_WEIGHTS
            )
            self.assertEqual(
                cfg.company.weights.weights, COMPANY_DEFAULT_WEIGHTS
            )
            # config_hash still works (no files → still a 16-char hash)
            self.assertEqual(len(cfg.config_hash), 16)

    def test_empty_root_decay_and_source_weights_empty(self):
        with tempfile.TemporaryDirectory() as td:
            loader = ConfigLoader(td)
            cfg = loader.load()
            self.assertEqual(cfg.decay_rules, {})
            self.assertEqual(cfg.source_weights, {})


# ---------- ConfigLoader: invalid configs ----------

class TestConfigLoaderInvalid(unittest.TestCase):
    def test_invalid_decay_function_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "decay.yaml").write_text(
                "decay:\n  my_signal:\n    function: garbled\n    half_life_days: 7\n",
                encoding="utf-8",
            )
            # Need a full set of files to load everything else
            for f in ("enabled.yaml", "source_weights.yaml",
                      "scorers/macro.yaml", "scorers/industry.yaml",
                      "scorers/company.yaml"):
                p = root / f
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("placeholder: 1\n", encoding="utf-8")
            loader = ConfigLoader(root)
            with self.assertRaises(ValueError) as cm:
                loader.load()
            self.assertIn("garbled", str(cm.exception))

    def test_source_weight_out_of_range_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "source_weights.yaml").write_text(
                "sources:\n  bad:\n    source_weight: 2.0\n",
                encoding="utf-8",
            )
            (root / "scorers").mkdir(parents=True, exist_ok=True)
            for f in ("enabled.yaml", "decay.yaml",
                      "scorers/macro.yaml", "scorers/industry.yaml",
                      "scorers/company.yaml"):
                p = root / f
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("placeholder: 1\n", encoding="utf-8")
            loader = ConfigLoader(root)
            with self.assertRaises(ValueError) as cm:
                loader.load()
            self.assertIn("out of", str(cm.exception).lower())

    def test_invalid_scorer_weights_sum_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scorers").mkdir(parents=True, exist_ok=True)
            # Macro weights that don't sum to 1.0
            (root / "scorers" / "macro.yaml").write_text(
                "default_weights:\n  economic: 0.7\n  monetary: 0.7\n",
                encoding="utf-8",
            )
            for f in ("enabled.yaml", "decay.yaml", "source_weights.yaml",
                      "scorers/industry.yaml", "scorers/company.yaml"):
                p = root / f
                p.write_text("placeholder: 1\n", encoding="utf-8")
            loader = ConfigLoader(root)
            with self.assertRaises(ValueError) as cm:
                loader.load()
            # ScorerWeights dataclass raises "weights must sum to 1.0"
            self.assertIn("sum", str(cm.exception).lower())


# ---------- ConfigLoader: subset / minimal viable ----------

class TestConfigLoaderSubset(unittest.TestCase):
    def test_only_enabled_uses_all_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "enabled.yaml").write_text("enabled: true\n",
                                                encoding="utf-8")
            loader = ConfigLoader(root)
            cfg = loader.load()
            self.assertTrue(cfg.enabled)
            # All weights defaulted
            self.assertEqual(
                cfg.macro.weights.weights, MACRO_DEFAULT_WEIGHTS
            )

    def test_enabled_string_true(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "enabled.yaml").write_text('enabled: "true"\n',
                                                encoding="utf-8")
            loader = ConfigLoader(root)
            cfg = loader.load()
            self.assertTrue(cfg.enabled)

    def test_enabled_string_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "enabled.yaml").write_text('enabled: "false"\n',
                                                encoding="utf-8")
            loader = ConfigLoader(root)
            cfg = loader.load()
            self.assertFalse(cfg.enabled)


if __name__ == "__main__":
    unittest.main()
