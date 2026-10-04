"""Phase 6.7A contract reconciliation — mechanical drift detection (ADR-016).

Validates the checked-in machine contract
(``docs/architecture/phase6-6/http-api-contract.json``) against the
executable transport over real HTTP:

* T1 (declared ⇒ producible): every ``(path, status)`` declared in
  the contract is produced live through an explicitly anchored trigger
  (valid read, bad parameter, unknown evidence ref, token-mode 401,
  method rejection 405, catch-all 500, persistence failure 503);
* T2 (observed ⇒ declared): every status observed on any probe is a
  member of the path's declared set — no undeclared status may leak;
* envelope conformance: every probed body is validated field-by-field
  against the contract components (Envelope / ErrorEnvelope /
  LivenessEnvelope / FreshnessMetadata) with a stdlib mini-validator
  (no external JSON-Schema dependency);
* the two envelope classes (dispatch = kind=<operation>, principal
  set; transport = kind="transport", principal_id="") are pinned
  where each is produced;
* HEAD mirrors GET and the 405/Allow/message text stay coherent with
  the read-only interactive plane.

Drift in the contract file, or drift in the transport behaviour it
declares, fails these tests. ADR-016 defines the resolution order
(runtime semantics first; never normalise behaviour that contradicts
an approved security/transport requirement). The work order §13
drift proof (a deliberate temporary contract mutation must trip the
manifest tests) is exercised as a separate evidence step against
these same tests.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = (
    REPO_ROOT / "docs" / "architecture" / "phase6-6" /
    "http-api-contract.json"
)

_JSON_CT = "application/json; charset=utf-8"
_TOKEN = "tok-67a-contract-xyz"

_LEAK_MARKERS = (
    # Phase 6.7B-R4 (ADR-019 §1): the lowercase dependency FAMILY word
    # ("sqlite"/"postgres" in details.dependency) is contractual — the
    # forbidden shapes are driver/class/path/topology detail.
    "SQLite", "OperationalError", "no such table",
    "no such column", "score_snapshot", "signal_log", "traceback",
    "Traceback", "/tmp/", ".db", "postgres://", "postgresql://",
)

# contract path template → (concrete sample, runtime _route op name)
_PATH_SAMPLES: dict[str, tuple[str, str]] = {
    "/healthz": ("/healthz", "liveness"),
    "/readyz": ("/readyz", "readiness"),
    "/v1/health": ("/v1/health", "health"),
    "/v1/intelligence/latest": (
        "/v1/intelligence/latest?limit=4", "latest_intelligence"),
    "/v1/intelligence/entity/{kind}/{entity_id}": (
        "/v1/intelligence/entity/company/2330", "entity_intelligence"),
    "/v1/evidence/{ref}": (
        "/v1/evidence/entity%3Acompany%3A2330", "evidence"),
    "/v1/freshness/{kind}/{entity_id}": (
        "/v1/freshness/company/2330?as_of=2026-07-09", "freshness"),
}

# bad-parameter probes — 400 INVALID_REQUEST, dispatch-produced
_BAD_PARAM_PROBES: dict[str, str] = {
    "/v1/intelligence/latest": "/v1/intelligence/latest?limit=zz",
    "/v1/intelligence/entity/{kind}/{entity_id}":
        "/v1/intelligence/entity/region/2330",
    "/v1/evidence/{ref}":
        "/v1/evidence/entity%3Acompany%3A2330?limit=zz",
    "/v1/freshness/{kind}/{entity_id}": "/v1/freshness/crypto/2330",
}

# persistence-failure probes against an un-migrated database
# (S-04 family): health/readiness/freshness → 503, domain reads → 500
_UNMIGRATED_PROBES: dict[str, tuple[str, int]] = {
    "/readyz": ("/readyz", 503),
    "/v1/health": ("/v1/health", 503),
    "/v1/freshness/{kind}/{entity_id}": ("/v1/freshness/company/2330", 503),
    "/v1/intelligence/latest": ("/v1/intelligence/latest?limit=4", 500),
    "/v1/intelligence/entity/{kind}/{entity_id}": (
        "/v1/intelligence/entity/company/2330", 500),
    "/v1/evidence/{ref}": ("/v1/evidence/entity%3Acompany%3A2330", 500),
}


def _load_contract() -> dict[str, Any]:
    with open(CONTRACT_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _resolve_ref(root: dict[str, Any], ref: str) -> Any:
    assert ref.startswith("#/"), f"only local refs supported: {ref}"
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _validate(instance: Any, schema: dict[str, Any], root: dict[str, Any],
              out: list[str], path: str = "$") -> None:
    """Stdlib subset-validator over the constructs the contract uses:
    $ref (local), type (single or list incl. "null"), const, enum,
    required, properties, items, oneOf. Any other construct is
    manifest drift the manifest tests refuse to silently accept."""
    if "$ref" in schema:
        _validate(instance, _resolve_ref(root, schema["$ref"]), root, out,
                  path)
        return
    declared = schema.get("type")
    if declared is not None:
        types = [declared] if isinstance(declared, str) else declared
        checks = {
            "null": lambda v: v is None,
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int)
            and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float))
            and not isinstance(v, bool),
        }
        unsupported = [t for t in types if t not in checks]
        if unsupported:
            out.append(f"{path}: unsupported type(s) {unsupported}")
            return
        if not any(checks[t](instance) for t in types):
            out.append(f"{path}: expected type {declared!r}, "
                       f"got {type(instance).__name__}")
            return
    if "const" in schema and instance != schema["const"]:
        out.append(f"{path}: expected const {schema['const']!r}, "
                   f"got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        out.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")
    if "oneOf" in schema:
        for branch in schema["oneOf"]:
            branch_errs: list[str] = []
            _validate(instance, branch, root, branch_errs, path)
            if not branch_errs:
                break
        else:
            out.append(f"{path}: satisfies no oneOf branch")
            return
    if isinstance(instance, dict):
        for key in schema.get("required", ()):
            if key not in instance:
                out.append(f"{path}: missing required key {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                _validate(instance[key], sub, root, out, f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            _validate(item, schema["items"], root, out, f"{path}[{index}]")


def _request(method: str, url: str, *, headers: dict[str, str] | None = None,
             body: bytes | None = None):
    """Return (status, headers, body-or-None); HEAD callers read None."""
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return (response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        try:
            return (exc.code, dict(exc.headers), exc.read())
        finally:
            exc.close()


def _conforms(testcase: unittest.TestCase, envelope: dict[str, Any],
              schema_name: str, contract: dict[str, Any]) -> None:
    errs: list[str] = []
    _validate(envelope, contract["components"]["schemas"][schema_name],
              contract, errs, f"$:{schema_name}")
    testcase.assertEqual(
        errs, [],
        f"envelope violates {schema_name}: {json.dumps(envelope)[:400]}")


def _assert_no_leak(testcase: unittest.TestCase, envelope: dict[str, Any],
                    extra: tuple[str, ...] = ()) -> None:
    blob = json.dumps(envelope, ensure_ascii=False)
    for marker in _LEAK_MARKERS + extra:
        testcase.assertNotIn(marker, blob)


# ---------------------------------------------------------------- servers


class _ExplodingAuthenticator:
    """Authenticator raising a non-TransportError → catch-all 500."""

    mode = "token"
    principal_id = ""

    def authenticate(self, headers: Any) -> None:
        raise RuntimeError("boom internal secret detail")


def _start_server(
    db_path: str,
    *,
    config: Any = None,
    authenticator: Any = None,
):
    """Open runtime + bind server on an ephemeral port; return shutdown."""
    from phase3.transport.config import load_transport_config
    from phase3.transport.http import FIEReferenceRuntime, make_http_server

    if config is None:
        config = load_transport_config(db_path)
    runtime = FIEReferenceRuntime.open(db_path)
    try:
        # port 0: always bind an ephemeral port so parallel suites and
        # strays never collide (config default is 8787)
        server = make_http_server(runtime, config,
                                  authenticator=authenticator, port=0)
    except BaseException:
        runtime.close()
        raise
    host, port = server.server_address[0], server.server_address[1]
    base = f"http://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown() -> None:
        server.shutdown()
        server.server_close()
        runtime.close()

    return base, shutdown


class _MigratedDBMixin:
    """Disposable scored database for production-path probes."""

    @classmethod
    def _bootstrap(cls) -> None:
        from tests.phase3.service import helpers

        cls.bundle = helpers.bootstrap_state(None)

    @classmethod
    def _teardown_db(cls) -> None:
        cls.bundle["store"].close()
        path = Path(cls.bundle["path"])
        if path.exists():
            path.unlink()


class _MigratedDBBase(_MigratedDBMixin, unittest.TestCase):
    """Noop-auth server over a scored database — the success battery."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._bootstrap()
        cls.base, cls.shutdown = _start_server(cls.bundle["path"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.shutdown()
        cls._teardown_db()


class _TokenDBBase(_MigratedDBMixin, unittest.TestCase):
    """Token-mode server — the 401 battery (auth gate precedes routing)."""

    @classmethod
    def setUpClass(cls) -> None:
        from phase3.transport.config import load_transport_config

        cls._bootstrap()
        config = load_transport_config(cls.bundle["path"], auth_mode="token",
                                       auth_token=_TOKEN)
        cls.base, cls.shutdown = _start_server(cls.bundle["path"],
                                               config=config)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.shutdown()
        cls._teardown_db()


class _CatchAllDBBase(_MigratedDBMixin, unittest.TestCase):
    """Exploding-authenticator server — the transport catch-all battery."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._bootstrap()
        cls.base, cls.shutdown = _start_server(
            cls.bundle["path"], authenticator=_ExplodingAuthenticator())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.shutdown()
        cls._teardown_db()


class _UnmigratedDBBase(unittest.TestCase):
    """Server over a deliberately un-migrated (empty) SQLite file."""

    @classmethod
    def setUpClass(cls) -> None:
        handle, cls.db_path = tempfile.mkstemp(
            prefix="fie_67a_unmigrated_", suffix=".db")
        os.close(handle)
        sqlite3.connect(cls.db_path).close()
        cls.base, cls.shutdown = _start_server(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.shutdown()
        if Path(cls.db_path).exists():
            os.unlink(cls.db_path)


# ------------------------------------------------------- manifest checks


class TestContractManifest(unittest.TestCase):
    """The checked-in machine contract itself (ADR-016 layer 2)."""

    def test_contract_file_parses_with_expected_identity(self) -> None:
        contract = _load_contract()
        self.assertEqual(contract["openapi"], "3.1.0")
        self.assertEqual(contract["info"]["schema_version"], "6.5")
        self.assertEqual(set(contract["paths"]), set(_PATH_SAMPLES))

    def test_every_ref_resolves(self) -> None:
        contract = _load_contract()
        found: list[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref":
                        found.append(value)
                    else:
                        _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(contract)
        self.assertTrue(found, "contract must declare components via $ref")
        for ref in found:
            _resolve_ref(contract, ref)

    def test_declared_status_matrix_is_pinned(self) -> None:
        """Per-path status sets — changed only with matching evidence."""
        expected = {
            # Phase 6.7B-R2/ADR-018: /healthz is the public operational
            # liveness probe — served before authentication (no 401) and
            # it never reaches the transport dispatch edge (no 500).
            "/healthz": {200, 405},
            "/readyz": {200, 401, 405, 500, 503},
            "/v1/health": {200, 401, 405, 500, 503},
            "/v1/intelligence/latest": {200, 400, 401, 405, 500},
            "/v1/intelligence/entity/{kind}/{entity_id}":
                {200, 400, 401, 405, 500},
            "/v1/evidence/{ref}": {200, 400, 401, 404, 405, 500},
            "/v1/freshness/{kind}/{entity_id}":
                {200, 400, 401, 405, 500, 503},
        }
        contract = _load_contract()
        for template, statuses in expected.items():
            with self.subTest(path=template):
                declared = {int(k) for k in
                            contract["paths"][template]["get"]["responses"]}
                self.assertEqual(declared, statuses)

    def test_get_is_the_only_declared_method(self) -> None:
        contract = _load_contract()
        for template, operations in contract["paths"].items():
            with self.subTest(path=template):
                self.assertEqual(set(operations), {"get"},
                                 "interactive plane is GET-only; HEAD is "
                                 "declared via x-method-behavior")

    def test_declared_routes_equal_runtime_routes(self) -> None:
        from phase3.transport.http import _route

        for template, (sample, op) in _PATH_SAMPLES.items():
            with self.subTest(path=template):
                got = _route(sample.split("?")[0])
                self.assertIsNotNone(got)
                self.assertEqual(got[0], op)

    def test_malformed_and_unknown_routes_are_not_silent(self) -> None:
        from phase3.transport.http import _route

        # malformed arity → ("invalid", {}) so the transport answers 400
        self.assertEqual(_route("/v1/intelligence/entity/company"),
                         ("invalid", {}))
        self.assertEqual(_route("/v1/evidence/a/b"), ("invalid", {}))
        self.assertEqual(_route("/v1/freshness/company"), ("invalid", {}))
        # wholly unknown families route nowhere (no-route → 400 transport)
        self.assertIsNone(_route("/v1/nope"))
        self.assertIsNone(_route("/v2/health"))
        self.assertIsNone(_route("/healthz/extra"))

    def test_status_mapping_matches_transport_errors(self) -> None:
        from phase3.transport.errors import HTTP_STATUS_BY_CODE

        contract = _load_contract()
        mapping = {key: value
                   for key, value in contract["x-http-status-mapping"].items()
                   if key != "description"}
        self.assertEqual(mapping, dict(HTTP_STATUS_BY_CODE))

    def test_method_behavior_declaration_present(self) -> None:
        contract = _load_contract()
        self.assertIn("HEAD mirrors GET",
                      contract["x-method-behavior"]["head"])
        self.assertIn("405", contract["x-method-behavior"]["non-get"])

    def test_canonical_sot_pointer(self) -> None:
        contract = _load_contract()
        self.assertTrue(
            any("ADR-016" in item
                for item in contract["x-transport-invariants"]),
            "contract must point at the canonical SoT ADR")
        self.assertTrue(
            (REPO_ROOT / "docs/adr/adr-016-canonical-machine-contract.md")
            .exists())


# --------------------------------------------------- success-side checks


class TestSuccessEnvelopeConformance(_MigratedDBBase):
    """T2 success side: live envelopes validate against components."""

    def test_liveness_readiness_health_conform(self) -> None:
        contract = _load_contract()
        status, headers, body = _request("GET", self.base + "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], _JSON_CT)
        liveness = json.loads(body)
        _conforms(self, liveness, "LivenessEnvelope", contract)
        self.assertEqual(liveness["kind"], "healthz")

        for path in ("/readyz", "/v1/health"):
            with self.subTest(path=path):
                status, headers, body = _request("GET", self.base + path)
                self.assertEqual(status, 200)
                envelope = json.loads(body)
                _conforms(self, envelope, "Envelope", contract)
                self.assertEqual(envelope["kind"], "health")
                self.assertEqual(headers["X-Request-Id"],
                                 envelope["request_id"])
                self.assertEqual(envelope["principal_id"], "anonymous")
                self.assertIsNone(envelope["freshness"])

    def test_latest_payload_is_array_and_freshness_slot_nullable(self) -> None:
        """S-01/S-05 regressions, validated against the contract."""
        contract = _load_contract()
        status, _, body = _request("GET",
                                   self.base + "/v1/intelligence/latest?limit=4")
        self.assertEqual(status, 200)
        envelope = json.loads(body)
        _conforms(self, envelope, "Envelope", contract)
        self.assertIsInstance(envelope["payload"], list,
                              "latest payload is an array of items")
        self.assertIsNone(
            envelope["freshness"],
            "freshness is null for multi-item reads (S-01)")
        self.assertTrue(envelope["payload"])
        schema = contract["components"]["schemas"]
        for index, item in enumerate(envelope["payload"]):
            # items are scored domain objects, not envelopes
            self.assertIsInstance(item, dict)
            self.assertIn("entity_id", item)
            self.assertIn("evidence_refs", item)
            if item["freshness"] is not None:
                errs: list[str] = []
                _validate(item["freshness"], schema["FreshnessMetadata"],
                          contract, errs, f"$.payload[{index}].freshness")
                self.assertEqual(errs, [])

    def test_entity_evidence_freshness_conform(self) -> None:
        contract = _load_contract()
        status, _, body = _request(
            "GET", self.base + "/v1/intelligence/entity/company/2330")
        self.assertEqual(status, 200)
        envelope = json.loads(body)
        _conforms(self, envelope, "Envelope", contract)
        self.assertEqual(envelope["kind"], "entity_intelligence")
        self.assertIsInstance(envelope["freshness"], dict,
                              "single-result read carries metadata object")
        self.assertEqual(envelope["payload"]["entity_id"], "2330")

        status, _, body = _request(
            "GET", self.base + "/v1/evidence/entity%3Acompany%3A2330")
        self.assertEqual(status, 200)
        envelope = json.loads(body)
        _conforms(self, envelope, "Envelope", contract)
        self.assertEqual(envelope["kind"], "evidence")
        self.assertIsNone(envelope["freshness"])
        self.assertEqual(envelope["payload"]["ref"], "entity:company:2330")

        status, _, body = _request(
            "GET", self.base + "/v1/freshness/company/2330?as_of=2026-07-09")
        self.assertEqual(status, 200)
        envelope = json.loads(body)
        _conforms(self, envelope, "Envelope", contract)
        self.assertEqual(envelope["kind"], "freshness")
        self.assertIsInstance(envelope["freshness"], dict)

    def test_bad_parameters_are_declared_400s(self) -> None:
        """S-06 bad-parameter battery — sanitised dispatch errors."""
        contract = _load_contract()
        for template, probe in _BAD_PARAM_PROBES.items():
            with self.subTest(probe=probe):
                status, headers, body = _request("GET", self.base + probe)
                self.assertEqual(status, 400)
                envelope = json.loads(body)
                _conforms(self, envelope, "ErrorEnvelope", contract)
                # 400s on parameterised paths are DISPATCH errors, not
                # transport errors — kind is the operation name
                self.assertFalse(template == "/v1/intelligence/latest"
                                 and envelope["kind"] == "transport")
                self.assertEqual(envelope["status"], "error")
                self.assertEqual(envelope["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(envelope["payload"], None)
                self.assertEqual(headers["X-Request-Id"],
                                 envelope["request_id"])
                _assert_no_leak(self, envelope)

    def test_evidence_unknown_ref_is_declared_404(self) -> None:
        contract = _load_contract()
        status, headers, body = _request(
            "GET", self.base + "/v1/evidence/does-not-exist")
        self.assertEqual(status, 404)
        envelope = json.loads(body)
        _conforms(self, envelope, "ErrorEnvelope", contract)
        self.assertEqual(envelope["kind"], "evidence")
        self.assertEqual(envelope["error"]["code"], "NOT_FOUND")
        self.assertNotEqual(envelope["principal_id"], "")
        self.assertEqual(headers["X-Request-Id"], envelope["request_id"])
        _assert_no_leak(self, envelope)

    def test_unknown_entity_is_200_unavailable_not_404(self) -> None:
        """S-06: entity 404 was declared-but-unreachable — removed 6.7A."""
        status, _, body = _request(
            "GET", self.base + "/v1/intelligence/entity/company/9999")
        self.assertEqual(status, 200)
        envelope = json.loads(body)
        self.assertEqual(envelope["status"], "ok")
        self.assertEqual(envelope["freshness"]["state"], "UNAVAILABLE")
        # unavailable payload: scored fields present but empty/None
        self.assertEqual(envelope["payload"]["entity_id"], "9999")
        self.assertIsNone(envelope["payload"]["score"])
        self.assertEqual(envelope["payload"]["signals"], [])
        self.assertIn("no deterministic score snapshot available",
                      " ".join(envelope["warnings"]))


# ---------------------------------------------------- method/HEAD checks


class TestMethodRejectionAndHead(_MigratedDBBase):
    """405 coherence (D-02) + HEAD mirroring (S-02) on every path."""

    def test_405_per_path_conforms(self) -> None:
        contract = _load_contract()
        for template, (sample, _) in _PATH_SAMPLES.items():
            with self.subTest(path=template):
                status, headers, body = _request("POST", self.base + sample,
                                                 body=b"{}")
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET, HEAD")
                envelope = json.loads(body)
                _conforms(self, envelope, "ErrorEnvelope", contract)
                self.assertEqual(envelope["kind"], "transport")
                self.assertEqual(envelope["principal_id"], "")
                self.assertEqual(envelope["error"]["code"], "INVALID_REQUEST")
                self.assertEqual(
                    envelope["error"]["message"],
                    "only GET or HEAD is supported; the interactive plane "
                    "is read-only")
                self.assertEqual(headers["X-Request-Id"],
                                 envelope["request_id"])
                self.assertEqual(headers["Content-Type"], _JSON_CT)
                _assert_no_leak(self, envelope)

    def test_put_patch_delete_are_rejected_too(self) -> None:
        status, _, _ = _request("DELETE", self.base + "/v1/health")
        self.assertEqual(status, 405)
        status, _, _ = _request("PUT", self.base + "/healthz")
        self.assertEqual(status, 405)

    def test_head_mirrors_get_on_every_path(self) -> None:
        contract = _load_contract()
        for template, (sample, _) in _PATH_SAMPLES.items():
            with self.subTest(path=template):
                get_status, get_headers, _ = _request("GET",
                                                      self.base + sample)
                head_status, head_headers, body = _request("HEAD",
                                                           self.base + sample)
                self.assertEqual(head_status, get_status)
                self.assertEqual(body, b"", "HEAD body must be empty")
                self.assertEqual(head_headers["Content-Type"], _JSON_CT)
                self.assertTrue(head_headers.get("X-Request-Id"))
                self.assertEqual(
                    int(head_headers["Content-Length"]),
                    int(get_headers["Content-Length"]))
                # 200 is declared for every path (noop auth)
                self.assertIn(200,
                              {int(k) for k in
                               contract["paths"][template]["get"]["responses"]})


# ------------------------------------------------------ request-ID paths


class TestRequestIDCoherence(_MigratedDBBase):
    """Supplied / generated / error paths — 6.6R1 invariants intact."""

    def test_supplied_generated_and_error_paths(self) -> None:
        for template, (sample, _) in _PATH_SAMPLES.items():
            with self.subTest(path=template):
                status, headers, body = _request(
                    "GET", self.base + sample,
                    headers={"X-Request-Id": "req-67a-1"})
                envelope = json.loads(body)
                self.assertEqual(envelope["request_id"], "req-67a-1")
                self.assertEqual(headers["X-Request-Id"], "req-67a-1")

                estatus, eheaders, ebody = _request(
                    "POST", self.base + sample, body=b"{}")
                eenv = json.loads(ebody)
                self.assertEqual(estatus, 405)
                self.assertEqual(eheaders["X-Request-Id"], eenv["request_id"])
                self.assertTrue(eenv["request_id"],
                                "error path carries a generated request id")
                self.assertNotEqual(eenv["request_id"], "unassigned")


# ----------------------------------------------------------- auth gate


class TestTransportAuthGate(_TokenDBBase):
    """401 UNAUTHENTICATED on every declared protected path (S-03).

    Phase 6.7B-R2/ADR-018: /healthz is the public operational liveness
    probe — served BEFORE authentication and verified separately below
    (200 credentialless in token mode, identical to authenticated).
    """

    def test_401_on_every_declared_path(self) -> None:
        contract = _load_contract()
        probe_templates = (t for t in _PATH_SAMPLES if t != "/healthz")
        for template in probe_templates:
            sample = _PATH_SAMPLES[template][0]
            with self.subTest(path=template):
                declared = {int(k) for k in
                            contract["paths"][template]["get"]["responses"]}
                status, headers, body = _request("GET", self.base + sample)
                self.assertEqual(status, 401)
                self.assertIn(401, declared)
                envelope = json.loads(body)
                _conforms(self, envelope, "ErrorEnvelope", contract)
                self.assertEqual(envelope["kind"], "transport")
                self.assertEqual(envelope["principal_id"], "")
                self.assertEqual(envelope["error"]["code"], "UNAUTHENTICATED")
                self.assertEqual(headers["X-Request-Id"],
                                 envelope["request_id"])
                self.assertEqual(headers["Content-Type"], _JSON_CT)
                _assert_no_leak(self, envelope, extra=(_TOKEN,))

    def test_liveness_probe_is_public_in_token_mode(self) -> None:
        """The HP-01 repair: /healthz must answer 200 with NO credential
        in token mode, identical to the response with a VALID credential
        (the probe never evaluates credentials — no validity oracle).
        A fixed X-Request-Id pins the volatile field so the bodies can
        be compared byte-identically."""
        contract = _load_contract()
        bodies = {}
        for label, headers in (
            ("none", {"X-Request-Id": "req-probe-oracle"}),
            ("valid", {"X-Request-Id": "req-probe-oracle",
                       "Authorization": f"Bearer {_TOKEN}"}),
            ("invalid", {"X-Request-Id": "req-probe-oracle",
                         "Authorization": "Bearer definitely-not-the-token"}),
        ):
            with self.subTest(credential=label):
                status, _, body = _request(
                    "GET", self.base + "/healthz", headers=headers)
                self.assertEqual(status, 200)
                _conforms(self, json.loads(body),
                          "LivenessEnvelope", contract)
                bodies[label] = body
        self.assertEqual(bodies["none"], bodies["valid"])
        self.assertEqual(bodies["none"], bodies["invalid"])

    def test_declared_401_shares_error_component(self) -> None:
        contract = _load_contract()
        for template in _PATH_SAMPLES:
            responses = contract["paths"][template]["get"]["responses"]
            if "401" not in responses:
                continue  # probe-surface path: no declared 401 (ADR-018)
            with self.subTest(path=template):
                declared = responses["401"]
                if "$ref" in declared:
                    declared = _resolve_ref(contract, declared["$ref"])
                ref = (declared["content"]["application/json"]["schema"]
                       ["$ref"])
                self.assertEqual(ref,
                                 "#/components/schemas/ErrorEnvelope")

    def test_valid_token_passes(self) -> None:
        status, _, body = _request(
            "GET", self.base + "/v1/health",
            headers={"Authorization": f"Bearer {_TOKEN}"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")


# -------------------------------------------------------- catch-all 500


class TestCatchAll500(_CatchAllDBBase):
    """Transport catch-all — kind=transport, sanitised, id-coherent."""

    def test_500_on_every_path_conforms(self) -> None:
        contract = _load_contract()
        for template, (sample, _) in _PATH_SAMPLES.items():
            if template == "/healthz":
                # Phase 6.7B-R2/ADR-018: the public liveness probe never
                # reaches the transport dispatch edge, so the catch-all
                # is unreachable on it — an exploding Authenticator (a
                # broken IdP integration) must NOT take liveness down.
                status, _, body = _request("GET", self.base + sample)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["status"], "alive")
                continue
            with self.subTest(path=template):
                status, headers, body = _request("GET", self.base + sample)
                self.assertEqual(status, 500)
                envelope = json.loads(body)
                _conforms(self, envelope, "ErrorEnvelope", contract)
                self.assertEqual(envelope["kind"], "transport")
                self.assertEqual(envelope["error"]["code"], "INTERNAL_ERROR")
                self.assertEqual(envelope["error"]["message"],
                                 "internal transport failure")
                self.assertEqual(headers["X-Request-Id"],
                                 envelope["request_id"])
                self.assertEqual(headers["Content-Type"], _JSON_CT)
                _assert_no_leak(self, envelope, extra=("boom",))


# --------------------------------------------- persistence failure family


class _UnmigratedStatuses(_UnmigratedDBBase):
    """S-04 family: un-migrated persistence — 503 DEPENDENCY_UNAVAILABLE
    for health/readiness/freshness, 500 INTERNAL_ERROR for domain reads.
    All conform to the declared ErrorEnvelope per path (T1 anchors)."""

    def test_error_statuses_conform_to_contract(self) -> None:
        contract = _load_contract()
        for template, (probe, expected_status) in _UNMIGRATED_PROBES.items():
            with self.subTest(path=template):
                declared = {int(k) for k in
                            contract["paths"][template]["get"]["responses"]}
                self.assertIn(expected_status, declared)
                status, headers, body = _request("GET", self.base + probe)
                self.assertEqual(status, expected_status)
                envelope = json.loads(body)
                _conforms(self, envelope, "ErrorEnvelope", contract)
                self.assertEqual(envelope["status"], "error")
                # persistence failures are DISPATCH errors: kind is the
                # operation name, principal preserved (health/readiness
                # dispatch under kind=health; freshness under its op)
                op = _PATH_SAMPLES[template][1]
                expected_kind = ("health" if op in ("readiness", "health")
                                 else op)
                self.assertEqual(envelope["kind"], expected_kind)
                self.assertNotEqual(envelope["principal_id"], "")
                self.assertEqual(headers["X-Request-Id"],
                                 envelope["request_id"])
                self.assertEqual(headers["Content-Type"], _JSON_CT)
                _assert_no_leak(self, envelope)

    def test_declared_503_references_error_envelope(self) -> None:
        contract = _load_contract()
        for template in ("/readyz", "/v1/health",
                         "/v1/freshness/{kind}/{entity_id}"):
            with self.subTest(path=template):
                responses = contract["paths"][template]["get"]["responses"]
                ref = responses["503"]["content"]["application/json"]["schema"]["$ref"]
                self.assertEqual(ref, "#/components/schemas/ErrorEnvelope")


# ----------------------------------------------- T1/T2 full status matrix


class TestDeclaredStatusMatrix(unittest.TestCase):
    """T1 + T2 in one two-way gate, driven over four live servers.

    For every path: produced statuses (union of all anchored triggers
    here) must EQUAL the declared set — every declaration producible,
    nothing undeclared reachable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from phase3.transport.config import load_transport_config

        cls._closers = []
        cls._bundles = []
        cls._dbs = []

        from tests.phase3.service import helpers

        bundle = helpers.bootstrap_state(None)  # migrated, noop auth
        cls._bundles.append(bundle)
        base, closer = _start_server(bundle["path"])
        cls.base_noop = base
        cls._closers.append(closer)

        config = load_transport_config(bundle["path"], auth_mode="token",
                                       auth_token=_TOKEN)
        base, closer = _start_server(bundle["path"], config=config)
        cls.base_token = base
        cls._closers.append(closer)

        base, closer = _start_server(bundle["path"],
                                     authenticator=_ExplodingAuthenticator())
        cls.base_explode = base
        cls._closers.append(closer)

        handle, cls.unmig_db = tempfile.mkstemp(
            prefix="fie_67a_matrix_", suffix=".db")
        os.close(handle)
        sqlite3.connect(cls.unmig_db).close()
        base, closer = _start_server(cls.unmig_db)
        cls.base_unmig = base
        cls._closers.append(closer)

    @classmethod
    def tearDownClass(cls) -> None:
        for closer in cls._closers:
            closer()
        for bundle in cls._bundles:
            bundle["store"].close()
            path = Path(bundle["path"])
            if path.exists():
                path.unlink()
        if Path(cls.unmig_db).exists():
            os.unlink(cls.unmig_db)

    def _probes(self, base: str, template: str, sample: str) -> list[int]:
        produced: list[int] = []
        get_status, _, _ = _request("GET", base + sample.split("?")[0]
                                    + ("?" + sample.split("?")[1]
                                       if "?" in sample else ""))
        produced.append(get_status)
        post_status, _, _ = _request("POST", base + sample.split("?")[0],
                                     body=b"{}")
        produced.append(post_status)
        head_status, _, body = _request("HEAD",
                                        base + sample.split("?")[0])
        produced.append(head_status)
        self.assertEqual(body, b"", "HEAD body must be empty")
        return produced

    def test_declared_equals_produced_per_path(self) -> None:
        contract = _load_contract()
        for template, (sample, _) in _PATH_SAMPLES.items():
            with self.subTest(path=template):
                declared = {int(k) for k in
                            contract["paths"][template]["get"]["responses"]}
                produced: set[int] = set()

                # noop server: success, bad parameter, unknown ref
                produced.update(self._probes(self.base_noop, template, sample))
                if template in _BAD_PARAM_PROBES:
                    status, _, body = _request(
                        "GET", self.base_noop + _BAD_PARAM_PROBES[template])
                    produced.add(status)
                    self.assertEqual(status, 400)
                    self.assertEqual(json.loads(body)["error"]["code"],
                                     "INVALID_REQUEST")
                if template == "/v1/evidence/{ref}":
                    status, _, _ = _request(
                        "GET", self.base_noop + "/v1/evidence/missing-67a")
                    produced.add(status)
                    self.assertEqual(status, 404)

                # token server: auth gate on every declared path
                # (EXCEPT /healthz — public operational probe, ADR-018)
                status, _, _ = _request("GET", self.base_token + sample)
                produced.add(status)
                self.assertEqual(status,
                                 401 if template != "/healthz" else 200)

                # exploding server: transport catch-all on every path
                # (EXCEPT /healthz — liveness survives authenticator
                # failure, ADR-018)
                status, _, _ = _request("GET", self.base_explode + sample)
                produced.add(status)
                self.assertEqual(status, 500 if template != "/healthz"
                                 else 200)

                # un-migrated server: persistence-failure statuses
                if template in _UNMIGRATED_PROBES:
                    probe, expected = _UNMIGRATED_PROBES[template]
                    status, _, body = _request("GET",
                                               self.base_unmig + probe)
                    produced.add(status)
                    self.assertEqual(status, expected)

                self.assertEqual(
                    produced, declared,
                    f"{template}: declared {sorted(declared)} but produced "
                    f"{sorted(produced)}")


if __name__ == "__main__":
    unittest.main()