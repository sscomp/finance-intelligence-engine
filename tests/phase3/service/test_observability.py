"""Observability contract (Phase 6.5 §15, §17).

Each reference-adapter invocation emits one structured (JSON) log
record carrying the §15 fields — request_id, operation, status,
duration, freshness state, as_of — and NEVER a payload, secret, or
stack trace. stdlib logging only; no vendor SDK (per §15, contracts
are sufficient for this phase).
"""
from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service import RequestContext, dispatch  # noqa: E402
from phase3.service.reference import REQUEST_LOGGER  # noqa: E402
from tests.phase3.service import helpers  # noqa: E402

CTX = RequestContext(principal_id="synthetic-alpha", request_id="req-log")


class ObservabilityCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = helpers.bootstrap_state()
        cls.service = cls.bundle["service"]

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.bundle["store"].close()
        finally:
            path = Path(cls.bundle["path"])
            if path.exists():
                path.unlink()

    def setUp(self) -> None:
        self._captured: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = lambda rec: self._captured.append(rec)
        REQUEST_LOGGER.addHandler(handler)
        REQUEST_LOGGER.setLevel(logging.INFO)
        self._handler = handler

    def tearDown(self) -> None:
        REQUEST_LOGGER.removeHandler(self._handler)
        REQUEST_LOGGER.setLevel(logging.NOTSET)

    def records(self) -> list[dict]:
        return [json.loads(r.getMessage()) for r in self._captured]

    def test_ok_request_record_fields(self) -> None:
        dispatch(self.service, "freshness",
                 {"kind": "company", "entity_id": "2330"}, CTX)
        [record] = [r for r in self.records()
                    if r["operation"] == "freshness"] or [{}]
        self.assertEqual(record["request_id"], "req-log")
        self.assertEqual(record["operation"], "freshness")
        self.assertEqual(record["status"], "ok")
        self.assertIsInstance(record["duration_ms"], (int, float))
        self.assertEqual(record["freshness_state"], "STALE")
        self.assertEqual(record["as_of"], "2026-10-03")

    def test_error_record_carries_taxonomy_code(self) -> None:
        dispatch(self.service, "evidence",
                 {"ref": "entity:company:9999"}, CTX)
        record = self.records()[-1]
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["error_code"], "NOT_FOUND")

    def test_no_payload_or_secret_in_logs(self) -> None:
        dispatch(self.service, "entity_intelligence",
                 {"kind": "company", "entity_id": "2330"}, CTX)
        blob = json.dumps(self.records())
        self.assertNotIn("signals", blob)
        self.assertNotIn("payload", blob)
        self.assertNotIn("postgres", blob)

    def test_logging_off_costs_no_records(self) -> None:
        REQUEST_LOGGER.setLevel(logging.WARNING)
        try:
            dispatch(self.service, "health", {}, CTX)
            self.assertEqual(self._captured, [])
        finally:
            pass


if __name__ == "__main__":
    unittest.main()