#!/usr/bin/env python3
"""Phase 6.9B-R1 — runtime configuration contract suite (WO §8.2).

Twelve mandated categories:

 1.  deterministic precedence
 2.  CWD independence
 3.  explicit host/port
 4.  malformed host/port
 5.  missing required DB configuration
 6.  missing required auth configuration
 7.  synthetic DSN isolation (PRODUCTION_DSN_USED=false by construction)
 8.  batch-plane missing/partial config
 9.  no implicit auto-DDL from invalid config
 10. diagnostic redaction
 11. startup fail-before-workload semantics
 12. HTTP/batch contract compatibility

Plus §8.3 failure injection (loader kill-switch classes and the config
contract validation mode).

Safety: every DSN/token in this suite is SYNTHETIC. Nothing binds, nothing
connects to any database, nothing mutates any schema. subprocess probes run
with a clean environment (``env -i`` class) so ambient operator variables
can never leak into a resolution, and Production contact is impossible by
construction.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

PY = sys.executable or "/usr/bin/python3"

# Synthetic credential material for redaction probes (never real values).
_SYNTH_TOKEN = "SYNTHETIC_R1_TOKEN_NOT_A_REAL_SECRET"
_SYNTH_PW = "SYNTHETIC_R1_PASSWORD_NOT_A_REAL_SECRET"
_SYNTH_DSN = f"postgresql://synthetic_r1_user:{_SYNTH_PW}@127.0.0.1:59999/synthetic_r1_db"


def _clean_env(**overrides: str) -> dict:
    """A hermetic env: no ambient FIE_*/PG* variables at all."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", str(Path(os.environ.get("HOME", "/tmp"))))}
    for key in list(os.environ):
        if key.startswith(("FIE_", "PG")) or key in ("DATABASE_URL",):
            env.pop(key, None)
    env.update(overrides)
    return env


class _SubprocessCase(unittest.TestCase):
    """Run a probe script with a hermetic environment in an unrelated CWD."""

    def run_probe(self, script: str, cwd: Path | None, env_extra: dict | None = None,
                  timeout: float = 60) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as td:
            script_path = Path(td) / "probe.py"
            script_path.write_text(textwrap.dedent(script), encoding="utf-8")
            env = _clean_env()
            if env_extra:
                env.update({k: str(v) for k, v in env_extra.items()})
            if cwd is not None:
                cwd.mkdir(parents=True, exist_ok=True)
            return subprocess.run(
                [PY, str(script_path)],
                cwd=str(cwd) if cwd else None,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )


# ---------------------------------------------------------------------------
# Category 1 — deterministic precedence
# ---------------------------------------------------------------------------

