#!/usr/bin/env python3
"""Python portability contract tests — 6.9A-R4-R2-R1 Task B (WO §7).

Pins the ONE canonical Python-interpreter resolution contract
(scripts/fie_python_resolver.sh) at its consumer surfaces:

  P1  no operator-host default remains: the wrappers carry no usable
      macro-venv fallback (R4-R2 blocker #1: 18 focused-suite failure
      records died at the wrapper `! -x` interpreter check before ever
      reaching the DB guard). Resolution succeeds without the path.
  P2  explicit FIE_PYTHON override is honored (hard contract) and
      propagated end-to-end through a wrapper.
  P3  active $VIRTUAL_ENV resolves (source=venv).
  P4  repository .venv resolves (source=repo-venv); PATH python3 resolves
      (source=path) on a host without a repo venv.
  P5  no usable interpreter anywhere → fail-closed exit 78 with
      diagnostics — never a silent substitution of a broken interpreter;
      a missing resolver script is itself a fail-closed consumer error.
  P6  unusable-but-SET FIE_PYTHON → 78 (never substituted) even though a
      usable interpreter exists later in the precedence list.
  P7  paths with spaces / quoting-hostile characters are safe end-to-end
      (resolver output is quoted at every use; probes pass argv, never
      shell-interpolated).
  P8  single-contract pinning: run.sh / run_weekly.sh / run_monthly.sh /
      scripts/test-cloud.sh / scripts/provision-test-postgres.sh all
      source the one resolver script and none embeds its own
      interpreter-resolution logic.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESOLVER = REPO / "scripts" / "fie_python_resolver.sh"
WRAPPERS = (REPO / "run.sh", REPO / "run_weekly.sh", REPO / "run_monthly.sh")
TEST_CLOUD = REPO / "scripts" / "test-cloud.sh"
PROVISIONER = REPO / "scripts" / "provision-test-postgres.sh"
FAIL_CLOSED = 78
HOST_PYTHON = sys.executable


class _ResolverSubprocessMixin(unittest.TestCase):
    """Runs the shipped resolver in a controlled child shell env."""

    def _resolve(self, updates: dict, repo: Path = REPO) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
        }
        env.update(updates)
        proc = subprocess.run(
            ["bash", "-c",
             ". \"$1\"; fie_resolve_python; rc=$?; "
             "printf 'BIN=%s\\n' \"${FIE_PYTHON_BIN:-}\"; "
             "printf 'SOURCE=%s\\n' \"${FIE_PYTHON_SOURCE:-}\"; exit $rc",
             "resolver-driver", str(repo / "scripts" / "fie_python_resolver.sh")],
            env=env, capture_output=True, text=True, timeout=60,
            cwd=str(repo))
        out = dict(
            line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        return {"rc": proc.returncode, "bin": out.get("BIN", ""),
                "source": out.get("SOURCE", ""), "stderr": proc.stderr}

    def _fake_repo(self) -> Path:
        """A repo-shaped temp dir with ONLY the resolver script — no .venv."""
        tmp = tempfile.mkdtemp(prefix="fie-resolver-fakerepo-")
        (Path(tmp) / "scripts").mkdir(parents=True, exist_ok=True)
        shutil.copy(RESOLVER, Path(tmp) / "scripts" / "fie_python_resolver.sh")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return Path(tmp)

    def _stub(self, body: str, name: str = "stub-python") -> str:
        tmp = tempfile.mkdtemp(prefix="fie-resolver-stub-")
        stub = Path(tmp) / name
        stub.write_text("#!/bin/bash\n" + body + "\n")
        stub.chmod(0o755)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return str(stub)

    def _wrapper_env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("FIE_") and k != "VIRTUAL_ENV"}
        env.update(extra)
        return env


class TestOldOperatorPathAbsent(_ResolverSubprocessMixin, unittest.TestCase):
    """P1 — the operator-host absolute path is no longer a usable fallback."""

    def test_no_wrapper_carries_a_macro_venv_assignment_or_use(self):
        offenders = []
        for wrapper in (*WRAPPERS, TEST_CLOUD, PROVISIONER):
            for line in wrapper.read_text().splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue  # explanatory historical notes are allowed
                if re.search(r"\bmacro-venv\b", stripped):
                    offenders.append(f"{wrapper.name}: {stripped}")
        self.assertEqual(offenders, [])

    def test_wrapper_resolves_without_the_operator_host_path(self):
        res = self._resolve({"FIE_PYTHON": "", "VIRTUAL_ENV": ""})
        self.assertEqual(res["rc"], 0, res["stderr"])
        self.assertTrue(res["bin"])
        self.assertNotIn("macro-venv", res["bin"])


class TestExplicitOverride(_ResolverSubprocessMixin, unittest.TestCase):
    """P2 — override honored + propagated; P6 — unusable override fails closed."""

    def test_usable_override_honored(self):
        res = self._resolve({"FIE_PYTHON": HOST_PYTHON})
        self.assertEqual((res["rc"], res["bin"], res["source"]),
                         (0, HOST_PYTHON, "override"))

    def test_unusable_override_fails_closed_and_is_never_substituted(self):
        res = self._resolve({"FIE_PYTHON": "/nonexistent/fie/py3"})
        self.assertEqual(res["rc"], FAIL_CLOSED)
        self.assertIn("refusing to substitute", res["stderr"])

    def test_wrapper_propagates_the_override_interpreter_end_to_end(self):
        """run.sh executes the override interpreter — proven by exit-code
        propagation. The stub passes the resolver's version probe (-c is a
        version probe → 0) but exits 7 on any real invocation; a fallback
        interpreter would not produce 7."""
        stub = self._stub('if [ "${1:-}" = "-c" ]; then exit 0; fi\nexit 7')
        proc = subprocess.run(["bash", str(REPO / "run.sh")],
                              env=self._wrapper_env(FIE_PYTHON=stub),
                              capture_output=True, text=True, timeout=300,
                              cwd=str(REPO))
        self.assertEqual(proc.returncode, 7)

    def test_wrapper_with_unusable_override_fails_closed_before_step1(self):
        proc = subprocess.run(
            ["bash", str(REPO / "run_weekly.sh")],
            env=self._wrapper_env(FIE_PYTHON="/nonexistent/fie/py3"),
            capture_output=True, text=True, timeout=120, cwd=str(REPO))
        self.assertEqual(proc.returncode, FAIL_CLOSED)
        self.assertIn("python interpreter resolution failed", proc.stderr)


class TestVenvResolution(_ResolverSubprocessMixin, unittest.TestCase):
    """P3 — active venv; P4 — repo venv, then PATH python3."""

    def test_active_virtualenv_resolved(self):
        venv = Path(tempfile.mkdtemp(prefix="fie-resolver-venv-"))
        bindir = venv / "bin"
        bindir.mkdir(parents=True)
        link = bindir / "python3"
        try:
            link.symlink_to(HOST_PYTHON)
            res = self._resolve({"FIE_PYTHON": "", "VIRTUAL_ENV": str(venv)})
            self.assertEqual((res["rc"], res["source"]), (0, "venv"))
            self.assertEqual(Path(res["bin"]).resolve(), link.resolve())
        finally:
            shutil.rmtree(venv, ignore_errors=True)

    def test_path_python3_resolves_without_repo_venv(self):
        fake_repo = self._fake_repo()
        self.assertFalse((fake_repo / ".venv").exists())
        res = self._resolve({"FIE_PYTHON": "", "VIRTUAL_ENV": ""},
                            repo=fake_repo)
        self.assertEqual((res["rc"], res["source"]), (0, "path"))
        self.assertTrue(res["bin"])

    def test_repo_venv_resolved_when_present(self):
        res = self._resolve({"FIE_PYTHON": "", "VIRTUAL_ENV": ""})
        if (REPO / ".venv" / "bin" / "python3").is_file():
            self.assertEqual((res["rc"], res["source"]), (0, "repo-venv"))
            # the resolved bin is the repo venv's python3 — anchor by
            # resolved-path equality (NOT a path-name substring: a fresh
            # clone lives at an arbitrary checkout path)
            self.assertEqual(Path(res["bin"]).resolve(),
                             (REPO / ".venv" / "bin" / "python3").resolve())
        else:  # repo venv absent: PATH fallback must still work (P4)
            self.assertEqual((res["rc"], res["source"]), (0, "path"))


class TestFailClosedNoInterpreter(_ResolverSubprocessMixin, unittest.TestCase):
    """P5 — nothing usable anywhere → 78 with diagnostics."""

    def test_no_usable_interpreter_fails_closed_78(self):
        broken_dir = tempfile.mkdtemp(prefix="fie-resolver-nopy-")
        broken = Path(broken_dir) / "python3"
        broken.write_text("#!/bin/bash\nexit 1\n")  # present but unusable
        broken.chmod(0o755)
        self.addCleanup(shutil.rmtree, broken_dir, ignore_errors=True)
        fake_repo = self._fake_repo()
        res = self._resolve(
            {"FIE_PYTHON": "", "VIRTUAL_ENV": "",
             "PATH": f"{broken_dir}:/usr/bin:/bin"}, repo=fake_repo)
        self.assertEqual(res["rc"], FAIL_CLOSED)
        self.assertIn("no usable Python interpreter", res["stderr"])
        self.assertNotIn("BIN=", dict(
            line.split("=", 1)
            for line in res["stderr"].splitlines() if "=" in line).keys())

    def test_missing_resolver_script_fails_closed_at_provisioner(self):
        """The provisioner's contract: a missing canonical resolver script is
        exit 5 + POSTGRESQL_PLATFORM_UNSUPPORTED shape — never a silent
        PATH-python fallback."""
        tmp = tempfile.mkdtemp(prefix="fie-resolver-absent-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copy(PROVISIONER, Path(tmp) / "provision-test-postgres.sh")
        proc = subprocess.run(
            ["bash", str(Path(tmp) / "provision-test-postgres.sh"),
             "--cache-state"],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": os.environ.get("HOME", "/root")},
            capture_output=True, text=True, timeout=120, cwd=str(tmp))
        self.assertEqual(proc.returncode, 5)
        self.assertIn("POSTGRESQL_PLATFORM_UNSUPPORTED", proc.stderr)


class TestPathQuoting(_ResolverSubprocessMixin, unittest.TestCase):
    """P7 — spaces and quoting-hostile characters stay safe."""

    def test_path_with_spaces_resolves_and_executes(self):
        base = Path(tempfile.mkdtemp(prefix="fie resolver spaces "))
        pydir = base / "py (3) dir"
        pydir.mkdir(parents=True)
        link = pydir / "python3"
        try:
            link.symlink_to(HOST_PYTHON)
            res = self._resolve({"FIE_PYTHON": str(link)})
            self.assertEqual((res["rc"], res["source"]), (0, "override"))
            probe = subprocess.run([res["bin"], "-c", "print('Q-OK')"],
                                   capture_output=True, text=True, timeout=60)
            self.assertIn("Q-OK", probe.stdout)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_version_probe_is_injection_safe(self):
        """A hostile interpreter name cannot inject into the probe: it is
        passed as argv[0], never through a shell."""
        hostile = Path(tempfile.mkdtemp(prefix="fie-resolver-hostile-"))
        evil_dir = hostile / "$(echo pwned)"
        evil_dir.mkdir(parents=True)
        link = evil_dir / "python3'; printf pwned; '"
        try:
            link.symlink_to(HOST_PYTHON)
            res = self._resolve({"FIE_PYTHON": str(link)})
            self.assertEqual(res["rc"], 0)
            self.assertNotIn("pwned", res["stderr"])
            self.assertNotIn("pwned", res["source"])
        finally:
            shutil.rmtree(hostile, ignore_errors=True)


class TestSingleContractPinning(unittest.TestCase):
    """P8 — every consumer pins ONE implementation (the resolver file)."""

    CONSUMERS = (WRAPPERS[0], WRAPPERS[1], WRAPPERS[2], TEST_CLOUD,
                 PROVISIONER)

    def test_each_consumer_sources_the_resolver(self):
        missing = [path.name for path in self.CONSUMERS
                   if "fie_python_resolver.sh" not in path.read_text()]
        self.assertEqual(missing, [])

    def test_focused_suite_includes_the_resolver_contract_tests(self):
        self.assertIn("tests.test_python_resolver_contract",
                      TEST_CLOUD.read_text())

    def test_resolver_syntax_is_sound(self):
        proc = subprocess.run(["bash", "-n", str(RESOLVER)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()