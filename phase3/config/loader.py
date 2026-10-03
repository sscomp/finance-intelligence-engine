"""Phase 3A config loader.

Loads Phase 3 YAML config files (config/phase3/*.yaml) and returns a
typed, validated config object that the engine and scorers consume.

Phase 3A uses Python's stdlib `yaml` (no PyYAML dependency added — falls
back to a minimal hand-rolled parser for the small subset of YAML we
use if PyYAML is unavailable). For now, we expect PyYAML; missing
import is reported as a clear ImportError.

Key files (config/phase3/):
  - enabled.yaml            → kill switch (just `enabled: true|false`)
  - decay.yaml              → signal_type → {function, half_life_days, ...}
  - source_weights.yaml     → source_type → {source_weight, type_weights}
  - scorers/macro.yaml      → ScorerWeights + dimension order for macro
  - scorers/industry.yaml   → same for industry
  - scorers/company.yaml    → same for company
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from phase3.datamodel import (
    COMPANY_DEFAULT_WEIGHTS,
    COMPANY_SCORER_TYPE,
    CompanyScorerConfig,
    DecayRule,
    INDUSTRY_DEFAULT_WEIGHTS,
    INDUSTRY_SCORER_TYPE,
    IndustryScorerConfig,
    MACRO_DEFAULT_WEIGHTS,
    MACRO_SCORER_TYPE,
    MacroScorerConfig,
    ScorerWeights,
    SourceWeightEntry,
)


# Default config root, overridable via env or explicit arg.
# Phase 6.1 portability: derived from the central runtime path boundary
# (FIE_CONFIG_DIR > project root) instead of the historical hard-coded
# /home/ubuntu/macro-report path. In a source checkout this resolves to
# <repo>/config/phase3.
_DEFAULT_ROOT = Path(os.environ.get("FIE_CONFIG_DIR") or Path(__file__).resolve().parents[2])
DEFAULT_CONFIG_ROOT = _DEFAULT_ROOT / "config" / "phase3"


def _try_import_yaml() -> Any:
    try:
        import yaml  # type: ignore[import-untyped]
        return yaml
    except ImportError:
        return None


class _MiniYamlError(ValueError):
    pass


def _mini_yaml_load(text: str) -> Any:
    """Tiny hand-rolled YAML loader for the strict subset Phase 3A uses.

    Supports (and only supports):
      - `# ...` end-of-line comments
      - `key: value` scalars (int / float / bool / str)
      - `key:` followed by an indented block of nested mappings
      - 2-space indentation only
      - blank lines

    Anything more exotic (lists, multi-line strings, anchors, flow
    style) raises _MiniYamlError. If the production deployment has
    PyYAML available, _try_import_yaml wins and we never get here.
    """
    # Strip comments and blank lines, keep indentation of body lines.
    raw_lines = text.splitlines()
    lines: list[tuple[int, str]] = []
    for ln in raw_lines:
        # Strip trailing comment (but not if the comment is inside a value
        # — our values are scalars, so this is safe for our subset).
        if "#" in ln:
            # Only treat # as comment if preceded by whitespace OR at col 0
            i = ln.find("#")
            if i == 0 or ln[i - 1] in (" ", "\t"):
                ln = ln[:i].rstrip()
        if not ln.strip():
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        body = ln.strip()
        if "\t" in ln[:indent]:
            raise _MiniYamlError(f"Tabs not allowed: {ln!r}")
        if indent % 2 != 0:
            raise _MiniYamlError(f"Indentation must be 2-space, got {indent}: {ln!r}")
        lines.append((indent, body))

    def _parse_scalar(s: str) -> Any:
        # Strip surrounding quotes if present
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            return s[1:-1]
        if s.lower() in ("true", "yes", "on"):
            return True
        if s.lower() in ("false", "no", "off"):
            return False
        if s.lower() in ("null", "~", ""):
            return None
        try:
            if "." in s or "e" in s.lower():
                return float(s)
            return int(s)
        except ValueError:
            return s

    pos = [0]  # mutable single-element list (closure pattern)

    def _parse_block(min_indent: int) -> dict:
        out: dict = {}
        while pos[0] < len(lines):
            indent, body = lines[pos[0]]
            if indent < min_indent:
                break
            if indent > min_indent:
                raise _MiniYamlError(
                    f"Unexpected indent jump at line {pos[0] + 1}: {body!r}"
                )
            if ":" not in body:
                raise _MiniYamlError(
                    f"Expected 'key: value' at line {pos[0] + 1}: {body!r}"
                )
            key, _, rest = body.partition(":")
            key = key.strip()
            rest = rest.strip()
            pos[0] += 1
            if not rest:
                # Nested block — determine its indent from the next non-empty line
                if pos[0] < len(lines) and lines[pos[0]][0] > indent:
                    out[key] = _parse_block(lines[pos[0]][0])
                else:
                    out[key] = None
            else:
                out[key] = _parse_scalar(rest)
        return out

    if not lines:
        return {}
    top_indent = lines[0][0]
    return _parse_block(top_indent)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read YAML using PyYAML if available, else our stdlib mini parser.

    Both code paths return the same shape (a nested dict) for our
    specific subset. If the file is missing, return {} (the loader
    treats missing files as 'use defaults').
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    yaml = _try_import_yaml()
    if yaml is not None:
        data = yaml.safe_load(text)
        return data or {}
    try:
        return _mini_yaml_load(text) or {}
    except _MiniYamlError as e:
        raise ValueError(
            f"Failed to parse {path} with stdlib YAML fallback: {e}. "
            "Install PyYAML or simplify the file to the supported subset."
        ) from e


def compute_config_hash(*paths: Path) -> str:
    """sha256 of concatenated file contents. Order-sensitive by argument order.

    Used to tag score results with a content hash so consumers can
    detect "this score was produced with a different config than the
    one I'm looking at now" without diffing full files.
    """
    h = hashlib.sha256()
    for p in paths:
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class LoadedConfig:
    """Top-level loaded config — what callers actually get back."""

    enabled: bool
    macro: MacroScorerConfig
    industry: IndustryScorerConfig
    company: CompanyScorerConfig
    decay_rules: dict[str, DecayRule]
    source_weights: dict[str, SourceWeightEntry]
    config_hash: str
    source_files: tuple[Path, ...]


class ConfigLoader:
    """Loads and validates Phase 3 YAML config.

    Validates:
      - All three scorer weight maps sum to 1.0 within float tolerance
      - Decay function names are known
      - source_weight is in [0, 1]
      - type_weights values are in [0, 1]

    Failures raise ValueError with a human-readable message and the
    offending field.
    """

    _ALLOWED_DECAY = {"linear", "exponential", "step", "none"}

    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root is not None else DEFAULT_CONFIG_ROOT

    @property
    def root(self) -> Path:
        return self._root

    def _read(self, rel: str) -> dict[str, Any]:
        return _read_yaml(self._root / rel)

    def _read_decay(self) -> dict[str, DecayRule]:
        data = self._read("decay.yaml")
        rules_raw = data.get("decay", data)  # accept {"decay": {...}} or flat
        rules: dict[str, DecayRule] = {}
        for signal_type, cfg in rules_raw.items():
            if not isinstance(cfg, dict):
                continue
            fn = str(cfg.get("function", "")).strip()
            if fn not in self._ALLOWED_DECAY:
                raise ValueError(
                    f"decay.yaml: signal_type {signal_type!r} has unknown "
                    f"function {fn!r}; expected one of {sorted(self._ALLOWED_DECAY)}"
                )
            rules[signal_type] = DecayRule.from_dict(
                {"signal_type": signal_type, **cfg}
            )
        return rules

    def _read_source_weights(self) -> dict[str, SourceWeightEntry]:
        data = self._read("source_weights.yaml")
        raw = data.get("sources", data)
        out: dict[str, SourceWeightEntry] = {}
        for source_id, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            sw = float(cfg.get("source_weight", 0.5))
            if not (0.0 <= sw <= 1.0):
                raise ValueError(
                    f"source_weights.yaml: {source_id!r} source_weight "
                    f"{sw} out of [0, 1]"
                )
            type_map_raw = cfg.get("type_weights", {}) or {}
            type_map: dict[str, float] = {}
            for k, v in type_map_raw.items():
                fv = float(v)
                if not (0.0 <= fv <= 1.0):
                    raise ValueError(
                        f"source_weights.yaml: {source_id!r} type_weights[{k!r}]"
                        f" {fv} out of [0, 1]"
                    )
                type_map[k] = fv
            out[source_id] = SourceWeightEntry(
                source_id=source_id,
                source_type=str(cfg.get("source_type", source_id)),
                source_weight=sw,
                type_weights=type_map,
            )
        return out

    def _read_scorer(
        self, rel: str, scorer_type: str, defaults: dict[str, float]
    ) -> tuple[ScorerWeights, int]:
        """Read one scorer's weights + ttl_hours from YAML.

        Returns (ScorerWeights, ttl_hours). If the YAML file is missing,
        falls back to the default weights (sum 1.0) and a 24h TTL.
        """
        data = self._read(rel)
        weights_raw = data.get("default_weights", data.get("weights", {}))
        if not weights_raw:
            weights_raw = defaults
        # The dataclass validates sum=1.0 within tolerance
        sw = ScorerWeights(
            scorer_type=scorer_type,
            weights={k: float(v) for k, v in weights_raw.items()},
            dimension_order=tuple(weights_raw.keys()),
        )
        ttl = int(data.get("ttl_hours", 24))
        return sw, ttl

    def load(self) -> LoadedConfig:
        enabled_raw = self._read("enabled.yaml")
        enabled_val = enabled_raw.get("enabled", True)
        if isinstance(enabled_val, str):
            enabled = enabled_val.lower() in ("1", "true", "yes", "on")
        else:
            enabled = bool(enabled_val)

        macro_sw, macro_ttl = self._read_scorer(
            "scorers/macro.yaml", MACRO_SCORER_TYPE, MACRO_DEFAULT_WEIGHTS
        )
        industry_sw, industry_ttl = self._read_scorer(
            "scorers/industry.yaml", INDUSTRY_SCORER_TYPE, INDUSTRY_DEFAULT_WEIGHTS
        )
        company_sw, company_ttl = self._read_scorer(
            "scorers/company.yaml", COMPANY_SCORER_TYPE, COMPANY_DEFAULT_WEIGHTS
        )

        decay_rules = self._read_decay()
        source_weights = self._read_source_weights()

        # Cross-layer tuning lives in cross_layer_adjustment.yaml (Phase 3B
        # makes this richer; Phase 3A reads it as a hint if present).
        cross = self._read("cross_layer_adjustment.yaml")
        macro_xl = float(cross.get("industry_to_company_weight", 0.10))
        company_macro_max = float(cross.get("macro_to_company_max_abs", 15.0))
        company_industry_max = float(cross.get("industry_to_company_max_abs", 10.0))

        macro_cfg = MacroScorerConfig(
            weights=macro_sw, decay_rules=decay_rules, source_weights=source_weights,
            ttl_hours=macro_ttl,
        )
        industry_cfg = IndustryScorerConfig(
            weights=industry_sw, decay_rules=decay_rules, source_weights=source_weights,
            ttl_hours=industry_ttl, cross_layer_macro_weight=macro_xl,
        )
        company_cfg = CompanyScorerConfig(
            weights=company_sw, decay_rules=decay_rules, source_weights=source_weights,
            ttl_hours=company_ttl,
            cross_layer_macro_max_abs=company_macro_max,
            cross_layer_industry_max_abs=company_industry_max,
        )

        # Hash the files we actually used (existence-aware)
        candidate_files = [
            self._root / "enabled.yaml",
            self._root / "decay.yaml",
            self._root / "source_weights.yaml",
            self._root / "scorers" / "macro.yaml",
            self._root / "scorers" / "industry.yaml",
            self._root / "scorers" / "company.yaml",
            self._root / "cross_layer_adjustment.yaml",
        ]
        existing = tuple(p for p in candidate_files if p.exists())
        cfg_hash = compute_config_hash(*existing)

        return LoadedConfig(
            enabled=enabled,
            macro=macro_cfg,
            industry=industry_cfg,
            company=company_cfg,
            decay_rules=decay_rules,
            source_weights=source_weights,
            config_hash=cfg_hash,
            source_files=existing,
        )


def load_config(root: Path | str | None = None) -> LoadedConfig:
    """Convenience wrapper. Same as ConfigLoader(root).load()."""
    return ConfigLoader(root).load()


__all__ = [
    "ConfigLoader",
    "LoadedConfig",
    "DEFAULT_CONFIG_ROOT",
    "load_config",
    "compute_config_hash",
]
