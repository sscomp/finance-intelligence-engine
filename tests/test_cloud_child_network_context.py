#!/usr/bin/env python3
"""Test-child network-context contract — 6.9A-R4-R2-R1 Task C (WO §8).

R4-R2 blocker #2: the suite's scrubbed child (tests/test_cloud_pg_provisioning
``_child_env``) constructed its environment by scrubbing EVERYTHING — the
published checksum sidecar could not be acquired at all (exit 91,
POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE) on a proxy/CA-configured runner,
because no network-runtime context reached the child.

Contract under test (tests/cloud_child_env.py is the ONE helper):

    N1    an allowlisted scrubbed child acquires the published-shape
          sidecar + artifact (network-true in SHAPE, local loopback
          server — never Maven Central, never Production)
    N2    proxy context CAUSALLY survives the scrub: allowlisted proxy
          variables reach the child and change its acquisition outcome
          (black-hole proxy + no_proxy ⇒ success; the same proxy minus
          the no_proxy entry ⇒ deterministic exit-91 acquisition
          failure), and CA context (CURL_CA_BUNDLE) causally enables TLS
          to a self-signed local server
    N3    unrelated ambient variables do NOT propagate
    N4    secret-like variables do NOT propagate (values never observable
          in the child env)
    N5    production DSN candidate variables do NOT propagate
    N6    checksum mismatch remains fail-closed (exit 92) through the
          allowlisted scrubbed child
    N7    partial cache artifact still rejected/reacquired through the
          allowlisted scrubbed child (exit 0 via reacquisition only)
    N8    unreachable artifact source → deterministic non-zero (exit 91)
          with explicit classification; no silent fallback

Offline-safe: acquisition legs run against a LOCAL loopback-only HTTP(S)
server serving a synthetic artifact (reuse of the R4-R1 task artifact
machinery from tests/test_cloud_pg_cache_resolver.py). The network-true
Maven acquisition is proven in the evidence layer (WO Task F), not here.
"""
from __future__ import annotations

import os
import socket
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.cloud_child_env import (
    NETWORK_RUNTIME_ALLOWLIST,
    child_env,
    network_context,
    redact_network_env,
)
from tests.test_cloud_pg_cache_resolver import (
    _ArtifactFixture,
    _ensure_only,
)

REPO = Path(__file__).resolve().parents[1]

# A proxy address that is guaranteed to black-hole: nothing listens there.
BLACKHOLE_PROXY = "http://127.0.0.1:9"

ENV_DUMP = ('env | grep -E "^(FIE_|PGPASSWORD|FOO_|EDITOR|GITHUB_TOKEN|'
            'GH_TOKEN|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|http_proxy|'
            'https_proxy|all_proxy|no_proxy|SSL_CERT|REQUESTS_CA_BUNDLE|'
            'CURL_CA_BUNDLE|DATABASE_URL|XDG_CACHE_HOME)" | sort; '
            'printf "PATH=%s\\nPWD=%s\\n" "$PATH" "$PWD"; '
            'printf "HOME=%s\\n" "${HOME:-}"; '
            'printf "TMPDIR=%s\\n" "${TMPDIR:-}"\n')


class NetworkAllowlistShapeTests(unittest.TestCase):
    """The allowlist itself — closed over network-runtime names only."""

    def test_allowlist_is_exactly_the_network_runtime_names(self):
        self.assertEqual(set(NETWORK_RUNTIME_ALLOWLIST), {
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
            "SSL_CERT_FILE", "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"})

    def test_redaction_never_carries_values(self):
        redacted = redact_network_env({
            "HTTPS_PROXY": "http://user:secret@proxy:3128",
            "NO_PROXY": "127.0.0.1,localhost"})
        self.assertEqual(redacted["HTTPS_PROXY"],
                         "present;credential-bearing-url")
        self.assertEqual(redacted["NO_PROXY"],
                         "present;no-embedded-credential")
        joined = repr(redacted)
        for secret in ("secret", "user:", "proxy:3128"):
            self.assertNotIn(secret, joined)


