"""Transport-neutral FIE service boundary (Phase 6.5).

The boundary is the *only* application-facing surface for clients
(ChatGPT in design; other authorized clients later). It exposes
read-only intelligence operations backed by the existing domain
logic and repositories — it is deliberately thin (§1.3): no scoring,
no persistence vocabulary (§5.1), no writes (§6).

Interactive/batch separation (§1.4):
* THIS module is the interactive read path — every operation is a
  pure read over an already-persisted state, and the service never
  triggers ingestion/scoring to satisfy a read.
* Scheduled computation enters through :mod:`phase3.service.batch`
  (the batch worker boundary), which drives the existing
  application entry points. The two paths are different call arcs.

Persistence abstraction (§1.5): the service depends on the
:class:`~phase3.persistence.contracts.DatabaseStore` seam + the
repository layer; it contains no dialect-specific SQL and works on
SQLite and PostgreSQL alike (contract-tested).
"""
from __future__ import annotations

from typing import Any, Protocol

from phase3.persistence.contracts import DatabaseStore
from phase3.persistence.migrations import MigrationManager
from phase3.persistence.score_repo import ScoreRepository, ScoreSnapshotRecord
from phase3.persistence.signal_repo import SignalRepository
from phase3.service import contracts as C
from phase3.service.errors import (
    ServiceError,
    ServiceErrorCode,
    sanitize_for_error,
)
from phase3.service import freshness as _freshness
from phase3.service.timeutil import utc_now_iso

__all__ = ["IntelligenceService", "DefaultIntelligenceService"]


class IntelligenceService(Protocol):
    """The application service boundary (Phase 6.5 §4).

    All operations are read-only. Operations take an optional
    :class:`~phase3.service.contracts.RequestContext` as their last
    positional argument so a future transport can always carry an
    opaque principal/request identity without a signature change.
    """

    def get_health(self, ctx: C.RequestContext | None = None) -> C.HealthReport: ...

    def get_latest_intelligence(
        self,
        kind: str | None = None,
        *,
        limit: int = 50,
        ctx: C.RequestContext | None = None,
    ) -> list[C.IntelligenceSummary]: ...

    def get_entity_intelligence(
        self,
        kind: str,
        entity_id: str,
        *,
        as_of: str | None = None,
        ctx: C.RequestContext | None = None,
    ) -> C.EntityIntelligence: ...

    def get_evidence(
        self,
        ref: str,
        *,
        limit: int = 50,
        ctx: C.RequestContext | None = None,
    ) -> C.EvidenceItem: ...

    def get_freshness(
        self,
        kind: str,
        entity_id: str,
        *,
        as_of: str | None = None,
        ctx: C.RequestContext | None = None,
    ) -> C.Freshness: ...


_KINDS = ("macro", "industry", "company")

#: Cap on evidence collections (§9: no unbounded dumps).
_MAX_EVIDENCE_LIMIT = 200
_DEFAULT_EVIDENCE_LIMIT = 50


def _validate_kind(kind: str) -> str:
    if kind not in _KINDS:
        raise ServiceError(
            ServiceErrorCode.INVALID_REQUEST,
            f"unknown kind {kind!r}; expected one of {list(_KINDS)}",
        )
    return kind


def _validate_limit(limit: int, *, cap: int = _MAX_EVIDENCE_LIMIT) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ServiceError(
            ServiceErrorCode.INVALID_REQUEST, "limit must be an integer"
        )
    if limit < 1:
        raise ServiceError(
            ServiceErrorCode.INVALID_REQUEST, "limit must be >= 1"
        )
    return min(limit, cap)


