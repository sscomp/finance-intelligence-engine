"""Pure data layer. No I/O. No imports from phase3/* siblings.

Module split follows docs/phase3/10_repo_structure.md §1 (slimmed for Phase 3A).
Each file owns one logical grouping of frozen dataclasses.
"""
from __future__ import annotations

from phase3.datamodel._version import (
    COMPANY_SCORER_TYPE,
    COMPANY_SCORER_VERSION,
    INDUSTRY_SCORER_TYPE,
    INDUSTRY_SCORER_VERSION,
    MACRO_SCORER_TYPE,
    MACRO_SCORER_VERSION,
    SCHEMA_VERSION,
)
from phase3.datamodel.config import (
    COMPANY_DEFAULT_WEIGHTS,
    CompanyScorerConfig,
    DecayRule,
    INDUSTRY_DEFAULT_WEIGHTS,
    IndustryScorerConfig,
    MACRO_DEFAULT_WEIGHTS,
    MacroScorerConfig,
    ScorerWeights,
    SourceWeightEntry,
)
from phase3.datamodel.evidence import Evidence
from phase3.datamodel.graph import (
    DEFAULT_GRAPH_CONFIG,
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    make_graph_edge_id,
)
from phase3.datamodel.scores import (
    CrossLayerAdjustment,
    DimensionResult,
    ScoreBreakdown,
    SubIndicatorResult,
    WeightedFactor,
)
from phase3.datamodel.scores_company import (
    COMPANY_DIMENSIONS,
    CompanyScore,
)
from phase3.datamodel.scores_industry import (
    INDUSTRY_DIMENSIONS,
    IndustryScore,
)
from phase3.datamodel.scores_macro import (
    MACRO_DIMENSIONS,
    MACRO_ENTITY_ID,
    MACRO_ENTITY_TYPE,
    MacroScore,
)
from phase3.datamodel.signals import (
    DEFAULT_DECAY_CONFIG,
    Direction,
    Signal,
    SignalSource,
    make_signal_id,
)

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
    # Signal family
    "Signal",
    "SignalSource",
    "make_signal_id",
    "Evidence",
    "WeightedFactor",
    "ScoreBreakdown",
    "SubIndicatorResult",
    "DimensionResult",
    "CrossLayerAdjustment",
    # Scorers
    "MacroScore",
    "IndustryScore",
    "CompanyScore",
    "MACRO_DIMENSIONS",
    "MACRO_ENTITY_ID",
    "MACRO_ENTITY_TYPE",
    "INDUSTRY_DIMENSIONS",
    "COMPANY_DIMENSIONS",
    # Graph
    "GraphNode",
    "GraphEdge",
    "make_graph_edge_id",
    # Config dataclasses
    "ScorerWeights",
    "DecayRule",
    "SourceWeightEntry",
    "MacroScorerConfig",
    "IndustryScorerConfig",
    "CompanyScorerConfig",
]
