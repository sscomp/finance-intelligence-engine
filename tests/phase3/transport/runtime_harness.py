"""Test harness: launch the reference HTTP runtime in-process.

Builds a scored disposable database through the production paths
(shared Phase 6.5 helpers), opens the Phase 6.6 runtime on an
ephemeral loopback port, and returns a small client. Every test
tears the server and store down cleanly (§15 teardown evidence).
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tests.phase3.service import helpers  # noqa: E402


class RuntimeHarness(unittest.TestCase):
    """Base: scored SQLite DB + in-process reference server on port 0."""

    #: overridden per test (e.g. PG DSN) or None for a fresh SQLite temp db
    db_spec: str | None = None
    #: extra load_transport_config kwargs (auth overrides etc.)
    config_overrides: dict[str, Any] = {}

    @classmethod
    def setUpClass(cls) -> None:
        from phase3.transport import load_transport_config
        from phase3.transport.auth import NoopAuthenticator
        from phase3.transport.http import FIEReferenceRuntime, make_http_server

        cls.bundle = helpers.bootstrap_state(cls.db_spec)
        cls.captured: list[logging.LogRecord] = []
        cls.log_handler = logging.Handler()
        cls.log_handler.emit = lambda rec: cls.captured.append(rec)

        cls.transport_logger = logging.getLogger("fie.transport")
        cls.transport_logger.addHandler(cls.log_handler)
        cls.transport_logger.setLevel(logging.INFO)
        cls.service_logger = logging.getLogger("fie.service")
        cls.service_logger.addHandler(cls.log_handler)
        cls.service_logger.setLevel(logging.INFO)

        cls.runtime = FIEReferenceRuntime.open(cls.bundle["path"])
        overrides = dict(cls.config_overrides)
        cls.authenticator_override = overrides.pop("_authenticator", None)
        cls.config = load_transport_config(cls.bundle["path"], **overrides)
        cls.server = make_http_server(
            cls.runtime,
            cls.config,
            logger=cls.transport_logger,
            authenticator=cls.authenticator_override
            if cls.authenticator_override is not None
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
        if cls.db_spec is None and path.suffix == ".db":
            path.unlink()
        cls.transport_logger.removeHandler(cls.log_handler)
        cls.service_logger.removeHandler(cls.log_handler)
        cls.transport_logger.setLevel(logging.NOTSET)
        cls.service_logger.setLevel(logging.NOTSET)

    # -- client -------------------------------------------------------------
    def get(self, path: str, *, headers: dict[str, str] | None = None):
        """GET; returns (http_status, envelope dict, response headers)."""
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

    def send(self, method: str, path: str, body: bytes = b"{}"):
        request = urllib.request.Request(
            self.base + path, data=body, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    # -- telemetry capture ----------------------------------------------------
    def records(self) -> list[dict]:
        out = []
        for rec in self.captured:
            try:
                out.append(json.loads(rec.getMessage()))
            except (ValueError, TypeError):
                continue
        return out

    def clear_records(self) -> None:
        self.captured.clear()