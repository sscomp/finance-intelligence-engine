"""Service-boundary contract tests (Phase 6.5 §17 areas 1-7).

Every case runs the **in-process reference adapter** (``dispatch``)
against a scored disposable SQLite database built by the production
paths — validating the shape clients will see:

1. health/readiness response contract;
2. latest intelligence contract (per-entity newest + freshness/warnings);
3. entity lookup contract (score/payload/signals/evidence/warnings);
4. evidence retrieval contract (exact + date-suffix fallback + cap);
5. freshness state mapping (FRESH/DEGRADED/STALE/UNAVAILABLE);
6. error taxonomy paths (INVALID_REQUEST / NOT_FOUND / INTERNAL);
7. synthetic principal/request context propagation into envelopes.

No raw persistence vocabulary appears in any envelope (§5.1) — also
asserted here so contract regressions cannot smuggle DSNs/SQL in.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service import (  # noqa: E402
    OPERATIONS,
    SCHEMA_VERSION,
    RequestContext,
    ServiceError,
    ServiceErrorCode,
    dispatch,
)
from phase3.service.freshness import SERVICE_FRESHNESS_STATES  # noqa: E402
from phase3.service.timeutil import utc_now_iso  # noqa: E402
from tests.phase3.service import helpers  # noqa: E402


class _ScoredCase(unittest.TestCase):
    """One scored disposable DB + read-only service per test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = helpers.bootstrap_state()
        cls.service = cls.bundle["service"]

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.bundle["store"].close()
        finally:
            path = cls.bundle["path"]
            if Path(path).exists():
                Path(path).unlink()

    def dispatch(self, op: str, params: dict | None = None):
        return dispatch(self.service, op, params or {}, CTX)


CTX = RequestContext(principal_id="synthetic-alpha", request_id="req-001")


def _assert_no_persistence_leakage(testcase: unittest.TestCase, envelope: dict) -> None:
    blob = json.dumps(envelope, ensure_ascii=False)
    for marker in (
        "SELECT ", "INSERT INTO", "graph_nodes", "score_snapshot",
        "signal_log ", "sqlite3.", "postgresql://", "postgres://",
        "sqlite://", "<dataclasses.",
    ):
        testcase.assertNotIn(marker, blob, f"leak marker {marker!r} in envelope")


class TestHealthContract(_ScoredCase):
    def test_contract_shape(self) -> None:
        env = self.dispatch("health")
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["schema_version"], SCHEMA_VERSION)
        self.assertEqual(env["kind"], "health")
        payload = env["payload"]
        self.assertEqual(payload["status"], "ok")
        self.assertIn(payload["backend_kind"], ("sqlite", "postgres"))
        self.assertGreaterEqual(payload["counts"]["scores"], 4)
        self.assertGreaterEqual(payload["counts"]["signals"], 3)
        self.assertEqual(payload["schema_current_version"], 1)
        self.assertRegex(payload["checked_at"], r"^\d{4}-\d{2}-\d{2}T")
        _assert_no_persistence_leakage(self, env)

    def test_protocol_signature(self) -> None:
        from phase3.service.boundary import IntelligenceService

        self.assertTrue(callable(getattr(self.service, "get_health")))
        ops = (
            "get_health", "get_latest_intelligence", "get_entity_intelligence",
            "get_evidence", "get_freshness",
        )
        for name in ops:
            self.assertTrue(hasattr(IntelligenceService, name), name)
            self.assertTrue(callable(getattr(self.service, name)))


class TestLatestIntelligenceContract(_ScoredCase):
    def test_per_entity_newest_summaries(self) -> None:
        env = self.dispatch("latest_intelligence", {"limit": 10})
        self.assertEqual(env["status"], "ok")
        items = env["payload"]
        keys = [(i["kind"], i["entity_id"]) for i in items]
        self.assertEqual(
            keys,
            [("company", "2330"), ("company", "1101"),
             ("industry", "半導體"), ("macro", "global")],
        )
        for item in items:
            self.assertIsInstance(item["score"], float)
            self.assertTrue(len(item["evidence_refs"]) >= 1)
            self.assertIn("state", item["freshness"])
            self.assertIn(item["freshness"]["state"], SERVICE_FRESHNESS_STATES)
            self.assertLessEqual(len(item["warnings"]), 1)
            self.assertIn(item["warnings"][0]
                          if item["warnings"] else "", {""} | _warnings())
        _assert_no_persistence_leakage(self, env)

    def test_kind_filter(self) -> None:
        env = self.dispatch("latest_intelligence", {"kind": "company"})
        self.assertEqual(
            [(i["kind"], i["entity_id"]) for i in env["payload"]],
            [("company", "2330"), ("company", "1101")],
        )

    def test_limit_truncates(self) -> None:
        env = self.dispatch("latest_intelligence", {"limit": 2})
        self.assertEqual(len(env["payload"]), 2)


