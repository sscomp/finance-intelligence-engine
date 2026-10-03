"""Typed external contracts for the FIE service boundary (Phase 6.5).

These dataclasses are the *only* objects the reference transport
adapter (``phase3.service.reference``) serializes for external
clients. Rules (work order §5/§5.1):

* contracts distinguish DOMAIN DATA, FRESHNESS METADATA,
  PROVENANCE/EVIDENCE and ERROR/AVAILABILITY STATE;
* no persistence backend vocabulary leaks through: no connection
  strings, no SQL, no table names as client-required knowledge, no
  local paths, no backend row objects, no credentials;
* only ``to_dict()`` JSON-safe views are the external shape —
  clients never depend on Python object identity;
* vocabulary reuses the repository/domain layer
  (``entity_type`` macro/industry/company, ``ScoreSnapshotRecord``,
  graph node ids such as ``score:company:2330``) — nothing invented.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: Version of *this service contract* (independent of the domain
#: ``schema_version`` 3.0 recorded inside persisted rows).
SCHEMA_VERSION = "6.5"

#: Read-only service operations (§6). Mutating operations are not
#: part of this contract — the set exists so tests can assert that
#: the reference adapter only routes these names.
OPERATIONS = (
    "health",
    "latest_intelligence",
    "entity_intelligence",
    "evidence",
    "freshness",
)


def _json(value: Any) -> Any:
    """Defensive JSON-safe normalization for external payloads."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


# ---------------------------------------------------------------------------
# Request context (multi-user boundary, work order §7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestContext:
    """Opaque per-call identity/context — synthetic in Phase 6.5.

    ``principal_id`` is an opaque token at the boundary (never an
    email address, hostname, or filesystem path). FIE core does not
    interpret it; future authn/authz layers may map external
    identities onto it. ``scopes`` restricts what the call may see;
    Phase 6.5 operations are read-only and ignore them functionally,
    but the field is honored/propagated so the contract cannot grow
    into one that cannot carry identity.
    """

    principal_id: str = ""
    request_id: str = ""
    scopes: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "request_id": self.request_id,
            "scopes": sorted(self.scopes),
        }


# ---------------------------------------------------------------------------
# Freshness (work order §8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Freshness:
    """Deterministic freshness statement for returned intelligence.

    ``state`` uses the Phase 6.5 service vocabulary; the governance
    policy (``phase3.freshness``) underneath is unchanged:

    =============== =========================== ======================
    service state   governance state            meaning for a client
    =============== =========================== ======================
    FRESH           FRESH                       computed from known
                                                current data
    DEGRADED        STALE                       result is valid but
                                                upstream observation
                                                is older than its
                                                freshness window
    STALE           EXPIRED                     result is past its
                                                hard freshness window
                                                — usable as history,
                                                not as current truth
    UNAVAILABLE     UNAVAILABLE / no record     no deterministic
                                                result exists yet
    =============== =========================== ======================

    A response never silently converts STALE/UNAVAILABLE data into a
    current-data claim: the state rides on the envelope.
    """

    state: str  # FRESH | DEGRADED | STALE | UNAVAILABLE
    as_of: str = ""  # the date the intelligence claims to describe
    checked_at: str = ""  # app-layer UTC stamp of the check itself
    source_date: str | None = None  # underlying observation date
    age: int | None = None  # in the data class's unit (§ phase3.freshness)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "as_of": self.as_of,
            "checked_at": self.checked_at,
            "source_date": self.source_date,
            "age": self.age,
        }


# ---------------------------------------------------------------------------
# Evidence / provenance (work order §9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceReference:
    """Stable, db-agnostic reference to one evidence item.

    ``ref`` is the domain node id (``score:company:2330``,
    ``signal:macro:global``, …) — domain vocabulary, not a database
    key/rowid. ``kind`` carries the node type; ``direction``/``edge_type``
    describe how this evidence was collected relative to the
    requesting entity when the reference came from graph traversal.
    """

    ref: str
    kind: str = ""
    edge_type: str = ""  # non-empty when reached via an edge
    direction: str = ""  # upstream | downstream (contextual, optional)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "edge_type": self.edge_type,
            "direction": self.direction,
        }


@dataclass(frozen=True)
class EvidenceItem:
    """One structured provenance record (bounded, never a raw dump)."""

    ref: str
    kind: str
    label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    computed_at: str = ""
    related: list[EvidenceReference] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _json(
            {
                "ref": self.ref,
                "kind": self.kind,
                "label": self.label,
                "metadata": self.metadata,
                "tags": self.tags,
                "computed_at": self.computed_at,
                "related": [r.to_dict() for r in self.related],
            }
        )


# ---------------------------------------------------------------------------
# Domain summaries (work order §4.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntelligenceSummary:
    """Latest-intelligence summary for one entity snapshot."""

    kind: str  # macro | industry | company
    entity_id: str
    score: float
    computed_at: str
    as_of: str  # date bucket the score describes
    freshness: Freshness
    evidence_refs: list[EvidenceReference] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _json(
            {
                "schema_version": self.schema_version,
                "kind": self.kind,
                "entity_id": self.entity_id,
                "score": self.score,
                "computed_at": self.computed_at,
                "as_of": self.as_of,
                "freshness": self.freshness.to_dict(),
                "evidence_refs": [r.to_dict() for r in self.evidence_refs],
                "warnings": self.warnings,
            }
        )


@dataclass(frozen=True)
class EntityIntelligence:
    """Entity-scoped intelligence: domain result + freshness + provenance."""

    kind: str
    entity_id: str
    as_of: str
    freshness: Freshness
    score: float | None = None  # None with EMPTY/UNAVAILABLE state
    payload: dict[str, Any] = field(default_factory=dict)  # domain result
    signals: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[EvidenceReference] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    computed_at: str = ""
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _json(
            {
                "schema_version": self.schema_version,
                "kind": self.kind,
                "entity_id": self.entity_id,
                "as_of": self.as_of,
                "freshness": self.freshness.to_dict(),
                "score": self.score,
                "payload": self.payload,
                "signals": self.signals,
                "evidence_refs": [r.to_dict() for r in self.evidence_refs],
                "warnings": self.warnings,
                "computed_at": self.computed_at,
            }
        )


@dataclass(frozen=True)
class HealthReport:
    """Readiness state without leaking backend implementation details.

    ``backend_kind`` is the coarse backend family (``sqlite`` /
    ``postgres``) — a deployment fact a client may care about for
    availability reasoning, never a connection string or path.
    """

    status: str  # ok | degraded | unavailable
    backend_kind: str = ""  # sqlite | postgres (no DSN/details)
    schema_current_version: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    checked_at: str = ""
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _json(
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "backend_kind": self.backend_kind,
                "schema_current_version": self.schema_current_version,
                "counts": self.counts,
                "warnings": self.warnings,
                "checked_at": self.checked_at,
            }
        )


__all__ = [
    "SCHEMA_VERSION",
    "OPERATIONS",
    "RequestContext",
    "Freshness",
    "EvidenceReference",
    "EvidenceItem",
    "IntelligenceSummary",
    "EntityIntelligence",
    "HealthReport",
]


def json_safe(value: Any) -> Any:
    """Public alias of the internal normalizer (transport adapters use it)."""
    return _json(value)


__all__ += ["json_safe"]