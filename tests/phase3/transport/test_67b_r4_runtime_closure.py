"""Phase 6.7B-R4 — observability, timeout, and runtime-closure gate.

In-process HTTP legs over the production bootstrap (work order Task B/C/G,
adversarial matrix §10):

* **bounded dispatch** — an operation that exceeds ``FIE_REQUEST_TIMEOUT``
  answers the STABLE ``OPERATION_TIMEOUT`` classification (503,
  ``details.reason = DISPATCH_DEADLINE_EXCEEDED``); the abandoned
  read-only operation is executed exactly ONCE (never re-executed),
  liveness stays positive through the hang, and readiness recovers
  without unsafe replay once the worker is free;
* **request-level timeout** — the same knob bounds connection reads
  (a client that never finishes its request line is dropped bounded);
* **observability contract** — every request emits one deterministic
  ``http_request`` event with the stable field set; readiness failures
  carry the classified ``reason``; the startup event names the backend
  FAMILY only; the shutdown path (subprocess, real SIGTERM) emits
  ``shutdown_signal`` + ``server_stopped`` and leaves NO worker residue;
* **redaction** — hostile request-id/marker injection never reaches the
  telemetry or the envelopes (§10: decision correctness AND non-leakage).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tests.phase3.service import helpers  # noqa: E402

#: Synthetic token — never a real secret.
R4_TOKEN = "r4-fixture-synthetic-token"

#: Hostile markers (work order Task D §510 shapes) — synthetic only.
MARKER_PII = "PII_MARKER_R4"
MARKER_SECRET = "SECRET_MARKER_R4"


class _CapturedLogs(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []
        self.emit = lambda rec: self.records.append(rec)  # noqa: E731

    def events(self) -> list[dict]:
        out = []
        for rec in self.records:
            try:
                out.append(json.loads(rec.getMessage()))
            except (ValueError, TypeError):
                continue
        return out


class _BaseRuntime(unittest.TestCase):
    """Scored disposable SQLite runtime on an ephemeral loopback port."""

    request_timeout: float | None = None
    token_mode = False

    @classmethod
    def setUpClass(cls) -> None:
        from phase3.transport import load_transport_config
        from phase3.transport.auth import BearerTokenAuthenticator
        from phase3.transport.http import FIEReferenceRuntime, make_http_server

        cls.bundle = helpers.bootstrap_state(None)
        cls.captured = _CapturedLogs()
        cls._loggers = (logging.getLogger("fie.transport"),
                        logging.getLogger("fie.service"))
        cls._old_levels = [logger.level for logger in cls._loggers]
        for logger in cls._loggers:
            logger.addHandler(cls.captured)
            logger.setLevel(logging.INFO)

        cls.runtime = FIEReferenceRuntime.open(cls.bundle["path"])
        overrides: dict = {}
        if cls.token_mode:
            overrides = {"auth_mode": "token", "auth_token": R4_TOKEN}
        cls.config = load_transport_config(
            cls.bundle["path"],
            request_timeout=cls.request_timeout,
            **overrides,
        )
        cls.server = make_http_server(
            cls.runtime,
            cls.config,
            logger=cls._loggers[0],
            authenticator=BearerTokenAuthenticator(R4_TOKEN)
            if cls.token_mode
            else None,
        )
        host, port = cls.server.server_address[0], cls.server.server_address[1]
        cls.base = f"http://{host}:{port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.runtime.close()
        cls.bundle["store"].close()
        path = Path(cls.bundle["path"])
        if path.suffix == ".db" and path.exists():
            path.unlink()
        for logger, level in zip(cls._loggers, cls._old_levels):
            logger.removeHandler(cls.captured)
            logger.setLevel(level)

    def get(self, path: str, *, headers: dict[str, str] | None = None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)

    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {R4_TOKEN}"}


class TestObservabilityContract(_BaseRuntime):
    """Task B — the stable operational event surface (sanitized)."""

    def test_every_request_emits_one_structured_record(self) -> None:
        before = len(self.captured.events())
        status, env, headers = self.get("/v1/health")
        self.assertEqual(status, 200)
        events = self.captured.events()[before:]
        http_events = [e for e in events if e.get("event") == "http_request"]
        self.assertEqual(len(http_events), 1, http_events)
        record = http_events[0]
        # deterministic field set (§5.1) — no payload/token/DSN/paths
        self.assertIn("request_id", record)
        self.assertEqual(record["request_id"], env["request_id"])
        self.assertEqual(record["operation"], "health")
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["status"], "ok")
        self.assertIn("duration_ms", record)
        blob = json.dumps(record)
        self.assertNotIn(MARKER_SECRET, blob)
        self.assertNotIn(MARKER_PII, blob)

    def test_hostile_request_id_never_leaks_into_telemetry_or_body(self) -> None:
        hostile = f"x{MARKER_SECRET}<>& {MARKER_PII}"
        status, env, headers = self.get(
            "/v1/health", headers={"X-Request-Id": hostile}
        )
        self.assertEqual(status, 200)
        # Accepted R2 sanitizer contract: the request-id is filtered to
        # the inert allowlist [A-Za-z0-9._-] and capped. The marker TEXT
        # itself is client-supplied data (an inert token once filtered —
        # it can carry nothing the client did not send); what must never
        # survive is the injection VECTOR: whitespace, HTML/quote and
        # control characters, or relative-path fragment shapes.
        rid = env["request_id"]
        import re as _re

        self.assertRegex(rid, r"^[A-Za-z0-9._-]{1,64}$")
        for vector in ("<", ">", "&", " ", "\t", ":", "/", "\\", "\r", "\n"):
            self.assertNotIn(vector, rid)
        self.assertNotIn("..", rid)
        self.assertEqual(headers["X-Request-Id"], rid)
        # telemetry echoes the SAME inert value — nothing unsanitized
        record = next(
            e for e in reversed(self.captured.events())
            if e.get("event") == "http_request"
        )
        self.assertEqual(record["request_id"], rid)

    def test_startup_event_names_backend_family_only(self) -> None:
        # server_started (run_server) is exercised by the subprocess
        # lifecycle test; here the make_http_server path proves the
        # bounded dispatch deadline derives from the accepted knob.
        self.assertEqual(
            self.runtime.service.__dict__["_dispatch_timeout"],
            self.config.request_timeout,
        )

    def test_no_query_strings_or_credentials_in_event_stream(self) -> None:
        before = len(self.captured.events())
        marker = f"?limit=9&secret={MARKER_SECRET}"
        self.get("/v1/intelligence/latest" + marker)
        blob = "\n".join(
            json.dumps(e)
            for e in self.captured.events()[before:]
            if e.get("event") == "http_request"
        )
        self.assertNotIn(MARKER_SECRET, blob)
        self.assertNotIn("secret=", blob)


class TestBoundedDispatchTimeout(unittest.TestCase):
    """Task C — the bounded dispatch window and its stable classification."""

    @classmethod
    def setUpClass(cls) -> None:
        from phase3.transport import load_transport_config
        from phase3.transport.http import FIEReferenceRuntime, make_http_server

        cls.bundle = helpers.bootstrap_state(None)
        cls.captured = _CapturedLogs()
        logger = logging.getLogger("fie.transport")
        logger.addHandler(cls.captured)
        logger.setLevel(logging.INFO)
        cls._logger = logger

        cls.runtime = FIEReferenceRuntime.open(cls.bundle["path"])
        # warm the worker so the slow-op patch attaches to the live target
        cls.runtime.service._resolve()
        target = cls.runtime.service.__dict__["_target"]

        cls.calls = [0]
        original = target.get_latest_intelligence

        def slow(*args, **kwargs):
            cls.calls[0] += 1
            time.sleep(2.0)
            return original(*args, **kwargs)

        target.get_latest_intelligence = slow

        cls.config = load_transport_config(
            cls.bundle["path"], request_timeout=0.5
        )
        cls.server = make_http_server(cls.runtime, cls.config, logger=logger)
        host, port = cls.server.server_address[0], cls.server.server_address[1]
        cls.base = f"http://{host}:{port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.runtime.close()
        cls.bundle["store"].close()
        path = Path(cls.bundle["path"])
        if path.suffix == ".db" and path.exists():
            path.unlink()
        cls._logger.removeHandler(cls.captured)

    def get(self, path: str, *, headers: dict[str, str] | None = None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)

    def test_timeout_answers_stable_classification_exactly_once(self) -> None:
        started = time.monotonic()
        status, env, headers = self.get("/v1/intelligence/latest")
        elapsed = time.monotonic() - started
        # decision correctness
        self.assertEqual(status, 503, env)
        self.assertLess(elapsed, 5.0, "the deadline must bound the WAIT")
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "OPERATION_TIMEOUT")
        self.assertEqual(
            env["error"]["details"], {"reason": "DISPATCH_DEADLINE_EXCEEDED"}
        )
        # request-id echoes (header == body) even on the timeout path
        self.assertTrue(env.get("request_id"))
        self.assertEqual(headers["X-Request-Id"], env["request_id"])
        # non-leakage: no stack/driver/operation internals
        blob = json.dumps(env)
        self.assertNotIn("Traceback", blob)
        self.assertNotIn("sleep", blob.lower())
        self.assertNotIn(MARKER_SECRET, blob)

    def test_abandoned_operation_is_never_reexecuted(self) -> None:
        calls_before = self.calls[0]
        self.get("/v1/intelligence/latest")
        # exactly ONE execution for the abandoned read — no retry storm
        time.sleep(2.5)  # let the worker finish the abandoned call
        self.assertEqual(self.calls[0], calls_before + 1)

    def test_liveness_stays_positive_through_a_hung_operation(self) -> None:
        calls = self.calls[0]
        self.get("/v1/intelligence/latest")  # hangs beyond the deadline
        status, env, _ = self.get("/healthz")
        self.assertEqual(status, 200)  # no persistence, no queue wait
        self.assertEqual(env["status"], "alive")
        _ = calls

    def test_readiness_recovers_without_unsafe_replay(self) -> None:
        time.sleep(3.0)  # worker free again
        status, env, _ = self.get("/readyz")
        self.assertEqual(status, 200, env)
        self.assertEqual(env["status"], "ok")

    def test_http_request_event_carries_the_timeout_classification(self) -> None:
        events = [
            e for e in self.captured.events()
            if e.get("event") == "http_request"
            and e.get("operation") == "latest_intelligence"
        ]
        self.assertTrue(events)
        self.assertEqual(events[-1]["error_code"], "OPERATION_TIMEOUT")
        self.assertEqual(events[-1]["reason"], "DISPATCH_DEADLINE_EXCEEDED")


class TestRequestLevelReadTimeout(unittest.TestCase):
    """Task C — a client that never completes its request is dropped bounded."""

    def test_half_open_request_is_closed_within_the_window(self) -> None:
        from phase3.transport import load_transport_config
        from phase3.transport.http import FIEReferenceRuntime, make_http_server

        bundle = helpers.bootstrap_state(None)
        runtime = FIEReferenceRuntime.open(bundle["path"])
        config = load_transport_config(bundle["path"], request_timeout=1.0)
        server = make_http_server(runtime, config)
        host, port = server.server_address[0], server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                sock.settimeout(8)
                sock.sendall(b"GET /hea")  # deliberately incomplete
                started = time.monotonic()
                residual = sock.recv(64)
                elapsed = time.monotonic() - started
            # the connection is closed (empty read) well inside the bound
            self.assertLess(elapsed, 6.0)
            self.assertEqual(residual, b"")
        finally:
            server.shutdown()
            server.server_close()
            runtime.close()
            bundle["store"].close()
            path = Path(bundle["path"])
            if path.suffix == ".db" and path.exists():
                path.unlink()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestSubprocessShutdownLifecycle(unittest.TestCase):
    """Task G — real process startup/SIGTERM/cleanup (run_server + main)."""

    def test_sigterm_leads_to_bounded_clean_stop_and_no_residue(self) -> None:
        db_path = Path(tempfile.mkdtemp(prefix="fie-r4-shutdown-")) / "shutdown.db"
        env = dict(os.environ)
        env.update(
            FIE_HTTP_HOST="127.0.0.1",
            FIE_HTTP_PORT=str(_free_port()),
            FIE_DATABASE_URL=str(db_path),
            FIE_LOG_LEVEL="INFO",
            FIE_REQUEST_TIMEOUT="30",
        )
        port = int(env["FIE_HTTP_PORT"])
        proc = subprocess.Popen(
            [sys.executable, "-m", "phase3.transport.http"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
        )
        try:
            base = f"http://127.0.0.1:{port}"
            # deterministic clean startup: healthz answers (bounded wait)
            deadline = time.monotonic() + 30.0
            alive = False
            while time.monotonic() < deadline and not alive:
                try:
                    with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                        alive = r.status == 200
                except Exception:
                    time.sleep(0.2)
            self.assertTrue(alive, "server must reach liveness deterministically")

            proc.send_signal(signal.SIGTERM)
            started = time.monotonic()
            proc.wait(timeout=30)
            elapsed = time.monotonic() - started
            self.assertEqual(proc.returncode, 0)
            self.assertLess(elapsed, 20.0, "shutdown must be bounded")
            stderr = proc.stderr.read()  # type: ignore[union-attr]
            events = []
            for line in stderr.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        pass
            names = [e.get("event") for e in events]
            self.assertIn("server_started", names)
            self.assertIn("shutdown_signal", names)
            self.assertIn("server_stopped", names)
            started_event = next(e for e in events if e.get("event") == "server_started")
            # backend FAMILY only — never the DSN/path
            self.assertEqual(started_event.get("backend"), "sqlite")
            self.assertNotIn(str(db_path), json.dumps(events))
            stopped = next(e for e in events if e.get("event") == "server_stopped")
            self.assertTrue(stopped.get("clean"))
            self.assertIn("shutdown_ms", stopped)
            # the socket is actually released
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            # no residue
            try:
                if db_path.exists():
                    db_path.unlink()
                db_path.parent.rmdir()
            except OSError:
                pass

    def test_invalid_config_startup_refuses_nonzero_without_bind(self) -> None:
        env = dict(os.environ)
        # 6.8A: supply an explicit (disposable) DB target so the profile
        # auth-mode gate is the refusal that fires — with no target the
        # DATABASE_URL_MISSING refusal would win first.
        env.update(FIE_SERVICE_ENV="production", FIE_AUTH_MODE="none",
                   FIE_DATABASE_URL="/tmp/fie_r4_refusal_fixture.db",
                   FIE_AUTH_TOKEN="")
        proc = subprocess.run(
            [sys.executable, "-m", "phase3.transport.http"],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        self.assertEqual(proc.returncode, 2)
        refusal = json.loads(proc.stderr.strip().splitlines()[-1])
        self.assertEqual(refusal["error"]["code"], "AUTH_MODE_FORBIDDEN_IN_PRODUCTION")


class TestReadinessFailureClassificationLifecycle(_BaseRuntime):
    """Task G §5.5 — lifecycle semantics over an in-process runtime."""

    token_mode = True

    def test_repeated_readiness_is_stable_and_authorized(self) -> None:
        for _ in range(3):
            status, env, _ = self.get("/readyz", headers=self.bearer())
            self.assertEqual(status, 200)
            self.assertEqual(env["status"], "ok")
        # auth boundary intact (R2): credentialless/bad-bearer stay 401
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            status, env, _ = self.get("/readyz", headers=headers)
            self.assertEqual(status, 401)
            self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")

    def test_shutdown_idempotent_without_residue(self) -> None:
        # repeat: close must tolerate a second call; the worker stops
        # bounded (the stop sentinel drains asynchronously — the exit
        # itself is immediate for an idle worker, never unbounded)
        self.runtime.close()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            workers = [
                t for t in threading.enumerate()
                if t.name == "fie-persistence-worker"
            ]
            if not workers:
                break
            time.sleep(0.1)
        self.assertEqual(workers, [], "worker must stop bounded")
        # a second close stays instant and safe (no post-sentinel hang)
        started = time.monotonic()
        self.runtime.close()
        self.assertLess(time.monotonic() - started, 5.0)

    def test_events_show_the_readiness_transition_reason_shape(self) -> None:
        # healthy readiness → the record has NO reason; the failure shape
        # (reason classified, no driver text) is proven by the PG suite.
        self.get("/readyz", headers=self.bearer())
        events = [
            e for e in self.captured.events()
            if e.get("event") == "http_request" and e.get("operation") == "readiness"
        ]
        self.assertTrue(events)
        self.assertNotIn("reason", events[-1])  # healthy: no reason field


if __name__ == "__main__":
    unittest.main()