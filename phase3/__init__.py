"""Phase 3 — Investment Intelligence Engine.

Public API surface. Internal modules under phase3/* should NOT be imported
by production code; only this __init__ exports are stable.

Phase 3A status: SCAFFOLD ONLY. No production wiring. No live data ingestion.
All scoring is in-memory and driven by explicit inputs.

Import boundary reminder (docs/phase3/10_repo_structure.md §2):
  - phase3 MUST NOT import from reports/* (Phase 2B framework internals).
  - phase3 MUST NOT import existing production modules (macro_daily, etc.).
  - phase3/datamodel/ MUST be pure data (no I/O, no other phase3 imports).

Note: Sub-package imports below are wrapped in try/except so that an
incomplete scaffold (e.g. only datamodel/ built) can still be imported
for partial smoke tests. Once Phase 3A scaffold is complete, every
symbol listed in __all__ will be available.
"""
from __future__ import annotations

# Stable public API (Phase 3A scaffold)
from phase3.datamodel import (
    COMPANY_DEFAULT_WEIGHTS,
    COMPANY_SCORER_TYPE,
    COMPANY_SCORER_VERSION,
    DEFAULT_DECAY_CONFIG,
    DEFAULT_GRAPH_CONFIG,
    INDUSTRY_DEFAULT_WEIGHTS,
    INDUSTRY_SCORER_TYPE,
    INDUSTRY_SCORER_VERSION,
    MACRO_DEFAULT_WEIGHTS,
    MACRO_SCORER_TYPE,
    MACRO_SCORER_VERSION,
    SCHEMA_VERSION,
    CompanyScore,
    CrossLayerAdjustment,
    Direction,
    DimensionResult,
    EdgeType,
    Evidence,
    GraphEdge,
    GraphNode,
    IndustryScore,
    MacroScore,
    NodeType,
    ScoreBreakdown,
    Signal,
    SignalSource,
    SubIndicatorResult,
    WeightedFactor,
)

# Optional downstream modules — import lazily so a partial scaffold can
# still be smoke-tested. After Phase 3A scaffold is complete, all of
# these will be present.
_OPTIONAL = (
    ("phase3.scoring.company", "CompanyScorer"),
    ("phase3.scoring.explain", "explain_score"),
    ("phase3.scoring.industry", "IndustryScorer"),
    ("phase3.scoring.macro", "MacroScorer"),
    ("phase3.graph.evidence_tracer", "EvidenceTracer"),
    ("phase3.graph.in_memory_store", "InMemoryGraphStore"),
    ("phase3.graph.traversal", "bfs"),
    ("phase3.graph.traversal", "shortest_path"),
    ("phase3.signals.aggregator", "SignalAggregator"),
    ("phase3.signals.confidence", "compute_confidence"),
    ("phase3.signals.decay", "DECAY_FUNCTIONS"),
    ("phase3.signals.decay", "apply_decay"),
    ("phase3.signals.engine", "SignalEngine"),
    ("phase3.signals.weighting", "combine_weights"),
    ("phase3.config.loader", "ConfigLoader"),
    ("phase3.config.loader", "load_config"),
)
_missing: list[str] = []
for _mod, _name in _OPTIONAL:
    try:
        _m = __import__(_mod, fromlist=[_name])
        globals()[_name] = getattr(_m, _name)
    except (ImportError, AttributeError) as _e:
        globals()[_name] = None
        _missing.append(f"{_mod}.{_name}: {_e}")

__all__ = [
    # Versioning
    "SCHEMA_VERSION",
    "MACRO_SCORER_TYPE",
    "MACRO_SCORER_VERSION",
    "INDUSTRY_SCORER_TYPE",
    "INDUSTRY_SCORER_VERSION",
    "COMPANY_SCORER_TYPE",
    "COMPANY_SCORER_VERSION",
    # Defaults
    "MACRO_DEFAULT_WEIGHTS",
    "INDUSTRY_DEFAULT_WEIGHTS",
    "COMPANY_DEFAULT_WEIGHTS",
    "DEFAULT_DECAY_CONFIG",
    "DEFAULT_GRAPH_CONFIG",
    # Enums
    "Direction",
    "NodeType",
    "EdgeType",
    # Dataclasses
    "Signal",
    "SignalSource",
    "Evidence",
    "WeightedFactor",
    "ScoreBreakdown",
    "SubIndicatorResult",
    "DimensionResult",
    "CrossLayerAdjustment",
    "MacroScore",
    "IndustryScore",
    "CompanyScore",
    "GraphNode",
    "GraphEdge",
    # Signal engine (None until scaffold complete)
    "SignalEngine",
    "apply_decay",
    "DECAY_FUNCTIONS",
    "combine_weights",
    "compute_confidence",
    "SignalAggregator",
    # Scorers (None until scaffold complete)
    "MacroScorer",
    "IndustryScorer",
    "CompanyScorer",
    "explain_score",
    # Graph (None until scaffold complete)
    "InMemoryGraphStore",
    "bfs",
    "shortest_path",
    "EvidenceTracer",
    # Config (None until scaffold complete)
    "ConfigLoader",
    "load_config",
]


def missing_optional() -> list[str]:
    """Return the list of optional symbols that are not yet importable.

    Useful as a single-call health check during Phase 3A scaffolding.
    Empty list = scaffold complete.
    """
    return list(_missing)
