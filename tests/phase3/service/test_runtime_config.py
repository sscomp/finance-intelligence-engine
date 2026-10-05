"""Runtime configuration contract (Phase 6.5 §10, §17 areas 8-11; 6.8A).

Precedence, validation, and secret-safety of the runtime config plus
the portability/secret audits:

* precedence: explicit argument > environment > (6.8A: NO implicit
  default — an absent DB target fails closed with
  ``DATABASE_URL_MISSING``; tests that need a target set a /tmp
  fixture explicitly);
* FIE_SERVICE_ENV / FIE_LOG_FORMAT validated with closed refuse-on-
  invalid semantics (an *absent* value keeps its accepted default);
* database DSN only ever materializes in masked (``:***``) form;
* no developer-specific absolute path inside ``phase3/service`` or the
  Phase 6.5 docs (§10/§20).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from phase3.service.runtime_config import (  # noqa: E402
    FIE_LOG_FORMAT,
    FIE_SERVICE_ENV,
    RUNTIME_ENV_VARS,
    load_runtime_config,
    redact,
)

# 6.8A: disposable (host-safe) SQLite target — absolute, /tmp, never a
# real or production-shaped DSN.
FIXTURE_TARGET = "/tmp/fie_runtime_cfg_fixture.db"


class TestPrecedenceAndValidation(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            k: os.environ.get(k) for k in RUNTIME_ENV_VARS
        }
        for k in RUNTIME_ENV_VARS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_absent_db_target_refuses_fail_closed(self) -> None:
        # Phase 6.8A: the portable CWD-relative SQLite default is
        # ABOLISHED — an absent target fails closed at load time.
        from phase3.service.runtime_config import ConfigurationError
        with self.assertRaises(ConfigurationError) as cm:
            load_runtime_config()
        self.assertEqual(cm.exception.code, "DATABASE_URL_MISSING")

    def test_explicit_target_resolves_with_local_defaults(self) -> None:
        cfg = load_runtime_config(FIXTURE_TARGET)
        self.assertEqual(cfg.database_source, "explicit")
        self.assertEqual(cfg.service_env, "local")
        self.assertEqual(cfg.log_format, "structured")
        self.assertTrue(Path(cfg.database_spec.split(":***")[0]).is_absolute()
                        or "file" in cfg.database_spec)

    def test_env_target_resolves(self) -> None:
        os.environ["FIE_DATABASE_URL"] = "/tmp/fie_env_override.db"
        cfg = load_runtime_config()
        self.assertEqual(cfg.database_source, "env")
        self.assertIn("fie_env_override", cfg.database_spec)

    def test_explicit_precedes_env(self) -> None:
        os.environ["FIE_DATABASE_URL"] = "/tmp/fie_env_override.db"
        cfg = load_runtime_config("/tmp/fie_explicit.db")
        self.assertEqual(cfg.database_source, "explicit")
        self.assertIn("fie_explicit", cfg.database_spec)

    def test_profile_and_log_format_fallbacks(self) -> None:
        # needs a DB target fixture — an *absent* target now refuses
        # before the return (refusal covered by its own test above).
        os.environ["FIE_DATABASE_URL"] = FIXTURE_TARGET
        os.environ[FIE_SERVICE_ENV] = "STAGING"
        cfg = load_runtime_config()
        self.assertEqual(cfg.service_env, "staging")
        self.assertEqual(cfg.log_format, "structured")  # absent → safe default

    def test_invalid_log_format_refuses(self) -> None:
        # Phase 6.7B-R4 (ADR-017 §7 ownership): the last silent
        # fallback is closed — an explicitly supplied invalid log
        # format refuses deterministically, it never becomes
        # "structured" (the pre-R4 behavior).
        from phase3.service.runtime_config import ConfigurationError
        os.environ["FIE_DATABASE_URL"] = FIXTURE_TARGET
        os.environ[FIE_LOG_FORMAT] = "bogus"
        with self.assertRaises(ConfigurationError) as cm:
            load_runtime_config()
        self.assertEqual(cm.exception.code, "INVALID_LOG_FORMAT")
        # the value never travels through the diagnostic
        self.assertNotIn("bogus", cm.exception.message)

    def test_invalid_service_env_refuses(self) -> None:
        # Phase 6.7B / ADR-017: an explicitly supplied invalid profile
        # never silently becomes `local` — it refuses deterministically.
        from phase3.service.runtime_config import ConfigurationError

        for bad in ("prodution", "foo", "prod", "PRODUCTION-x"):
            with self.subTest(bad=bad):
                os.environ[FIE_SERVICE_ENV] = bad
                try:
                    with self.assertRaises(ConfigurationError) as cm:
                        load_runtime_config()
                    self.assertEqual(cm.exception.code, "UNKNOWN_SERVICE_ENV")
                finally:
                    os.environ.pop(FIE_SERVICE_ENV, None)

    def test_absent_service_env_keeps_local_default(self) -> None:
        # Local compatibility boundary: absence still means `local`
        # (6.8A: with a DB target present — an absent *DB target* is the
        # separate fail-closed refusal tested above).
        os.environ.pop(FIE_SERVICE_ENV, None)
        os.environ["FIE_DATABASE_URL"] = FIXTURE_TARGET
        self.assertEqual(load_runtime_config().service_env, "local")

    def test_explicit_service_env_argument_beats_env_and_validates(self) -> None:
        from phase3.service.runtime_config import ConfigurationError

        os.environ["FIE_DATABASE_URL"] = FIXTURE_TARGET
        os.environ[FIE_SERVICE_ENV] = "test"
        try:
            cfg = load_runtime_config(service_env="production")
            self.assertEqual(cfg.service_env, "production")
            with self.assertRaises(ConfigurationError):
                load_runtime_config(service_env="typo")
        finally:
            os.environ.pop(FIE_SERVICE_ENV, None)


class TestSecretSafety(unittest.TestCase):
    def test_masked_dsn_only(self) -> None:
        secret = "postgresql:// fie:SuperSecret42@db.internal:5432/fie_db"
        cfg = load_runtime_config(secret.replace(" ", ""))
        blob = repr(cfg)
        self.assertNotIn("SuperSecret42", blob)
        self.assertIn(":***", cfg.database_spec)

    def test_to_dict_is_log_safe(self) -> None:
        cfg = load_runtime_config("postgresql://user:secretvalue@h/db")
        self.assertNotIn("secretvalue", str(cfg.to_dict()))

    def test_redact_scrubs_urls_and_paths(self) -> None:
        line = "failed to reach postgresql://u:pw@h/db under /tmp/secret/dir"
        scrubbed = redact(line)
        self.assertNotIn("pw@", scrubbed)
        self.assertNotIn("/tmp/secret", scrubbed)
        self.assertIn("<redacted>", scrubbed)

    def test_error_details_never_carry_raw_urls(self) -> None:
        from phase3.service.errors import sanitize_for_error

        self.assertEqual(
            sanitize_for_error("cannot open postgresql://u:pw@h/db"),
            "cannot open <redacted>",
        )


class TestPortabilityAudit(unittest.TestCase):
    """§10/§20: no developer-host specificity in the new surface."""

    # built dynamically so this file does not literally contain its own
    # audit markers (the markers must name *other* files only).
    FORBIDDEN_MARKERS = (
        "/home/" + "sscomp",
        "/home/" + "ubuntu",
        "aee-" + "runtime-bridge",
        "192.168" + ".",
        "macro-" + "report",
        ".venv-" + "pg",
        "fie" + "63-fresh",
    )

    TARGETS = [
        *sorted((REPO_ROOT / "phase3" / "service").glob("*.py")),
        *sorted((REPO_ROOT / "tests" / "phase3" / "service").glob("*.py")),
        *sorted((REPO_ROOT / "docs" / "architecture" / "phase6-5").glob("*.md")),
        *sorted((REPO_ROOT / "docs" / "adr").glob("adr-00[5-9]*.md")),
        REPO_ROOT / "docs" / "adr" / "adr-010-transport-decision.md",
    ]

    def test_no_developer_host_markers(self) -> None:
        for target in self.TARGETS:
            text = target.read_text(encoding="utf-8")
            for marker in self.FORBIDDEN_MARKERS:
                with self.subTest(file=target.name, marker=marker):
                    self.assertNotIn(marker, text)

    def test_runtime_env_surface_is_complete(self) -> None:
        self.assertEqual(
            set(RUNTIME_ENV_VARS),
            {"FIE_DATABASE_URL", "FIE_DATA_DIR", "FIE_CONFIG_DIR",
             "FIE_ARTIFACT_DIR", "FIE_SERVICE_ENV", "FIE_LOG_FORMAT"},
        )


if __name__ == "__main__":
    unittest.main()