class DefaultIntelligenceService:
    """Default :class:`IntelligenceService` over a persistence seam.

    Parameters
    ----------
    store:
        An open :class:`~phase3.persistence.contracts.DatabaseStore`
        (schema already migrated — the service never creates
        ad-hoc schema, work order §13).
    read_only:
        When True (default) the underlying connection is set
        read-only for the whole service lifetime
        (:meth:`~phase3.persistence.contracts.DatabaseStore.set_query_only`
        seam), structurally enforcing the read-only boundary.
    """

    def __init__(self, store: DatabaseStore, *, read_only: bool = True) -> None:
        self._store = store
        if read_only:
            store.set_query_only()
        self._score_repo = ScoreRepository(store)
        self._signal_repo = SignalRepository(store)
        # Evidence graph over the same persistence seam (§9): the
        # GraphRepository is the domain-facing graph contract, not the
        # backend store — nothing dialect-specific is reachable here.
        from phase3.persistence.graph_repo import GraphRepository

        self._graph_repo = GraphRepository(store)

    # -- health -----------------------------------------------------------

    def get_health(self, ctx: C.RequestContext | None = None) -> C.HealthReport:
        """Readiness across the persistence abstraction, not its innards."""
        warnings: list[str] = []
        try:
            counts = {
                "signals": int(self._signal_repo.count()),
                "scores": int(self._score_repo.count()),
            }
        except Exception as exc:  # noqa: BLE001
            raise ServiceError(
                ServiceErrorCode.DEPENDENCY_UNAVAILABLE,
                "persistence layer is not readable",
                {"reason": sanitize_for_error(str(exc))},
            ) from exc
        try:
            mgr = MigrationManager(self._store, [])
            current = int(mgr.current_version())
        except Exception:  # noqa: BLE001 - version probe is best-effort
            warnings.append("schema version could not be determined")
            current = None
        return C.HealthReport(
            status="ok",
            backend_kind=self._store.backend,
            schema_current_version=current,
            counts=counts,
            warnings=warnings,
            checked_at=utc_now_iso(),
        )

    # -- latest intelligence ----------------------------------------------

    def get_latest_intelligence(
        self,
        kind: str | None = None,
        *,
        limit: int = 50,
        ctx: C.RequestContext | None = None,
    ) -> list[C.IntelligenceSummary]:
        if kind is not None:
            _validate_kind(kind)
        limit = _validate_limit(limit, cap=_MAX_EVIDENCE_LIMIT)
        rows = self._latest_snapshots(kind, limit)
        return [self._summary(row) for row in rows]

    def _latest_snapshots(
        self, kind: str | None, limit: int
    ) -> list[ScoreSnapshotRecord]:
        from collections import OrderedDict

        rows = self._score_repo.history(limit=1000)
        if kind is not None:
            rows = [r for r in rows if r.entity_type == kind]
        # history() is ordered newest-first; keep the single newest
        # snapshot per (entity_type, entity_id) and truncate to limit.
        per_entity: OrderedDict[tuple[str, str], ScoreSnapshotRecord] = OrderedDict()
        for r in rows:
            per_entity.setdefault((r.entity_type, r.entity_id), r)
        return list(per_entity.values())[:limit]

    def _summary(self, row: ScoreSnapshotRecord) -> C.IntelligenceSummary:
        refs = self._entity_evidence_refs(row.entity_type, row.entity_id)
        fresh = self.get_freshness(row.entity_type, row.entity_id)
        warnings: list[str] = []
        if fresh.state == "DEGRADED":
            warnings.append("upstream observation older than freshness window")
        elif fresh.state == "STALE":
            warnings.append("observation past hard freshness window")
        elif fresh.state == "UNAVAILABLE":
            warnings.append("no upstream observation available")
        return C.IntelligenceSummary(
            kind=row.entity_type,
            entity_id=row.entity_id,
            score=float(row.score),
            computed_at=row.computed_at or "",
            as_of=self._as_of(row),
            freshness=fresh,
            evidence_refs=refs,
            warnings=warnings,
        )

    @staticmethod
    def _as_of(row: ScoreSnapshotRecord) -> str:
        """The date the snapshot describes (domain date_bucket first)."""
        for source in (row.breakdown, row.inputs):
            value = (source or {}).get("date_bucket")
            if value:
                return str(value)[:10]
        computed = row.computed_at or ""
        return computed[:10]

    # -- entity intelligence ------------------------------------------------

    def get_entity_intelligence(
        self,
        kind: str,
        entity_id: str,
        *,
        as_of: str | None = None,
        ctx: C.RequestContext | None = None,
    ) -> C.EntityIntelligence:
        _validate_kind(kind)
        entity_id = _text(entity_id, "entity_id")
        snapshot = self._score_repo.history(
            scorer=None,
            entity_type=kind,
            entity_id=entity_id,
            limit=1,
        )
        fresh = self.get_freshness(kind, entity_id, as_of=as_of)
        signals = [
            self._signal_dict(s)
            for s in self._signal_repo.query(
                entity_type=kind, entity_id=entity_id, limit=100
            )
        ]
        refs = self._entity_evidence_refs(kind, entity_id)
        if snapshot:
            row = snapshot[0]
            return C.EntityIntelligence(
                kind=kind,
                entity_id=entity_id,
                as_of=self._as_of(row),
                freshness=fresh,
                score=float(row.score),
                payload=self._payload(row),
                signals=signals,
                evidence_refs=refs,
                warnings=self._entity_warnings(fresh),
                computed_at=row.computed_at or "",
            )
        return C.EntityIntelligence(
            kind=kind,
            entity_id=entity_id,
            as_of=as_of or "",
            freshness=fresh,
            score=None,
            payload={},
            signals=signals,
            evidence_refs=refs,
            warnings=self._entity_warnings(fresh)
            + ["no deterministic score snapshot available for this entity"],
        )

    @staticmethod
    def _entity_warnings(fresh: C.Freshness) -> list[str]:
        """Availability-state wording for non-FRESH reads (§8)."""
        if fresh.state == "DEGRADED":
            return ["upstream observation older than freshness window"]
        if fresh.state == "STALE":
            return ["observation past hard freshness window"]
        if fresh.state == "UNAVAILABLE":
            return ["no upstream observation available"]
        return []

    def _payload(self, row: ScoreSnapshotRecord) -> dict[str, Any]:
        """The stored domain result (already JSON in breakdown_json)."""
        # json round-trip guards against leaking any non-JSON-safe object
        # that may have been persisted by another producer.
        return C.json_safe(row.breakdown or {})

    def _signal_dict(self, signal: Any) -> dict[str, Any]:
        return C.json_safe(
            {
                "signal_id": signal.signal_id,
                "signal_type": signal.signal_type,
                "value": signal.value,
                "unit": signal.unit,
                "direction": signal.direction,
                "timestamp": signal.timestamp,
                "source_type": signal.source_type,
                "ref": signal.ref,
            }
        )

    def _entity_evidence_refs(self, kind: str, entity_id: str) -> list[C.EvidenceReference]:
        """Best-effort provenance references for an entity (§9).

        Uses only domain graph vocabulary: the entity node
        (``entity:<kind>:<id>``) and, when present, the entity's
        latest score node (``score:<kind>:<id>:<date>``) plus the
        signals wired into it by the ``contributes_to`` edges of the
        production graph writer. Bounded and read-only.
        """
        refs = [
            C.EvidenceReference(ref=f"entity:{kind}:{entity_id}", kind="entity")
        ]
        latest = self._latest_score_node(kind, entity_id)
        if latest is not None:
            refs.append(
                C.EvidenceReference(ref=latest.node_id, kind="score")
            )
            try:
                inbound = self._graph_repo.list_edges(to_node_id=latest.node_id) or []
            except Exception:  # noqa: BLE001 - evidence enrichment is best-effort
                return refs
            for e in [
                ed for ed in inbound if "signal" in ed.from_node_id
            ][: _MAX_EVIDENCE_LIMIT]:
                refs.append(
                    C.EvidenceReference(
                        ref=e.from_node_id,
                        kind="signal",
                        edge_type=e.edge_type,
                        direction="upstream",
                    )
                )
        return refs

    def _latest_score_node(self, kind: str, entity_id: str) -> Any | None:
        """Latest (max date suffix) score node for the entity, or None."""
        prefix = f"score:{kind}:{entity_id}:"
        try:
            candidates = [
                n.node_id
                for n in self._graph_repo.list_nodes(node_type="score")
                if n.node_id.startswith(prefix)
            ]
        except Exception:  # noqa: BLE001
            return None
        return (
            self._graph_repo.get_node(max(candidates))
            if candidates
            else None
        )

    # -- evidence ------------------------------------------------------------

    def get_evidence(
        self,
        ref: str,
        *,
        limit: int = 50,
        ctx: C.RequestContext | None = None,
    ) -> C.EvidenceItem:
        ref = _text(ref, "ref")
        limit = _validate_limit(limit)
        node = self._resolve_evidence_node(ref)
        if node is None:
            raise ServiceError(
                ServiceErrorCode.NOT_FOUND, f"unknown evidence ref {ref!r}"
            )
        edges = self._graph_repo.fetch_edges_by_nodes(
            [node.node_id], direction="both"
        )[:limit]
        related: list[C.EvidenceReference] = [
            C.EvidenceReference(
                ref=(
                    e.to_node_id
                    if e.from_node_id == node.node_id
                    else e.from_node_id
                ),
                kind="evidence",
                edge_type=e.edge_type,
                direction=(
                    "upstream" if e.to_node_id == node.node_id else "downstream"
                ),
            )
            for e in edges
        ]
        meta = C.json_safe(node.metadata if node.metadata else {})
        return C.EvidenceItem(
            ref=node.node_id,
            kind=node.node_type,
            label=node.label,
            metadata=meta,
            tags=list(node.tags),
            computed_at=node.created_at,
            related=related,
        )

    def _resolve_evidence_node(self, ref: str) -> Any | None:
        """Resolve an evidence ref to a graph node, or None (NOT_FOUND later).

        Exact node ids resolve directly; an undated score ref
        (``score:<kind>:<entity>``) resolves to that entity's latest
        score node (max ISO-date suffix) so a ChatGPT client never has
        to know the internal wall-clock suffix vocabulary.
        """
        node = self._graph_repo.get_node(ref)
        if node is not None:
            return node
        try:
            candidates = [
                n.node_id
                for n in self._graph_repo.list_nodes(node_type="score")
                if n.node_id.startswith(f"{ref}:")
            ]
        except Exception:  # noqa: BLE001 - graph unreadable → unresolvable
            return None
        return (
            self._graph_repo.get_node(max(candidates))
            if candidates
            else None
        )

    # -- freshness -------------------------------------------------------------

    def get_freshness(
        self,
        kind: str,
        entity_id: str,
        *,
        as_of: str | None = None,
        ctx: C.RequestContext | None = None,
    ) -> C.Freshness:
        _validate_kind(kind)
        entity_id = _text(entity_id, "entity_id")
        return _freshness.freshness_for(
            self._signal_repo, kind, entity_id, as_of
        )


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(
            ServiceErrorCode.INVALID_REQUEST, f"{name} must be a non-empty string"
        )
    return value.strip()
