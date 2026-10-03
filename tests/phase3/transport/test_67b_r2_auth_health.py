"""Phase 6.7B-R2 — authentication & health-probe runtime hardening.

Focused permanent gate for the R2 slice of the accepted 6.7B kickoff
(work order §11–§20). The defect repaired is HP-01: token mode put the
bearer gate on EVERY path including GET /healthz, while the Docker
HEALTHCHECK is (by design) credentialless — so a correctly configured
authenticated container could never report healthy.

The accepted contract (ADR-018 — ``docs/adr/adr-018-operational-probe-
surface.md``):

* ``GET/HEAD /healthz`` is the ONE public operational liveness probe
  (closed single-member set :data:`OPERATIONAL_PROBE_OPERATIONS`,
  ``frozenset({"liveness"})``). Served BEFORE authentication in every
  profile; the handler builds a fixed minimal envelope — no database,
  no filesystem, no business service, no protected data — and the
  Authenticator is NOT invoked, so a probe can never double as a
  credential-validity oracle.
* ``GET /readyz`` stays AUTHENTICATED (it exercises the persistence
  seam via the service ``health`` op): credentialless callers keep
  receiving 401 in token mode; full readiness detail remains at the
  authenticated ``/v1/health``.
* There is NO bypass mechanism of any kind: no header, query
  parameter, environment variable, source address, User-Agent token or
  probe token can disable authentication anywhere or widen the probe
  surface. Everything in this file proves that affirmatively.

Synthetic fixtures only: the bearer marker ``R2_SYNTHETIC_BEARER_DO_
NOT_USE`` is a designated synthetic credential — every leak assertion
below also verifies it never escapes into responses or telemetry.
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from phase3.service import SCHEMA_VERSION  # noqa: E402
from phase3.transport.http import (  # noqa: E402
    OPERATIONAL_PROBE_OPERATIONS,
    _route,
)
from tests.phase3.transport.runtime_harness import RuntimeHarness  # noqa: E402

#: designated synthetic bearer — "do not use" is the point of the name
R2_TOKEN = "R2_SYNTHETIC_BEARER_DO_NOT_USE"  # noqa: S105 - synthetic fixture
_OTHER_TOKEN = "also-synthetic-but-wrong"  # noqa: S105 - synthetic fixture

_PROTECTED = "/v1/health"
_HEALTHZ = "/healthz"
_READYZ = "/readyz"

_ENVELOPE_LEAK_MARKERS = (
    "SELECT ", "INSERT INTO", "signal_log", "score_snapshot",
    "postgres://", "postgresql://", "sqlite:", "/tmp/", ".db",
)


def _blob(data: bytes | dict) -> str:
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return json.dumps(data, ensure_ascii=False)


class TestProtectedSurface(RuntimeHarness):
    """§11A — the bearer gate on the protected surface, unregressed."""

    config_overrides = {"auth_mode": "token", "auth_token": R2_TOKEN}

    def test_missing_authorization_401(self) -> None:
        status, env, headers = self.get(_PROTECTED)
        self.assertEqual(status, 401)
        self.assertEqual(env["kind"], "transport")
        self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
        self.assertEqual(headers["X-Request-Id"], env["request_id"])

    def test_malformed_schemes_401(self) -> None:
        for header in (
            "Basic dXNlcjpwd2Q=",           # wrong scheme
            "Bearer",                        # scheme with no token
            "Bearer     ",                   # scheme, whitespace only
            "",                              # empty header
            "Tok " + R2_TOKEN,               # unknown scheme
            "Bearer\t" + R2_TOKEN,           # tab separator is not "Bearer[ ]+"
            "bearer" + R2_TOKEN,             # missing separator
        ):
            with self.subTest(header=header):
                status, env, _ = self.get(
                    _PROTECTED, headers={"Authorization": header})
                self.assertEqual(status, 401, repr(header))
                self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")

    def test_wrong_bearer_401(self) -> None:
        status, env, _ = self.get(
            _PROTECTED, headers={"Authorization": "Bearer " + _OTHER_TOKEN})
        self.assertEqual(status, 401)
        # the presented credential never echoes back in the failure
        self.assertNotIn(_OTHER_TOKEN, _blob(env))

    def test_wrong_bearer_message_is_fixed_string(self) -> None:
        status, env, _ = self.get(
            _PROTECTED, headers={"Authorization": "Bearer " + _OTHER_TOKEN})
        self.assertEqual(status, 401)
        self.assertEqual(env["error"]["message"], "invalid credentials")
        self.assertEqual(env["principal_id"], "")

    def test_valid_bearer_allowed(self) -> None:
        status, env, _ = self.get(
            _PROTECTED, headers={"Authorization": "Bearer " + R2_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(env["kind"], "health")
        self.assertEqual(env["status"], "ok")

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        # documented behaviour: the header VALUE is stripped before the
        # Bearer[ ]+(\S+) fullmatch, so surrounding padding is accepted
        status, env, _ = self.get(
            _PROTECTED,
            headers={"Authorization": f"  Bearer {R2_TOKEN}  "})
        self.assertEqual(status, 200)
        self.assertEqual(env["status"], "ok")

    def test_multiple_whitespace_runs_accepted(self) -> None:
        status, env, _ = self.get(
            _PROTECTED,
            headers={"Authorization": f"Bearer   {R2_TOKEN}"})
        self.assertEqual(status, 200)

    def test_duplicate_authorization_headers_first_wins(self) -> None:
        """Documented framework behaviour: with two Authorization
        headers on the wire, the transport edge sees the FIRST one
        (email.message.Message.get semantics). A matching first header
        authenticates; a mismatched first header rejects. Either way a
        probe surface is never reached and the pair is never combined."""
        for first, expected in ((R2_TOKEN, 200), (_OTHER_TOKEN, 401)):
            with self.subTest(first=expected):
                conn = http.client.HTTPConnection(
                    self.base.split("//", 1)[1])
                try:
                    conn.connect()
                    conn.sock.sendall(
                        (f"GET {_PROTECTED} HTTP/1.1\r\n"
                         f"Host: {self.base.split('//', 1)[1]}\r\n"
                         f"Authorization: Bearer {first}\r\n"
                         f"Authorization: Bearer {_OTHER_TOKEN}\r\n"
                         "Connection: close\r\n\r\n").encode())
                    response = http.client.HTTPResponse(
                        conn.sock, method="GET")
                    response.begin()
                    self.assertEqual(response.status, expected)
                    response.read()
                finally:
                    conn.close()

    def test_startup_refuses_token_without_credential(self) -> None:
        # fail-closed configuration (R1 boundary preserved in R2):
        # token mode with no credential refuses AT CONSTRUCTION
        from phase3.transport.auth import build_authenticator
        from phase3.transport.errors import TransportError

        with self.assertRaises(TransportError):
            build_authenticator("token", token="")


class TestHealthProbeSurface(RuntimeHarness):
    """§11B/§8 — the probe surface contract in token mode."""

    config_overrides = {"auth_mode": "token", "auth_token": R2_TOKEN}

    def test_healthz_public_contract_no_credential(self) -> None:
        status, env, headers = self.get(_HEALTHZ)
        self.assertEqual(status, 200)
        self.assertEqual(env["kind"], "healthz")
        self.assertEqual(env["status"], "alive")
        self.assertEqual(env["schema_version"], SCHEMA_VERSION)
        # minimal fixed envelope: nothing else is disclosed
        self.assertEqual(
            sorted(env), ["kind", "request_id", "schema_version", "status"])
        self.assertNotIn("principal_id", env)
        self.assertNotIn("backend_kind", env)
        self.assertNotIn("counts", env)
        self.assertEqual(headers["X-Request-Id"], env["request_id"])

    def test_healthz_never_carries_leak_markers(self) -> None:
        _, env, _ = self.get(_HEALTHZ)
        blob = _blob(env)
        for marker in _ENVELOPE_LEAK_MARKERS + (R2_TOKEN,):
            self.assertNotIn(marker, blob)

    def test_readyz_stays_authenticated(self) -> None:
        status, env, _ = self.get(_READYZ)
        self.assertEqual(status, 401)
        self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
        # the readiness probe never becomes a credentialless data surface
        self.assertNotIn("backend_kind", env)
        self.assertNotIn("counts", env)

    def test_readyz_with_valid_bearer(self) -> None:
        status, env, _ = self.get(
            _READYZ, headers={"Authorization": "Bearer " + R2_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(env["kind"], "health")
        self.assertEqual(env["status"], "ok")

    def test_healthz_touches_no_dispatch(self) -> None:
        self.clear_records()
        self.get(_HEALTHZ)
        # transport liveness telemetry only — the service boundary was
        # never invoked for the probe
        records = self.records()
        self.assertTrue(
            any(r.get("operation") == "liveness" and r.get("http_status") == 200
                for r in records))
        self.assertFalse(
            any(r.get("operation") == "health" for r in records),
            "liveness must not dispatch into the service")

    def test_health_probe_telemetry_is_standard_shape(self) -> None:
        self.clear_records()
        self.get(_HEALTHZ)
        record = next(r for r in self.records()
                      if r.get("operation") == "liveness")
        self.assertEqual(record["event"], "http_request")
        self.assertEqual(record["principal_id"], "")  # unauthenticated probe
        self.assertEqual(record["path"], _HEALTHZ)
        blob = _blob(record)
        self.assertNotIn(R2_TOKEN, blob)
        self.assertNotIn("Authorization", blob)


class TestHealthProbeSurfaceNoopMode(TestHealthProbeSurface):
    """local + auth none: the liveness contract is mode-independent."""

    config_overrides = {}

    def test_readyz_stays_authenticated(self) -> None:
        # mode=none means the anonymous principal IS the authenticated
        # shape; readiness serves the health envelope as always
        status, env, _ = self.get(_READYZ)
        self.assertEqual(status, 200)
        self.assertEqual(env["kind"], "health")

    def test_healthz_touches_no_dispatch(self) -> None:
        # noop mode: telemetry still uses the transport probe path
        super().test_healthz_touches_no_dispatch()


class TestProductionLikeTokenMode(unittest.TestCase):
    """§11B — staging/production-like token configuration, over HTTP,
    without any production infrastructure (disposable local DB)."""

    @classmethod
    def setUpClass(cls) -> None:
        from phase3.transport.config import load_transport_config
        from phase3.transport.http import FIEReferenceRuntime, make_http_server
        from tests.phase3.service import helpers

        cls.bundle = helpers.bootstrap_state(None)
        cls.servers = []
        for profile in ("staging", "production"):
            config = load_transport_config(
                cls.bundle["path"],
                auth_mode="token",
                auth_token=R2_TOKEN,
                service_env=profile,
            )
            runtime = FIEReferenceRuntime.open(cls.bundle["path"])
            server = make_http_server(runtime, config, port=0)
            server._fie_runtime = runtime
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.servers.append(server)

    @classmethod
    def tearDownClass(cls) -> None:
        for server in cls.servers:
            server.shutdown()
            server.server_close()
            server._fie_runtime.close()
        cls.bundle["store"].close()
        Path(cls.bundle["path"]).unlink()

    def test_healthz_public_readyz_authenticated(self) -> None:
        for index in (0, 1):
            base = self._base(index)
            with self.subTest(server=index):
                status, env = _http_get(base, _HEALTHZ)
                self.assertEqual(status, 200)
                self.assertEqual(env["status"], "alive")
                self.assertNotIn("principal_id", env)
                status, env = _http_get(base, _READYZ)
                self.assertEqual(status, 401)
                self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
                status, env = _http_get(
                    base, _READYZ,
                    headers={"Authorization": "Bearer " + R2_TOKEN})
                self.assertEqual(status, 200)
                self.assertEqual(env["kind"], "health")

    def test_production_like_protected_surface_still_gated(self) -> None:
        base = self._base(1)  # production-like profile
        status, env = _http_get(base, _PROTECTED)
        self.assertEqual(status, 401)
        status, env = _http_get(
            base, _PROTECTED,
            headers={"Authorization": "Bearer " + R2_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(env["status"], "ok")

    def _base(self, index: int) -> str:
        host, port = self.servers[index].server_address[:2]
        return f"http://{host}:{port}"


def _http_get(base: str, path: str, headers: dict | None = None):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestProbeNoCredentialValidityOracle(RuntimeHarness):
    """ADR-018: the probe must never reveal whether a credential was
    valid — /healthz does not invoke the Authenticator at all."""

    config_overrides = {"auth_mode": "token", "auth_token": R2_TOKEN}

    def test_identical_response_for_missing_malformed_invalid_valid(self) -> None:
        bodies = {}
        for label, headers in (
            ("none", {"X-Request-Id": "req-r2-oracle"}),
            ("malformed", {"X-Request-Id": "req-r2-oracle",
                           "Authorization": "not-a-scheme"}),
            ("invalid", {"X-Request-Id": "req-r2-oracle",
                         "Authorization": "Bearer " + _OTHER_TOKEN}),
            ("valid", {"X-Request-Id": "req-r2-oracle",
                       "Authorization": "Bearer " + R2_TOKEN}),
        ):
            with self.subTest(credential=label):
                status, body = _http_get_raw(self.base, _HEALTHZ, headers)
                self.assertEqual(status, 200)
                bodies[label] = body
        self.assertEqual(bodies["none"], bodies["malformed"])
        self.assertEqual(bodies["none"], bodies["invalid"])
        self.assertEqual(bodies["none"], bodies["valid"])


def _http_get_raw(base: str, path: str, headers: dict | None = None):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestAdversarialIsolation(RuntimeHarness):
    """§11C/§12 — NOTHING probe-like bypasses the protected surface."""

    config_overrides = {"auth_mode": "token", "auth_token": R2_TOKEN}

    def test_health_like_headers_cannot_bypass(self) -> None:
        for headers in (
            {"User-Agent": "Docker-Healthcheck/1.0"},
            {"X-Healthcheck": "true"},
            {"X-Kubernetes-Health-Probe": "liveness"},
            {"User-Agent": "kube-probe/1.29"},
            {"Healthcheck": "true"},
            {"X-Forwarded-For": "127.0.0.1"},
        ):
            with self.subTest(headers=headers):
                status, env, _ = self.get(
                    _PROTECTED + "?healthcheck=true", headers=headers)
                self.assertEqual(status, 401, repr(headers))
                self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")

    def test_health_like_query_parameters_cannot_bypass(self) -> None:
        for query in ("?healthcheck=true", "?probe=1", "?healthz",
                      "?skip_auth=true", "?auth=none"):
            with self.subTest(query=query):
                status, _, _ = self.get(_PROTECTED + query)
                self.assertEqual(status, 401)

    def test_localhost_source_cannot_bypass(self) -> None:
        # every request in this harness ALREADY originates from loopback;
        # proving the point: loopback + missing credential => 401
        status, env, _ = self.get(_PROTECTED)
        self.assertEqual(status, 401)

    def test_host_header_cannot_bypass(self) -> None:
        status, _, _ = self.get(
            _PROTECTED, headers={"Host": "healthz.internal"})
        self.assertEqual(status, 401)

    def test_request_id_cannot_reclassify_a_protected_path(self) -> None:
        status, env, _ = self.get(
            _PROTECTED, headers={"X-Request-Id": "healthz"})
        self.assertEqual(status, 401)
        self.assertNotEqual(env["kind"], "healthz")

    def test_traversal_paths_never_cross_the_boundary(self) -> None:
        # traversal stays UNROUTED (400 transport) — no alternate path
        # representation can map a protected endpoint onto the probe or
        # vice versa; both directions are asserted
        for path in ("/healthz/../v1/health", "/v1/health/../healthz",
                     "/healthz/%2e%2e/v1/health",
                     "/%68ealthz/../v1/health"):
            with self.subTest(path=path):
                status, env, _ = self.get(path)
                self.assertEqual(status, 400, path)
                self.assertEqual(env["kind"], "transport", path)


class TestPathNormalization(RuntimeHarness):
    """§13 — /healthz matching is deterministic; no representation can
    classify a PROTECTED endpoint as a probe (or the reverse)."""

    config_overrides = {"auth_mode": "token", "auth_token": R2_TOKEN}

    def test_healthz_variants_serve_liveness(self) -> None:
        for path in ("/healthz", "/healthz/", "//healthz", "/%68ealthz",
                     "/healthz%2f", "/healthz%2F", "/healthz?probe=x"):
            with self.subTest(path=path):
                status, env, headers = self.get(path)
                self.assertEqual(status, 200, path)
                self.assertEqual(env["kind"], "healthz", path)
                self.assertEqual(env["status"], "alive", path)
                self.assertNotIn("principal_id", env)

    def test_uppercase_and_children_are_not_probes(self) -> None:
        # ("/ healthz" is refused by every well-behaved HTTP client; its
        # routing level is covered by
        # TestProbeSurfaceClassification.test_space_prefixed_routes_nowhere)
        for path in ("/HEALTHZ", "/Healthz", "/healthzextra",
                     "/x/healthz", "/healthz/x"):
            with self.subTest(path=path):
                status, env, _ = self.get(path)
                self.assertEqual(status, 400, path)
                self.assertEqual(env["kind"], "transport", path)

    def test_encoded_protected_stays_protected(self) -> None:
        # percent-decoding happens BEFORE routing, so /%76%31/health IS
        # /v1/health — still authenticated, never liveness; case variants
        # of protected paths route nowhere (case-sensitive routes)
        status, env, _ = self.get("/%76%31/health")
        self.assertEqual(status, 401)
        self.assertEqual(env["kind"], "transport")
        for path in ("/V1/HEALTH", "/v1/HEALTH"):
            with self.subTest(path=path):
                status, env, _ = self.get(path)
                self.assertEqual(status, 400, path)

    def test_readyz_normalization_stays_readyz(self) -> None:
        status, env, _ = self.get("/readyz/")
        self.assertEqual(status, 401)   # readiness remains authenticated
        self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
        status, env, _ = self.get(
            "/readyz", headers={"Authorization": "Bearer " + R2_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(env["kind"], "health")

    def test_percent_encoded_healthz_is_only_ever_liveness(self) -> None:
        # every representation that normalises into the probe serves the
        # probe envelope — none can serve protected behaviour
        for path in ("/%68ealthz", "/health%7a", "/healthz/"):
            status, env, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(env["kind"], "healthz", path)


class TestMethodBehaviour(RuntimeHarness):
    """§11D — unexpected methods never reach application functionality."""

    config_overrides = {"auth_mode": "token", "auth_token": R2_TOKEN}

    def test_non_get_on_probes_is_405(self) -> None:
        for path in (_HEALTHZ, _READYZ):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(path=path, method=method):
                    status, env = self.send(method, path)
                    self.assertEqual(status, 405)
                    self.assertEqual(env["kind"], "transport")
                    self.assertEqual(env["error"]["code"], "INVALID_REQUEST")

    def test_head_mirrors_get_on_probes_empty_body(self) -> None:
        import urllib.request

        request = urllib.request.Request(self.base + _HEALTHZ, method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertEqual(response.headers["Content-Length"],
                             str(len(json.dumps(
                                 {"schema_version": SCHEMA_VERSION,
                                  "kind": "healthz",
                                  "request_id": "x" * 32,
                                  "status": "alive"},
                                 ensure_ascii=False).encode("utf-8"))))


class TestRequestIDContract(RuntimeHarness):
    """§18 — request-id coherence across auth failures and probes."""

    config_overrides = {"auth_mode": "token", "auth_token": R2_TOKEN}

    def test_probe_echoes_supplied_request_id(self) -> None:
        status, env, headers = self.get(
            _HEALTHZ, headers={"X-Request-Id": "req-r2-probe-1"})
        self.assertEqual(status, 200)
        self.assertEqual(env["request_id"], "req-r2-probe-1")
        self.assertEqual(headers["X-Request-Id"], "req-r2-probe-1")

    def test_auth_failure_echoes_request_id(self) -> None:
        status, env, headers = self.get(
            _PROTECTED, headers={"X-Request-Id": "req-r2-auth-fail"})
        self.assertEqual(status, 401)
        self.assertEqual(env["request_id"], "req-r2-auth-fail")
        self.assertEqual(headers["X-Request-Id"], "req-r2-auth-fail")

    def test_invalid_request_id_characters_sanitised(self) -> None:
        status, env, headers = self.get(
            _HEALTHZ, headers={"X-Request-Id": "bad/../id"})
        self.assertEqual(status, 200)
        self.assertNotIn("/", env["request_id"])
        self.assertNotIn("..", env["request_id"])

    def test_probes_generate_uuid_request_ids(self) -> None:
        _, env1, _ = self.get(_HEALTHZ)
        _, env2, _ = self.get(_HEALTHZ)
        self.assertNotEqual(env1["request_id"], env2["request_id"])
        self.assertEqual(len(env1["request_id"]), 32)

    def test_diagnostic_blob_never_contains_synthetic_credential(self) -> None:
        self.clear_records()
        self.get(_PROTECTED)                                    # missing
        self.get(_PROTECTED, headers={
            "Authorization": "Bearer " + _OTHER_TOKEN})
        self.get(_HEALTHZ)
        self.get(_READYZ, headers={
            "Authorization": "Bearer " + _OTHER_TOKEN})
        records = self.records()
        self.assertTrue(records)
        blob = _blob(records)
        self.assertNotIn(R2_TOKEN, blob)
        self.assertNotIn(_OTHER_TOKEN, blob)
        self.assertNotIn("Authorization", blob)


class TestCredentialComparisonSafety(unittest.TestCase):
    """§16 — constant-time credential comparison, reviewed + pinned."""

    def test_comparison_uses_hmac_compare_digest(self) -> None:
        import hmac

        from phase3.transport.auth import BearerTokenAuthenticator

        calls: list[tuple[bytes, bytes]] = []
        real = hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real(a, b)

        with unittest.mock.patch(
                "phase3.transport.auth.hmac.compare_digest", spy):
            auth = BearerTokenAuthenticator(R2_TOKEN)
            ctx = auth.authenticate(
                {"Authorization": "Bearer " + R2_TOKEN})
        self.assertEqual(len(calls), 1)
        supplied, configured = calls[0]
        self.assertIsInstance(supplied, bytes)
        self.assertIsInstance(configured, bytes)
        self.assertEqual(configured, R2_TOKEN.encode("utf-8"))

    def test_no_plain_equality_in_authenticator(self) -> None:
        # the ONLY equality mechanism on the credential is the
        # constant-time comparator — never "==" on the token value
        import inspect

        from phase3.transport import auth

        source = inspect.getsource(auth.BearerTokenAuthenticator)
        self.assertIn("compare_digest", source)
        self.assertNotIn("supplied == self._token", source)


class TestCredentialSourceSafety(unittest.TestCase):
    """§17 — credentials come from the environment; never the image,
    the repo or any health surface."""

    def test_dockerfile_healthcheck_contains_no_credential(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("HEALTHCHECK", dockerfile)
        for forbidden in ("FIE_AUTH_TOKEN", "Authorization",
                          "Bearer ", "R2_SYNTHETIC"):
            self.assertNotIn(forbidden, dockerfile)

    def test_synthetic_credential_absent_from_all_health_responses(self) -> None:
        from phase3.transport.config import load_transport_config
        from phase3.transport.http import FIEReferenceRuntime, make_http_server
        from tests.phase3.service import helpers

        bundle = helpers.bootstrap_state(None)
        try:
            config = load_transport_config(
                bundle["path"], auth_mode="token", auth_token=R2_TOKEN)
            runtime = FIEReferenceRuntime.open(bundle["path"])
            server = make_http_server(runtime, config, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address[0], server.server_address[1]

            conn = http.client.HTTPConnection(f"{host}:{port}")
            for path in (_HEALTHZ, _READYZ, _READYZ):
                conn.request(
                    "GET", path,
                    headers={"Authorization": "Bearer " + _OTHER_TOKEN})
                response = conn.getresponse()
                body = response.read()
                blob = body.decode("utf-8", "replace")
                self.assertNotIn(R2_TOKEN, blob)
                self.assertNotIn(_OTHER_TOKEN, blob)
            conn.close()
            server.shutdown()
            server.server_close()
            runtime.close()
        finally:
            bundle["store"].close()
            Path(bundle["path"]).unlink(missing_ok=True)


class TestReadinessFailureSemantics(unittest.TestCase):
    """§15 — liveness and readiness remain conceptually separate: an
    unreadable persistence layer degrades readiness, never liveness."""

    def test_healthz_alive_while_readiness_dependency_unavailable(self) -> None:
        import os
        import sqlite3
        import tempfile

        from phase3.transport.config import load_transport_config
        from phase3.transport.http import FIEReferenceRuntime, make_http_server
        from tests.phase3.service import helpers

        bundle = helpers.bootstrap_state(None)
        try:
            handle, db_path = tempfile.mkstemp(
                prefix="fie_r2_unmig_", suffix=".db")
            os.close(handle)
            sqlite3.connect(db_path).close()
            try:
                config = load_transport_config(
                    db_path, auth_mode="token", auth_token=R2_TOKEN)
                runtime = FIEReferenceRuntime.open(db_path)
                server = make_http_server(runtime, config, port=0)
                thread = threading.Thread(
                    target=server.serve_forever, daemon=True)
                thread.start()
                host, port = server.server_address[0], server.server_address[1]
                base = f"http://{host}:{port}"

                # liveness stays 200 on the broken-persistence server
                status, env = _http_get(base, _HEALTHZ)
                self.assertEqual(status, 200)
                self.assertEqual(env["status"], "alive")
                # readiness reports dependency failure, sanitized
                status, env = _http_get(
                    base, _READYZ,
                    headers={"Authorization": "Bearer " + R2_TOKEN})
                self.assertEqual(status, 503)
                self.assertEqual(env["error"]["code"],
                                 "DEPENDENCY_UNAVAILABLE")
                blob = json.dumps(env, ensure_ascii=False)
                for marker in ("no such table", "OperationalError", "sqlite",
                               "/tmp/", R2_TOKEN):
                    self.assertNotIn(marker, blob)
                # process remains alive and serving after the failure
                status, env = _http_get(base, _HEALTHZ)
                self.assertEqual(status, 200)

                server.shutdown()
                server.server_close()
                runtime.close()
            finally:
                Path(db_path).unlink(missing_ok=True)
        finally:
            bundle["store"].close()
            Path(bundle["path"]).unlink(missing_ok=True)


class TestProbeSurfaceClassification(unittest.TestCase):
    """§6/§13 hard invariants on the classification itself."""

    def test_probe_set_is_exactly_liveness(self) -> None:
        self.assertEqual(
            OPERATIONAL_PROBE_OPERATIONS, frozenset({"liveness"}))

    def test_protected_operations_can_never_be_probes(self) -> None:
        known = {
            "/healthz": "liveness",
            "/readyz": "readiness",
            "/v1/health": "health",
            "/v1/intelligence/latest": "latest_intelligence",
            "/v1/intelligence/entity/company/2330": "entity_intelligence",
            "/v1/evidence/entity%3Acompany%3A2330": "evidence",
            "/v1/freshness/company/2330": "freshness",
        }
        ops = set()
        for path, op in known.items():
            routed = _route(path.split("?")[0])
            self.assertIsNotNone(routed, path)
            self.assertEqual(routed[0], op, path)
            ops.add(op)
        # ONLY liveness is a probe; every protected operation can NEVER
        # join the frozen set (pinned to exactly {"liveness"} above)
        protected = ops - {"liveness"}
        self.assertEqual(protected & OPERATIONAL_PROBE_OPERATIONS, set())

    def test_no_path_form_reclassifies_protected_as_probe(self) -> None:
        for path in ("/HEALTHZ", "/healthz/x", "/x/healthz", "/health",
                     "/healthz/../v1/health", "/v1/health/../healthz",
                     "/v1/HEALTH"):
            routed = _route(path)
            # no such form may produce the PROTECTED health/readiness ops
            # under a probe classification or vice versa:
            if routed is not None and routed != ("invalid", {}):
                op = routed[0]
                with self.subTest(path=path, op=op):
                    # the only routed forms are exact families; none of
                    # them is a hybrid (e.g. liveness with captured args)
                    self.assertEqual(routed[1], {})
                    if op == "liveness":
                        self.assertIn("healthz", path.lower())
                    else:
                        self.assertNotIn("healthz", path.lower())

    def test_space_prefixed_routes_nowhere(self) -> None:
        # client-side refused forms still route to NOTHING at the server
        # edge — they can never be classified as a probe
        self.assertIsNone(_route("/ healthz"))
        self.assertIsNone(_route("/healthz%00"))
        self.assertIsNone(_route("/healthz/%00"))


if __name__ == "__main__":
    unittest.main()