class _ScrubProbeFixture(unittest.TestCase):
    """Executed-child scrub proof: run a probe that dumps the child env."""

    PARENT = {
        "PATH": "/usr/bin:/bin",
        # N3 — unrelated ambient noise
        "FOO_BAR_WIDGET": "noisy",
        "EDITOR": "vim",
        # N4 — secret-like values
        "GITHUB_TOKEN": "ghp_XXnotarealtokenXX",
        "GH_TOKEN": "ghp_XXnotarealtokenXX",
        "PGPASSWORD": "prod-db-password",
        # N5 — production DSN candidates
        "DATABASE_URL": "postgres://produser:prodpw@db.internal/fie_prod",
        "FIE_INTELLIGENCE_DB":
            "postgres://produser:prodpw@db.internal/fie_prod",
        # allowlisted network context WITH a credential inside the URL
        "HTTPS_PROXY": "http://proxyuser:proxypw@proxy.internal:3128",
        "no_proxy": "127.0.0.1,localhost",
    }

    def _child_dump(self, **kwargs) -> tuple[list, str]:
        with tempfile.TemporaryDirectory(prefix="fie-net-probe-") as td:
            probe_path = Path(td) / "probe.sh"
            probe_path.write_text(ENV_DUMP + 'echo "SCRUB_DONE"\n',
                                  encoding="utf-8")
            env = child_env("/tmp", td, kwargs.get("blob"),
                            network_context_map=kwargs.get("ctx"),
                            **({"parent_env": self.PARENT}))
            res = subprocess.run(["bash", str(probe_path)], env=env,
                                 capture_output=True, text=True, timeout=60)
        lines = [line for line in res.stdout.splitlines()
                 if line and line != "SCRUB_DONE"]
        names = {line.split("=", 1)[0] for line in lines}
        return names, res.stdout + res.stderr

    def test_n3_unrelated_vars_do_not_propagate(self):
        names, _ = self._child_dump(ctx={
            "HTTPS_PROXY": self.PARENT["HTTPS_PROXY"]})
        self.assertNotIn("FOO_BAR_WIDGET", names)
        self.assertNotIn("EDITOR", names)

    def test_n4_secret_like_vars_do_not_propagate(self):
        names, out_and_err = self._child_dump(ctx={
            "HTTPS_PROXY": self.PARENT["HTTPS_PROXY"]})
        for name in ("GITHUB_TOKEN", "GH_TOKEN", "PGPASSWORD"):
            self.assertNotIn(name, names)
        for secret in ("ghp_", "prod-db-password"):
            self.assertNotIn(secret, out_and_err)

    def test_n5_production_dsn_does_not_propagate(self):
        names, out_and_err = self._child_dump(ctx={
            "HTTPS_PROXY": self.PARENT["HTTPS_PROXY"]})
        for name in ("DATABASE_URL", "FIE_INTELLIGENCE_DB"):
            self.assertNotIn(name, names)
        for token in ("produser", "prodpw", "db.internal"):
            self.assertNotIn(token, out_and_err)

    def test_child_shows_only_allowlisted_network_names_in_env_dump(self):
        names, _ = self._child_dump(ctx={
            "HTTPS_PROXY": self.PARENT["HTTPS_PROXY"],
            "no_proxy": self.PARENT["no_proxy"]})
        self.assertEqual(names, {"PATH", "PWD", "HOME", "TMPDIR",
                                 "HTTPS_PROXY", "no_proxy"})

    def test_network_context_from_live_parent_is_allowlist_filtered(self):
        netenv = network_context()
        self.assertLessEqual(set(netenv), set(NETWORK_RUNTIME_ALLOWLIST))


