"""Phase 6.7B-R1 — production configuration fail-closed (ADR-017).

Focused gate for the R1 slice of the accepted 6.7B kickoff:

* per-control positive/negative coverage for every changed knob
  (Gate A of the work order §9);
* the mandatory `FIE_SERVICE_ENV=prodution` typo regression — a typo
  must NEVER silently become `local`;
* staging/production fail-closed: missing/invalid required
  configuration refuses deterministically, with sanitized diagnostics;
* local/test compatibility: absent values keep the accepted safe
  defaults (behavior-unchanged proof, not an assumption);
* leak/secret markers (Gate E): no configuration value — including
  injected synthetic prohibited markers — can appear on any new error
  surface;
* negative startup tests (Gate D): the console entry point exits
  non-zero, emits only the stable refusal category, and nothing binds.

Scope boundary: R1 hardens configuration semantics only. The
authenticator architecture stays replaceable (R2 owns its redesign).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service.runtime_config import (  # noqa: E402
    ConfigurationError,
    load_runtime_config,
)
from phase3.transport.config import (  # noqa: E402
    PRODUCTION_LIKE_PROFILES,
    REFUSAL_CODES,
    VALID_PROFILES,
    load_transport_config,
)

_ENV_KEYS = (
    "FIE_SERVICE_ENV",
    "FIE_AUTH_MODE",
    "FIE_AUTH_TOKEN",
    "FIE_AUTH_PRINCIPAL",
    "FIE_DATABASE_URL",
    "FIE_SQLITE_ACCESS_MODE",
    "FIE_HTTP_HOST",
    "FIE_HTTP_PORT",
    "FIE_LOG_LEVEL",
    "FIE_REQUEST_TIMEOUT",
    "FIE_LOG_FORMAT",
    "FIE_DATA_DIR",
    "FIE_CONFIG_DIR",
    "FIE_ARTIFACT_DIR",
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


def _clear_fie_env() -> dict[str, str | None]:
    return _env(**dict.fromkeys(_ENV_KEYS))


#: synthetic prohibited markers (Gate E) — a configuration value that
#: contains them must never appear on the refusal surface
MARKER_TOKEN = "fie-r1-synthetic-bearer-marker"
MARKER_PATH = "/home/r1-synthetic-secret-dir"
MARKER_DSN = "postgres://r1:synthetic-pass-word@db.internal/fie"


class TestServiceEnvProfileVocabulary(unittest.TestCase):
    """FIE_SERVICE_ENV: the mandatory profile matrix (Gate A)."""

    def test_all_valid_profiles_accept_and_normalize(self) -> None:
        for profile, canonical in [
            ("local", "local"), ("test", "test"),
            ("staging", "staging"), ("production", "production"),
            ("Staging", "staging"), ("Prod", None),
        ]:
            with self.subTest(profile=profile):
                if canonical is None:
                    # near-miss spellings refuse ("Prod" is not a profile)
                    saved = _env(FIE_SERVICE_ENV=profile,
                                 FIE_DATABASE_URL="sqlite:///tmp/r1-profile.db")
                    try:
                        with self.assertRaises(ConfigurationError) as cm:
                            load_transport_config()
                        self.assertEqual(cm.exception.code, "UNKNOWN_SERVICE_ENV")
                    finally:
                        _restore(saved)
                    continue
                saved = _env(FIE_SERVICE_ENV=profile,
                             FIE_AUTH_MODE="token",
                             FIE_AUTH_TOKEN="r1-fixture-token",
                             FIE_DATABASE_URL="sqlite:///tmp/r1-profile.db")
                try:
                    self.assertEqual(
                        load_transport_config().service_env, canonical
                    )
                finally:
                    _restore(saved)

    def test_absent_keeps_local_default(self) -> None:
        saved = _env(FIE_SERVICE_ENV=None)
        try:
            self.assertEqual(load_transport_config().service_env, "local")
        finally:
            _restore(saved)

    def test_vocabulary_is_exactly_the_contract(self) -> None:
        self.assertEqual(VALID_PROFILES, ("local", "test", "staging", "production"))
        self.assertEqual(PRODUCTION_LIKE_PROFILES, ("staging", "production"))

    def test_typo_prodution_never_becomes_local(self) -> None:
        # MANDATORY regression (work order §9): `prodution` (typo) must
        # refuse startup — it must never silently become `local`.
        saved = _env(**{"FIE_SERVICE_ENV": "prodution",
                        "FIE_DATABASE_URL": "sqlite:///tmp/x.db"})
        try:
            with self.assertRaises(ConfigurationError) as cm:
                load_transport_config()
            self.assertEqual(cm.exception.code, "UNKNOWN_SERVICE_ENV")
            # and the runtime config refuses with the same category
            with self.assertRaises(ConfigurationError):
                load_runtime_config()
        finally:
            _restore(saved)

    def test_unknown_profile_values_refuse(self) -> None:
        for bad in ("foo", "prod", "dev", "staging/production"):
                saved = _env(FIE_SERVICE_ENV=bad)
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    self.assertEqual(cm.exception.code, "UNKNOWN_SERVICE_ENV")
                finally:
                    _restore(saved)

    def test_profiles_normalize_case_but_never_spell(self) -> None:
        # case folding is allowed; any other spelling refuses
        saved = _env(FIE_SERVICE_ENV="Prodution")
        try:
            with self.assertRaises(ConfigurationError):
                load_transport_config()
        finally:
            _restore(saved)


class TestProductionFailClosed(unittest.TestCase):
    """staging/production refuse missing/invalid required config."""

    _ABS_SQLITE = "sqlite:///tmp/fie-r1-absolute-test.db"

    def _prod(self, **vars):
        return _env(**{"FIE_SERVICE_ENV": "production", **vars})

    def test_auth_mode_none_forbidden(self) -> None:
        for profile in ("staging", "production"):
            with self.subTest(profile=profile):
                saved = self._prod(FIE_SERVICE_ENV=profile,
                                   FIE_AUTH_MODE="none",
                                   FIE_DATABASE_URL=self._ABS_SQLITE)
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    self.assertEqual(
                        cm.exception.code, "AUTH_MODE_FORBIDDEN_IN_PRODUCTION"
                    )
                finally:
                    _restore(saved)

    def test_invalid_auth_mode_refuses(self) -> None:
        saved = self._prod(FIE_AUTH_MODE="basik",
                           FIE_DATABASE_URL=self._ABS_SQLITE)
        try:
            with self.assertRaises(ConfigurationError) as cm:
                load_transport_config()
            self.assertEqual(cm.exception.code, "UNKNOWN_AUTH_MODE")
        finally:
            _restore(saved)

    def test_token_mode_requires_token(self) -> None:
        for token in (None, "", "   "):
            with self.subTest(token=(token is None, token or "")):
                saved = self._prod(FIE_AUTH_MODE="token", FIE_AUTH_TOKEN=token,
                                   FIE_DATABASE_URL=self._ABS_SQLITE)
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    self.assertEqual(cm.exception.code, "AUTH_CREDENTIAL_MISSING")
                finally:
                    _restore(saved)

    def test_valid_production_config_resolves(self) -> None:
        saved = self._prod(FIE_AUTH_MODE="token", FIE_AUTH_TOKEN="r1-fixture-token",
                           FIE_DATABASE_URL="postgresql://u:r1pw@db.example/fie")
        try:
            config = load_transport_config()
            self.assertEqual(config.auth_mode, "token")
            self.assertEqual(config.service_env, "production")
            self.assertNotIn("r1pw", json.dumps(config.to_dict()))
        finally:
            _restore(saved)

    def test_missing_database_url_refuses(self) -> None:
        # HP-04: the portable CWD-relative SQLite default is forbidden
        # under staging/production — absence refuses with a stable code.
        for profile in ("staging", "production"):
            saved = self._prod(FIE_SERVICE_ENV=profile,
                               FIE_AUTH_MODE="token",
                               FIE_AUTH_TOKEN="r1-fixture-token",
                               FIE_DATABASE_URL=None)
            with self.subTest(profile=profile):
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    self.assertEqual(cm.exception.code, "DATABASE_URL_MISSING")
                finally:
                    _restore(saved)

    def test_empty_database_url_refuses_as_missing(self) -> None:
        saved = self._prod(FIE_AUTH_MODE="token", FIE_AUTH_TOKEN="t",
                           FIE_DATABASE_URL="   ")
        try:
            with self.assertRaises(ConfigurationError) as cm:
                load_transport_config()
            self.assertEqual(cm.exception.code, "DATABASE_URL_MISSING")
        finally:
            _restore(saved)

    def test_cwd_relative_sqlite_dsn_refuses(self) -> None:
        # A relative SQLite path under staging/production is exactly
        # the CWD-dependence HP-04 forbids. The DSN-shape gate lives at
        # the composition root (run_server — the one module permitted
        # to resolve a full spec; the frozen 6.7A import surface is
        # preserved) and refuses BEFORE anything opens or binds.
        from phase3.transport.http import run_server

        for bad in ("intelligence.db", "sqlite:relative/x.db", "./data/loc.db"):
            with self.subTest(bad=bad):
                saved = self._prod(FIE_AUTH_MODE="token",
                                   FIE_AUTH_TOKEN="r1-fixture-token",
                                   FIE_DATABASE_URL=bad)
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        run_server(load_transport_config())
                    self.assertEqual(
                        cm.exception.code, "DATABASE_URL_INVALID"
                    )
                finally:
                    _restore(saved)

    def test_local_relative_sqlite_dsn_still_works(self) -> None:
        # local/test compatibility: only production-like profiles gate
        # DSN shape; the portable default and relative paths stay legal
        # where the contract permits them.
        from phase3.transport.config import TransportConfig

        saved = _env(FIE_SERVICE_ENV="local", FIE_DATABASE_URL="r1-relative.db")
        try:
            config = load_transport_config()
            self.assertIsInstance(config, TransportConfig)
            self.assertEqual(config.service_env, "local")
        finally:
            _restore(saved)

    def test_absolute_sqlite_and_pg_dsns_resolve(self) -> None:
        for dsn in (self._ABS_SQLITE,
                    "postgresql://u:p@db.example:5432/fie"):
            with self.subTest(dsn=dsn):
                saved = self._prod(FIE_AUTH_MODE="token",
                                   FIE_AUTH_TOKEN="r1-fixture-token",
                                   FIE_DATABASE_URL=dsn)
                try:
                    config = load_transport_config()
                    self.assertEqual(config.service_env, "production")
                finally:
                    _restore(saved)


class TestLocalTestCompatibility(unittest.TestCase):
    """Gate B: absent values keep every accepted local/test default."""

    def test_absent_values_keep_accepted_defaults(self) -> None:
        saved = _clear_fie_env()
        try:
            config = load_transport_config()
            self.assertEqual(config.host, "127.0.0.1")  # never a public bind
            self.assertEqual(config.port, 8787)
            self.assertEqual(config.auth_mode, "none")
            self.assertEqual(config.log_level, "INFO")
            self.assertEqual(config.request_timeout, 60.0)
            self.assertEqual(config.sqlite_access_mode, "writable")
            self.assertEqual(config.service_env, "local")
            self.assertEqual(config.runtime["database_source"], "default")
            self.assertEqual(config.runtime["log_format"], "structured")
        finally:
            _restore(saved)

    def test_local_explicit_none_auth_still_valid(self) -> None:
        # the dev/test anonymous mode remains a deliberate local choice
        saved = _env(FIE_AUTH_MODE="none", FIE_DATABASE_URL="sqlite:///tmp/r1.db")
        try:
            config = load_transport_config()
            self.assertEqual(config.auth_mode, "none")
        finally:
            _restore(saved)

    def test_test_profile_behaves_like_local(self) -> None:
        saved = _env(FIE_SERVICE_ENV="test", FIE_AUTH_MODE=None,
                     FIE_DATABASE_URL=None)
        try:
            config = load_transport_config()
            self.assertEqual(config.auth_mode, "none")  # dev default kept
            self.assertEqual(config.runtime["database_source"], "default")
        finally:
            _restore(saved)

    def test_invalid_values_refuse_in_local_too(self) -> None:
        # determinism is not profile-scoped: a mistyped value refuses
        # everywhere. Only *absent* values fall back (ADR-017).
        cases = [
            ({"FIE_AUTH_MODE": "basik"}, "UNKNOWN_AUTH_MODE"),
            ({"FIE_LOG_LEVEL": "verbose"}, "INVALID_LOG_LEVEL"),
            ({"FIE_HTTP_PORT": "nope"}, "INVALID_HTTP_PORT"),
            ({"FIE_REQUEST_TIMEOUT": "0"}, "INVALID_REQUEST_TIMEOUT"),
            ({"FIE_SQLITE_ACCESS_MODE": "writeable"}, "UNKNOWN_ACCESS_MODE"),
        ]
        for vars, code in cases:
            with self.subTest(vars=vars):
                saved = _env(**vars)  # type: ignore[arg-type]
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    self.assertEqual(cm.exception.code, code)
                finally:
                    _restore(saved)


class TestSqliteAccessModeVocabulary(unittest.TestCase):
    def test_vocabulary_matches_store_contract(self) -> None:
        from phase3.persistence.sqlite import ACCESS_MODES

        from phase3.transport import config as transport_config

        self.assertEqual(
            transport_config.VALID_ACCESS_MODES, tuple(ACCESS_MODES)
        )

    def test_valid_modes_resolve(self) -> None:
        for mode in ("writable", "readonly", "immutable_snapshot"):
            with self.subTest(mode=mode):
                saved = _env(FIE_SQLITE_ACCESS_MODE=mode.upper())
                try:
                    self.assertEqual(
                        load_transport_config().sqlite_access_mode, mode
                    )
                finally:
                    _restore(saved)

    def test_invalid_modes_refuse(self) -> None:
        for bad in ("writeable", "read-only", "", "IMMUTABLE"):
            with self.subTest(bad=bad):
                saved = _env(FIE_SQLITE_ACCESS_MODE=bad)
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    self.assertEqual(cm.exception.code, "UNKNOWN_ACCESS_MODE")
                finally:
                    _restore(saved)

    def test_absent_keeps_writable(self) -> None:
        saved = _env(FIE_SQLITE_ACCESS_MODE=None)
        try:
            self.assertEqual(load_transport_config().sqlite_access_mode, "writable")
        finally:
            _restore(saved)


class TestRefusalVocabulary(unittest.TestCase):
    """The refusal category surface is stable and closed."""

    def test_codes_stable_and_machine_testable(self) -> None:
        self.assertEqual(set(REFUSAL_CODES), {
            "UNKNOWN_SERVICE_ENV", "UNKNOWN_AUTH_MODE",
            "AUTH_MODE_FORBIDDEN_IN_PRODUCTION", "AUTH_CREDENTIAL_MISSING",
            "DATABASE_URL_MISSING", "DATABASE_URL_INVALID",
            "UNKNOWN_ACCESS_MODE", "INVALID_HTTP_PORT",
            "INVALID_LOG_LEVEL", "INVALID_REQUEST_TIMEOUT",
        })

    def test_error_message_never_carries_the_value(self) -> None:
        # Gate E core property: refusal messages name the variable and
        # category only — a hostile or secret-bearing value cannot
        # enter the diagnostic.
        for vars in (
            {"FIE_AUTH_MODE": f"token-{MARKER_TOKEN}"},
            {"FIE_LOG_LEVEL": MARKER_TOKEN},
            {"FIE_HTTP_PORT": f"8{MARKER_TOKEN}7"},
            {"FIE_REQUEST_TIMEOUT": MARKER_TOKEN},
            {"FIE_SQLITE_ACCESS_MODE": MARKER_TOKEN},
            {"FIE_SERVICE_ENV": MARKER_TOKEN},
        ):
            with self.subTest(vars=vars):
                saved = _env(**vars)  # type: ignore[arg-type]
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_transport_config()
                    blob = f"{cm.exception}"
                    self.assertNotIn(MARKER_TOKEN, blob)
                    self.assertNotIn("synthetic-pass-word", blob)
                    self.assertNotIn(MARKER_PATH, blob)
                finally:
                    _restore(saved)

    def test_dsn_marker_never_reaches_the_refusal(self) -> None:
        # Gate E, DSN form: a production-invalid DSN carrying synthetic
        # prohibited markers refuses — and the refusal (message,
        # stderr path) never carries the marker.
        from phase3.transport.http import run_server

        saved = _env(FIE_SERVICE_ENV="production",
                     FIE_AUTH_MODE="token",
                     FIE_AUTH_TOKEN="r1-fixture-token",
                     FIE_DATABASE_URL="relative-db-" + MARKER_TOKEN)
        try:
            with self.assertRaises(ConfigurationError) as cm:
                run_server(load_transport_config())
            blob = f"{cm.exception}"
            self.assertNotIn(MARKER_TOKEN, blob)
            self.assertNotIn("relative-db-", blob)
        finally:
            _restore(saved)

    def test_composition_root_gate_runs_before_binding(self) -> None:
        # Gate D (in-process): no partial service — the refusal raises
        # from run_server and nothing opens or binds.
        from phase3.transport.http import run_server

        saved = _env(FIE_SERVICE_ENV="production",
                     FIE_AUTH_MODE="token",
                     FIE_AUTH_TOKEN="r1-fixture-token",
                     FIE_DATABASE_URL="sqlite:relative/not-abs.db")
        try:
            with self.assertRaises(ConfigurationError) as cm:
                run_server(load_transport_config())
            self.assertEqual(cm.exception.code, "DATABASE_URL_INVALID")
        finally:
            _restore(saved)


class TestStartupRefusalSubprocess(unittest.TestCase):
    """Gate D: `python -m phase3.transport.http` refuses start-up."""

    def _run(self, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "phase3.transport.http"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
            env=env,
        )

    def test_production_missing_db_refuses_nonzero_no_bind(self) -> None:
        proc = self._run({"FIE_SERVICE_ENV": "production",
                          "FIE_AUTH_MODE": "token",
                          "FIE_AUTH_TOKEN": "r1-fixture-token",
                          "FIE_HTTP_PORT": "8931"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DATABASE_URL_MISSING", proc.stderr)
        self.assertNotIn("r1-fixture-token", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self._assert_no_listener(8931)

    def test_typo_prodution_refuses_nonzero(self) -> None:
        proc = self._run({"FIE_SERVICE_ENV": "prodution",
                          "FIE_AUTH_MODE": "token",
                          "FIE_AUTH_TOKEN": "r1-fixture-token",
                          "FIE_DATABASE_URL": "sqlite:///tmp/r1.db"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("UNKNOWN_SERVICE_ENV", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self._assert_no_listener(8931)

    def test_production_auth_none_refuses(self) -> None:
        proc = self._run({"FIE_SERVICE_ENV": "production",
                          "FIE_AUTH_MODE": "none",
                          "FIE_DATABASE_URL": "sqlite:///tmp/r1.db"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("AUTH_MODE_FORBIDDEN_IN_PRODUCTION", proc.stderr)
        self._assert_no_listener(8931)

    def test_refusal_output_is_pure_json(self) -> None:
        proc = self._run({"FIE_SERVICE_ENV": "production",
                          "FIE_AUTH_MODE": "basik",
                          "FIE_AUTH_TOKEN": "r1-fixture-token",
                          "FIE_DATABASE_URL": "sqlite:///tmp/r1.db"})
        self.assertNotEqual(proc.returncode, 0)
        line = proc.stderr.strip().splitlines()[-1]
        payload = json.loads(line)
        self.assertEqual(payload["error"]["code"], "UNKNOWN_AUTH_MODE")
        self.assertIn("FIE_AUTH_MODE", payload["error"]["message"])
        # the withheld value never appears on stdout either
        self.assertNotIn("r1-fixture-token", proc.stdout)
        self.assertNotIn("basik", proc.stderr)

    def test_invalid_dsn_marker_absent_from_stderr(self) -> None:
        # Gate E: the synthetic prohibited marker injected into a
        # production-invalid DSN never reaches stderr/stdout (Gates D+E)
        proc = self._run({"FIE_SERVICE_ENV": "production",
                          "FIE_AUTH_MODE": "token",
                          "FIE_AUTH_TOKEN": "r1-fixture-token",
                          "FIE_DATABASE_URL": "relative-db-" + MARKER_TOKEN})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DATABASE_URL_INVALID", proc.stderr)
        self.assertNotIn(MARKER_TOKEN, proc.stderr)
        self.assertNotIn(MARKER_TOKEN, proc.stdout)
        # sanitized refusal contract: never a raw traceback — even when
        # the refusal originates at the composition-root gate
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("r1-fixture-token", proc.stderr)

    def _assert_no_listener(self, port: int) -> None:
        """Nothing was bound: the refusal happened before any accept."""
        import socket

        with socket.socket() as probe:
            probe.settimeout(0.2)
            refused = probe.connect_ex(("127.0.0.1", port)) != 0
        self.assertTrue(
            refused, f"something is listening on port {port} after refusal"
        )


if __name__ == "__main__":
    unittest.main()