"""In-process reference transport adapter (Phase 6.5 §11 fallback).

Phase 6.5 deliberately ships the transport-neutral boundary and this
deterministic in-process adapter instead of an HTTP server
(ADR-06): the adapter proves that the boundary's JSON contracts are
complete and that a transport can dispatch them with no domain
knowledge. A future HTTP adapter (Phase 6.6+) would do exactly what
this module does — map an operation name + params to the service
and wrap the result in an envelope — only with a socket instead of
a function call.

Envelope shape (work order §5):

    {
      "schema_version", "request_id", "principal_id",
      "status":        "ok" | "error",
      "kind":          operation name (health|latest_intelligence|...)
      "freshness":     top-level freshness metadata (single-result ops)
      "payload":       domain result                  # DOMAIN DATA
      "evidence_refs": structured provenance refs     # PROVENANCE
      "error":         {code, message, ...}           # ERROR/AVAILABILITY
      "warnings":      [...]
    }

Errors are the stable taxonomy mapped through
:class:`~phase3.service.errors.ServiceError`; a non-ServiceError
exception becomes INTERNAL_ERROR with the stable generic message —
full diagnostics stay in server-side logging only (Phase 6.6R1
DEFECT-A repair).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from phase3.service import contracts as C
from phase3.service.errors import ServiceError, ServiceErrorCode

__all__ = ["dispatch", "ok_envelope", "error_envelope"]

#: Structured request log (Phase 6.5 §15): stdlib logging only, one
#: JSON line per invocation — never payloads, secrets or stack traces.
#: A future HTTP adapter MUST emit the same fields per request.
REQUEST_LOGGER = logging.getLogger("fie.service")


def _observability_record(
    *,
    operation: str,
    ctx: C.RequestContext | None,
    duration_ms: float,
    envelope: dict[str, Any],
) -> dict[str, Any]:
    """The §15 fields for one service invocation (payload-free)."""
    freshness = envelope.get("freshness")
    fresh_dict = freshness if isinstance(freshness, dict) else None
    record: dict[str, Any] = {
        "request_id": ctx.request_id if ctx else "",
        "operation": operation,
        "status": envelope.get("status"),
        "duration_ms": round(duration_ms, 3),
        "freshness_state": fresh_dict.get("state") if fresh_dict else None,
        "as_of": fresh_dict.get("as_of") if fresh_dict else None,
    }
    error = envelope.get("error")
    if isinstance(error, dict):
        record["error_code"] = error.get("code")
    return record


def dispatch(
    service: Any,
    operation: str,
    params: dict[str, Any] | None = None,
    ctx: C.RequestContext | None = None,
) -> dict[str, Any]:
    """Call one read-only service operation and wrap its result.

    Unknown/invalid operation names are INVALID_REQUEST — never
    routed to domain code.
    """
    params = dict(params or {})
    started = time.perf_counter()
    envelope: dict[str, Any] = {}
    try:
        try:
            handler = _HANDLERS.get(operation)
            if handler is None:
                raise ServiceError(
                    ServiceErrorCode.INVALID_REQUEST,
                    f"unknown operation {operation!r}; "
                    f"expected one of {list(C.OPERATIONS)}",
                )
            envelope = handler(service, params, ctx, operation)
        except ServiceError as exc:
            envelope = error_envelope(exc, operation=operation, ctx=ctx)
        except Exception as exc:  # noqa: BLE001 - boundary must not leak stacks
            # (6.6R1 DEFECT-A) the exception repr used to become the
            # client-visible message; full diagnostics now stay in
            # server-side logging and the envelope carries the stable
            # generic message only — never arguments, locals or envs.
            REQUEST_LOGGER.exception(
                "service internal failure: operation=%s", operation
            )
            envelope = error_envelope(
                ServiceError(
                    ServiceErrorCode.INTERNAL_ERROR, "internal service failure"
                ),
                operation=operation,
                ctx=ctx,
            )
        return envelope
    finally:
        if REQUEST_LOGGER.isEnabledFor(logging.INFO):
            record = _observability_record(
                operation=operation,
                ctx=ctx,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                envelope=envelope,
            )
            REQUEST_LOGGER.info(json.dumps(record, sort_keys=True))


def _h_health(
    service: Any, params: dict[str, Any], ctx: C.RequestContext | None, op: str
) -> dict[str, Any]:
    return ok_envelope(service.get_health(ctx=ctx), operation=op, ctx=ctx)


def _h_latest(
    service: Any, params: dict[str, Any], ctx: C.RequestContext | None, op: str
) -> dict[str, Any]:
    result = service.get_latest_intelligence(
        params.get("kind"), limit=_int_limit(params), ctx=ctx
    )
    items = [s.to_dict() for s in result]
    warnings = sorted({w for s in items for w in s.get("warnings", [])})
    freshness = items[0]["freshness"] if len(items) == 1 else None
    return {
        "schema_version": C.SCHEMA_VERSION,
        "kind": op,
        "request_id": ctx.request_id if ctx else "",
        "principal_id": ctx.principal_id if ctx else "",
        "status": "ok",
        # FRESHNESS METADATA rides beside the payload (per entity below)
        "freshness": freshness,
        # DOMAIN DATA — one summary per entity with its own freshness
        "payload": items,
        # PROVENANCE — refs are consolidated for evidence lookup
        "evidence_refs": [
            r for s in items for r in s.get("evidence_refs", [])
        ],
        "warnings": sorted(set(warnings)),
    }


def _h_entity(
    service: Any, params: dict[str, Any], ctx: C.RequestContext | None, op: str
) -> dict[str, Any]:
    result = service.get_entity_intelligence(
        params.get("kind") or "",
        params.get("entity_id") or "",
        as_of=params.get("as_of"),
        ctx=ctx,
    )
    return ok_envelope(result, operation=op, ctx=ctx)


def _h_evidence(
    service: Any, params: dict[str, Any], ctx: C.RequestContext | None, op: str
) -> dict[str, Any]:
    result = service.get_evidence(
        params.get("ref") or "", limit=_int_limit(params), ctx=ctx
    )
    return ok_envelope(result, operation=op, ctx=ctx)


def _h_freshness(
    service: Any, params: dict[str, Any], ctx: C.RequestContext | None, op: str
) -> dict[str, Any]:
    result = service.get_freshness(
        params.get("kind") or "",
        params.get("entity_id") or "",
        as_of=params.get("as_of"),
        ctx=ctx,
    )
    # A dedicated freshness read IS freshness metadata — surface it
    # in the envelope's first-class freshness slot as well as payload.
    envelope = ok_envelope(result, operation=op, ctx=ctx)
    envelope["freshness"] = {
        k: envelope["payload"][k]
        for k in ("state", "as_of", "checked_at", "source_date", "age")
        if k in envelope["payload"]
    }
    return envelope


_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "health": _h_health,
    "latest_intelligence": _h_latest,
    "entity_intelligence": _h_entity,
    "evidence": _h_evidence,
    "freshness": _h_freshness,
}


def _int_limit(params: dict[str, Any]) -> int:
    value = params.get("limit", 50)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ServiceError(
            ServiceErrorCode.INVALID_REQUEST, "limit must be an integer"
        ) from exc


def ok_envelope(
    result: Any,
    *,
    operation: str,
    ctx: C.RequestContext | None = None,
) -> dict[str, Any]:
    """Wrap a single service result in the response envelope."""
    body = result.to_dict()
    freshness = body.pop("freshness", None)
    evidence_refs = body.pop("evidence_refs", [])
    warnings = body.pop("warnings", [])
    return {
        "schema_version": C.SCHEMA_VERSION,
        "kind": operation,
        "request_id": ctx.request_id if ctx else "",
        "principal_id": ctx.principal_id if ctx else "",
        "status": "ok",
        # FRESHNESS METADATA — first-class, beside the domain data
        "freshness": freshness,
        # DOMAIN DATA
        "payload": body,
        # PROVENANCE / EVIDENCE
        "evidence_refs": evidence_refs,
        "warnings": warnings,
    }


def error_envelope(
    error: ServiceError,
    *,
    operation: str,
    ctx: C.RequestContext | None = None,
) -> dict[str, Any]:
    """Transport-ready error state (§5.2 taxonomy; no internal leak)."""
    return {
        "schema_version": C.SCHEMA_VERSION,
        "kind": operation,
        "request_id": ctx.request_id if ctx else "",
        "principal_id": ctx.principal_id if ctx else "",
        "status": "error",
        # ERROR / AVAILABILITY STATE
        "error": error.to_dict(),
        "payload": None,
    }