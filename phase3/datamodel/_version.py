"""Phase 3 schema version constants. Bump on breaking dataclass changes."""
from __future__ import annotations

SCHEMA_VERSION: str = "3.0"

# Scorer type discriminator (also used in JSON manifest)
MACRO_SCORER_TYPE: str = "macro"
INDUSTRY_SCORER_TYPE: str = "industry"
COMPANY_SCORER_TYPE: str = "company"

# Semver-like (Phase 3A = 0.1.0, matches "scaffold" status)
MACRO_SCORER_VERSION: str = "0.1.0"
INDUSTRY_SCORER_VERSION: str = "0.1.0"
COMPANY_SCORER_VERSION: str = "0.1.0"
