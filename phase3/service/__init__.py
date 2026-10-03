"""FIE service boundary (Phase 6.5) — transport-neutral read side.

Public surface:

* :class:`IntelligenceService` protocol +
  :class:`DefaultIntelligenceService` implementation
  (:mod:`phase3.service.boundary`);
* typed external contracts (:mod:`phase3.service.contracts`);
* stable error taxonomy (:mod:`phase3.service.errors`);
* service freshness vocabulary over the unchanged governance policy
  (:mod:`phase3.service.freshness`);
* in-process reference transport adapter
  (:mod:`phase3.service.reference`);
* batch worker boundary (:mod:`phase3.service.batch`);
* cloud-safe runtime configuration
  (:mod:`phase3.service.runtime_config`).

All ChatGPT-facing operations are read-only in this phase.
"""
from __future__ import annotations

from phase3.service.batch import BatchResult, BatchWorker
from phase3.service.boundary import (
    DefaultIntelligenceService,
    IntelligenceService,
)
from phase3.service.contracts import (
    OPERATIONS,
    SCHEMA_VERSION,
    EntityIntelligence,
    EvidenceItem,
    EvidenceReference,
    Freshness,
    HealthReport,
    IntelligenceSummary,
    RequestContext,
    json_safe,
)
from phase3.service.errors import ServiceError, ServiceErrorCode
from phase3.service.reference import dispatch

__all__ = [
    "IntelligenceService",
    "DefaultIntelligenceService",
    "RequestContext",
    "Freshness",
    "EvidenceReference",
    "EvidenceItem",
    "IntelligenceSummary",
    "EntityIntelligence",
    "HealthReport",
    "BatchWorker",
    "BatchResult",
    "ServiceError",
    "ServiceErrorCode",
    "dispatch",
    "json_safe",
    "OPERATIONS",
    "SCHEMA_VERSION",
]