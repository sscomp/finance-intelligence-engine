"""Auth boundary tests (Phase 6.6 §6).

Covers: anonymous dev mode, bearer-token mode, missing/invalid
credentials rejected with 401, secrets never logged or echoed, and
fail-closed token configuration.
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service.contracts import SCHEMA_VERSION  # noqa: E402
from phase3.transport.auth import (  # noqa: E402
    FIE_AUTH_MODE,
    FIE_AUTH_PRINCIPAL,
    FIE_AUTH_TOKEN,
    NoopAuthenticator,
    _request_id_from_headers,
    build_authenticator,
)
from phase3.transport.config import load_transport_config  # noqa: E402
from phase3.transport.errors import TransportError, TransportErrorCode  # noqa: E402
from tests.phase3.transport.runtime_harness import RuntimeHarness  # noqa: E402


class Unit(unittest.TestCase):
    """Header-free unit tests — the Authenticator contract itself."""

    def test_noop_is_anonymous_read_scope(self) -> None:
        ctx = NoopAuthenticator().authenticate(None)
        self.assertEqual(ctx.principal_id, "anonymous")
        self.assertIn("intelligence:read", ctx.scopes)

    def test_missing_token_header_rejected(self) -> None:
        auth = build_authenticator("token", token="s3cret")
        with self.assertRaises(TransportError) as caught:
            auth.authenticate({})
        self.assertEqual(caught.exception.code.value, "UNAUTHENTICATED")

    def test_invalid_token_rejected(self) -> None:
        auth = build_authenticator("token", token="s3cret")
        with self.assertRaises(TransportError) as caught:
            auth.authenticate({"Authorization": "Bearer wrong"})
        self.assertEqual(caught.exception.code.value, "UNAUTHENTICATED")

    def test_valid_token_returns_configured_principal(self) -> None:
        auth = build_authenticator("token", token="s3cret", principal_id="acme")
        ctx = auth.authenticate({"Authorization": "Bearer s3cret"})
        self.assertEqual(ctx.principal_id, "acme")
        self.assertTrue(ctx.request_id)  # generated when header absent

    def test_case_insensitive_bearer_scheme(self) -> None:
        auth = build_authenticator("token", token="s3cret")
        ctx = auth.authenticate({"Authorization": "bearer s3cret"})
        self.assertEqual(ctx.principal_id, "service-consumer")

    def test_fail_closed_empty_token(self) -> None:
        # an empty configured token refuses to construct AT ALL — the
        # reference runtime can never come up silently accepting anyone
        with self.assertRaises(TransportError):
            build_authenticator("token", token="")

    def test_default_principal_from_env(self) -> None:
        import os
        saved = os.environ.get(FIE_AUTH_PRINCIPAL)
        os.environ[FIE_AUTH_PRINCIPAL] = "kiosk-terminal"
        try:
            ctx = build_authenticator("token", token="t").authenticate(
                {"Authorization": "Bearer t"}
            )
            self.assertEqual(ctx.principal_id, "kiosk-terminal")
        finally:
            if saved is None:
                os.environ.pop(FIE_AUTH_PRINCIPAL, None)
            else:
                os.environ[FIE_AUTH_PRINCIPAL] = saved

    def test_unknown_mode_raises_forbidden(self) -> None:
        with self.assertRaises(TransportError) as caught:
            build_authenticator("oauth2")
        self.assertEqual(caught.exception.code.value, "FORBIDDEN")

    def test_transport_code_isolated_from_service_taxonomy(self) -> None:
        # §5/§6: transport error codes are a separate UNAUTHENTICATED/
        # FORBIDDEN pair; the service taxonomy stays untouched
        self.assertEqual({c.value for c in TransportErrorCode},
                         {"UNAUTHENTICATED", "FORBIDDEN"})


class TestTokenModeOverHTTP(RuntimeHarness):
    """The 401 path end-to-end (auth happens before any dispatch)."""

    config_overrides = {
        "auth_mode": "token",
        "auth_token": "correct-horse-battery",  # noqa: S105 - test fixture
    }

    def test_missing_credentials_401(self) -> None:
        status, env, _ = self.get("/v1/health")
        self.assertEqual(status, 401)
        self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
        self.assertEqual(env["schema_version"], SCHEMA_VERSION)

    def test_invalid_credentials_401(self) -> None:
        status, env, _ = self.get(
            "/v1/health", headers={"Authorization": "Bearer nope"}
        )
        self.assertEqual(status, 401)

    def test_valid_credentials_pass_with_principal(self) -> None:
        status, env, _ = self.get(
            "/v1/health", headers={"Authorization": "Bearer correct-horse-battery"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(env["status"], "ok")

    def test_unauthenticated_request_never_reaches_domain(self) -> None:
        self.clear_records()
        self.get("/v1/intelligence/latest")  # no Authorization header
        # only the transport-side 401 record exists; no service dispatch
        # telemetry was emitted for the rejected request
        self.assertTrue(
            any(r.get("error_code") == "UNAUTHENTICATED"
                for r in self.records())
        )
        self.assertFalse(
            any(r.get("operation") == "latest_intelligence"
                for r in self.records())
        )
        # no credentials ever reach telemetry
        blob = json.dumps(self.records())
        self.assertNotIn("correct-horse-battery", blob)
        self.assertNotIn("Authorization", blob)

    def test_error_never_contains_token(self) -> None:
        _, env, _ = self.get(
            "/v1/health", headers={"Authorization": "Bearer correct-horse-b"}
        )
        blob = json.dumps(env, ensure_ascii=False)
        self.assertNotIn("correct-horse", blob)

    def test_token_never_serialised_by_config(self) -> None:
        config = load_transport_config(
            "sqlite:///tmp-none.db", auth_mode="token", auth_token="correct-horse-battery",
        )
        blob = json.dumps(config.to_dict())
        self.assertNotIn("correct-horse-battery", blob)
        self.assertNotIn("auth_token", blob)

    def test_env_precedence_for_auth_mode(self) -> None:
        import os
        saved = {k: os.environ.get(k) for k in (FIE_AUTH_MODE, FIE_AUTH_TOKEN)}
        os.environ[FIE_AUTH_MODE] = "token"
        os.environ[FIE_AUTH_TOKEN] = "from-env"
        try:
            config = load_transport_config()
            # explicit argument wins over env
            override = load_transport_config(auth_token="from-arg")
            self.assertEqual(config.auth_token, "from-env")
            self.assertEqual(override.auth_token, "from-arg")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()