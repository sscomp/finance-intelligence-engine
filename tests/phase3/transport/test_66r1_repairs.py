"""Phase 6.6R1 transport-contract repair regression tests.

Covers the three 6.6R closure-gate findings:

* DEFECT-A — internal exception / persistence implementation detail
  must never appear in external error envelopes (§5.A);
* DEFECT-B — every JSON route family emits the contract-declared
  ``Content-Type: application/json; charset=utf-8`` on success AND
  error responses (§5.B);
* request-ID consistency — ``X-Request-Id`` header equals
  ``response_body.request_id`` on every transport-produced envelope,
  for both client-supplied and server-generated IDs (§5.C).

Error-path coverage runs against an *un-migrated* empty SQLite file
(the exact operator-misconfiguration scenario that reproduced the
Phase 6.6R leaks): every path maps to stable typed codes with the
generic safe message and no internal detail.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

from tests.phase3.transport.runtime_harness import RuntimeHarness  # noqa: E402

_JSON_CT = "application/json; charset=utf-8"

# §5.A forbidden markers — internal persistence/exception detail.
# Phase 6.7B-R4 (ADR-019 §1): the lowercase dependency FAMILY word
# ("sqlite"/"postgres" as details.dependency) is now contractual — the
# forbidden shapes are the driver/class/path/topology detail.
_LEAK_MARKERS = (
    "SQLite", "OperationalError", "no such table",
    "no such column", "score_snapshot", "signal_log", "traceback",
    "Traceback", "/tmp/", ".db",
)


def _build_server(db_path: str, authenticator=None):
    from phase3.transport import load_transport_config
    from phase3.transport.http import FIEReferenceRuntime, make_http_server

    runtime = FIEReferenceRuntime.open(db_path)
    config = load_transport_config(db_path)
    server = make_http_server(
        runtime, config, host="127.0.0.1", port=0, authenticator=authenticator
    )
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return runtime, f"http://{host}:{port}", server, thread


def _full(method: str, url: str, headers: dict[str, str] | None = None):
    """Full request: returns (status, envelope, headers) incl. on errors."""
    import urllib.error
    import urllib.request

    data = None if method in ("GET", "HEAD") else b"{}"
    request = urllib.request.Request(
        url, data=data, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return (
                response.status,
                json.loads(response.read()),
                dict(response.headers),
            )
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), dict(exc.headers)


class _ClientMixin:
    def get(self, path: str, *, headers: dict[str, str] | None = None):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return (
                    response.status,
                    json.loads(response.read()),
                    dict(response.headers),
                )
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)

    def request(self, method: str, path: str):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.base + path, data=b"{}" if method != "GET" else None,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return (
                    response.status,
                    json.loads(response.read()),
                    dict(response.headers),
                )
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)

    def _assert_no_leak(self, env: dict) -> None:
        blob = json.dumps(env, ensure_ascii=False)
        for marker in _LEAK_MARKERS:
            self.assertNotIn(marker, blob)


class _UnmigratedDBBase(_ClientMixin, unittest.TestCase):
    """Base: runtime opened over a deliberately un-migrated empty .db.

    Phase 6.7B-R4 repair: the ``unittest.TestCase`` base was MISSING
    since this file's creation — these five tests were silently invisible
    to the canonical unittest-discovery runner (pytest picked them up
    and they failed there). Revived and aligned with the current
    accepted contracts (R3 schema gate, R4 ADR-019 §1 redaction).
    """

    @classmethod
    def setUpClass(cls) -> None:
        handle, cls.db_path = tempfile.mkstemp(
            prefix="fie_66r1_unmigrated_", suffix=".db"
        )
        os.close(handle)
        sqlite3.connect(cls.db_path).close()  # empty file, no migrations
        cls.runtime, cls.base, cls.server, cls.thread = _build_server(
            cls.db_path, authenticator=None
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.runtime.close()
        if Path(cls.db_path).exists():
            os.unlink(cls.db_path)


class TestErrorSanitizationUnmigratedDB(_UnmigratedDBBase):
    """§5.A — DEFECT-A regression over the operator-misconfig path."""

    LEAKY_ROUTES = (
        ("/readyz", 503, "DEPENDENCY_UNAVAILABLE"),
        ("/v1/health", 503, "DEPENDENCY_UNAVAILABLE"),
        ("/v1/intelligence/latest?limit=10", 500, "INTERNAL_ERROR"),
        ("/v1/intelligence/entity/company/2330", 500, "INTERNAL_ERROR"),
        ("/v1/freshness/company/2330", 503, "DEPENDENCY_UNAVAILABLE"),
    )

    def test_error_paths_are_typed_and_sanitized(self) -> None:
        for path, status, code in self.LEAKY_ROUTES:
            with self.subTest(path=path):
                got_status, env, headers = self.get(path)
                self.assertEqual(got_status, status)
                self.assertEqual(env["status"], "error")
                self.assertEqual(env["error"]["code"], code)
                self.assertTrue(env.get("request_id"))
                self.assertEqual(headers["Content-Type"], _JSON_CT)
                self._assert_no_leak(env)

    def test_latest_intelligence_carries_generic_safe_message(self) -> None:
        _, env, _ = self.get("/v1/intelligence/latest?limit=10")
        self.assertEqual(env["error"]["code"], "INTERNAL_ERROR")
        self.assertEqual(env["error"]["message"], "internal service failure")

    def test_health_reason_sanitized(self) -> None:
        # Phase 6.7B-R4 (ADR-019 §1): the readiness outage envelope
        # carries ONLY the stable family classification — the sanitized
        # driver reason moved to server-side logs exclusively.
        _, env, _ = self.get("/v1/health")
        self.assertEqual(env["error"]["code"], "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(env["error"]["details"], {"dependency": "sqlite"})
        self._assert_no_leak(env)

    def test_envelope_shape_stays_contractual(self) -> None:
        _, env, _ = self.get("/v1/intelligence/latest?limit=10")
        self.assertEqual(env["schema_version"], "6.5")
        self.assertEqual(env["kind"], "latest_intelligence")
        self.assertIsNone(env["payload"])


class TestCatchallTransportFailure500(_UnmigratedDBBase):
    """The transport catch-all must also honor request-ID + Content-Type."""

    class _ExplodingAuthenticator:
        # a *generic* Exception (not TransportError) must escape the
        # TransportError-only auth path into the do_GET catch-all
        mode = "token"
        principal_id = ""

        def authenticate(self, headers) -> None:  # noqa: ANN001
            raise RuntimeError("boom internal secret detail")

    def test_catchall_500_is_json_with_matching_request_id(self) -> None:
        runtime, base, server, thread = _build_server(
            self.db_path, authenticator=self._ExplodingAuthenticator()
        )
        try:
            status, env, headers = _full("GET", base + "/v1/health")
            self.assertEqual(status, 500)
            self.assertEqual(env["error"]["code"], "INTERNAL_ERROR")
            self.assertEqual(env["error"]["message"],
                             "internal transport failure")
            self.assertEqual(headers["Content-Type"], _JSON_CT)
            self.assertEqual(headers["X-Request-Id"], env["request_id"])
            self.assertTrue(env["request_id"])
            self.assertNotIn("boom", json.dumps(env))
        finally:
            server.shutdown()
            server.server_close()
            runtime.close()


class TestJsonContentTypeAllRouteFamilies(RuntimeHarness, _ClientMixin):
    """§5.B — DEFECT-B regression: Content-Type on 200/400/404/405."""

    def _check(self, method: str, path: str, status: int, code: str) -> None:
        got, env, headers = self.request(method, path)
        self.assertEqual(got, status)
        self.assertEqual(headers["Content-Type"], _JSON_CT)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], code)

    def test_success_routes_200(self) -> None:
        for path in (
            "/healthz",
            "/readyz",
            "/v1/health",
            "/v1/intelligence/latest?limit=10",
            "/v1/intelligence/entity/company/2330",
            "/v1/freshness/company/2330",
        ):
            with self.subTest(path=path):
                status, env, headers = self.get(path)
                self.assertEqual(status, 200)
                # /healthz says "alive", the readiness/health family "ok"
                self.assertIn(env["status"], ("ok", "alive"))
                self.assertEqual(headers["Content-Type"], _JSON_CT)

    def test_invalid_request_400(self) -> None:
        self._check("GET", "/v1/intelligence/entity/region/2330", 400,
                    "INVALID_REQUEST")

    def test_no_such_route_400(self) -> None:
        self._check("GET", "/v1/nope", 400, "INVALID_REQUEST")

    def test_not_found_404(self) -> None:
        self._check("GET", "/v1/evidence/nonexistent" + uuid.uuid4().hex,
                    404, "NOT_FOUND")

    def test_method_not_allowed_405(self) -> None:
        self._check("POST", "/healthz", 405, "INVALID_REQUEST")

    def test_head_has_content_type(self) -> None:
        import urllib.request

        request = urllib.request.Request(self.base + "/healthz",
                                         method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.headers["Content-Type"], _JSON_CT)


class TestRequestIDConsistency(RuntimeHarness, _ClientMixin):
    """§5.C — header == body.request_id for supplied and generated IDs."""

    def test_client_supplied_request_id(self) -> None:
        status, env, headers = self.get(
            "/v1/intelligence/latest?limit=2",
            headers={"X-Request-Id": "req-cli-99"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(env["request_id"], "req-cli-99")
        self.assertEqual(headers["X-Request-Id"], env["request_id"])

    def test_generated_request_id(self) -> None:
        status, env, headers = self.get("/v1/intelligence/latest?limit=2")
        self.assertEqual(status, 200)
        rid = env["request_id"]
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", rid))
        self.assertEqual(headers["X-Request-Id"], rid)

    def test_error_envelope_400(self) -> None:
        status, env, headers = self.get("/v1/nope")
        self.assertEqual(status, 400)
        self.assertEqual(headers["X-Request-Id"], env["request_id"])

    def test_error_envelope_405(self) -> None:
        status, env, headers = self.request("POST", "/healthz")
        self.assertEqual(status, 405)
        self.assertEqual(headers["X-Request-Id"], env["request_id"])
        self.assertEqual(env["principal_id"], "")


if __name__ == "__main__":
    unittest.main()