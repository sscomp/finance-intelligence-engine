#!/usr/bin/env python3
"""
Fake-endpoint tests for twse_fetcher.py — zero real network.

A local http.server instance plays the TWSE stand-in so every scenario
(security page, 307 without signature, recovered JSON, probe pacing) runs
against 127.0.0.1 only, per the operator instruction "測試時請打假端點".

Scenario matrix (mirrors docs/operations/TWSE_RATELIMIT_LOG.md §1–§2):
  1. clean JSON              -> parsed, pacing timestamp recorded
  2. security page (200/307) -> SecurityPageError + state records cooldown,
                                and a NEXT call raises FetchCooldownActive
                                without any network side effect
  3. 307 without signature   -> None (plain miss), NO cooldown recorded
  4. cooldown expiry probe   -> success clears the block; failure does NOT
                                extend it (only schedules next probe)
  5. pacing                  -> sleep called with the remaining gap

How to run:  python3 -B -m unittest discover -s tests -v
"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import institutional
import twse_fetcher
from twse_fetcher import (
    DEFAULT_COOLDOWN_S,
    FetchCooldownActive,
    SecurityPageError,
    check_security_page,
    fetch_json,
)

SECURITY_BODY = (
    "<html><body> 因為安全性考量，您所執行的頁面無法呈現。<br>"
    "FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED.</body></html>"
).encode("utf-8")

GOOD = {"stat": "OK", "data": [["2330", "1,000"]], "date": "20261110"}


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"          # ok | security | notfound307 | empty
    hits = 0

    def do_GET(self):  # noqa: N802  (http.server API)
        type(self).hits += 1
        mode = type(self).mode
        if mode == "security":
            body, status = SECURITY_BODY, 307
        elif mode == "notfound307":
            body, status = b"<html><body>redirect</body></html>", 307
        elif mode == "empty":
            body, status = b"", 200
        else:
            body, status = json.dumps(GOOD).encode("utf-8"), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002  (http.server API)  # silence per-request stderr
        pass


class _FakeClock:
    """Deterministic time.time() replacement controllable by tests."""

    def __init__(self, start: float = 1_000_000.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class FakeEndpointTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/fund/T86?response=json&date=20261110"
        self.state_dir = TemporaryDirectory()
        self.state_path = Path(self.state_dir.name) / "state.json"
        self.clock = _FakeClock()
        self.slept = []
        _Handler.hits = 0
        _Handler.mode = "ok"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.state_dir.cleanup()

    def fetch(self, **kw):
        kw.setdefault("state_path", self.state_path)
        kw.setdefault("now", self.clock)
        kw.setdefault("sleeper", self.slept.append)
        kw.setdefault("cooldown_s", 1800.0)
        kw.setdefault("probe_interval_s", 600.0)
        kw.setdefault("request_interval_s", 0.3)
        return fetch_json(self.url, **kw)

    # -- 1. clean JSON -------------------------------------------------

    def test_clean_json_parsed_and_pacing_recorded(self):
        result = self.fetch()
        self.assertEqual(result, GOOD)
        state = json.loads(self.state_path.read_text())
        entry = state["paths"][_host_path(self.url)]
        self.assertEqual(entry["last_request_ts"], self.clock())

    def test_check_security_page_accepts_variants(self):
        self.assertTrue(check_security_page(SECURITY_BODY))
        self.assertTrue(check_security_page(b"for security reasons, this page can not be accessed."))
        self.assertFalse(check_security_page(b""))
        self.assertFalse(check_security_page(json.dumps(GOOD).encode()))
        self.assertFalse(check_security_page(b"<html>redirect-loop</html>"))

    # -- 2. security page ----------------------------------------------

    def test_security_page_raises_and_records_cooldown(self):
        _Handler.mode = "security"
        with self.assertRaises(SecurityPageError):
            self.fetch()
        state = json.loads(self.state_path.read_text())
        entry = state["paths"][_host_path(self.url)]
        self.assertAlmostEqual(entry["blocked_until"], self.clock() + 1800.0)
        self.assertEqual(entry["last_block_status"], 307)

        # Next call must refuse WITHOUT any network activity.
        hits_before = _Handler.hits
        self.clock.advance(10)
        with self.assertRaises(FetchCooldownActive) as ctx:
            self.fetch()
        self.assertGreater(ctx.exception.retry_after, 1700)
        self.assertEqual(_Handler.hits, hits_before, "cooldown must not touch the fake endpoint")

    def test_security_page_on_http_error_body(self):
        # A 200-with-HTML and a 307-with-HTML are the same throttle signal.
        _Handler.mode = "security"
        with self.assertRaises(SecurityPageError):
            self.fetch(opener=lambda url, timeout: (200, SECURITY_BODY))
        state = json.loads(self.state_path.read_text())
        entry = state["paths"][_host_path(self.url)]
        self.assertEqual(entry["last_block_status"], 200)

    # -- 3. 307 without the signature ----------------------------------

    def test_plain_307_is_miss_not_throttle(self):
        _Handler.mode = "notfound307"
        self.assertIsNone(self.fetch())
        state = json.loads(self.state_path.read_text())
        entry = state["paths"][_host_path(self.url)]
        self.assertNotIn("blocked_until", entry, "307 without signature must not trigger cooldown")

    def test_empty_body_is_plain_miss(self):
        _Handler.mode = "empty"
        self.assertIsNone(self.fetch())
        state = json.loads(self.state_path.read_text())
        self.assertNotIn("blocked_until", state["paths"][_host_path(self.url)])

    # -- 4. cooldown expiry → controlled probe --------------------------

    def test_probe_success_clears_cooldown(self):
        _Handler.mode = "security"
        with self.assertRaises(SecurityPageError):
            self.fetch()
        self.clock.advance(1801)  # past the 30-minute block
        _Handler.mode = "ok"
        result = self.fetch()
        self.assertEqual(result, GOOD)
        state = json.loads(self.state_path.read_text())
        entry = state["paths"][_host_path(self.url)]
        self.assertNotIn("blocked_until", entry)

    def test_failed_probe_does_not_extend_cooldown(self):
        _Handler.mode = "security"
        with self.assertRaises(SecurityPageError):
            self.fetch()

        self.clock.advance(1801)
        _Handler.mode = "empty"  # probe returns a non-JSON miss
        probe_at = self.clock()
        with self.assertRaises(FetchCooldownActive):
            self.fetch()

        state = json.loads(self.state_path.read_text())
        entry = state["paths"][_host_path(self.url)]
        # §2.2/§4: failed probe may schedule the next probe, never extend
        # to a fresh full cooldown.
        self.assertAlmostEqual(entry["blocked_until"], probe_at + 600.0, places=6)
        self.assertLess(entry["blocked_until"], probe_at + 1800.0)
        # Next attempt again refused (probe pacing), still zero-storm.
        self.clock.advance(601)
        self.clock.t += 1
        with self.assertRaises(FetchCooldownActive):
            self.fetch()

    def test_probe_307_without_signature_fails_cleanly(self):
        _Handler.mode = "security"
        with self.assertRaises(SecurityPageError):
            self.fetch()
        self.clock.advance(1801)
        _Handler.mode = "notfound307"
        with self.assertRaises(FetchCooldownActive):
            self.fetch()

    # -- 5. pacing ------------------------------------------------------

    def test_inter_request_gap_slept(self):
        self.fetch()
        self.clock.advance(0.1)  # less than the 0.3 s floor
        self.fetch()
        self.assertEqual(len(self.slept), 1)
        self.assertAlmostEqual(self.slept[0], 0.2, places=6)

    def test_schema_miss_keeps_pacing_bookkeeping(self):
        _Handler.mode = "empty"
        self.assertIsNone(self.fetch())
        self.assertEqual(_Handler.hits, 1)


class InstitutionalIntegrationTests(unittest.TestCase):
    """Wire-level checks through institutional.fetch_t86_daily itself.

    Uses mock.patch.object so the module-internal fetch_json reference is
    the one exercised — the exact path production T86 calls take.
    """

    def setUp(self):
        self.state_dir = TemporaryDirectory()
        self.state_path = Path(self.state_dir.name) / "integration_state.json"
        self.clock = _FakeClock()
        self.slept = []

    def tearDown(self):
        self.state_dir.cleanup()

    def _patch_fetcher(self, **impl_kw):
        """Patch institutional.fetch_json with a kwarg-forwarding stub.

        patch.object kwargs configure the mock's attributes, NOT call
        arguments — so route defaults through a stub into the real
        fetch_json, keeping every contract path live.
        """
        real = twse_fetcher.fetch_json
        defaults = {"state_path": self.state_path, "now": self.clock, "sleeper": self.slept.append}
        defaults.update(impl_kw)

        def stub(url, **kw):
            kw.update(defaults)
            return real(url, **kw)

        return mock.patch.object(institutional, "fetch_json", stub)

    def test_fetch_t86_daily_parses_t86_rows(self):
        payload = {"stat": "OK", "data": [
            ["2330"] + ["0"] * 3 + ["100", "0", "0", "10", "0", "0", "7", "0", "0", "0", "0", "0", "0", "0", "117"],
        ]}
        with self._patch_fetcher(
            opener=lambda url, timeout: (200, json.dumps(payload).encode()),
        ):
            result = institutional.fetch_t86_daily("20261110")
        self.assertEqual(result["2330"], {"foreign": 110, "prop": 7, "total": 117})

    def test_fetch_t86_daily_propagates_cooldown(self):
        state = {"schema": 1, "paths": {"www.twse.com.tw/fund/T86": {"blocked_until": _FakeClock().t + 999}}}
        self.state_path.write_text(json.dumps(state))
        with self._patch_fetcher():
            with self.assertRaises(FetchCooldownActive):
                institutional.fetch_t86_daily("20261110")

    def test_accumulate_stops_remaining_dates_on_cooldown(self):
        state = {"schema": 1, "paths": {"www.twse.com.tw/fund/T86": {"blocked_until": _FakeClock().t + 999}}}
        self.state_path.write_text(json.dumps(state))
        with self._patch_fetcher():
            with mock.patch.object(
                institutional, "get_weekday_dates", return_value=["20261101", "20261102", "20261103"]
            ):
                _, fetched = institutional.accumulate_institutional_data(["2330"], days_back=3)
        self.assertEqual(fetched, 0)  # stopped before any date was fetched
        # Only ONE attempt occurred (the first date), not one per date.
        self.assertEqual(self.slept, [])


def _host_path(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


if __name__ == "__main__":
    unittest.main()
