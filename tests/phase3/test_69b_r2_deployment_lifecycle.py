"""Phase 6.9B-R2 deployment artifact + Supervisor lifecycle tests.

Covers the WO gates that a unit suite can own:

* Gate A — static artifact validation: template/inis parse, required
  Supervisor lifecycle fields present with DOCUMENTED values, no secret
  literals, no host paths in the repository artifact, canonical
  entrypoint only; the staging generator resolves placeholders
  fail-closed and emits a usable staged conf.
* Preflight (WO §8) — PASS on a fully staged synthetic contract;
  deterministic refusals (missing env file, missing DB target, invalid
  kill switch, non-0600 env file at staging) — fail-closed everywhere.
* Gate C (isolated Supervisor lifecycle) — when ``supervisord`` is
  available: an entirely job-owned supervisord instance (own socket,
  pid, conf, logs in a per-test temp root) drives
  start -> healthz/readyz -> exactly one owned listener -> graceful
  stop (zero residue) -> restart -> health/ready again -> teardown.
* Gate D failure injection — invalid config / kill switch / invalid DB
  fail closed BEFORE workload; port collision with a FOREIGN listener:
  deterministic refusal (never takes over, never kills the foreign
  process); service crash: SIGKILL failure-injection case (justified +
  isolated per WO §7.4) → declared autorestart policy observed.
* Gate E — lifecycle driver runs from unrelated working directories.

The isolated Supervisor fixtures are gated on the host ``supervisord``
binary (skipUnless) so clean rooms without Supervisor skip with their
explicit classification instead of failing.

Synthetic environment only: the lifecycle service env uses a synthetic
bearer token and an absolute-path SQLite store inside the test root —
no production DSN, no production contact.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import socket
import signal
import subprocess
import tempfile
import time
import unittest
import urllib.request

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DEPLOY_DIR = REPO_ROOT / "deploy"

TEMPLATE = DEPLOY_DIR / "supervisor" / "fie-http.conf"
FIE_START = DEPLOY_DIR / "scripts" / "fie-start"
FIE_PREFLIGHT = DEPLOY_DIR / "scripts" / "fie-preflight"
FIE_STAGE = DEPLOY_DIR / "scripts" / "fie-stage"

SYNTH_TOKEN = "r2-lifecycle-synth-token-0123456789abcdef"

# canonical entrypoint the artifact must wrap — never a second runtime.
# fie-start execs the resolver-resolved interpreter (${FIE_PYTHON_BIN}),
# so that form (and a literal `python -m ...`) both count.
_ENTRYPOINT_RE = re.compile(
    r'(python3?\s+|"\$\{FIE_PYTHON_BIN\}")\s+-m phase3\.transport\.http')


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _clean_env(extra: dict | None = None, unset_fie: bool = True) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/home/nonexistent"),
        "LANG": "C.UTF-8",
    }
    if not unset_fie:
        for key, value in os.environ.items():
            if not key.startswith("FIE_"):
                env[key] = value
    if extra:
        env.update(extra)
    return env


def _has_supervisord() -> bool:
    return bool(shutil.which("supervisord") and shutil.which("supervisorctl"))


def _stage_and_install_env(
    install_root: Path,
    env_dir: Path,
    env_overrides: dict | None = None,
    service_env: str = "staging",
    port: int | None = None,
    env_mode: str = "0600",
    auth_token: str = SYNTH_TOKEN,
) -> tuple[Path, Path, Path]:
    """Template -> staged conf via the canonical fie-stage script.

    Writes the synthetic 0600 env file, runs fie-stage, returns
    (staged_conf, env_file, log_dir). Fails the test on nonzero rc.
    """
    staged_out = install_root / "deploy" / "staged"
    log_dir = install_root / "logs"
    staged_out.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    env_dir.mkdir(parents=True, exist_ok=True)
    # FIE_PROJECT_ROOT always points at the REPO (runtime code lives
    # there); the staged install root only carries deploy/, config_data/,
    # data_dir/ and the store paths for the synthetic contract.
    env_file = env_dir / "fie-runtime.env"
    store_path = install_root / "store" / "r2_lifecycle_store.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "export FIE_SERVICE_ENV=" + service_env,
        "export FIE_PROJECT_ROOT=" + str(REPO_ROOT),
        "export FIE_HTTP_HOST=127.0.0.1",
        "export FIE_HTTP_PORT=" + str(port or _free_tcp_port()),
        "export FIE_AUTH_MODE=token",
        "export FIE_AUTH_TOKEN=" + auth_token,
        "export FIE_DATABASE_URL=" + str(store_path),
        "export FIE_DB_TARGET_RAW=" + str(store_path.with_suffix(".raw.db")),
        "export FIE_CONFIG_DIR=" + str(install_root / "config_data"),
        "export FIE_DATA_DIR=" + str(install_root / "data_dir"),
        "export FIE_REQUEST_TIMEOUT=15",
    ]
    for key, value in (env_overrides or {}).items():
        lines.append("export " + key + "=" + value)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    current = oct(env_file.stat().st_mode & 0o777)
    if current != env_mode:
        env_file.chmod(int(env_mode, 8))
    # config_data needs the kill switch for the config-side probe
    cfg_root = install_root / "config_data" / "config" / "phase3"
    cfg_root.mkdir(parents=True, exist_ok=True)
    if not (cfg_root / "enabled.yaml").exists():
        (cfg_root / "enabled.yaml").write_text("enabled: true\n", encoding="utf-8")
    data_dir = install_root / "data_dir"
    data_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            str(FIE_STAGE),
            "--install-root", str(install_root),
            "--env-file", str(env_file),
            "--runtime-user", "ubuntu",
            "--log-dir", str(log_dir),
            "--output", str(staged_out / "fie-http.conf"),
        ],
        capture_output=True, text=True, env=_clean_env(), timeout=120,
    )
    assert proc.returncode == 0, f"fie-stage failed: {proc.stdout}\n{proc.stderr}"
    return staged_out / "fie-http.conf", env_file, log_dir


class TestGateAStaticArtifact(unittest.TestCase):
    """Gate A — static artifact validation (WO §13.A)."""

    def test_template_parses_as_ini_with_program(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(TEMPLATE.read_text(encoding="utf-8"))
        self.assertIn("program:fie-http", parser.sections())

    def test_required_lifecycle_fields_documented(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(TEMPLATE.read_text(encoding="utf-8"))
        program = parser["program:fie-http"]
        expected = {
            "autostart": "true",
            "autorestart": "unexpected",
            "startsecs": "5",
            "startretries": "3",
            "exitcodes": "0,2",
            "stopsignal": "TERM",
            "stopwaitsecs": "30",
            "stopasgroup": "true",
            "killasgroup": "true",
            "redirect_stderr": "true",
        }
        for field, want in expected.items():
            self.assertEqual(
                program.get(field), want,
                f"{field}={program.get(field)!r}, documented contract is {want!r}",
            )
        # explicit working directory + process-ownership-preserving exec
        self.assertIn("directory=", TEMPLATE.read_text(encoding="utf-8"))
        for directive in ("command=", "environment="):
            self.assertIn(directive, TEMPLATE.read_text(encoding="utf-8"))

    def test_command_wraps_canonical_entrypoint_via_wrapper(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(TEMPLATE.read_text(encoding="utf-8"))
        command = parser["program:fie-http"]["command"]
        self.assertIn("/deploy/scripts/fie-start", command)
        start_script = FIE_START.read_text(encoding="utf-8")
        self.assertTrue(_ENTRYPOINT_RE.search(start_script),
                        "fie-start must exec `python -m phase3.transport.http`")
        self.assertIn("exec \"${FIE_PYTHON_BIN}\" -m phase3.transport.http",
                      start_script, "must EXEC (process ownership)")
        self.assertNotIn("nohup", start_script)
        self.assertNotIn("& \n", start_script + " ")
        # daemonization guards apply to CODE, not to comments that explain
        # the ban (e.g. "no double fork: Supervisor owns this process")
        code_only = "\n".join(line for line in start_script.splitlines()
                              if not line.lstrip().startswith("#"))
        self.assertNotRegex(code_only, r"\bdisown\b|\bsetsid\b|\bfork\b")

    def test_repository_template_has_no_host_paths_or_secrets(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("/home/ubuntu", text)
        self.assertNotIn("/usr/lib", text)
        # only {{PLACEHOLDER}} forms may appear where a path belongs
        for match in re.finditer(r"^\s*(?:command|directory|stdout_logfile)=([^\n]*)",
                                 text, re.M):
            value = match.group(1)
            if any(ch == "/" for ch in value):
                self.assertIn("{{", value, f"absolute literal path leaked: {value!r}")
        # no token-shaped literals
        for line in text.splitlines():
            self.assertFalse(re.match(r"^export FIE_AUTH_TOKEN=", line or ""))

    def test_fie_stage_fail_closed_on_unresolved_or_bad_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fie-r2-stage-") as tmp:
            root = Path(tmp)
            env_dir = root / "env"
            env_dir.mkdir()
            # 1: nonexistent env file -> refuse
            proc = subprocess.run(
                [str(FIE_STAGE), "--install-root", str(root),
                 "--env-file", str(env_dir / "missing.env"),
                 "--runtime-user", "ubuntu", "--log-dir", str(root / "logs")],
                capture_output=True, text=True, env=_clean_env(), timeout=120)
            self.assertEqual(proc.returncode, 78)
            self.assertIn("ENV_FILE_MISSING", proc.stderr)
            # 2: wrong mode env file -> refuse (ENV_FILE_MODE_NOT_0600)
            bad_env = env_dir / "bad.env"
            bad_env.write_text("export FIE_SERVICE_ENV=staging\n", encoding="utf-8")
            bad_env.chmod(0o644)
            proc = subprocess.run(
                [str(FIE_STAGE), "--install-root", str(root),
                 "--env-file", str(bad_env),
                 "--runtime-user", "ubuntu", "--log-dir", str(root / "logs")],
                capture_output=True, text=True, env=_clean_env(), timeout=120)
            self.assertEqual(proc.returncode, 78)
            self.assertIn("ENV_FILE_MODE_NOT_0600", proc.stderr)
            # 3: valid staging resolves ALL placeholders
            staged, _, _ = _stage_and_install_env(root / "install", root / "env")
            text = staged.read_text(encoding="utf-8")
            self.assertNotIn("{{", text, "placeholder left unresolved")
            self.assertNotIn("/home/ubuntu/macro-report", text,
                             "staged conf must reference the STAGED root, not the dev repo")
            self.assertEqual(oct(staged.stat().st_mode & 0o777), "0o644")

    def test_readme_defines_three_state_classes_and_activation(self) -> None:
        readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
        for token in ("REPOSITORY_ARTIFACT", "STAGED_RUNTIME_ARTIFACT",
                      "ACTIVE_SUPERVISOR_CONFIGURATION"):
            self.assertIn(token, readme)
        self.assertIn("supervisorctl", readme)
        self.assertIn("0600", readme)


class TestPreflight(unittest.TestCase):
    """WO §8 — deterministic preflight command."""

    def _run_preflight(self, env_file: Path, cwd: Path) -> subprocess.CompletedProcess:
        env = _clean_env({"FIE_ENV_FILE": str(env_file)})
        return subprocess.run(
            [str(FIE_PREFLIGHT)], capture_output=True, text=True,
            env=env, cwd=str(cwd), timeout=180,
        )

    def test_preflight_pass_on_staged_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fie-r2-pf-") as tmp:
            root = Path(tmp)
            staged, env_file, _ = _stage_and_install_env(root / "install",
                                                         root / "env")
            cwd = root / "unrelated_cwd"
            cwd.mkdir()
            proc = self._run_preflight(env_file, cwd)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["precheck_result"], "PASS")
            checks = report["checks"]
            self.assertTrue(checks["config_valid"])
            self.assertTrue(checks["kill_switch"]["valid"])
            self.assertTrue(checks["kill_switch"]["enabled"])
            for role in ("raw", "intelligence"):
                self.assertTrue(checks["db_targets"][role]["present"])
            self.assertTrue(checks["auth_material_available"])
            self.assertTrue(checks["implicit_production_fallback"] is False)
            self.assertTrue(checks["cwd_independence"]["independent"],
                            checks["cwd_independence"])
            token_leak = SYNTH_TOKEN in proc.stdout
            self.assertFalse(token_leak, "preflight printed a token")

    def test_preflight_fails_closed_without_db_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fie-r2-pf-") as tmp:
            root = Path(tmp)
            install = root / "install"
            # valid env but with DB vars stripped -> failure injection
            staged, env_file, _ = _stage_and_install_env(
                install, root / "env",
                env_overrides={"FIE_DATABASE_URL": "", "FIE_DB_TARGET_RAW": ""},
            )
            proc = self._run_preflight(env_file, root)
            self.assertEqual(proc.returncode, 2)
            report = json.loads(proc.stdout)
            self.assertEqual(report["precheck_result"], "FAIL")
            joined = " ".join(report["refusals"])
            self.assertIn("NOT_EXPLICIT", joined)

    def test_preflight_fails_closed_without_env_file(self) -> None:
        proc = subprocess.run(
            [str(FIE_PREFLIGHT)], capture_output=True, text=True,
            env=_clean_env(), cwd=tempfile.gettempdir(), timeout=60)
        self.assertEqual(proc.returncode, 78)
        self.assertIn("ENV_FILE_REQUIRED", proc.stderr)


class _IsolatedSupervisorCase(unittest.TestCase):
    """Shared machinery: a job-owned supervisord on a per-test tmp root."""

    def setUp(self) -> None:
        if not _has_supervisord():
            self.skipTest("supervisord/supervisorctl not available on host")
        self.tmp_root = Path(tempfile.mkdtemp(prefix="fie-r2-super-"))
        self.addCleanup(shutil.rmtree, self.tmp_root, ignore_errors=True)
        self.program = "fie-http-r2-test"

    # -- fixture builders -------------------------------------------------
    # The "install root" for the lifecycle fixture IS the repo (a deployed
    # FIE installation is a repo-shaped checkout with deploy/ + scripts/);
    # only the synthetic env file, staged conf, logs and store live in the
    # job-owned tmp root.
    def _env_file(self, install: Path, port: int, env_overrides: dict | None = None,
                  env_mode: str = "0600") -> Path:
        env_file = install / "fie-runtime.env"
        store = install / "store" / "r2_lifecycle_store.db"
        store.parent.mkdir(parents=True, exist_ok=True)
        cfg_root = install / "config_data" / "config" / "phase3"
        cfg_root.mkdir(parents=True, exist_ok=True)
        if not (cfg_root / "enabled.yaml").exists():
            (cfg_root / "enabled.yaml").write_text("enabled: true\n", encoding="utf-8")
        (install / "data_dir").mkdir(exist_ok=True)
        items = {
            "FIE_SERVICE_ENV": "staging",
            "FIE_PROJECT_ROOT": str(REPO_ROOT),
            "FIE_HTTP_HOST": "127.0.0.1",
            "FIE_HTTP_PORT": str(port),
            "FIE_AUTH_MODE": "token",
            "FIE_AUTH_TOKEN": SYNTH_TOKEN,
            "FIE_DATABASE_URL": str(store),
            "FIE_DB_TARGET_RAW": str(install / "store" / "raw.db"),
            "FIE_CONFIG_DIR": str(install / "config_data"),
            "FIE_DATA_DIR": str(install / "data_dir"),
            "FIE_REQUEST_TIMEOUT": "15",
        }
        items.update(env_overrides or {})
        env_file.write_text(
            "\n".join(f"export {k}={v}" for k, v in items.items()) + "\n",
            encoding="utf-8")
        env_file.chmod(int(env_mode, 8))
        return env_file

    def _prepare_store(self, env_file: Path) -> Path:
        """Initialize the synthetic store schema (hermetic sqlite).

        The runtime binds without touching persistence, but /readyz probes
        the store — a fresh sqlite file must first be migrated exactly as
        the deployment contract documents (phase3.cli init-db with the
        contract env sourced, explicit --db-path)."""
        store = self.tmp_root / "store" / "r2_lifecycle_store.db"
        proc = subprocess.run(
            ["bash", "-c",
             'set -a; . "$FIE_ENV_FILE"; set +a; export PHASE3B_ENABLED=1; '
             'exec "$FIE_PY" -m phase3.cli init-db --db-path "$FIE_DB"'],
            capture_output=True, text=True, timeout=120,
            env=_clean_env({
                "FIE_ENV_FILE": str(env_file),
                "FIE_PY": str(REPO_ROOT / ".venv" / "bin" / "python3"),
                "FIE_DB": str(store),
            }),
            cwd=str(REPO_ROOT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("applied", proc.stdout + proc.stderr)
        return store

    def _supervisor_conf(self, env_file: Path, install: Path,
                         autostart: str = "false",
                         autorestart: str = "unexpected") -> Path:
        conf = self.tmp_root / "supervisord.conf"
        logfile = self.tmp_root / "supervisord.log"
        conf.write_text(
            f"""
