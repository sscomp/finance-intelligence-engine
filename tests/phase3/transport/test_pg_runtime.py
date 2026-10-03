"""Disposable-PostgreSQL reference runtime gate (§15) + parity (§16).

The SAME HTTP contract suite shape as the SQLite gate, executed against
an isolated disposable PostgreSQL instance; then transport-level
SQLite/PostgreSQL parity over the read path with only
repository-approved volatile normalisation (identical keys, masks and
orderings as in tests/phase3/service/test_portability_and_backends.py).
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service import SCHEMA_VERSION  # noqa: E402
from tests.phase3.transport.test_http_contract import (  # noqa: E402
    _assert_envelope_common,
)
from tests.phase3.transport import runtime_harness  # noqa: E402


def _pg_dsn() -> str | None:
    import os

    dsn = os.environ.get("FIE_TEST_PG_DSN")
    if dsn:
        return dsn
    # the documented disposable-cluster default used by Phase 6.3/6.4/6.5
    candidate = "postgresql://fie@/fie_contract?host=/tmp/fie-pg&port=54329"
    if Path("/tmp/fie-pg/pgdata").exists():
        return candidate
    return None


@unittest.skipIf(_pg_dsn() is None, "disposable PostgreSQL cluster not available")
class TestPostgresRuntimeGate(runtime_harness.RuntimeHarness):
    """§15: the full public operation set over HTTP on PostgreSQL."""

    db_spec = _pg_dsn()

    def test_readiness_reports_postgres(self) -> None:
        status, env, _ = self.get("/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(env["payload"]["backend_kind"], "postgres")
        self.assertEqual(env["payload"]["counts"]["scores"], 4)

    def test_all_public_operations(self) -> None:
        for path, kind in (
            ("/v1/health", "health"),
            ("/v1/intelligence/latest?limit=10", "latest_intelligence"),
            ("/v1/intelligence/entity/company/2330", "entity_intelligence"),
            ("/v1/evidence/entity%3Acompany%3A2330", "evidence"),
            ("/v1/freshness/company/2330?as_of=2026-10-03", "freshness"),
        ):
            with self.subTest(path=path):
                status, env, _ = self.get(path)
                self.assertEqual(status, 200)
                _assert_envelope_common(self, env, kind)
                self.assertEqual(env["schema_version"], SCHEMA_VERSION)
                self.assertEqual(env["status"], "ok")

    def test_error_contract_matches_sqlite(self) -> None:
        status, env, _ = self.get("/v1/evidence/entity%3Acompany%3A9999")
        self.assertEqual(status, 404)
        self.assertEqual(env["error"]["code"], "NOT_FOUND")
        status, env, _ = self.get("/v1/intelligence/latest?limit=zz")
        self.assertEqual(status, 400)
        self.assertEqual(env["error"]["code"], "INVALID_REQUEST")
        status, env, _ = self.get("/v1/nope")
        self.assertEqual(status, 400)


class TestTransportParity(unittest.TestCase):
    """§16: identical envelopes across backends modulo volatile fields."""

    def test_parity_over_all_operations(self) -> None:
        try:
            self._run()
        except unittest.SkipTest:
            raise
        except Exception as exc:  # pragma: no cover - surfaced as failure
            self.fail(f"parity run failed: {exc}")

    def _run(self) -> None:
        import os
        import urllib.request

        from tests.phase3.service import helpers as svc_helpers

        pg_dsn = _pg_dsn()
        if pg_dsn is None:
            self.skipTest("disposable PostgreSQL cluster not available")
        # build both bundles through the shared production bootstrap
        sqlite_bundle = svc_helpers.bootstrap_state(None)
        pg_bundle = None
        try:
            pg_bundle = svc_helpers.bootstrap_state(pg_dsn)
            if pg_bundle is None:
                self.skipTest("PostgreSQL bootstrap unavailable")
            import threading
            from phase3.transport import load_transport_config
            from phase3.transport.http import FIEReferenceRuntime, make_http_server

            servers = []
            bases = {}
            for label, bundle in (("sqlite", sqlite_bundle), ("pg", pg_bundle)):
                rt = FIEReferenceRuntime.open(bundle["path"])
                cfg = load_transport_config(bundle["path"], port=0)
                srv = make_http_server(rt, cfg)
                th = threading.Thread(target=srv.serve_forever, daemon=True)
                th.start()
                host, port = srv.server_address[0], srv.server_address[1]
                servers.append((rt, srv, th, bundle))
                bases[label] = f"http://{host}:{port}"
            try:
                for path in (
                    "/v1/health",
                    "/v1/intelligence/latest?limit=10",
                    "/v1/intelligence/entity/company/2330",
                    "/v1/evidence/entity%3Acompany%3A2330",
                    "/v1/freshness/company/2330?as_of=2026-10-03",
                ):
                    envelopes: dict[str, dict] = {}
                    request_ids: dict[str, str] = {}
                    for label, base in bases.items():
                        with urllib.request.urlopen(base + path, timeout=10) as resp:
                            envelopes[label] = json.loads(resp.read())
                            request_ids[label] = resp.headers["X-Request-Id"]
                    # envelope shape must match exactly (§16: same keys;
                    # only the generated request_id may differ)
                    self.assertEqual(
                        sorted(envelopes["sqlite"].keys()),
                        sorted(envelopes["pg"].keys()),
                        f"key mismatch for {path}",
                    )
                    self.assertTrue(request_ids["sqlite"])
                    self.assertTrue(request_ids["pg"])
                    # domain content identical modulo approved volatility
                    left = self._normalise(envelopes["sqlite"])
                    right = self._normalise(envelopes["pg"])
                    self.assertEqual(left, right, f"content mismatch for {path}")
            finally:
                for rt, srv, _th, _bundle in servers:
                    srv.shutdown()
                    srv.server_close()
                    rt.close()
        finally:
            for bundle in (sqlite_bundle, pg_bundle):
                if bundle is not None:
                    bundle["store"].close()
                    Path(bundle["path"]).unlink(missing_ok=True)

    def _normalise(self, envelope: dict) -> dict:
        """Strip repository-approved volatile fields (§16).

        Same classification as the Phase 6.5 cross-backend parity:
        generated ids, wall-clock stamps/durations per-request; domain
        content must be byte-identical otherwise.
        """
        volatile_key = re.compile(r"(?i)(run_id|_at$|timestamp|duration|valid_until)")
        sequential_keys = {"snapshot_id", "_score_id"}

        def walk(value):
            if isinstance(value, dict):
                out: dict = {}
                for key, item in value.items():
                    if key == "backend_kind":
                        continue  # backend self-report, compared separately
                    if key == "request_id" or volatile_key.search(key):
                        continue  # per-request volatility
                    if key in sequential_keys:
                        out[key] = "<seq>"
                        continue
                    out[key] = walk(item)
                return out
            if isinstance(value, list):
                return [walk(item) for item in value]  # stable server ordering
            if isinstance(value, str):
                value = re.sub(r"ipr-[0-9a-f]{12}", "ipr-<masked>", value)
                value = re.sub(r"\b[A-Za-z0-9]{32}\b", "<hash32>", value)
            return value

        return walk(envelope)


if __name__ == "__main__":
    unittest.main()