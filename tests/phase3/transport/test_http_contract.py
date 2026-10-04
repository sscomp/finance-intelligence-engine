"""SQLite reference-runtime gate (Phase 6.6 §14) + HTTP contract (§5).

Exercises ALL public operations over real HTTP (urllib against an
in-process server on an ephemeral loopback port) with schemas,
errors, freshness metadata, telemetry and clean teardown.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from phase3.service import SCHEMA_VERSION  # noqa: E402
from tests.phase3.transport.runtime_harness import RuntimeHarness  # noqa: E402


def _assert_envelope_common(testcase, env: dict, kind: str) -> None:
    testcase.assertEqual(env["schema_version"], SCHEMA_VERSION)
    testcase.assertEqual(env["kind"], kind)
    blob = json.dumps(env, ensure_ascii=False)
    for marker in (
        "SELECT ", "INSERT INTO", "signal_log", "score_snapshot",
        "graph_nodes", "postgres://", "postgresql://", "sqlite:", "/tmp/",
        ".venv",
    ):
        testcase.assertNotIn(marker, blob)


class TestHealthEndpoints(RuntimeHarness):
    def test_liveness_does_not_touch_persistence(self) -> None:
        status, env, headers = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(env["status"], "alive")
        self.assertEqual(headers["X-Request-Id"], env["request_id"])
        self.assertNotIn("backend_kind", env)

    def test_readiness_maps_to_get_health(self) -> None:
        status, env, _ = self.get("/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["payload"]["counts"]["scores"], 4)
        self.assertEqual(env["payload"]["backend_kind"], "sqlite")

    def test_versioned_health_endpoint(self) -> None:
        status, env, _ = self.get("/v1/health")
        self.assertEqual(status, 200)
        _assert_envelope_common(self, env, "health")
        self.assertEqual(env["payload"]["schema_current_version"], 1)


class TestIntelligenceEndpoints(RuntimeHarness):
    def test_latest_intelligence(self) -> None:
        status, env, _ = self.get("/v1/intelligence/latest?limit=10")
        self.assertEqual(status, 200)
        _assert_envelope_common(self, env, "latest_intelligence")
        self.assertEqual(
            [(i["kind"], i["entity_id"]) for i in env["payload"]],
            [("company", "2330"), ("company", "1101"),
             ("industry", "半導體"), ("macro", "global")],
        )
        for item in env["payload"]:
            self.assertIn(item["freshness"]["state"],
                          ("FRESH", "DEGRADED", "STALE", "UNAVAILABLE"))
        # consolidated provenance refs are followable evidence refs
        self.assertGreaterEqual(len(env["evidence_refs"]), 4)

    def test_latest_kind_filter(self) -> None:
        status, env, _ = self.get("/v1/intelligence/latest?kind=company")
        self.assertEqual(status, 200)
        self.assertEqual(
            [i["entity_id"] for i in env["payload"]], ["2330", "1101"]
        )

    def test_entity_intelligence(self) -> None:
        # as_of pinned explicitly: the default (absent) as_of resolves to
        # the wall clock "today"; pinning that day made this test date-
        # fragile (it silently depended on the fixture being authored the
        # same day). Phase 6.7B-R4: same assertions, clock-independent.
        status, env, _ = self.get(
            "/v1/intelligence/entity/company/2330?as_of=2026-10-03"
        )
        self.assertEqual(status, 200)
        _assert_envelope_common(self, env, "entity_intelligence")
        payload = env["payload"]
        self.assertIsInstance(payload["score"], float)
        self.assertGreaterEqual(len(payload["signals"]), 1)
        # freshness metadata retained at envelope level (§8)
        self.assertEqual(env["freshness"]["state"], "STALE")
        self.assertEqual(env["freshness"]["as_of"], "2026-10-03")

    def test_entity_as_of_query_param(self) -> None:
        status, env, _ = self.get(
            "/v1/intelligence/entity/company/2330?as_of=2026-07-09"
        )
        self.assertEqual(status, 200)
        self.assertEqual(env["freshness"]["state"], "FRESH")


class TestEvidenceEndpoint(RuntimeHarness):
    def test_evidence_url_encoded_ref(self) -> None:
        status, env, _ = self.get("/v1/evidence/entity%3Acompany%3A2330")
        self.assertEqual(status, 200)
        _assert_envelope_common(self, env, "evidence")
        payload = env["payload"]
        self.assertEqual(payload["ref"], "entity:company:2330")
        self.assertGreaterEqual(len(payload["related"]), 1)

    def test_evidence_limit_param(self) -> None:
        _, env, _ = self.get("/v1/evidence/score%3Acompany%3A2330?limit=1")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(len(env["payload"]["related"]), 1)

    def test_unknown_ref_404(self) -> None:
        status, env, _ = self.get("/v1/evidence/entity%3Acompany%3A9999")
        self.assertEqual(status, 404)
        self.assertEqual(env["error"]["code"], "NOT_FOUND")
        self.assertIsNone(env["payload"])


class TestFreshnessEndpoint(RuntimeHarness):
    def test_freshness_states_over_http(self) -> None:
        for as_of, expected in (
            ("2026-07-09", "FRESH"),
            ("2026-08-20", "DEGRADED"),
            ("2026-10-03", "STALE"),
        ):
            with self.subTest(as_of=as_of):
                status, env, _ = self.get(
                    f"/v1/freshness/company/2330?as_of={as_of}"
                )
                self.assertEqual(status, 200)
                self.assertEqual(env["payload"]["state"], expected)
                self.assertEqual(env["freshness"]["state"], expected)

    def test_unavailable_entity(self) -> None:
        status, env, _ = self.get("/v1/freshness/company/9999")
        self.assertEqual(status, 200)
        self.assertEqual(env["payload"]["state"], "UNAVAILABLE")


class TestErrorMapping(RuntimeHarness):
    def test_bad_request(self) -> None:
        status, env, _ = self.get("/v1/intelligence/latest?limit=zz")
        self.assertEqual(status, 400)
        self.assertEqual(env["error"]["code"], "INVALID_REQUEST")

    def test_unknown_kind(self) -> None:
        status, env, _ = self.get("/v1/freshness/crypto/ABC")
        self.assertEqual(status, 400)
        self.assertEqual(env["error"]["code"], "INVALID_REQUEST")

    def test_unknown_route(self) -> None:
        status, env, _ = self.get("/v1/nope")
        self.assertEqual(status, 400)
        self.assertEqual(env["error"]["code"], "INVALID_REQUEST")

    def test_no_stack_trace_in_errors(self) -> None:
        for path in ("/v1/evidence/bogus", "/v1/intelligence/latest?limit=x"):
            with self.subTest(path=path):
                _, env, _ = self.get(path)
                self.assertEqual(env["status"], "error")
                self.assertNotIn("Traceback", json.dumps(env))
                self.assertNotIn(".py", json.dumps(env.get("error", {})))


class TestReadOnlyTransport(RuntimeHarness):
    def test_non_get_rejected_405(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, env = self.send(method, "/v1/health")
                self.assertEqual(status, 405)
                self.assertEqual(env["error"]["code"], "INVALID_REQUEST")

    def test_no_mutation_endpoints(self) -> None:
        # every registered route is a GET read (§3.5)
        from phase3.transport.http import _route

        for path in (
            "/v1/health", "/healthz", "/readyz",
            "/v1/intelligence/latest",
            "/v1/intelligence/entity/company/2330",
            "/v1/evidence/entity:company:2330",
            "/v1/freshness/company/2330",
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(_route(path))


class TestTelemetry(RuntimeHarness):
    def test_structured_http_records(self) -> None:
        self.clear_records()
        status, env, _ = self.get("/v1/freshness/company/2330?as_of=2026-10-03")
        self.assertEqual(status, 200)
        [record] = [
            r for r in self.records()
            if r.get("event") == "http_request" and r.get("operation") == "freshness"
        ]
        self.assertEqual(record["event"], "http_request")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["freshness_state"], "STALE")
        self.assertEqual(record["as_of"], "2026-10-03")
        self.assertIsInstance(record["duration_ms"], (int, float))
        # §11 forbidden content never appears in telemetry
        blob = json.dumps(self.records())
        self.assertNotIn("payload", blob)
        self.assertNotIn("Authorization", blob)
        self.assertNotIn("?as_of", blob)


class TestRequestId(RuntimeHarness):
    def test_client_request_id_is_echoed(self) -> None:
        _, env, headers = self.get("/v1/health", headers={"X-Request-Id": "req-cli-42"})
        self.assertEqual(headers.get("X-Request-Id"), "req-cli-42")
        self.assertEqual(env["request_id"], "req-cli-42")

    def test_request_id_generated_when_absent(self) -> None:
        _, env, headers = self.get("/v1/health")
        self.assertEqual(headers.get("X-Request-Id"), env["request_id"])
        self.assertTrue(env["request_id"])

    def test_request_id_sanitized(self) -> None:
        _, env, headers = self.get(
            "/v1/health", headers={"X-Request-Id": "../etc/passwd drop;"}
        )
        self.assertNotIn("..", env["request_id"])
        self.assertNotIn("/", env["request_id"])
        self.assertNotIn(";", env["request_id"])


if __name__ == "__main__":
    unittest.main()