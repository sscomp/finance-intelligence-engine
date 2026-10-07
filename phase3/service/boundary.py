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

import json
import logging
import os
from typing import Any, Protocol

from phase3.persistence.contracts import DatabaseStore
from phase3.persistence.identity_gate import (
    IdentityGateUnavailable,
    IdentityIncompatible,
    check_runtime_identity,
    expected_runtime_expectations,
)
from phase3.persistence.migration_authority import (
    _as_target_spec,
    is_production_shaped,
)
from phase3.persistence.score_repo import ScoreRepository, ScoreSnapshotRecord
from phase3.persistence.schema_gate import (
    SchemaGateUnavailable,
    SchemaIncompatible,
    check_schema_compatibility,
)
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

#: Server-side diagnostics for readiness dependency failures (Phase
#: 6.7B-R4). Records the sanitized failure detail (URL/DSN/path-class
#: redacted) for operators — the EXTERNAL envelope deliberately carries
#: the stable classification + dependency kind only.
_DEPENDENCY_LOGGER = logging.getLogger("fie.service")


def _readiness_schema_gate(store: DatabaseStore) -> dict[str, Any]:
    """Deterministic fail-closed schema verdict for readiness (R3).

    Translates the persistence-layer gate into the stable service
    taxonomy: incompatible metadata → ``SCHEMA_INCOMPATIBLE`` (503),
    unreadable metadata → ``DEPENDENCY_UNAVAILABLE`` (503). Both carry
    sanitized details only — the gate's stable code, never a raw
    driver exception, DSN or query text.
    """
    try:
        return check_schema_compatibility(store)
    except SchemaIncompatible as exc:
        raise ServiceError(
            ServiceErrorCode.SCHEMA_INCOMPATIBLE,
            "persistent store schema is not compatible with this application",
            {"reason": exc.code, "detail": exc.detail},
        ) from exc
    except SchemaGateUnavailable as exc:
        raise ServiceError(
            ServiceErrorCode.DEPENDENCY_UNAVAILABLE,
            "persistence layer is not readable",
            {"reason": "schema metadata is not readable"},
        ) from exc


def _truthy_env(name: str, env: dict[str, str]) -> bool:
    return (env.get(name, "") or "").strip().lower() in ("1", "true", "yes", "on")


def _identity_gate_applicable(
    store: DatabaseStore, *, env: dict[str, str] | None = None
) -> bool:
    """Phase 6.9B-R3 gate APPLICABILITY scoping (fail closed).

    The runtime-identity contract is ENFORCED on a PostgreSQL connection
    when any of the following holds:

    * the target is provably production-shaped (R1 production contract
      fingerprint match — the same detection the R3 migration-authority
      guard uses, deliberately identical scoping);
    * the operator pinned runtime expectations
      (``FIE_EXPECTED_RUNTIME_DB`` / ``_SCHEMA`` / ``_ROLE``) — a
      deployment that pins its runtime identity proves it wherever it
      runs;
    * an explicit opt-in ``FIE_RUNTIME_IDENTITY_GATE=1`` forces
      enforcement onto any PostgreSQL target (staging/rehearsal
      hardening).

    Disposable non-production PostgreSQL targets (test clusters,
    throwaway databases) keep their historical behavior — the same
    "unprovable identity is NOT treated as production identity" rule
    as the migration-authority guard; the R1 rehearsal guards remain
    the fail-closed net for the write funnels. A PostgreSQL store whose
    DSN cannot be parsed is NOT provably non-production: it stays
    ENFORCED. Non-PostgreSQL backends delegate to the gate itself
    (sqlite → ``{"applicable": false}``).
    """
    e = os.environ if env is None else env
    if any(v is not None for v in expected_runtime_expectations(e).values()):
        return True
    if _truthy_env("FIE_RUNTIME_IDENTITY_GATE", e):
        return True
    if getattr(store, "backend", "sqlite") != "postgres":
        # delegates to check_runtime_identity (which marks sqlite
        # non-applicable through the R1 target-resolution contract)
        return True
    spec = _as_target_spec(getattr(store, "_dsn", ""))
    if spec is None:
        return True  # cannot parse the target → cannot prove it non-Production
    return is_production_shaped(spec, env)


