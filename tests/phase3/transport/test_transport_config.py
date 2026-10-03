"""Transport configuration precedence tests (Phase 6.6 §9).

Precedence (tested): explicit argument > environment > safe default.
No developer paths, no checked-in secrets, non-loopback-free defaults.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service.runtime_config import load_runtime_config  # noqa: E402
from phase3.transport import (  # noqa: E402
    TRANSPORT_ENV_VARS,
    load_transport_config,
)
from phase3.transport.config import (  # noqa: E402
    FIE_AUTH_MODE,
    FIE_HTTP_HOST,
    FIE_HTTP_PORT,
    FIE_LOG_LEVEL,
    FIE_REQUEST_TIMEOUT,
    TransportConfig,
)


def _env(**vars: str | None) -> dict[str, str | None]:
    saved = {key: os.environ.get(key) for key in vars}
    for key, value in vars.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return saved


def _restore(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _clear_transport_env() -> dict[str, str | None]:
    return _env(**dict.fromkeys(TRANSPORT_ENV_VARS))


class TestDefaults(unittest.TestCase):
    def test_safe_defaults(self) -> None:
        saved = _clear_transport_env()
        try:
            config = load_transport_config()
            self.assertEqual(config.host, "127.0.0.1")  # never a public bind
            self.assertEqual(config.port, 8787)
            self.assertEqual(config.auth_mode, "none")
            self.assertEqual(config.auth_token, "")
            self.assertEqual(config.log_level, "INFO")
            self.assertEqual(config.request_timeout, 60.0)
        finally:
            _restore(saved)

    def test_defaults_derived_not_hardcoded(self) -> None:
        # the runtime diagnostic dirs are *derived* from the project root
        # via phase3.paths (portable across checkouts/hosts — they are
        # never literal developer-host paths in source; the §20 file-content
        # audit lives in test_portability_audit).
        from phase3.paths import artifact_dir, data_dir

        saved = _clear_transport_env()
        try:
            runtime = load_transport_config().to_dict()["runtime"]
            self.assertEqual(runtime["data_dir"], str(data_dir()))
            self.assertEqual(runtime["artifact_dir"], str(artifact_dir()))
            self.assertFalse(Path(runtime["database_spec"]).is_absolute())
        finally:
            _restore(saved)


class TestPrecedence(unittest.TestCase):
    def test_env_beats_default(self) -> None:
        saved = _env(**{
            FIE_HTTP_HOST: "0.0.0.0",
            FIE_HTTP_PORT: "9001",
            FIE_AUTH_MODE: "TOKEN",  # normalised
            FIE_LOG_LEVEL: "warning",
            FIE_REQUEST_TIMEOUT: "17",
        })
        try:
            config = load_transport_config()
            self.assertEqual(config.host, "0.0.0.0")
            self.assertEqual(config.port, 9001)
            self.assertEqual(config.auth_mode, "token")
            self.assertEqual(config.log_level, "WARNING")
            self.assertEqual(config.request_timeout, 17.0)
        finally:
            _restore(saved)

    def test_argument_beats_env(self) -> None:
        saved = _env(**{
            FIE_HTTP_PORT: "9001",
            FIE_LOG_LEVEL: "ERROR",
            FIE_REQUEST_TIMEOUT: "17",
        })
        try:
            config = load_transport_config(
                port=9100, log_level="debug", request_timeout=33.0,
            )
            self.assertEqual(config.port, 9100)
            self.assertEqual(config.log_level, "DEBUG")
            self.assertEqual(config.request_timeout, 33.0)
        finally:
            _restore(saved)

    def test_invalid_auth_mode_refuses(self) -> None:
        # Phase 6.7B / ADR-017 (HP-00): an invalid auth mode used to
        # silently fall back to `none`, disabling authentication; it
        # now refuses deterministically — in every profile.
        from phase3.service.runtime_config import ConfigurationError

        for bad in ("basic", "oidc", "NONE-CERTIFIED", ""):
            with self.subTest(bad=bad):
                saved = _env(**{FIE_AUTH_MODE: bad})
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    self.assertEqual(cm.exception.code, "UNKNOWN_AUTH_MODE")
                finally:
                    _restore(saved)

    def test_invalid_log_level_refuses(self) -> None:
        # ADR-017: invalid explicit values never silently become INFO.
        from phase3.service.runtime_config import ConfigurationError

        saved = _env(**{FIE_LOG_LEVEL: "verbose"})
        try:
            with self.assertRaises(ConfigurationError) as cm:
                load_transport_config()
            self.assertEqual(cm.exception.code, "INVALID_LOG_LEVEL")
        finally:
            _restore(saved)

    def test_invalid_port_refuses(self) -> None:
        # ADR-017: an invalid port never silently becomes 8787.
        from phase3.service.runtime_config import ConfigurationError

        for bad in ("not-a-port", "-1", "99999", ""):
            with self.subTest(bad=bad):
                saved = _env(**{FIE_HTTP_PORT: bad})
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    self.assertEqual(cm.exception.code, "INVALID_HTTP_PORT")
                finally:
                    _restore(saved)

    def test_zero_port_is_documented_ephemeral(self) -> None:
        # `0` = deliberate ephemeral bind (tests/clean-room probes);
        # pinned here permanently (see ADR-017 §4.6).
        saved = _env(**{FIE_HTTP_PORT: "0"})
        try:
            self.assertEqual(load_transport_config().port, 0)
        finally:
            _restore(saved)

    def test_timeout_invalid_values_refuse(self) -> None:
        # ADR-017 (HP-08): one timeout vocabulary — non-numeric, zero,
        # negative and non-finite all refuse; no split-brain fallbacks.
        from phase3.service.runtime_config import ConfigurationError

        for bad in ("abc", "0", "-3", "-0.5", "inf", "nan"):
            with self.subTest(bad=bad):
                saved = _env(**{FIE_REQUEST_TIMEOUT: bad})
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    self.assertEqual(cm.exception.code, "INVALID_REQUEST_TIMEOUT")
                finally:
                    _restore(saved)

    def test_invalid_explicit_arguments_refuse_too(self) -> None:
        # The explicit-argument surface obeys the same vocabulary; no
        # value class silently falls back to the safe default.
        from phase3.service.runtime_config import ConfigurationError

        cases = [
            {"auth_mode": "basic"},
            {"log_level": "verbose"},
            {"port": -5},
            {"port": 70000},
            {"request_timeout": 0.0},
            {"request_timeout": -3.0},
            {"request_timeout": float("nan")},
            {"sqlite_access_mode": "not-a-mode"},
        ]
        for kwargs in cases:
            with self.subTest(**kwargs):
                with self.assertRaises(ConfigurationError):
                    load_transport_config(**kwargs)


class TestLogSafety(unittest.TestCase):
    def test_to_dict_never_serialises_token_or_dsn(self) -> None:
        secret = "fie-test-credential-xyzzy"  # noqa: S105
        config = load_transport_config(
            "postgresql://fie:topsecret@localhost/fie",  # noqa: S105 fixture
            auth_mode="token",
            auth_token=secret,
        )
        blob = json.dumps(config.to_dict())
        self.assertNotIn(secret, blob)
        self.assertNotIn("auth_token", blob)
        self.assertNotIn("topsecret", blob)  # masked (Phase 6.3 contract)
        # the raw fields remain on the object (repr=False) for actual use
        self.assertEqual(config.auth_token, secret)

    def test_frozen(self) -> None:
        with self.assertRaises(Exception):
            _ = TransportConfig().host = "0.0.0.0"  # type: ignore[misc]

    def test_env_documented_in_module(self) -> None:
        self.assertEqual(
            set(TRANSPORT_ENV_VARS),
            {
                FIE_HTTP_HOST, FIE_HTTP_PORT, FIE_AUTH_MODE,
                "FIE_AUTH_TOKEN", "FIE_AUTH_PRINCIPAL",
                FIE_LOG_LEVEL, FIE_REQUEST_TIMEOUT,
                "FIE_DATABASE_URL", "FIE_SERVICE_ENV",
                # Phase 6.6R4: SQLite deployment access mode (ADR-013)
                "FIE_SQLITE_ACCESS_MODE",
            },
        )


if __name__ == "__main__":
    unittest.main()