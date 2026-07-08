"""Scoring subpackage. See docs/phase3/03..05 macro/industry/company
scoring specs and 07_data_model.md for the result shape.

The three scorers (Macro/Industry/Company) are independent of each
other; cross-layer adjustments live in scoring.cross_layer (Phase 3B)
or inline in the CompanyScorer for Phase 3A scaffold.
"""
from __future__ import annotations

from phase3.scoring.explain import explain_score
from phase3.scoring.macro import MacroScorer

try:
    from phase3.scoring.industry import IndustryScorer
except ImportError:
    IndustryScorer = None  # type: ignore[assignment]

try:
    from phase3.scoring.company import CompanyScorer
except ImportError:
    CompanyScorer = None  # type: ignore[assignment]

__all__ = [
    "CompanyScorer",
    "IndustryScorer",
    "MacroScorer",
    "explain_score",
]