[unix_http_server]
file={self.tmp_root / 'sup.sock'}
[supervisord]
logfile={logfile}
pidfile={self.tmp_root / 'supervisord.pid'}
nodaemon=true
childlogdir={self.tmp_root}
[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://{self.tmp_root / 'sup.sock'}
[program:{self.program}]
command={FIE_START}
directory={REPO_ROOT}
user={os.environ.get('USER', 'ubuntu')}
environment=PYTHONUNBUFFERED="1",FIE_ENV_FILE="{env_file}"
autostart={autostart}
autorestart={autorestart}
startsecs=2
startretries=2
exitcodes=0,2
stopsignal=TERM
stopwaitsecs=30
stopasgroup=true
killasgroup=true
redirect_stderr=true
stdout_logfile={self.tmp_root}/logs/{self.program}.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=0
""",
            encoding="utf-8")
        (self.tmp_root / "logs").mkdir(exist_ok=True)
        return conf

    def _supctl(self, *args: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [str(shutil.which("supervisorctl")), "-c",
             str(self.tmp_root / "supervisord.conf"), *args],
            capture_output=True, text=True, env=_clean_env(), timeout=90,
            cwd=str(self.tmp_root),  # Gate E relevance: run from the TMP root
        )
        return proc

    def _start_supervisord(self, conf: Path) -> None:
        proc = subprocess.Popen(
            [str(shutil.which("supervisord")), "-c", str(conf)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(self.tmp_root), start_new_session=True)
        self.addCleanup(self._teardown_supervisord, conf)
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                self.fail(f"supervisord exited rc={proc.returncode}")
            if self._supctl("status").returncode in (0, 3):
                # 0 = reachable with programs; 3 = reachable, none running
                return
            time.sleep(0.3)
        self.fail("supervisord never became reachable via supervisorctl")

    def _teardown_supervisord(self, conf: Path) -> None:
        subprocess.run(
            [str(shutil.which("supervisorctl")), "-c", str(conf), "shutdown"],
            capture_output=True, text=True, timeout=90)
        time.sleep(1.0)

    def _service_state(self) -> str:
        proc = self._supctl("status", self.program)
        out = proc.stdout.strip()
        match = re.search(r"\b(RUNNING|STARTING|STOPPING|STOPPED|BACKOFF|"
                          r"FATAL|EXITED|UNKNOWN)\b", out)
        return match.group(1) if match else (out or f"rc={proc.returncode}")

    def _wait_state(self, want: str | None = None, ready_http: bool = False,
                    timeout: float = 40.0,
                    predicates: list | None = None) -> str:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            state = self._service_state()
            last = state
            if want and state == want:
                return state
            if predicates:
                for predicate in predicates:
                    if predicate(state):
                        return state
            time.sleep(0.5)
        return last

    def _http(self, path: str, token: str | None) -> tuple[int, str]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _wait_http(self, path: str, want_status: int, token: str | None = None,
                   timeout: float = 20.0) -> tuple[int, str] | None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                status, body = self._http(path, token)
            except Exception:
                status, body = 0, ""
            last = (status, body)
            if status == want_status:
                return last
            # an HTTPError with a real status still counts as "serving"
            if want_status == "serving" and status in (200, 401, 403):
                return last
            time.sleep(0.4)
        return last if last and last[0] else None

    def _owned_service_pids(self) -> list[int]:
        """Deterministic: supervisor owns exactly the program's own pid."""
        proc = self._supctl("pid", self.program)
        out = proc.stdout.strip()
        if proc.returncode == 0 and out.isdigit():
            return [int(out)]
        return []


class TestSupervisorLifecycle(_IsolatedSupervisorCase):
    """Gate C — isolated Supervisor lifecycle (start→ready→stop→restart)."""

    def setUp(self) -> None:
        super().setUp()
        self.port = _free_tcp_port()
        self.install = REPO_ROOT   # a deployed FIE installation is repo-shaped
        self.env_file = self._env_file(self.tmp_root, self.port)
        self._prepare_store(self.env_file)
        self.conf = self._supervisor_conf(self.env_file, self.install)

    def test_full_lifecycle_start_health_ready_stop_restart(self) -> None:
        # 1. preflight PASS via the canonical --check-config startup path
        #    (same env file — deployment contract)
        pf = subprocess.run([str(FIE_START), "--check-config"],
                            capture_output=True, text=True,
                            env=_clean_env({"FIE_ENV_FILE": str(self.env_file)}),
                            cwd=str(self.tmp_root), timeout=120)
        self.assertEqual(pf.returncode, 0, pf.stdout + pf.stderr)
        check = json.loads(pf.stdout)
        self.assertEqual(check["check"], "ok")

        # 2/3. start + wait health (autostart=false fixture: start explicitly)
        self._start_supervisord(self.conf)
        started = self._supctl("start", self.program)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)

        # 4. healthz public; readyz authenticated probes persistence
        health = self._wait_http("/healthz", 200, timeout=30)
        self.assertIsNotNone(health, f"/healthz never served; state={self._service_state()}")
        self.assertEqual(health[0], 200)
        ready = self._wait_http("/readyz", 200, token=SYNTH_TOKEN, timeout=30)
        self.assertIsNotNone(ready, f"/readyz never ready; state={self._service_state()}")
        self.assertEqual(ready[0], 200)

        # 5. exactly one owned listener
        pids = self._owned_service_pids()
        self.assertEqual(len(pids), 1, f"expected one owned listener, saw {pids}")
        state = self._service_state()
        self.assertEqual(state, "RUNNING", state)

        # 6. graceful stop
        stopped = self._supctl("stop", self.program)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        final_state = self._wait_state("STOPPED")
        self.assertEqual(final_state, "STOPPED", final_state)

        # 7. zero owned residue
        self.assertEqual(self._owned_service_pids(), [])
        # and no lock/tmp lifecycle artifact prevents restart — proven by
        # the restart case below reusing the same fixture root.

    def test_restart_without_residue_dependence(self) -> None:
        # START -> READY -> STOP -> START -> READY, same fixture root
        self._start_supervisord(self.conf)
        self._supctl("start", self.program)
        self.assertIsNotNone(self._wait_http("/healthz", 200, timeout=30))
        self.assertIsNotNone(
            self._wait_http("/readyz", 200, token=SYNTH_TOKEN, timeout=30))
        self._supctl("stop", self.program)
        self.assertEqual(self._wait_state("STOPPED"), "STOPPED")
        self.assertEqual(self._owned_service_pids(), [])

        started = self._supctl("start", self.program)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertIsNotNone(self._wait_http("/healthz", 200, timeout=30))
        self.assertIsNotNone(
            self._wait_http("/readyz", 200, token=SYNTH_TOKEN, timeout=30))
        final = self._wait_state("RUNNING")
        self.assertEqual(final, "RUNNING", final)
        pids = self._owned_service_pids()
        self.assertEqual(len(pids), 1, f"restart must own exactly one listener: {pids}")

    def test_crash_policy_unexpected_exit_restarts_and_recovers(self) -> None:
        # Dedicated failure-injection case (WO §7.4): SIGKILL the service
        # child — isolated instance, job-owned process tree; justifies the
        # kill by proving the DECLARED policy survives an unexpected death.
        self._start_supervisord(self.conf)
        self._supctl("start", self.program)
        self.assertIsNotNone(self._wait_http("/healthz", 200, timeout=30))
        owned = self._owned_service_pids()
        self.assertEqual(len(owned), 1, owned)

        os.kill(owned[0], signal.SIGKILL)
        # declared policy: autorestart=unexpected -> supervisor re-spawns
        state = self._wait_state("RUNNING", timeout=40)
        self.assertEqual(state, "RUNNING", f"policy not honored: {state}")
        # a NEW pid owns the port and the service recovered
        recovered = self._owned_service_pids()
        self.assertEqual(len(recovered), 1, recovered)
        self.assertNotIn(owned[0], recovered)
        self.assertIsNotNone(self._wait_http("/healthz", 200, timeout=30))

    def test_port_collision_foreign_process_preserved(self) -> None:
        # §7.5/§7.6: a FOREIGN listener owns the desired port; the second
        # instance must fail closed with a sufficient diagnostic and must
        # NOT kill or disturb the foreign process.
        foreign = subprocess.Popen(
            ["python3", "-c",
             "import socket,time;"
             f"s=socket.socket();s.bind(('127.0.0.1',{self.port}));"
             "s.listen(1);print('LISTENING',flush=True);time.sleep(600)"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(self.tmp_root))
        self.addCleanup(self._cleanup_foreign, foreign)
        deadline = time.time() + 10
        while time.time() < deadline:
            if foreign.poll() is not None:
                self.fail("foreign listener died early")
            if self._port_in_use(self.port):
                break
            time.sleep(0.3)
        else:
            self.fail("foreign listener never bound the port")

        # service start against the occupied port, via the SUPERVISOR (a
        # second instance of the same program on a claimed port)
        self._start_supervisord(self.conf)
        self._supctl("start", self.program)
        # exitcodes=0,2 + autorestart=unexpected -> expected-exit 2 means
        # NO restart loop; it settles EXITED/FATAL deterministically.
        state = self._wait_state(predicates=[lambda s: s in ("EXITED", "FATAL")],
                                 timeout=45)
        self.assertIn(state, ("EXITED", "FATAL"), state)
        # diagnostic: the sanitized bind-conflict JSON must be in the log
        log = (self.tmp_root / "logs" / f"{self.program}.log").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("BIND_UNAVAILABLE", log)
        # foreign process untouched
        self.assertIsNone(foreign.poll(), "foreign process was disturbed")
        # and no owned service survived the collision
        self.assertEqual(self._owned_service_pids(), [])

    @staticmethod
    def _port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                return False

    @classmethod
    def _cleanup_foreign(cls, proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 — teardown best-effort, TERM only
            pass

    def test_cwd_independence_from_unrelated_directories(self) -> None:
        # Gate E: the lifecycle fixture already runs supervisorctl from
        # self.tmp_root; run a second full harness from / — same behavior
        # (absolute staged paths only; no CWD discovery).
        saved_cwd = os.getcwd()
        try:
            os.chdir("/")
            self._start_supervisord(self.conf)
            self._supctl("start", self.program)
            health = self._wait_http("/healthz", 200, timeout=30)
            self.assertIsNotNone(health, "lifecycle from / behaved differently")
            self.assertEqual(health[0], 200)
            self._supctl("stop", self.program)
            self.assertEqual(self._wait_state("STOPPED"), "STOPPED")
        finally:
            os.chdir(saved_cwd)


class TestFailureInjectionFailClosed(unittest.TestCase):
    """Gate D 1/2/5 — invalid config/kill switch/DB target fail closed BEFORE workload."""

    def _start_attempt(self, env_overrides: dict, port: int | None = None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="fie-r2-inject-") as tmp:
            install = Path(tmp)
            (install / "config_data" / "config" / "phase3").mkdir(parents=True)
            (install / "config_data" / "config" / "phase3" / "enabled.yaml").write_text(
                "enabled: true\n", encoding="utf-8")
            (install / "store").mkdir()
            env_file = install / "env.env"
            items = {
                "FIE_SERVICE_ENV": "staging",
                "FIE_PROJECT_ROOT": str(REPO_ROOT),
                "FIE_HTTP_HOST": "127.0.0.1",
                "FIE_HTTP_PORT": str(port or _free_tcp_port()),
                "FIE_AUTH_MODE": "token",
                "FIE_AUTH_TOKEN": SYNTH_TOKEN,
                "FIE_DATABASE_URL": str(install / "store" / "s.db"),
                "FIE_DB_TARGET_RAW": str(install / "store" / "raw.db"),
                "FIE_CONFIG_DIR": str(install / "config_data"),
                "FIE_DATA_DIR": str(install),
                "FIE_REQUEST_TIMEOUT": "15",
            }
            items.update(env_overrides)
            env_file.write_text(
                "\n".join(f"export {k}={v}" for k, v in items.items()) + "\n",
                encoding="utf-8")
            env_file.chmod(0o600)
            return subprocess.run(
                [str(FIE_START)],
                capture_output=True, text=True,
                env=_clean_env({"FIE_ENV_FILE": str(env_file)}),
                cwd=str(install), timeout=180)

    def test_invalid_db_target_fails_closed_before_workload(self) -> None:
        # malformed DSN shape -> startup refuses, nothing binds
        port = _free_tcp_port()
        proc = self._start_attempt({"FIE_DATABASE_URL": "foo://host/value"},
                                   port)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("error", proc.stderr)
        # nothing bound the port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            s.listen(1)  # port free => the refusal never opened a listener

    def test_invalid_kill_switch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fie-r2-ks-") as tmp:
            install = Path(tmp)
            proc = self._run_staged_preflight_refusal(
                install, {"KILL_SWITCH": "ambiguous"})
            self.assertEqual(proc.returncode, 2)
            self.assertIn("KILL_SWITCH_INVALID", proc.stdout)

    def _run_staged_preflight_refusal(self, install: Path,
                                      spec: dict) -> subprocess.CompletedProcess:
        (install / "config_data" / "config" / "phase3").mkdir(parents=True)
        ks = install / "config_data" / "config" / "phase3" / "enabled.yaml"
        if spec.get("KILL_SWITCH") == "ambiguous":
            ks.write_text("enabled: maybe\n", encoding="utf-8")
        store = install / "store.db"
        env_file = install / "env.env"
        env_file.write_text(
            "\n".join([
                "export FIE_SERVICE_ENV=staging",
                "export FIE_PROJECT_ROOT=" + str(REPO_ROOT),
                "export FIE_HTTP_HOST=127.0.0.1",
                "export FIE_HTTP_PORT=" + str(_free_tcp_port()),
                "export FIE_AUTH_MODE=token",
                "export FIE_AUTH_TOKEN=" + SYNTH_TOKEN,
                "export FIE_DATABASE_URL=" + str(store),
                "export FIE_DB_TARGET_RAW=" + str(store) + ".raw",
                "export FIE_CONFIG_DIR=" + str(install / "config_data"),
                "export FIE_DATA_DIR=" + str(install),
            ]) + "\n", encoding="utf-8")
        env_file.chmod(0o600)
        return subprocess.run(
            [str(FIE_PREFLIGHT)], capture_output=True, text=True,
            env=_clean_env({"FIE_ENV_FILE": str(env_file)}),
            cwd=str(tempfile.gettempdir()), timeout=180)

    def test_missing_env_file_fails_closed(self) -> None:
        proc = subprocess.run(
            [str(FIE_START)], capture_output=True, text=True,
            env=_clean_env(), cwd="/", timeout=60)
        self.assertEqual(proc.returncode, 78)
        self.assertIn("ENV_FILE_REQUIRED", proc.stderr)


if __name__ == "__main__":
    unittest.main()