class _AcquisitionFixture(_ArtifactFixture):
    """N1/N2/N6/N7/N8 — allowlisted scrubbed children acquiring against the
    local loopback artifact server (shape-true acquisition)."""

    def _acquire(self, ovr: Path, srv, ctx=None, scheme="http") -> tuple[
            str, str, str]:
        home = self._readonly()  # explicit cache override is the only route
        blob = {"FIE_TEST_PG_CACHE_DIR": str(ovr),
                "FIE_TEST_PG_TEST_BASE_URL":
                    f"{scheme}://127.0.0.1:{srv.port}"}
        env = child_env(str(home), str(self._writable()), blob,
                        network_context_map=ctx if ctx is not None else {},
                        parent_env={})
        probe_temp = tempfile.mkdtemp(prefix="fie-net-ensure-")
        self.probes.append(probe_temp)
        probe_path = Path(probe_temp) / "probe.sh"
        probe_text, _ = _ensure_only()
        probe_path.write_text(probe_text, encoding="utf-8")
        res = subprocess.run(["bash", str(probe_path)], env=env,
                             capture_output=True, text=True, timeout=180)
        return res.returncode, res.stdout, res.stderr


class N1ScrubbedChildAcquisitionTests(_AcquisitionFixture):
    """N1 — scrubbed child with allowlist reaches the sidecar publication."""

    def test_n1_scrubbed_child_acquires_published_shape_sidecar(self):
        ovr = Path(self._writable()) / "ov"
        srv = self._server_for(self.jar, self.sha)
        try:
            rc, out, err = self._acquire(ovr, srv)
            self.assertIn("ENSURE_RC=0", out, msg=err)
            sidecar = ovr / "artifact.sha256"
            self.assertTrue(sidecar.is_file(),
                            "published-shape checksum sidecar must exist "
                            "after acquisition (R4-R2 blocker #2)")
            content = sidecar.read_text().strip()
            self.assertEqual(len(content), 64)
            self.assertEqual(
                content, "".join(c for c in content
                                 if c in "0123456789abcdef"))
            self.assertTrue((ovr / "bin" / "initdb").is_file())
            self.assertTrue((ovr / "portable-postgres.jar").is_file())
        finally:
            srv.stop()