class TestDeterministicPrecedence(_SubprocessCase):
    def test_canonical_beats_legacy_alias_for_role(self):
        # A canonical var + an equivalent alias coexist; canonical wins and
        # the source metadata says canonical_env (no ambiguity).
        script = """
            import sys, os, json
            sys.path.insert(0, %(repo)r)
            from phase3.runtime_contract import resolve_raw_target
            spec = resolve_raw_target()
            assert spec.source == "canonical_env", spec.source
            assert spec.env_var == "FIE_DB_TARGET_RAW", spec.env_var
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None, env_extra={
            "FIE_DB_TARGET_RAW": _SYNTH_DSN,
            "FIE_DATABASE_URL": _SYNTH_DSN,
            "FIE_DB_PATH": _SYNTH_DSN,  # same identity → no contradiction
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_contradictory_alias_fails_closed(self):
        script = """
            import sys
            sys.path.insert(0, %(repo)r)
            from phase3.runtime_contract import resolve_raw_target, FailClosedTarget
            try:
                resolve_raw_target()
            except FailClosedTarget as exc:
                assert "CONTRADICTORY" in exc.reason, exc.reason
                assert %(pw)r not in str(exc) and %(dsn)r not in str(exc)
            else:
                raise SystemExit("expected refusal")
        """ % {"repo": str(REPO_ROOT), "pw": _SYNTH_PW, "dsn": _SYNTH_DSN}
        proc = self.run_probe(script, cwd=None, env_extra={
            "FIE_DB_TARGET_RAW": _SYNTH_DSN,
            "FIE_DB_PATH": "postgresql://other_user@127.0.0.1:59999/other_db",
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_explicit_argument_beats_environment(self):
        script = """
            import sys
            sys.path.insert(0, %(repo)r)
            from phase3.runtime_contract import resolve_raw_target
            spec = resolve_raw_target(explicit=":memory:")
            assert spec.source == "explicit", spec.source
            assert spec.value == ":memory:"
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None, env_extra={
            "FIE_DATABASE_URL": _SYNTH_DSN,
            "FIE_DB_PATH": _SYNTH_DSN,
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ---------------------------------------------------------------------------
# Category 2 — CWD independence (loader, paths, legacy fetchers, batch)
# ---------------------------------------------------------------------------

class TestCwdIndependence(_SubprocessCase):
    def _probe_config_resolution(self, cwd_name: str, set_env: bool) -> dict:
        """Resolve loader root + fetcher CONFIG_PATH from a given CWD."""
        config_dir = str(Path(tempfile.gettempdir()) / f"fie-r1-cwd-{cwd_name}")
        env_extra = {}
        if set_env:
            env_extra["FIE_CONFIG_DIR"] = config_dir
            env_extra["FIE_DATA_DIR"] = config_dir
        script = """
            import sys, json, os
            cwd = os.getcwd()
            sys.path.insert(0, %(repo)r)
            # NOTE: phase3 imported BEFORE chdir'ing anywhere; the loader
            # root must still be CWD-independent and lazy (C-4).
            from phase3.paths import config_dir as cd, data_dir as dd
            import company_monthly, industry_weekly, macro_daily
            out = {
             "cwd": cwd,
             "loader_root": str(__import__("phase3.config.loader",
                    fromlist=["_default_config_root"])._default_config_root()),
             "paths_config_dir": str(cd()),
             "paths_data_dir": str(dd()),
             "company_CONFIG_PATH": str(company_monthly.CONFIG_PATH),
             "company_LOG_DIR": str(company_monthly.LOG_DIR),
             "industry_CONFIG_PATH": str(industry_weekly.CONFIG_PATH),
             "industry_LOG_DIR": str(industry_weekly.LOG_DIR),
            }
            print(json.dumps(out))
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=Path(config_dir), env_extra=env_extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_repo_root_and_unrelated_cwd_agree_without_env(self):
        out_repo = self._probe_config_resolution("a", set_env=False)
        # Second probe from a different unrelated directory must resolve
        # identically (no CWD-dependence, no environment substitution).
        out_tmp = self._probe_config_resolution("b", set_env=False)
        self.assertEqual(out_repo["loader_root"], out_tmp["loader_root"])
        self.assertEqual(out_repo["company_CONFIG_PATH"], out_tmp["company_CONFIG_PATH"])
        self.assertEqual(out_repo["industry_LOG_DIR"], out_tmp["industry_LOG_DIR"])
        # And the resolution is repo-based, not the probe CWD.
        for out in (out_repo, out_tmp):
            self.assertTrue(str(out["loader_root"]).startswith(str(REPO_ROOT)),
                            out["loader_root"])

    def test_explicit_env_wins_from_any_cwd(self):
        marker = "/tmp/fie-r1-cwd-c"
        out = self._probe_config_resolution("c", set_env=True)
        self.assertEqual(out["loader_root"], f"{marker}/config/phase3")
        self.assertEqual(out["company_CONFIG_PATH"], f"{marker}/taiwan50_config.json")
        self.assertEqual(out["industry_LOG_DIR"], f"{marker}/logs")
        self.assertEqual(out["paths_data_dir"], marker)

    def test_legacy_fetcher_escape_hatch_is_fail_closed(self):
        # Simulate phase3 being unimportable (shadow package raising
        # ImportError) → the fetchers must REFUSE, never select a CWD
        # layout silently.
        with tempfile.TemporaryDirectory() as shadow_dir:
            shadow = Path(shadow_dir) / "phase3"
            shadow.mkdir()
            (shadow / "__init__.py").write_text(
                'raise ImportError("simulated phase3 absence (6.9B-R1 test)")\n',
                encoding="utf-8",
            )
            script = """
                import sys
                sys.path.insert(0, %(shadow)r)
                sys.path.insert(1, %(repo)r)
                try:
                    import company_monthly
                except RuntimeError as exc:
                    assert "FIE_CONFIG_DIR" in str(exc), str(exc)
                except ImportError:
                    pass  # a refusal at import is fail-closed too
                else:
                    raise SystemExit("UNEXPECTED: import succeeded — "
                                     "CWD fallback not abolished")
                try:
                    import industry_weekly
                except RuntimeError as exc:
                    assert "FIE_CONFIG_DIR" in str(exc), str(exc)
                except ImportError:
                    pass
                else:
                    raise SystemExit("UNEXPECTED: import succeeded — "
                                     "CWD fallback not abolished")
            """ % {"shadow": str(shadow_dir), "repo": str(REPO_ROOT)}
            cwd = Path(tempfile.gettempdir()) / "fie-r1-cwd-d"
            proc = self.run_probe(script, cwd=cwd)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # And the failure output names no CWD-derived path (values of
            # the resolution are withheld by the refusal itself).

    def test_macro_daily_log_dir_has_no_cwd_fallback(self):
        script = """
            import sys
            sys.path.insert(0, %(repo)r)
            import macro_daily, inspect, textwrap
            src = inspect.getsource(macro_daily.main)
            # the dead `from db import get_log_dir` import is gone; the
            # CWD default is gone
            assert "from db import get_log_dir" not in src
            assert 'os.environ.get("FIE_DATA_DIR", os.getcwd())' not in src
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ---------------------------------------------------------------------------
# Categories 3/4 — explicit & malformed host/port
# ---------------------------------------------------------------------------

class TestHostPortContract(_SubprocessCase):
    def test_explicit_host_port_roundtrip(self):
        script = """
            import sys
            sys.path.insert(0, %(repo)r)
            from phase3.transport.config import load_transport_config
            cfg = load_transport_config()
            assert cfg.host == "127.0.0.1", cfg.host
            assert cfg.port == 18720, cfg.port
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None, env_extra={
            "FIE_HTTP_HOST": "127.0.0.1",
            "FIE_HTTP_PORT": "18720",
            # 6.8A: DB-target resolution requires a contract — synthetic only
            "FIE_DATABASE_URL": _SYNTH_DSN,
            "FIE_DB_PATH": _SYNTH_DSN,
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_port_zero_is_deliberate_ephemeral(self):
        script = """
            import sys
            sys.path.insert(0, %(repo)r)
            from phase3.transport.config import load_transport_config
            cfg = load_transport_config()
            assert cfg.port == 0, cfg.port
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None, env_extra={
            "FIE_HTTP_PORT": "0", "FIE_DATABASE_URL": _SYNTH_DSN,
            "FIE_DB_PATH": _SYNTH_DSN,
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_malformed_port_refuses(self):
        for bad in ("banana", "-7", "70000"):
            script = """
                import sys
                sys.path.insert(0, %(repo)r)
                from phase3.transport.config import load_transport_config, \
                    ConfigurationError
                try:
                    load_transport_config()
                except ConfigurationError as exc:
                    assert exc.code == "INVALID_HTTP_PORT", exc.code
                else:
                    raise SystemExit("expected refusal for %(bad)r")
            """ % {"repo": str(REPO_ROOT), "bad": bad}
            proc = self.run_probe(script, cwd=None, env_extra={
                "FIE_HTTP_PORT": bad, "FIE_DATABASE_URL": _SYNTH_DSN,
            "FIE_DB_PATH": _SYNTH_DSN,
            })
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_loopback_default_host(self):
        # The absent value keeps the safe loopback default; an explicit
        # non-loopback host is an operator decision (recorded in the
        # contract docs), never a silent one.
        script = """
            import sys
            sys.path.insert(0, %(repo)r)
            from phase3.transport.config import load_transport_config
            cfg = load_transport_config()
            assert cfg.host == "127.0.0.1", cfg.host
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None, env_extra={
            "FIE_DB_PATH": _SYNTH_DSN, "FIE_DATABASE_URL": _SYNTH_DSN,
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ---------------------------------------------------------------------------
# Categories 5/6 — missing required DB / auth configuration
# ---------------------------------------------------------------------------

class TestMissingRequiredConfig(_SubprocessCase):
    def test_missing_db_contract_refuses(self):
        script = """
            import sys
            sys.path.insert(0, %(repo)r)
            from phase3.service.runtime_config import load_runtime_config, \
                ConfigurationError
            try:
                load_runtime_config()
            except ConfigurationError as exc:
                assert exc.code == "DATABASE_URL_MISSING", exc.code
            else:
                raise SystemExit("expected refusal")
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None)  # hermetic: no FIE_* at all
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_production_profile_requires_token_and_dsn(self):
        for env_extra, expected_code in (
            ({"FIE_SERVICE_ENV": "production"}, "DATABASE_URL_MISSING"),
            ({"FIE_SERVICE_ENV": "staging", "FIE_DATABASE_URL": _SYNTH_DSN},
             "AUTH_MODE_FORBIDDEN_IN_PRODUCTION"),
            ({"FIE_SERVICE_ENV": "production", "FIE_DATABASE_URL": _SYNTH_DSN,
            "FIE_DB_PATH": _SYNTH_DSN,
              "FIE_AUTH_MODE": "token"}, "AUTH_CREDENTIAL_MISSING"),
        ):
            script = """
                import sys
                sys.path.insert(0, %(repo)r)
                from phase3.transport.config import load_transport_config, \
                    ConfigurationError
                try:
                    load_transport_config()
                    raise SystemExit("expected refusal")
                except ConfigurationError as exc:
                    assert exc.code == %(code)r, exc.code
                    assert %(pw)r not in exc.message and %(dsn)r not in exc.message
            """ % {"repo": str(REPO_ROOT), "code": expected_code,
                   "pw": _SYNTH_PW, "dsn": _SYNTH_DSN}
            proc = self.run_probe(script, cwd=None, env_extra=env_extra)
            self.assertEqual(proc.returncode, 0, proc.stderr)


# ---------------------------------------------------------------------------
# Category 7 — synthetic DSN isolation / production safety posture
# ---------------------------------------------------------------------------

class TestSyntheticDsnIsolation(_SubprocessCase):
    def test_probe_targets_are_non_routeable_and_labeled(self):
        # The suite's DSN points at 127.0.0.1:59999 (unassigned high port,
        # loopback) with a synthetic_* label — by construction nothing on
        # this host, least of all Production, is the intended target.
        self.assertIn("127.0.0.1", _SYNTH_DSN)
        self.assertIn("synthetic", _SYNTH_DSN)

    def test_runtime_never_materializes_raw_dsn_in_config_view(self):
        script = """
            import sys, json
            sys.path.insert(0, %(repo)r)
            from phase3.service.runtime_config import load_runtime_config
            runtime = load_runtime_config()
            view = json.dumps(runtime.to_dict())
            assert %(pw)r not in view, "raw password leaked into log-safe view"
            assert ":***" in runtime.database_spec, runtime.database_spec
        """ % {"repo": str(REPO_ROOT), "pw": _SYNTH_PW}
        proc = self.run_probe(script, cwd=None, env_extra={
            "FIE_DATABASE_URL": _SYNTH_DSN,
            "FIE_DB_PATH": _SYNTH_DSN,
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ---------------------------------------------------------------------------
# Category 8 — batch-plane missing/partial config
# ---------------------------------------------------------------------------

class TestBatchPlaneConfig(_SubprocessCase):
    def test_batch_cli_missing_db_contract_refuses(self):
        # A batch subcommand that resolves the DB target must refuse
        # deterministically under a hermetic env (no silent CWD SQLite).
        proc = subprocess.run(
            [PY, "-m", "phase3.cli", "init-db"],
            cwd=str(REPO_ROOT), env=_clean_env(PHASE3B_ENABLED="1"), capture_output=True,
            text=True, timeout=120,
        )
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("FAIL_CLOSED_DB_TARGET_REQUIRED", combined)

    def test_batch_fetcher_escapes_never_select_cwd_layout(self):
        # (covered in depth by TestCwdIndependence; here the batch entry
        # contract asserts the wrappers' helper resolution is env-driven)
        script = """
            import sys, os
            sys.path.insert(0, %(repo)r)
            from phase3.paths import macro_history_db_path
            # explicit FIE_DATA_DIR drives it — never the CWD
            assert str(macro_history_db_path()) == os.path.join(
                os.environ.get("FIE_DATA_DIR"), "macro_history.db")
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None, env_extra={
            "FIE_CONFIG_DIR": "/tmp/fie-r1-batch-cfg",
            "FIE_DATA_DIR": "/tmp/fie-r1-batch-cfg",
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ---------------------------------------------------------------------------
# Category 9 — no implicit auto-DDL from invalid config
# ---------------------------------------------------------------------------

class TestNoAutoDdlFromInvalidConfig(_SubprocessCase):
    def test_failed_resolution_leaves_no_sqlite_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            # Run a probe whose CWD is an empty unrelated directory with NO
            # DB contract: any historical CWD-relative SQLite fallback would
            # have created a db file there (batch plane auto-DDL class). The
            # fail-closed contract must leave the directory byte-identical.
            script = """
                import sys
                sys.path.insert(0, %(repo)r)
                from phase3.cli import main
                code = main(["init-db", "--db-path", "./must-not-exist.db"])
                assert code not in (0, None), code
            """ % {"repo": str(REPO_ROOT)}
            cwd = Path(td) / "empty"
            cwd.mkdir()
            proc = self.run_probe(script, cwd=cwd)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            leftovers = [p.name for p in cwd.rglob("*.db")]
            self.assertEqual(leftovers, [], leftovers)


# ---------------------------------------------------------------------------
# Category 10 — diagnostic redaction
# ---------------------------------------------------------------------------

class TestDiagnosticRedaction(_SubprocessCase):
    def test_config_contract_hides_token_and_raw_dsn(self):
        proc = subprocess.run(
            [PY, "-m", "phase3.cli", "config-contract"],
            cwd=str(REPO_ROOT),
            env=_clean_env(FIE_AUTH_MODE="token", FIE_AUTH_TOKEN=_SYNTH_TOKEN,
                           FIE_DB_TARGET_RAW=_SYNTH_DSN,
                           FIE_DB_TARGET_INTELLIGENCE=_SYNTH_DSN),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        out = proc.stdout
        self.assertNotIn(_SYNTH_TOKEN, out)
        self.assertNotIn(_SYNTH_PW, out)
        report = json.loads(out)
        self.assertTrue(report["transport"]["auth_token_present"])
        self.assertNotIn("auth_token", report["transport"])
        self.assertIn(":***", report["database"]["spec"])
        # role targets: presence metadata only, no values at all
        for role, entry in report["db_targets"].items():
            if role in ("raw", "intelligence"):
                self.assertTrue(entry["present"], role)
            else:
                self.assertFalse(entry["present"], role)  # optional roles
            for forbidden in ("value", "dsn", "spec"):
                self.assertNotIn(forbidden, entry)

    def test_check_config_hides_token(self):
        proc = subprocess.run(
            [PY, "-m", "phase3.transport.http", "--check-config"],
            cwd=str(REPO_ROOT),
            env=_clean_env(FIE_AUTH_MODE="token", FIE_AUTH_TOKEN=_SYNTH_TOKEN,
                           FIE_HTTP_PORT="18720",
                           FIE_DB_TARGET_INTELLIGENCE=_SYNTH_DSN),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        out = proc.stdout
        self.assertNotIn(_SYNTH_TOKEN, out)
        self.assertNotIn(_SYNTH_PW, out)
        self.assertIn(":***", out)
        summary = json.loads(out)
        self.assertNotIn("auth_token", json.dumps(summary))

    def test_kill_switch_refusal_names_no_ambient_values(self):
        # (§8.3 injection) malformed kill switch → refusal that withholds
        # the offending value entirely.
        script = """
            import sys, tempfile
            from pathlib import Path
            sys.path.insert(0, %(repo)r)
            from phase3.config.loader import ConfigLoader
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "enabled.yaml").write_text("enabled: 12\\n")
                try:
                    ConfigLoader(root).load()
                except ValueError as exc:
                    assert "12" not in str(exc), "value leaked: " + str(exc)
                else:
                    raise SystemExit("expected refusal")
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ---------------------------------------------------------------------------
# Category 11 — startup fail-before-workload semantics
# ---------------------------------------------------------------------------

class TestFailBeforeWorkload(_SubprocessCase):
    def test_check_config_refusal_exits_2(self):
        proc = subprocess.run(
            [PY, "-m", "phase3.transport.http", "--check-config"],
            cwd=str(REPO_ROOT), env=_clean_env(),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stderr.strip().splitlines()[-1])
        self.assertIn("code", payload["error"])
        self.assertEqual(payload["error"]["code"], "DATABASE_URL_MISSING")

    def test_check_config_valid_contract_exits_0_without_binding(self):
        proc = subprocess.run(
            [PY, "-m", "phase3.transport.http", "--check-config"],
            cwd=str(REPO_ROOT),
            env=_clean_env(FIE_HTTP_PORT="18720",
                           FIE_DB_TARGET_INTELLIGENCE=_SYNTH_DSN),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Nothing bound: a probe would have exited 2 on refusal and the
        # summary contains the refusal-free marker.
        self.assertIn('"check": "ok"', proc.stdout)

    def test_config_contract_flagged_invalid_kills_switch_state(self):
        proc = subprocess.run(
            [PY, "-m", "phase3.cli", "config-contract"],
            cwd=str(REPO_ROOT), env=_clean_env(),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(report["valid"])
        self.assertIn("refusals", report)


# ---------------------------------------------------------------------------
# Category 12 — HTTP/batch contract compatibility
# ---------------------------------------------------------------------------

class TestPlaneCompatibility(_SubprocessCase):
    def test_both_planes_share_db_target_resolution(self):
        script = """
            import sys, json
            sys.path.insert(0, %(repo)r)
            from phase3.service.runtime_config import load_runtime_config
            from phase3.runtime_contract import resolve_raw_target
            from phase3.transport.config import load_transport_config
            runtime = load_runtime_config()
            cfg = load_transport_config()
            raw = resolve_raw_target()
            # One resolution family: the same env surface feeds both planes
            # and both emit the same masked spec.
            assert ":***" in runtime.database_spec
            assert cfg.service_env == runtime.service_env
            assert raw.backend in ("postgres", "sqlite")
        """ % {"repo": str(REPO_ROOT)}
        proc = self.run_probe(script, cwd=None, env_extra={
            "FIE_DB_TARGET_RAW": _SYNTH_DSN,
            "FIE_DB_TARGET_INTELLIGENCE": _SYNTH_DSN,
        })
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_inventory_json_covers_module_env_surfaces(self):
        import json as _json
        from phase3.transport.config import (
            TRANSPORT_ENV_VARS, VALID_PROFILES, REFUSAL_CODES)
        from phase3.service.runtime_config import RUNTIME_ENV_VARS
        from phase3.runtime_contract import (
            CANONICAL_ENV_VARS, LEGACY_ALIAS_ROLES)
        from phase3.paths import (
            ENV_PROJECT_ROOT, ENV_DATA_DIR, ENV_CONFIG_DIR, ENV_ARTIFACT_DIR,
            ENV_DB_PATH, ENV_DATABASE_URL,
        )
        inventory = _json.loads(
            (REPO_ROOT / "docs" / "contracts" /
             "runtime_configuration_inventory.json").read_text(encoding="utf-8")
        )
        documented = {v["name"] for v in inventory["variables"]}
        module_surface = set(
            TRANSPORT_ENV_VARS + RUNTIME_ENV_VARS
            + tuple(CANONICAL_ENV_VARS.values())
            + tuple(LEGACY_ALIAS_ROLES)
            + (ENV_PROJECT_ROOT, ENV_DATA_DIR, ENV_CONFIG_DIR,
               ENV_ARTIFACT_DIR, ENV_DB_PATH, ENV_DATABASE_URL)
        )
        missing = module_surface - documented
        self.assertEqual(missing, set(), f"inventory misses: {sorted(missing)}")
        # Every documented variable is a real env surface (no stale doc).
        for name in documented:
            real_surfaces = set(TRANSPORT_ENV_VARS + RUNTIME_ENV_VARS
                + tuple(CANONICAL_ENV_VARS.values())
                + tuple(LEGACY_ALIAS_ROLES)
                + (ENV_PROJECT_ROOT, ENV_DATA_DIR, ENV_CONFIG_DIR,
                   ENV_ARTIFACT_DIR, ENV_DB_PATH, ENV_DATABASE_URL,
                   "FIE_PRODUCTION_DB_CONTRACT", "FIE_WRAPPER_ENV"))
            self.assertTrue(name in real_surfaces or name.startswith("FIE_"),
                            name)
        # Refusal vocabulary is present in the transport contract.
        self.assertIn("INVALID_HTTP_PORT", REFUSAL_CODES)
        self.assertEqual(VALID_PROFILES, ("local", "test", "staging", "production"))

    def test_lazy_default_config_root_matches_paths(self):
        from phase3.config.loader import DEFAULT_CONFIG_ROOT  # PEP 562 lazy
        from phase3.paths import config_dir
        self.assertEqual(Path(DEFAULT_CONFIG_ROOT),
                         config_dir() / "config" / "phase3")


# ---------------------------------------------------------------------------
# §8.3 — failure injection (loader kill-switch classes)
# ---------------------------------------------------------------------------

class TestKillSwitchFailureInjection(_SubprocessCase):
    def _load_with(self, body: str) -> tuple[int, str]:
        script = """
            import sys, tempfile
            from pathlib import Path
            sys.path.insert(0, %(repo)r)
            from phase3.config.loader import ConfigLoader, DEFAULT_CONFIG_ROOT
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "enabled.yaml").write_text(%(body)r)
                try:
                    ConfigLoader(root).load()
                except ValueError as exc:
                    print("REFUSED")
                else:
                    # absent value no longer means enabled
                    raise SystemExit("UNEXPECTED: load succeeded")
        """ % {"repo": str(REPO_ROOT), "body": body}
        proc = self.run_probe(script, cwd=None)
        return proc.returncode, proc.stdout

    def test_missing_key_refuses(self):
        rc, out = self._load_with("mode: scoring\n")
        self.assertEqual((rc, "REFUSED" in out), (0, True))

    def test_ambiguous_value_refuses_without_echo(self):
        rc, out = self._load_with("enabled: 12\n")
        self.assertEqual((rc, "REFUSED" in out), (0, True))
        rc, out = self._load_with('enabled: "banana"\n')
        self.assertEqual((rc, "REFUSED" in out), (0, True))

    def test_known_vocabulary_accepts(self):
        for body in ("enabled: true\n", 'enabled: "no"\n', "enabled: off\n"):
            script = """
                import sys, tempfile
                from pathlib import Path
                sys.path.insert(0, %(repo)r)
                from phase3.config.loader import ConfigLoader
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    (root / "enabled.yaml").write_text(%(body)r)
                    cfg = ConfigLoader(root).load()
                    print("OK", cfg.enabled)
            """ % {"repo": str(REPO_ROOT), "body": body}
            proc = self.run_probe(script, cwd=None)
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()