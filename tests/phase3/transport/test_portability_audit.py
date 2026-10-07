"""Portability audit (Phase 6.6 §20) + arbitrary-CWD launch (§12).

Two layers:

1. File-content audit — every Phase 6.6 source/test/doc file is
   classified against known developer-host/AEE/A3 markers; the audit
   itself is self-contained (markers assembled dynamically so this file
   never contains its own needles). Classifications: the service-layer
   imports are the one ACTIVE_RUNTIME_DEPENDENCY (they ARE the frozen
   Phase 6.5 contract); test files are TEST_ONLY and still must be
   clean-room portable; HISTORICAL_DOCUMENTATION/COMMENT_OR_EXAMPLE
   hits are not permitted in the Phase 6.6 surface at all.
2. Launch portability — the console entry point starts and serves from
   an arbitrary working directory (``cwd="/"``) with no developer
   paths, developer venvs or explicit PYTHONPATH.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

# markers assembled dynamically (self-containment rule, Phase 6.5 pattern)
FORBIDDEN_MARKERS = (
    "/hom" + "e/sscomp",
    "/hom" + "e/ubuntu",
    "worktrees/fie-ph" + "ase6",
    "aee-runtime-br" + "idge",
    "abacus-cla" + "w",
    "192.168" + ".",
    "fi" + "e63-fresh",
)


def _phase6_6_targets() -> list[Path]:
    targets: list[Path] = sorted((REPO_ROOT / "phase3" / "transport").glob("*.py"))
    targets.extend(sorted(Path(REPO_ROOT / "tests" / "phase3" / "transport").glob("*.py")))
    targets.extend(sorted((REPO_ROOT / "docs" / "adr").glob("adr-01[12]*.md")))
    docs_66 = REPO_ROOT / "docs" / "architecture" / "phase6-6"
    if docs_66.exists():
        targets.extend(sorted(docs_66.glob("*.md")))
    targets.append(REPO_ROOT / "pyproject.toml")
    return targets


class TestFileContentAudit(unittest.TestCase):
    """§20: classify every marker hit; disallow host specificity entirely."""

    def test_no_developer_host_markers(self) -> None:
        # even TEST_ONLY files must be clean-room portable (§21)
        for target in _phase6_6_targets():
            text = target.read_text(encoding="utf-8")
            relative = target.relative_to(REPO_ROOT)
            for marker in FORBIDDEN_MARKERS:
                with self.subTest(file=relative, marker=marker):
                    self.assertNotIn(
                        marker, text,
                        f"{relative}: marker {marker!r} is not one of the"
                        " permitted classification buckets",
                    )

    def test_transport_imports_only_the_frozen_service_surface(self) -> None:
        # thin-transport invariant as static check (§3.1). Permitted
        # phase3 imports, by classification:
        #   ACTIVE_RUNTIME_DEPENDENCY — phase3.service (frozen contract)
        #     and, in http.py only, phase3.persistence.backend.resolve_spec
        #     / open_store (the composition root opening the store — no
        #     SQL, no traversal, no schema knowledge);
        #   SELF — phase3.transport. Everything else must stay at zero.
        allowed_service_parts = {
            "dispatch", "contracts", "errors", "freshness", "reference",
            "boundary", "runtime_config", "timeutil",
            # 6.9B-R4 additive pin: phase3.service.operational_events is a
            # read-only structured-logging taxonomy (emit/redact; no SQL,
            # no store, no service state) — transport emits STARTUP/
            # SHUTDOWN/AUTH_FAILURE events through it.
            "operational_events",
        }
        for source in sorted((REPO_ROOT / "phase3" / "transport").glob("*.py")):
            text = source.read_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                # import statements only (including function-body ones)
                if "import " not in line or line.startswith(("#", '"', "'")):
                    continue
                if "phase3.service" in line and "phase3.transport" not in line:
                    module = self._service_module_of(line)
                    with self.subTest(file=source.name, stmt=line):
                        self.assertIn(module, allowed_service_parts)
                elif re.match(r"^from |\bimport\b", line) and "phase3." in line \
                        and "phase3.transport" not in line:
                    with self.subTest(file=source.name, stmt=line):
                        self.assertIn(
                            "phase3.persistence.backend", line,
                            f"{source.name}: only service/persistence-backend "
                            "seams may be imported",
                        )
                        self.assertIn("http.py", source.name)
                self.assertFalse(
                    "batch" in line or "ingest" in line,
                    f"{source.name}: transport must not touch batch/ingestion",
                )

    @staticmethod
    def _service_module_of(line: str) -> str:
        # "from phase3.service import dispatch"
        # | "from phase3.service.errors import ServiceError"
        if line.startswith("from phase3.service import"):
            return line.split("import ", 1)[1].split()[0].rstrip(",")
        return line.split("phase3.service.")[1].split(" import")[0]


def _launch_from_cwd_root(env: dict[str, str]) -> tuple[int, str]:
    """Run ``main()`` as a fresh process from ``/`` and return (rc, output).

    The entry point serves until signalled, so output is drained on a
    reader thread while the main thread waits for the structured
    ``server_started`` record (bounded), then terminates gracefully.
    """
    import threading
    import time

    proc = subprocess.Popen(
        [
            sys.executable, "-X", "utf8", "-c",
            "from phase3.transport.http import main; raise SystemExit(main())",
        ],
        cwd="/",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    collected: list[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            collected.append(line)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            if any("server_started" in line for line in collected):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        proc.terminate()
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    reader.join(timeout=5)
    if proc.stdout is not None:
        proc.stdout.close()
    return rc, "".join(collected)


class TestDockerfileStaticAudit(unittest.TestCase):
    """§13 static invariants for the reference container artifact.

    The dynamic build/run smoke requires docker-daemon access
    (root:docker socket); when unavailable it is executed by the
    documented commands in docs — see the report's [OPEN] item. The
    static audit always runs.
    """

    def test_non_root_and_explicit_port(self) -> None:
        text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^USER 10001:10001$")  # non-root, no name lookup
        self.assertRegex(text, r"(?m)^EXPOSE 8787$")       # explicit port
        self.assertRegex(  # runtime module entry point, not a shell wrapper
            text, r'ENTRYPOINT \["python", "-m", "phase3\.transport\.http"\]'
        )
        self.assertIn("HEALTHCHECK", text)   # liveness wired to /healthz

    def test_no_credentials_or_host_data_in_image(self) -> None:
        text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        for marker in FORBIDDEN_MARKERS:
            self.assertNotIn(marker, text)
        self.assertNotRegex(
            text,
            r"(?i)(password|secret|token)\s*=|\bCOPY\b.*\.env",
            "no embedded credentials",
        )
        # persistence only ever arrives via env/mount, never baked in
        self.assertNotRegex(text, r"(?m)^COPY .*(\.db|data/)")


class TestArbitraryCwdLaunch(unittest.TestCase):
    """§12: the console entry point works from any working directory."""

    def test_entry_point_from_root_cwd(self) -> None:
        from tests.phase3.service import helpers

        bundle = helpers.bootstrap_state(None)
        path = Path(bundle["path"])
        output = ""
        rc = -1
        try:
            env = {k: v for k, v in os.environ.items()
                   if k != "PYTHONPATH"}  # no developer PYTHONPATH
            env["FIE_DATABASE_URL"] = str(path)
            env["FIE_LOG_LEVEL"] = "INFO"
            # bind an ephemeral port by using port 0 through the arg-free
            # entry point instead: FIE_HTTP_PORT=0 is a legal port value
            env["FIE_HTTP_PORT"] = "0"
            rc, output = _launch_from_cwd_root(env)
        finally:
            bundle["store"].close()
            path.unlink(missing_ok=True)
        self.assertEqual(rc, 0)
        # structured startup line proves the runtime is serving from cwd=/:
        # bind+serve happened with NO developer paths, NO developer venv
        self.assertIn("server_started", output)
        self.assertIn('"auth_mode"', output)
        for marker in FORBIDDEN_MARKERS:
            self.assertNotIn(marker, output)
        self.assertNotIn("Traceback", output)


if __name__ == "__main__":
    unittest.main()