class N2NetworkContextCausalityTests(_AcquisitionFixture):
    """N2 — proxy/CA context CAUSALLY changes the child's outcome."""

    def test_proxy_with_no_proxy_reaches_local_artifact(self):
        ovr = Path(self._writable()) / "ov"
        srv = self._server_for(self.jar, self.sha)
        try:
            rc, out, err = self._acquire(ovr, srv, ctx={
                "HTTP_PROXY": BLACKHOLE_PROXY, "http_proxy": BLACKHOLE_PROXY,
                "ALL_PROXY": BLACKHOLE_PROXY, "all_proxy": BLACKHOLE_PROXY,
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost"})
            self.assertIn("ENSURE_RC=0", out,
                          msg="black-holed proxy + no_proxy must still reach "
                              f"the loopback artifact: {err}")
            self.assertTrue((ovr / "artifact.sha256").is_file())
        finally:
            srv.stop()

    def test_proxy_context_without_no_proxy_fails_closed(self):
        """The SAME proxy context minus no_proxy: acquisition dies 91
        (POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE) — executed proof that the
        proxy variables genuinely reach the child (R4-R2 blocker #2
        causality: dropped context ⇒ wrong-world outcome)."""
        ovr = Path(self._writable()) / "ov"
        srv = self._server_for(self.jar, self.sha)
        try:
            rc, out, err = self._acquire(ovr, srv, ctx={
                "HTTP_PROXY": BLACKHOLE_PROXY, "http_proxy": BLACKHOLE_PROXY,
                "ALL_PROXY": BLACKHOLE_PROXY, "all_proxy": BLACKHOLE_PROXY})
            self.assertIn("ENSURE_RC=91", out, msg=err)
            self.assertIn("POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE", err)
            self.assertFalse((ovr / "portable-postgres.jar").exists(),
                             "a failed acquisition must never promote a jar")
        finally:
            srv.stop()

    def test_ca_context_enables_selfsigned_tls_acquisition(self):
        """CURL_CA_BUNDLE carried through the allowlist: a scrubbed child can
        TLS-acquire from a SELF-SIGNED local server; the same child without
        CA context fails TLS verification — causal in both directions."""
        cert_path = self._self_signed_cert()
        if cert_path is None:
            self.skipTest("openssl unavailable — CA-context leg proven in "
                          "the evidence layer (A2) instead")
        srv = self._tls_server_for(self.jar, self.sha, cert_path)
        try:
            ovr_ok = Path(self._writable()) / "ov-ok"
            rc, out, err = self._acquire(
                ovr_ok, srv, scheme="https",
                ctx={"CURL_CA_BUNDLE": str(cert_path),
                     "NO_PROXY": "127.0.0.1,localhost",
                     "no_proxy": "127.0.0.1,localhost"})
            self.assertIn("ENSURE_RC=0", out,
                          msg=f"CA-bundled child must TLS-acquire: {err}")
            self.assertTrue((ovr_ok / "artifact.sha256").is_file())
            # Control: the same child WITHOUT CA context fails TLS.
            ovr_ctrl = Path(self._writable()) / "ov-ctrl"
            rc, out, err = self._acquire(ovr_ctrl, srv, scheme="https",
                                         ctx={"NO_PROXY":
                                              "127.0.0.1,localhost"})
            self.assertIn("ENSURE_RC=91", out, msg=err)
            self.assertNotIn("ENSURE_RC=0", out)
        finally:
            srv.stop()

    # -- TLS fixture ---------------------------------------------------

    def _self_signed_cert(self):
        import tempfile
        td = tempfile.mkdtemp(prefix="fie-net-tls-")
        self.probes.append(td)
        cert = Path(td) / "ca.pem"
        key = Path(td) / "ca.key"
        res = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", "2",
             "-subj", "/CN=127.0.0.1",
             "-addext", "subjectAltName=IP:127.0.0.1"],
            capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            return None
        return cert

    def _tls_server_for(self, jar, sha, cert_path):
        from tests.test_cloud_pg_cache_resolver import (
            ARCH_TAG, VER, _StaticFileServer)

        sidecar = sha.encode("ascii") + b"\n"
        base = f"/embedded-postgres-binaries-{ARCH_TAG}/{VER}"
        files = {
            f"{base}/embedded-postgres-binaries-{ARCH_TAG}-{VER}.jar": jar,
            f"{base}/embedded-postgres-binaries-{ARCH_TAG}-{VER}.jar.sha256":
                sidecar,
        }
        server = _StaticFileServer(files, delay=0.0)
        key_path = Path(cert_path).parent / "ca.key"

        def _ssl_start():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cert_path), str(key_path))
            server.srv.socket = ctx.wrap_socket(server.srv.socket,
                                                server_side=True)
            return _StaticFileServer.start(server)

        server.start = _ssl_start  # narrow override: TLS-only wrapping
        return _ssl_start()


class N6N7N8FailClosedThroughScrubTests(_AcquisitionFixture):
    """N6/N7/N8 — fail-closed classes survive the allowlisted scrub."""

    def _scrubbed_probe(self, ovr, srv, ctx=None):
        return self._acquire(ovr, srv, ctx=ctx)

    def test_n6_checksum_mismatch_fail_closed(self):
        ovr = Path(self._writable()) / "ov"
        srv = self._server_for(self.jar, self.sha, tampered=True)
        try:
            rc, out, err = self._scrubbed_probe(ovr, srv, ctx={
                "NO_PROXY": "127.0.0.1,localhost"})
            self.assertIn("ENSURE_RC=92", out, msg=err)
            self.assertIn("POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED",
                          err)
        finally:
            srv.stop()

    def test_n7_partial_cache_artifact_rejected_and_reacquired(self):
        ovr = Path(self._writable()) / "ov"
        ovr.mkdir(parents=True, exist_ok=True)
        (ovr / "portable-postgres.jar").write_bytes(b"PK-PARTIAL-GARBAGE")
        (ovr / "artifact.sha256").write_text("f" * 64 + "\n")
        srv = self._server_for(self.jar, self.sha, delay=0.0)
        try:
            rc, out, err = self._scrubbed_probe(ovr, srv, ctx={
                "NO_PROXY": "127.0.0.1,localhost"})
            self.assertIn("ENSURE_RC=0", out, msg=err)
            self.assertFalse(
                (ovr / "artifact.sha256").read_text().startswith("f" * 64))
            self.assertTrue((ovr / "bin" / "initdb").is_file())
        finally:
            srv.stop()

    def test_n8_unreachable_artifact_source_deterministic_91(self):
        ovr = Path(self._writable()) / "ov"
        closed_port = self._find_closed_port()
        outcomes = []
        for _ in range(2):  # determinism: same outcome twice
            probe_target = type("_SrvStub", (), {"port": closed_port})()
            rc, out, err = self._acquire(ovr, probe_target)
            outcomes.append(out)
            self.assertIn("ENSURE_RC=91", out, msg=err)
            self.assertIn("POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE", err)
        self.assertEqual(outcomes[0], outcomes[1],
                         "unreachable-source classification must be "
                         "deterministic")
        self.assertFalse((ovr / "portable-postgres.jar").exists())

    @staticmethod
    def _find_closed_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port


