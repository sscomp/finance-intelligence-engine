"""FIE reference HTTP runtime (Phase 6.6 §4/§5) — a THIN transport.

Wraps the frozen Phase 6.5 transport-neutral boundary
(:mod:`phase3.service`) with a dependency-free stdlib HTTP server
(``http.server.ThreadingHTTPServer``) — the smallest reasonable,
repository-declared, provider-neutral reference runtime (ADR-011).

Thin-transport discipline (§3.1): this module performs request
parsing, auth/principal extraction, request ids, dispatch, stable
error→HTTP mapping and structured telemetry — and NOTHING else. No
scoring, no SQL, no repository traversal, no ingestion, no scheduler,
no freshness computation (that stays in :mod:`phase3.service.freshness`
over the unchanged governance policy).

Read-only (§3.5): only GET is served (HEAD mirrors GET with an empty
body — Phase 6.7A contract reconciliation); the wrapped service itself
is opened with query-only guarantees. Batch (ingestion/scoring) remains
a separate execution plane (:mod:`phase3.service.batch`) that this
runtime never invokes.

Operation mapping (§4: every public HTTP operation has an explicit
Phase 6.5 tool/service contract equivalent):

=========================== ==========================================
GET /healthz                liveness — process alive (no persistence)
GET /readyz                 readiness — persistence reachable
GET /v1/health              fie.health            -> get_health
GET /v1/intelligence/latest fie.latest_intelligence -> get_latest_intelligence
GET /v1/intelligence/entity/{kind}/{entity_id}
                            fie.entity_intelligence -> get_entity_intelligence
GET /v1/evidence/{ref}      fie.evidence          -> get_evidence
GET /v1/freshness/{kind}/{entity_id}
                            fie.freshness         -> get_freshness
=========================== ==========================================

Every response body is a JSON envelope with two classes (Phase 6.7A
contract reconciliation — the canonical machine contract,
``docs/architecture/phase6-6/http-api-contract.json``, declares both):

* **dispatch envelopes** (produced by the Phase 6.5 boundary/adapter,
  verbatim) — ``kind`` is the operation name and ``principal_id`` is
  the authenticated principal; successes carry ``freshness`` (possibly
  ``null``), ``payload``, ``evidence_refs`` and ``warnings``; dispatch
  errors carry ``status: "error"`` + ``error`` + ``payload: null``;

* **transport-produced error envelopes** — auth failures, 405
  rejections, unknown routes and the catch-all 500 set
  ``kind = "transport"`` and ``principal_id = ""``; their ``error``
  block uses the same stable taxonomy and mapping.

HTTP status is the deterministic mapping in
:mod:`phase3.transport.errors` — never a replacement for the domain
error code.

Operational probe surface (Phase 6.7B-R2, HP-01): exactly ONE route
operation — liveness (``GET /healthz``) — is classified as a public
operational probe endpoint and is served before authentication.
See :data:`OPERATIONAL_PROBE_OPERATIONS` for the security shape of
that classification and :mod:`docs/adr/adr-018` for its record.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from phase3.service import dispatch
from phase3.service.errors import ServiceError, ServiceErrorCode
from phase3.service.contracts import RequestContext
from phase3.transport.auth import _request_id_from_headers, build_authenticator
from phase3.transport.config import TransportConfig, load_transport_config
from phase3.transport.errors import (
    TransportErrorCode,
    TransportError,
    http_status_for,
)

__all__ = [
    "FIEReferenceRuntime",
    "make_http_server",
    "run_server",
    "main",
    "TRANSPORT_LOGGER",
    "API_VERSION",
    "OPERATIONAL_PROBE_OPERATIONS",
]

API_VERSION = "v1"

TRANSPORT_LOGGER = logging.getLogger("fie.transport")

_MAX_LIMIT = 200

#: The operational probe surface (Phase 6.7B-R2, HP-01) — the CLOSED set
#: of route operations served as public, credentialless liveness probes.
#:
#: Security shape of the classification (work order §6/§8; ADR-018):
#:
#: * ``liveness`` is the ONLY member. It is produced by ``_route()`` for
#:   exactly one canonical request form (``GET/HEAD /healthz`` after URL
#:   decoding and empty-segment normalisation — see ``_route``), and its
#:   handler (``_liveness_payload``) builds a fixed, minimal envelope
#:   from constants: it touches NO database, NO filesystem, NO business
#:   service (``dispatch`` is never reached) and NO protected data, and
#:   it does NOT invoke the authenticator — so a probe can never leak
#:   credential validity (an invalid bearer behaves byte-identically to
#:   a missing one).
#: * There is deliberately NO bypass primitive: no header, query
#:   parameter, environment variable, source address, User-Agent or
#:   probe token can put a protected operation into this set or skip
#:   authentication anywhere else. Every non-liveness route — including
#:   ``readiness`` (``/readyz``, which exercises the persistence seam) —
#:   still passes the authenticator exactly as before (ADR-018).
#: * The set is frozen and pinned by the R2 suite
#:   (``tests/phase3/transport/test_67b_r2_auth_health.py``): adding a
#:   protected operation here is a test failure, and the reconciliation
#:   matrix proves every protected path stays authenticated under
#:   probe-like request characteristics.
OPERATIONAL_PROBE_OPERATIONS = frozenset({"liveness"})


class _OneThreadExecutor:
    """Runs every submitted callable on ONE dedicated thread.

    Why (ADR-011): the persistence seam's connections are single-thread
    (SQLite ``check_same_thread`` affinity is part of Phase 6.3's
    tested behaviour), while the reference HTTP server handles each
    request on its own thread. The transport therefore funnels ALL
    domain calls through a single persistence-worker thread instead of
    changing persistence-layer threading semantics. No domain logic
    lives here — it is transport-side scheduling only.
    """

    def __init__(self) -> None:
        import queue

        self._queue: "queue.SimpleQueue[tuple]" = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._loop, name="fie-persistence-worker", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while True:
            fn, args, kwargs, out = self._queue.get()
            if fn is None:
                break
            try:
                out.put((True, fn(*args, **kwargs)))
            except BaseException as exc:  # noqa: BLE001 - forwarded verbatim
                out.put((False, exc))

    def call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        import queue

        out: "queue.SimpleQueue[tuple]" = queue.SimpleQueue()
        self._queue.put((fn, args, kwargs, out))
        ok, value = out.get()
        if ok:
            return value
        raise value

    def stop(self) -> None:
        self._queue.put((None, (), {}, None))


class _ThreadAffineService:
    """Proxy: forwards every service call through the executor."""

    def __init__(self, executor: _OneThreadExecutor, factory: Any) -> None:
        self.__dict__["_executor"] = executor
        self.__dict__["_factory"] = factory
        self.__dict__["_target"] = None

    def _resolve(self) -> Any:
        # The store (and therefore the service) is *created* on the
        # worker thread so connection affinity matches its use.
        if self.__dict__["_target"] is None:
            self.__dict__["_target"] = self.__dict__["_executor"].call(
                self.__dict__["_factory"]
            )
        return self.__dict__["_target"]

    def __getattr__(self, name: str) -> Any:  # dispatched read operations
        def _call(*args: Any, **kwargs: Any) -> Any:
            return self._executor.call(
                getattr(self._resolve(), name), *args, **kwargs
            )

        return _call

    def close(self) -> None:
        target = self.__dict__["_target"]
        if target is not None:
            try:
                self.__dict__["_executor"].call(target.store.close)
            except Exception:  # noqa: BLE001 - shutdown must not mask real errors
                pass
        self.__dict__["_executor"].stop()


class FIEReferenceRuntime:
    """The Phase 6.5 read-only service behind a thread-affine executor."""

    def __init__(self, service_factory: Any) -> None:
        self._executor = _OneThreadExecutor()
        self.service = _ThreadAffineService(self._executor, service_factory)
        # liveness uses no persistence; readiness probes the service
        self.store = None

    @classmethod
    def open(
        cls,
        db_spec: str | None = None,
        *,
        access_mode: str | None = None,
    ) -> "FIEReferenceRuntime":
        """Open the reference runtime.

        ``access_mode`` (Phase 6.6R4) selects the SQLite read-only
        deployment open semantics (writable | readonly |
        immutable_snapshot); ``None``/``writable`` keeps the
        historical behavior. PostgreSQL specs only accept the writable
        default (rejected by :func:`open_store` otherwise).
        """
        from phase3.persistence.backend import open_store, resolve_spec
        from phase3.service.boundary import DefaultIntelligenceService

        def factory() -> DefaultIntelligenceService:
            spec = resolve_spec(db_spec) if db_spec else resolve_spec()
            store = open_store(spec, access_mode=access_mode)
            return DefaultIntelligenceService(store)

        return cls(factory)

    def close(self) -> None:
        try:
            self.service.close()
        except Exception:  # noqa: BLE001
            pass


def _json(body: bytes, status: int, request_id: str) -> tuple[bytes, int, dict[str, str]]:
    return body, status, {"X-Request-Id": request_id}


class _TransportHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FIE-Reference/6.6"
    timeout = 60.0  # may be overridden via server instance attribute

    # -- attributes injected by make_http_server ---------------------------
    runtime: FIEReferenceRuntime
    authenticator: Any
    logger: logging.Logger

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stdlib noise
        return

    # -- verb gate ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        started = time.perf_counter()
        try:
            body, status, headers = self.handle_get()
        except Exception as exc:  # noqa: BLE001 - transport must never leak stacks
            # (6.6R1) request-ID consistency: the fallback path used to
            # send the literal header "unassigned" with no matching body
            # field; use the same canonical request-id derivation as the
            # auth-failure path so header == body.request_id.
            self.logger.exception("unhandled transport failure")  # server log only
            request_id = _request_id_from_headers(self.headers)
            envelope = {
                "schema_version": _schema_version(),
                "kind": "transport",
                "request_id": request_id,
                "principal_id": "",
                "status": "error",
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "internal transport failure",
                },
                "payload": None,
            }
            data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
            body, status, headers = _json(data, 500, request_id)
        self._send(body, status, headers, started)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - read-only invariant (§3.5)
        self._reject_non_get()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def _reject_non_get(self) -> None:
        # (6.6R1) request-ID consistency: the 405 envelope now carries
        # the same request_id / principal_id shape as every other
        # transport error, and the header echoes the body field.
        request_id = uuid.uuid4().hex
        self._send(
            json.dumps(
                {
                    "schema_version": _schema_version(),
                    "kind": "transport",
                    "request_id": request_id,
                    "principal_id": "",
                    "status": "error",
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "only GET or HEAD is supported; "
                        "the interactive plane is read-only",
                    },
                    "payload": None,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            405,
            {"Allow": "GET, HEAD", "X-Request-Id": request_id},
            time.perf_counter(),
        )

    # -- routing ------------------------------------------------------------
    def handle_get(self) -> tuple[bytes, int, dict[str, str]]:
        started = time.perf_counter()
        from urllib.parse import unquote

        raw_path = unquote(self.path.split("?", 1)[0])
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = _parse_query(query)
        route = _route(raw_path)
        if route is None or route[0] == "invalid":
            request_id = uuid.uuid4().hex
            envelope = {
                "schema_version": _schema_version(),
                "kind": "transport",
                "request_id": request_id,
                "principal_id": "",
                "status": "error",
                "error": ServiceError(
                    ServiceErrorCode.INVALID_REQUEST, "no such route"
                ).to_dict(),
                "payload": None,
            }
            data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
            self._telemetry("transport", None, 400, envelope, started)
            return data, 400, {"X-Request-Id": request_id}
        op, captured = route
        # Phase 6.7B-R2 (HP-01 / ADR-018): the operational probe surface
        # is served BEFORE authentication — see OPERATIONAL_PROBE_OPERATIONS
        # for why `liveness` alone qualifies (fixed minimal body, nothing
        # protected reachable, no credential evaluation, no bypass
        # primitives). Every other route — including readiness — still
        # authenticates below, so no probe characteristic can widen this.
        if op in OPERATIONAL_PROBE_OPERATIONS:
            return self._liveness_payload(started)
        # auth happens at the transport edge, BEFORE any domain dispatch
        try:
            ctx = self.authenticator.authenticate(self.headers)
        except TransportError as exc:
            # auth failures never reach the domain and map through the
            # same stable envelope/error table as domain errors
            request_id = _request_id_from_headers(self.headers)
            error_envelope = {
                "schema_version": _schema_version(),
                "kind": "transport",
                "request_id": request_id,
                "principal_id": "",
                "status": "error",
                "error": exc.to_dict(),
                "payload": None,
            }
            data = json.dumps(error_envelope, ensure_ascii=False).encode("utf-8")
            self._telemetry("transport", None, http_status_for(exc.code.value),
                            error_envelope, started)
            return data, http_status_for(exc.code.value), {
                "X-Request-Id": request_id,
            }

        return self._dispatch_route(op, captured, params, ctx, started)

    def _dispatch_route(
        self,
        op: str,
        captured: dict[str, str],
        params: dict[str, str],
        ctx: RequestContext,
        started: float,
    ) -> tuple[bytes, int, dict[str, str]]:
        if op == "readiness":
            return self._readiness_payload(ctx, started)
        merged = {**params, **captured}
        envelope = dispatch(self.runtime.service, op, merged, ctx)
        status = 200
        if envelope.get("status") == "error":
            status = http_status_for(envelope["error"]["code"])
        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        self._telemetry(op, ctx, status, envelope, started)
        return data, status, {"X-Request-Id": ctx.request_id}

    def _liveness_payload(
        self, started: float
    ) -> tuple[bytes, int, dict[str, str]]:
        """Serve ``GET/HEAD /healthz`` (Phase 6.7B-R2, ADR-018).

        Public liveness probe: minimal fixed envelope, request-id
        derived with the shared sanitiser, NO authenticator invocation
        and NO dispatch — the process answers from constants alone, so
        the probe can neither expose protected application
        functionality nor reveal whether a presented credential was
        valid. Telemetry keeps the standard record shape with the
        transport-error principal convention (``principal_id=""``) —
        the caller is unauthenticated by definition.
        """
        request_id = _request_id_from_headers(self.headers)
        envelope = {
            "schema_version": _schema_version(),
            "kind": "healthz",
            "request_id": request_id,
            "status": "alive",
        }
        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        probe_ctx = RequestContext(
            principal_id="", request_id=request_id, scopes=frozenset(),
        )
        self._telemetry("liveness", probe_ctx, 200, envelope, started)
        return data, 200, {"X-Request-Id": request_id}

    def _readiness_payload(
        self, ctx: RequestContext, started: float
    ) -> tuple[bytes, int, dict[str, str]]:
        """Serve ``GET/HEAD /readyz`` — AUTHENTICATED readiness (ADR-018).

        Readiness exercises the persistence seam (service ``health`` op)
        and reports service metadata, so it keeps the Phase 6.6
        authenticated access policy: in token mode a credentialless
        caller receives 401 like any protected path. Credentialless
        orchestrators use the public liveness probe ``/healthz``.
        """
        envelope = dispatch(self.runtime.service, "health", {}, ctx)
        status = 200 if envelope.get("status") == "ok" else http_status_for(
            envelope["error"]["code"]
        )
        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        self._telemetry("readiness", ctx, status, envelope, started)
        return data, status, {"X-Request-Id": ctx.request_id}

    def _telemetry(
        self,
        op: str,
        ctx: RequestContext | None,
        status: int,
        envelope: dict[str, Any],
        started: float,
    ) -> None:
        if not self.logger.isEnabledFor(logging.INFO):
            return
        duration_ms = (time.perf_counter() - started) * 1000.0
        fresh = envelope.get("freshness")
        record: dict[str, Any] = {
            "event": "http_request",
            "request_id": ctx.request_id if ctx else "",
            "principal_id": ctx.principal_id if ctx else "",
            "operation": op,
            "method": "GET",
            "path": self.path.split("?", 1)[0],
            "http_status": status,
            "status": envelope.get("status"),
            "duration_ms": round(duration_ms, 3),
        }
        if isinstance(fresh, dict):
            record["freshness_state"] = fresh.get("state")
            record["as_of"] = fresh.get("as_of")
        error = envelope.get("error")
        if isinstance(error, dict):
            record["error_code"] = error.get("code")
        # NOTE (§11): never log query strings, Authorization headers,
        # tokens, or payloads — only the fields assembled above.
        self.logger.info(json.dumps(record, sort_keys=True))

    # -- send ---------------------------------------------------------------
    def _send(
        self,
        body: bytes,
        status: int,
        headers: dict[str, str],
        started: float,
    ) -> None:
        try:
            self.send_response(status)
            # (6.6R1 DEFECT-B) every response body is a JSON envelope —
            # success and error alike — so the contract-declared
            # Content-Type belongs here, in the one shared response
            # writer, not on individual call sites.
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _schema_version() -> str:
    from phase3.service.contracts import SCHEMA_VERSION

    return SCHEMA_VERSION


def _route(raw_path: str) -> tuple[str, dict[str, str]] | None:
    parts = [p for p in raw_path.split("/") if p]
    if not parts:
        return None
    if parts == ["healthz"]:
        return ("liveness", {})
    if parts == ["readyz"]:
        return ("readiness", {})
    if parts == [API_VERSION, "health"]:
        return ("health", {})
    if parts[:3] == [API_VERSION, "intelligence", "latest"]:
        return ("latest_intelligence", {})
    if parts[:3] == [API_VERSION, "intelligence", "entity"]:
        if len(parts) != 5:
            return ("invalid", {})
        return ("entity_intelligence", {"kind": parts[3], "entity_id": parts[4]})
    if parts[:2] == [API_VERSION, "evidence"]:
        if len(parts) != 3:
            return ("invalid", {})
        return ("evidence", {"ref": parts[2]})
    if parts[:2] == [API_VERSION, "freshness"]:
        if len(parts) != 4:
            return ("invalid", {})
        return ("freshness", {"kind": parts[2], "entity_id": parts[3]})
    return None


def _parse_query(query: str) -> dict[str, str]:
    from urllib.parse import parse_qs

    out: dict[str, str] = {}
    for key, values in parse_qs(query, keep_blank_values=True).items():
        if values:
            out[key] = values[0]
    return out


class _HandlerFactory:
    """Bind per-server attributes onto the handler class without globals.

    ``socketserver`` invokes the handler factory as
    ``factory(request, client_address, server)`` — delegate to the
    generated class with the injected attributes.
    """

    def __init__(
        self, runtime: FIEReferenceRuntime, authenticator: Any, logger: logging.Logger,
        timeout: float,
    ) -> None:
        attrs = {
            "runtime": runtime,
            "authenticator": authenticator,
            "logger": logger,
            "timeout": timeout,
        }
        self._cls = type("FIEHandler", (_TransportHandler,), attrs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._cls(*args, **kwargs)


def make_http_server(
    runtime: FIEReferenceRuntime,
    config: TransportConfig | None = None,
    *,
    logger: logging.Logger | None = None,
    host: str | None = None,
    port: int | None = None,
    authenticator: Any | None = None,
) -> ThreadingHTTPServer:
    """Build (bind) the reference server; caller runs it / shuts it down."""
    config = config or load_transport_config()
    logger = logger or TRANSPORT_LOGGER
    authenticator = authenticator or build_authenticator(
        config.auth_mode, token=config.auth_token,
        principal_id=config.auth_principal,
    )
    factory = _HandlerFactory(
        runtime, authenticator, logger, config.request_timeout
    )
    return ThreadingHTTPServer(
        (host or config.host, port if port is not None else config.port),
        factory,
    )


def _validate_production_dsn(config: TransportConfig) -> None:
    """Refuse CWD-relative SQLite DSNs under staging/production (HP-04).

    Composition-root gate (ADR-017 §4.4): this is the one transport
    module permitted to resolve a full spec
    (:func:`phase3.persistence.backend.resolve_spec` — see the Phase
    6.7A frozen import-surface audit). Runs BEFORE anything is opened
    or bound, so an invalid production DSN can never produce a
    partially-configured listening service.
    """
    from pathlib import Path

    from phase3.persistence.backend import resolve_spec
    from phase3.service.runtime_config import ConfigurationError
    from phase3.transport.config import PRODUCTION_LIKE_PROFILES

    if config.service_env not in PRODUCTION_LIKE_PROFILES:
        return
    spec = resolve_spec(config.db_spec)
    if spec.backend == "sqlite" and not Path(spec.dsn).is_absolute():
        raise ConfigurationError(
            "DATABASE_URL_INVALID",
            "FIE_DATABASE_URL invalid under staging/production: a SQLite "
            "path must be absolute (no CWD-relative DSN), or a postgres:// "
            "DSN must be used; value withheld",
        )


def run_server(
    config: TransportConfig | None = None,
    *,
    db_spec: str | None = None,
) -> ThreadingHTTPServer:
    """Open the runtime and serve until KeyboardInterrupt/SIGTERM.

    Returns the (already serving) server object so embedders/tests can
    ``shutdown()`` deterministically.

    Raises :class:`~phase3.service.runtime_config.ConfigurationError`
    for fail-closed production-like DSN validation — before the
    runtime or the socket exist (ADR-017).
    """
    config = config or load_transport_config(db_spec)
    _validate_production_dsn(config)
    runtime = FIEReferenceRuntime.open(
        config.db_spec,
        access_mode=getattr(config, "sqlite_access_mode", None),
    )
    server = make_http_server(runtime, config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.getLogger("fie.transport").info(
        json.dumps(
            {
                "event": "server_started",
                "host": config.host,
                "port": config.port,
                "auth_mode": config.auth_mode,
                "schema_version": _schema_version(),
            },
            sort_keys=True,
        )
    )
    server._fie_runtime = runtime  # type: ignore[attr-defined]
    return server


def main() -> int:
    """Console entry point (``fie-http-server``) — repository-declared."""
    import signal
    import sys

    from phase3.service.runtime_config import ConfigurationError

    try:
        config = load_transport_config()
    except ConfigurationError as exc:
        # Fail-closed startup refusal (Phase 6.7B / ADR-017): nothing
        # binds, no partial service, no fallback mode. The diagnostic
        # is a single sanitized line: a stable refusal category plus a
        # message that names only the offending variable — the value
        # itself is withheld, so no token/DSN/path/marker can leak.
        print(
            json.dumps(
                {"error": {"code": exc.code, "message": exc.message}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(message)s",
    )
    if config.auth_mode == "token" and not config.auth_token:
        # fail-closed: refuse to start token auth without a token
        # (outside production-like profiles, where the configuration
        # contract refuses earlier — ADR-017)
        print(
            json.dumps(
                {"error": "FIE_AUTH_MODE=token requires FIE_AUTH_TOKEN"},
                sort_keys=True,
            )
        )
        return 2
    try:
        server = run_server(config)
    except ConfigurationError as exc:
        # the composition-root DSN-shape gate (ADR-017 §4.4) refuses
        # before anything opens or binds — same sanitized refusal
        # contract; never a raw traceback, never a partial service
        print(
            json.dumps(
                {"error": {"code": exc.code, "message": exc.message}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    stop = threading.Event()

    def _terminate(*_a: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    stop.wait()
    server.shutdown()
    server.server_close()
    runtime = getattr(server, "_fie_runtime", None)
    if runtime is not None:
        runtime.close()
    return 0

if __name__ == "__main__":  # `python -m phase3.transport.http`
    raise SystemExit(main())