def _readiness_identity_gate(store: DatabaseStore) -> dict[str, Any] | None:
    """Deterministic fail-closed runtime-identity verdict for readiness.

    Phase 6.9B-R3: WHO the runtime is connected as is proved, not
    inferred from a successful connect. Incompatible identity →
    ``IDENTITY_INCOMPATIBLE`` (503, stable code + the gate's reason
    code only — no credentials, no driver text); unreadable identity
    metadata → ``DEPENDENCY_UNAVAILABLE``. Returns the raw report for
    server-side use on success; non-applicable scopes (SQLite backends
    and non-enforced disposable PostgreSQL targets, see
    :func:`_identity_gate_applicable`) return a small dict carrying
    ``applicable: false`` — never a silent skip, the scope reason is
    explicit.
    """
    if not _identity_gate_applicable(store):
        backend = getattr(store, "backend", "sqlite")
        return {
            "applicable": False,
            "backend": backend,
            "scope": "NON_PRODUCTION_TARGET"
            if backend == "postgres" else "NON_POSTGRESQL_BACKEND",
        }
    try:
        return check_runtime_identity(store)
    except IdentityIncompatible as exc:
        raise ServiceError(
            ServiceErrorCode.IDENTITY_INCOMPATIBLE,
            "connected database identity violates the runtime role contract",
            {"reason": exc.code},
        ) from exc
    except IdentityGateUnavailable as exc:
        raise ServiceError(
            ServiceErrorCode.DEPENDENCY_UNAVAILABLE,
            "persistence layer is not readable",
            {"reason": "runtime identity metadata is not readable"},
        ) from exc


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
        """Readiness across the persistence abstraction, not its innards.

        Phase 6.7B-R3 (HP-07/06 / OI-08): readiness is now strictly
        schema-gated and fail closed. The historical best-effort version
        probe (warn-and-continue with ``status="ok"`` on unreadable or
        missing ``schema_migrations`` metadata) allowed a reachable but
        EMPTY / STALE / FOREIGN-schema database to answer ready — that
        false-ready path is this work order's defect. The gate now:

        * raises SCHEMA_INCOMPATIBLE (deterministic, stable code, 503)
          when the schema registry is missing/failed/stale/future/
          malformed/divergent-with-expected or required application
          objects are absent;
        * raises DEPENDENCY_UNAVAILABLE (503, sanitized) when the
          metadata itself is unreadable (DB outage);

        while the reported ``schema_current_version``/payload shape stays
        what the accepted Phase 6.5/6.7A contracts pin.
        """
        warnings: list[str] = []
        try:
            counts = {
                "signals": int(self._signal_repo.count()),
                "scores": int(self._score_repo.count()),
            }
        except Exception as exc:  # noqa: BLE001
            # Phase 6.7B-R4 (Architecture ruling §4.3): the EXTERNAL
            # envelope carries the stable classification only — the
            # dependency KIND (backend family, e.g. "postgres"), never
            # driver text, which can name internal hosts/services/topology.
            # The sanitized detail (DSN- and URL-masked) stays in
            # SERVER-SIDE logs only.
            _DEPENDENCY_LOGGER.warning(json.dumps(
                {
                    "event": "readiness_dependency_failure",
                    "dependency": self._store.backend,
                    "reason": sanitize_for_error(str(exc)),
                },
                sort_keys=True,
            ))
            raise ServiceError(
                ServiceErrorCode.DEPENDENCY_UNAVAILABLE,
                "persistence layer is not readable",
                {"dependency": self._store.backend},
            ) from exc

        current: int | None
        gate = _readiness_schema_gate(self._store)
        # Phase 6.9B-R3: prove WHO is connected, not just WHAT schema.
        # The verdict is fail-closed (raises on an incompatible runtime
        # identity); outside the enforcement scope (sqlite backend or a
        # non-production-shaped disposable PostgreSQL target without
        # pinned expectations) the gate reports applicable=false and
        # readiness is unchanged — see _identity_gate_applicable.
        _readiness_identity_gate(self._store)
        current = int(gate["current_version"])
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