class HelperCanonicalAgreementTests(_ArtifactFixture):
    """C3 (WO §9/D3) — the test helper resolves the ACTIVE cache by
    EXECUTING the shipped canonical resolver; exact semantic agreement
    (mode/persistent) and byte-exact dir agreement whenever persistent."""

    def test_helper_state_matches_direct_runtime_resolution(self):
        from tests.cloud_child_env import canonical_cache_state, \
            CACHE_VISIBILITY_PASSTHRU
        helper_state = canonical_cache_state(force_refresh=True)
        # Independent runtime resolution, fresh child, the SAME parent
        # cache context (HOME/XDG/TMPDIR/override passthru):
        from tests.test_cloud_pg_cache_resolver import _probe, _RESOLVE_ONLY
        blob = {name: os.environ[name] for name in CACHE_VISIBILITY_PASSTHRU
                if os.environ.get(name)}
        rc, out, err = _probe(_RESOLVE_ONLY, blob=blob)
        keys = dict(line.split("=", 1) for line in out.splitlines()
                    if "=" in line)
        self.assertIn("RESOLVE_RC=0", out, msg=err)
        self.assertEqual(helper_state["mode"], keys["CACHE_MODE"])
        self.assertEqual("yes" if helper_state["persistent"] else "no",
                         keys["CACHE_PERSISTENT"])
        if helper_state["persistent"]:
            self.assertEqual(helper_state["dir"], keys["CACHE_DIR"],
                             "helper and runtime MUST agree byte-exact on "
                             "the active persistent cache (NC16-shape: one "
                             "resolution, zero second-guessing)")
        # temp mode is per-invocation by design: both dirs must fall under
        # the same TMPDIR root.
        else:
            self.assertEqual(Path(helper_state["dir"]).parent.parent if
                             Path(helper_state["dir"]).parent.name ==
                             "fie" else Path(helper_state["dir"]).parent,
                             Path(keys["CACHE_DIR"]).parent.parent if
                             Path(keys["CACHE_DIR"]).parent.name == "fie"
                             else Path(keys["CACHE_DIR"]).parent)


class TempModeWarmDetectionTests(_ArtifactFixture):
    """C4-class — WarmPortableCache temp-mode behavior: cold persistent
    cache with allow=0 → attributed skip, zero acquisition attempts; the
    network-true warm leg is proven in the evidence layer (Task F)."""

    def test_allow0_cache_cold_classifies_and_never_warms(self):
        from tests.cloud_child_env import WarmPortableCache
        readonly_home = self._readonly()
        prior = {k: os.environ.get(k) for k in
                 ("FIE_TEST_PG_CACHE_DIR", "FIE_TEST_PG_ALLOW_REAL_ACQUISITION",
                  "XDG_CACHE_HOME", "HOME", "TMPDIR")}
        try:
            os.environ.pop("FIE_TEST_PG_CACHE_DIR", None)
            os.environ.pop("XDG_CACHE_HOME", None)
            os.environ["HOME"] = readonly_home
            os.environ["TMPDIR"] = str(self._writable())
            os.environ["FIE_TEST_PG_ALLOW_REAL_ACQUISITION"] = "0"
            with WarmPortableCache() as wc:
                self.assertFalse(wc.dir)
                self.assertIn("cache cold", wc.classification.lower())
                self.assertIn("--ensure-cache", wc.classification)
        finally:
            for name, value in prior.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()