def _warnings() -> set:
    return {
        "upstream observation older than freshness window",
        "observation past hard freshness window",
        "no upstream observation available",
    }


class TestEntityIntelligenceContract(_ScoredCase):
    def test_company_full_payload(self) -> None:
        env = self.dispatch(
            "entity_intelligence", {"kind": "company", "entity_id": "2330"}
        )
        self.assertEqual(env["status"], "ok")
        payload = env["payload"]
        self.assertEqual(payload["kind"], "company")
        self.assertEqual(payload["entity_id"], "2330")
        self.assertIsInstance(payload["score"], float)
        self.assertTrue(payload["payload"])  # breakdown JSON present
        self.assertGreaterEqual(len(payload["signals"]), 1)
        self.assertRegex(payload["computed_at"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertIn("state", env["freshness"])
        _assert_no_persistence_leakage(self, env)

    def test_unknown_entity_snapshot_warning(self) -> None:
        env = self.dispatch(
            "entity_intelligence", {"kind": "company", "entity_id": "9999"}
        )
        self.assertEqual(env["status"], "ok")
        self.assertIsNone(env["payload"]["score"])
        self.assertEqual(env["payload"]["signals"], [])
        self.assertIn(
            "no deterministic score snapshot available for this entity",
            env["warnings"],
        )

    def test_as_of_influences_freshness_only(self) -> None:
        env_a = self.dispatch("entity_intelligence", {
            "kind": "company", "entity_id": "2330", "as_of": "2026-07-09",
        })
        self.assertEqual(env_a["freshness"]["state"], "FRESH")
        env_b = self.dispatch("entity_intelligence", {
            "kind": "company", "entity_id": "2330", "as_of": "2026-10-03",
        })
        self.assertEqual(env_b["freshness"]["state"], "STALE")
        # domain data itself is identical between the two reads
        self.assertEqual(env_a["payload"], env_b["payload"])


class TestEvidenceContract(_ScoredCase):
    def test_undated_score_ref_resolves_to_latest(self) -> None:
        env = self.dispatch("evidence", {"ref": "score:company:2330"})
        self.assertEqual(env["status"], "ok")
        payload = env["payload"]
        self.assertTrue(payload["ref"].startswith("score:company:2330:"))
        refs = [r["ref"] for r in payload["related"]]
        self.assertIn("entity:company:2330", refs)
        self.assertTrue(
            any(r.startswith("signal:") for r in refs),
            "score node must expose its contributing signals",
        )
        for related in payload["related"]:
            self.assertIn(related["direction"], ("upstream", "downstream"))
        _assert_no_persistence_leakage(self, env)

    def test_entity_ref_and_limit(self) -> None:
        env = self.dispatch("evidence", {
            "ref": "entity:company:2330", "limit": 1,
        })
        self.assertEqual(env["status"], "ok")
        self.assertEqual(len(env["payload"]["related"]), 1)

    def test_unknown_ref_not_found(self) -> None:
        env = self.dispatch("evidence", {"ref": "entity:company:9999"})
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "NOT_FOUND")
        self.assertIsNone(env["payload"])

    def test_evidence_refs_are_followable(self) -> None:
        latest = self.dispatch("entity_intelligence", {
            "kind": "company", "entity_id": "2330",
        })
        self.assertGreater(len(latest["evidence_refs"]), 1)
        for ref in latest["evidence_refs"]:
            follow = self.dispatch("evidence", {"ref": ref["ref"]})
            self.assertEqual(follow["status"], "ok", ref)
            _assert_no_persistence_leakage(self, follow)


class TestFreshnessContract(_ScoredCase):
    def test_state_mapping_total(self) -> None:
        from phase3.service.freshness import to_service_state

        self.assertEqual(
            {g: to_service_state(g) for g in
             ("FRESH", "STALE", "EXPIRED", "UNAVAILABLE", "anything-else")},
            {"FRESH": "FRESH", "STALE": "DEGRADED", "EXPIRED": "STALE",
             "UNAVAILABLE": "UNAVAILABLE", "anything-else": "UNAVAILABLE"},
        )

    def test_policy_windows_drive_service_states(self) -> None:
        fresh = self.dispatch("freshness", {
            "kind": "company", "entity_id": "2330",
            "as_of": helpers.COMPANY_POLICY_FRESH_AS_OF,
        })
        self.assertEqual(fresh["payload"]["state"], "FRESH")
        degraded = self.dispatch("freshness", {
            "kind": "company", "entity_id": "2330",
            "as_of": helpers.COMPANY_POLICY_DEGRADED_AS_OF,
        })
        self.assertEqual(degraded["payload"]["state"], "DEGRADED")
        stale = self.dispatch("freshness", {
            "kind": "company", "entity_id": "2330",
            "as_of": helpers.COMPANY_POLICY_STALE_AS_OF,
        })
        self.assertEqual(stale["payload"]["state"], "STALE")

    def test_no_observation_is_unavailable(self) -> None:
        env = self.dispatch("freshness", {
            "kind": "company", "entity_id": "9999", "as_of": "2026-10-03",
        })
        self.assertEqual(env["payload"]["state"], "UNAVAILABLE")
        self.assertIsNone(env["payload"]["source_date"])
        _assert_no_persistence_leakage(self, env)

    def test_metadata_fields(self) -> None:
        env = self.dispatch("freshness", {
            "kind": "company", "entity_id": "2330", "as_of": "2026-10-03",
        })
        payload = env["payload"]
        self.assertEqual(payload["as_of"], "2026-10-03")
        self.assertEqual(payload["source_date"], helpers.DATE_BUCKET)
        self.assertIsInstance(payload["age"], int)
        self.assertRegex(payload["checked_at"], r"^\d{4}-\d{2}-\d{2}T")


class TestErrorTaxonomyAndContext(_ScoredCase):
    def test_taxonomy_values(self) -> None:
        self.assertEqual(
            {c.value for c in ServiceErrorCode},
            {"INVALID_REQUEST", "NOT_FOUND", "STALE_DATA", "DATA_UNAVAILABLE",
             "DEPENDENCY_UNAVAILABLE",
             # Phase 6.7B-R3: deterministic fail-closed schema
             # compatibility verdict for readiness (HP-07/06 / OI-08).
             "SCHEMA_INCOMPATIBLE",
             "INTERNAL_ERROR"},
        )

    def test_invalid_request_paths(self) -> None:
        for op, params in (
            ("bogus_operation", {}),
            ("latest_intelligence", {"kind": "crypto"}),
            ("latest_intelligence", {"limit": "many"}),
            ("latest_intelligence", {"limit": 0}),
            ("freshness", {"kind": "macro", "entity_id": ""}),
            ("entity_intelligence", {"kind": "macro", "entity_id": "  "}),
        ):
            with self.subTest(op=op, params=params):
                env = self.dispatch(op, params)
                self.assertEqual(env["status"], "error", (op, params))
                self.assertEqual(env["error"]["code"], "INVALID_REQUEST")

    def test_error_messages_sanitized(self) -> None:
        err = ServiceError(
            ServiceErrorCode.INTERNAL_ERROR,
            "boom at postgresql://user:secret@host/db and /home/x/y",
        )
        blob = json.dumps(err.to_dict())
        self.assertNotIn("postgres", blob)
        self.assertNotIn("/home/", blob)
        self.assertIn("<redacted>", blob)

    def test_principal_context_propagates(self) -> None:
        env = self.dispatch("health")
        self.assertEqual(env["principal_id"], "synthetic-alpha")
        self.assertEqual(env["request_id"], "req-001")
        self.assertEqual(CTX.principal_id, "synthetic-alpha")
        self.assertEqual(CTX.scopes, frozenset())

    def test_operations_list_is_frozen(self) -> None:
        self.assertEqual(
            OPERATIONS,
            ("health", "latest_intelligence", "entity_intelligence",
             "evidence", "freshness"),
        )

    def test_dependency_unavailable_envelope(self) -> None:
        from phase3.persistence.backend import open_store, resolve_spec

        import tempfile as _tempfile
        handle, path = _tempfile.mkstemp(prefix="fie_noschema_", suffix=".db")
        import os as _os
        _os.close(handle)

        # no migrations applied: repos unreadable → DEPENDENCY_UNAVAILABLE
        store = open_store(resolve_spec(path))
        from phase3.service.boundary import DefaultIntelligenceService

        broken = DefaultIntelligenceService(store)
        try:
            env = dispatch(broken, "health", {}, CTX)
            self.assertEqual(env["status"], "error")
            self.assertEqual(env["error"]["code"], "DEPENDENCY_UNAVAILABLE")
        finally:
            store.close()
            Path(path).unlink()


class TestEnvelopeShapeNoLeaks(unittest.TestCase):
    def test_utc_stamp_format(self) -> None:
        self.assertRegex(utc_now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


if __name__ == "__main__":
    unittest.main()