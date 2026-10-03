"""Backend independence + arbitrary-CWD portability (§17 areas 8-11, 13).

* The Phase 6.5 service operations answer identically at the contract
  level on SQLite and (when a disposable cluster is reachable) on
  PostgreSQL — the boundary contains no dialect vocabulary.
* The full read path works from an arbitrary working directory with
  explicit portable paths — no CWD-relative or developer-host path.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service import RequestContext, dispatch  # noqa: E402
from tests.phase3.service import helpers  # noqa: E402

CTX = RequestContext(principal_id="synthetic-beta", request_id="req-cwd")


def _contract_snapshot(service) -> dict:
    """Backend-independent view of the five operations (domain content)."""
    health = dispatch(service, "health", {}, CTX)
    latest = dispatch(service, "latest_intelligence", {"limit": 10}, CTX)
    entity = dispatch(service, "entity_intelligence",
                      {"kind": "company", "entity_id": "2330"}, CTX)
    evidence = dispatch(service, "evidence",
                        {"ref": "score:company:2330"}, CTX)
    fresh = dispatch(service, "freshness",
                     {"kind": "company", "entity_id": "2330",
                      "as_of": "2026-10-03"}, CTX)
    return {
        "health": health["payload"],
        "latest": [
            {k: v for k, v in item.items() if k != "freshness"}
            for item in latest["payload"]
        ],
        "entity": entity["payload"],
        "evidence_refs": [r["ref"] for r in evidence["payload"]["related"]],
        "freshness_state": fresh["payload"]["state"],
    }


class TestArbitraryCwd(unittest.TestCase):
    def test_full_read_path_from_unrelated_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fie_cwd_") as cwd:
            bundle = helpers.bootstrap_state()
            try:
                prev = os.getcwd()
                os.chdir(cwd)
                try:
                    snap = _contract_snapshot(bundle["service"])
                    self.assertEqual(
                        [i["entity_id"] for i in snap["latest"]],
                        ["2330", "1101", "半導體", "global"],
                    )
                    self.assertEqual(snap["health"]["status"], "ok")
                    self.assertTrue(snap["evidence_refs"])
                finally:
                    os.chdir(prev)
            finally:
                bundle["store"].close()
                with contextlib.suppress(FileNotFoundError):
                    Path(bundle["path"]).unlink()

    def test_paths_module_is_cwd_independent(self) -> None:
        from phase3.paths import project_root

        with tempfile.TemporaryDirectory(prefix="fie_cwd_") as cwd:
            prev = os.getcwd()
            os.chdir(cwd)
            try:
                self.assertEqual(project_root(), REPO_ROOT)
            finally:
                os.chdir(prev)
            self.assertTrue((project_root() / "phase3").exists())


class TestPostgresServiceContract(unittest.TestCase):
    """Same service contract over the disposable PostgreSQL cluster."""

    def setUp(self) -> None:
        self.bundle = helpers.bootstrap_pg_state()
        self.skipped = self.bundle is None

    def tearDown(self) -> None:
        if self.bundle is not None:
            try:
                self.bundle["store"].close()
            except Exception:  # noqa: BLE001 - backend may already be gone
                pass

    def test_pg_service_answers_contract(self) -> None:
        if self.bundle is None:
            self.skipTest(
                "disposable PostgreSQL cluster unavailable "
                "(FIE_TEST_PG_DSN unreachable; psycopg optional extra)"
            )
        snap = _contract_snapshot(self.bundle["service"])
        self.assertEqual(snap["health"]["backend_kind"], "postgres")
        self.assertEqual(
            [i["entity_id"] for i in snap["latest"]],
            ["2330", "1101", "半導體", "global"],
        )
        self.assertEqual(snap["freshness_state"], "STALE")
        self.assertTrue(snap["evidence_refs"])


class TestSqliteVsPostgresParity(unittest.TestCase):
    def test_contract_snapshots_match_across_backends(self) -> None:
        sqlite_bundle = helpers.bootstrap_state()
        try:
            pg_bundle = helpers.bootstrap_pg_state()
            if pg_bundle is None:
                self.skipTest(
                    "disposable PostgreSQL cluster unavailable "
                    "(FIE_TEST_PG_DSN unreachable; psycopg optional extra)"
                )
            try:
                import re

                _RUN_ID_RE = re.compile(r"ipr-[0-9a-f]{12}")

                def _strip_volatile(obj):
                    if isinstance(obj, dict):
                        return {
                            k: _strip_volatile(v)
                            for k, v in obj.items()
                            if not re.search(
                                r"(?i)(run_id|_at$|timestamp|duration|valid_until)",
                                k,
                            )
                        }
                    if isinstance(obj, list):
                        return [_strip_volatile(v) for v in obj]
                    if isinstance(obj, str):
                        return _RUN_ID_RE.sub("ipr-<run>", obj)
                    return obj

                a = _strip_volatile(_contract_snapshot(sqlite_bundle["service"]))
                b = _strip_volatile(_contract_snapshot(pg_bundle["service"]))
                a["health"].pop("backend_kind", None)
                b["health"].pop("backend_kind", None)
                self.assertEqual(a, b)
            finally:
                try:
                    pg_bundle["store"].close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                sqlite_bundle["store"].close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    unittest.main()