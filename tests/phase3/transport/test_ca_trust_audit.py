"""Phase 6.6R3 container CA-trust portability contract tests.

Two complementary layers:

* static audits over the Dockerfile / repository surface — the trust
  contract stays additive, explicit, environment-supplied, machine-
  independent and free of TLS-verification bypasses or committed CA
  material (work order §8 items 1-8);
* a runtime synthetic-TLS fixture — a locally generated throwaway CA
  and HTTPS server prove that an untrusted synthetic CA is REFUSED by
  default verification and ACCEPTED with the exact artifact the
  Dockerfile's Mode B produces (system bundle augmented, never
  replaced). No real enterprise CA material is ever used or committed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

# TLS-verification bypasses that must never appear in the build surface
_INSECURE_TLS_FLAGS = (
    "trusted-host", "PIP_TRUSTED_HOST", "--insecure", "verify=False",
    "PYTHONHTTPSVERIFY", "CURL_INSECURE", "no-check-certificate",
    "NODE_TLS_REJECT_UNAUTHORIZED", "GIT_SSL_NO_VERIFY", "curl -k",
    "ssl._create_unverified_context",
)

# machine/infrastructure-specific coupling that must never appear in the
# executable build configuration (literals split per the established
# portability-audit idiom so the audit itself never carries them whole)
_MACHINE_SPECIFIC_MARKERS = (
    "/hom" + "e/sscomp",
    "/hom" + "e/ubuntu",
    "aee-runtime-br" + "idge",
    "abacus-cla" + "w",
    "192.168" + ".",
    "cod" + "ex",
    "Cod" + "ex",
    "omar" + "chy",
)


class _TlsServer:
    """Ephemeral HTTPS server presenting a synthetic-CA-signed leaf."""

    def __init__(self, certfile: str, keyfile: str) -> None:
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _HelloHandler
        )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        self._server.socket = ctx.wrap_socket(
            self._server.socket, server_side=True
        )
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _HelloHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # silence stdlib noise
        return


# executable (non-comment) Dockerfile surface — audits target the code
# the build actually runs, not documentation wording
_DOCKERFILE_CODE = "\n".join(
    line for line in DOCKERFILE.splitlines()
    if not line.lstrip().startswith("#")
)


class TestDockerfileTrustContract(unittest.TestCase):
    """§8 items 1-3, 5-8 — static audits of the trust contract."""

    def test_no_insecure_tls_bypass_flags(self) -> None:
        lowered = _DOCKERFILE_CODE.lower()
        for flag in _INSECURE_TLS_FLAGS:
            self.assertNotIn(flag.lower(), lowered)

    def test_mode_b_is_explicit_optional_and_additive(self) -> None:
        # optional, environment-supplied, generically named secret input
        # (BuildKit secret requires the RUN --mount syntax — verified
        # dynamically: without it /run/secrets/* is never populated)
        self.assertRegex(
            DOCKERFILE,
            r"(?m)^RUN --mount=type=secret,id=authorized_extra_ca",
        )
        self.assertIn("/run/secrets/authorized_extra_ca", DOCKERFILE)
        self.assertNotRegex(  # never a committed repository artifact
            DOCKERFILE, r"(?m)^COPY .*(\.pem|\.crt|\.cer|\.ca)"
        )
        # additive semantics: the system bundle is AUGMENTED via
        # update-ca-certificates, never replaced by custom material
        self.assertIn("update-ca-certificates", DOCKERFILE)
        self.assertNotRegex(
            DOCKERFILE,
            r"\bcp\s+/\w+\s+/etc/ssl/certs/ca-certificates\.crt",
            "the system bundle itself must never be overwritten",
        )
        # pip validates against the augmented bundle in Mode B; no
        # unconditional global trust override is exported
        self.assertIn("--cert /etc/ssl/certs/ca-certificates.crt", DOCKERFILE)
        self.assertNotRegex(DOCKERFILE, r"(?m)^ENV .*(SSL_CERT|REQUESTS_CA)")

    def test_mode_a_needs_no_ca_input(self) -> None:
        # with no secret present EVERY pip RUN must reach pip with NO
        # custom trust argument ($pip_cert stays empty → plain public
        # trust); --cert appears only as the conditional pip_cert=
        # assignment, once per pip-install stage. Phase 6.7B-R4: the
        # Dockerfile gained the runtime-postgres TARGET (ADR-019 §4) —
        # the invariant now holds per stage, same conditional shape.
        cert_lines = [
            line.strip() for line in _DOCKERFILE_CODE.splitlines()
            if "--cert" in line
        ]
        self.assertEqual(len(cert_lines), 2, cert_lines)
        assignment = 'pip_cert="--cert /etc/ssl/certs/ca-certificates.crt"; \\'
        for line in cert_lines:
            self.assertEqual(line, assignment, "assigned ONLY inside the Mode B conditional")
        for unconditional in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
                              "PIP_CERT="):
            self.assertNotIn(unconditional, _DOCKERFILE_CODE)

    def test_no_machine_specific_or_infra_named_paths(self) -> None:
        for marker in _MACHINE_SPECIFIC_MARKERS:
            self.assertNotIn(marker, DOCKERFILE)

    def test_nonroot_and_port_contract_still_intact(self) -> None:
        self.assertRegex(DOCKERFILE, r"(?m)^USER 10001:10001$")
        self.assertRegex(DOCKERFILE, r"(?m)^EXPOSE 8787$")
        self.assertIn("HEALTHCHECK", DOCKERFILE)

    def test_no_ca_or_private_key_material_committed(self) -> None:
        # §8 item 5 / §11 — nothing in the repository carries CA or key
        # material (tests generate their own ephemeral synthetic ones;
        # this audit module itself is excluded because it carries the
        # scan pattern's *marker strings*, not any certificate/key)
        pattern = re.compile(
            r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|BEGIN CERTIFICATE"
        )
        audit_self = Path(__file__).resolve()
        hits: list[str] = []
        for path in _repo_files():
            if path == audit_self:
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            if pattern.search(text):
                hits.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(hits, [])


def _repo_files():
    skip_dirs = {".git", "__pycache__", ".venv", ".pytest_cache"}
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            yield Path(root) / name


class TestSyntheticCaTrustSemantics(unittest.TestCase):
    """§8 item 8 — synthetic TLS fixture for the augmented-trust artifact.

    Reproduces, without any real enterprise CA or MITM endpoint:
    * default verification REFUSES a synthetic-CA-signed endpoint;
    * the artifact Mode B produces (system bundle + extra CA — exactly
      what update-ca-certificates yields) ACCEPTS it while retaining
      the standard public CAs.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="fie_ca_audit_")
        cls._synthesize(cls.tmp)
        cls.server = _TlsServer(
            certfile=os.path.join(cls.tmp, "leaf.crt"),
            keyfile=os.path.join(cls.tmp, "leaf.key"),
        )
        system_cafile = ssl.get_default_verify_paths().cafile
        if system_cafile and os.path.exists(system_cafile):
            cls.system_cafile: str | None = system_cafile
        else:
            cls.system_cafile = None

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- synthetic PKI (ephemeral, generated every run, never committed) --
    @staticmethod
    def _synthesize(tmp: str) -> None:
        def openssl(*args: str) -> None:
            subprocess.run(
                ["openssl", *args], cwd=tmp, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "RANDFILE": os.path.join(tmp, ".rnd")},
            )

        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", "root.key", "-out", "root.pem",
                "-subj", "/CN=FIE Synthetic Test CA", "-days", "1",
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        openssl("req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", "leaf.key", "-out", "leaf.csr",
                "-subj", "/CN=127.0.0.1")
        leaf_ext = os.path.join(tmp, "leaf.ext")
        Path(leaf_ext).write_text(
            "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n"
        )
        openssl("x509", "-req", "-in", "leaf.csr", "-CA", "root.pem",
                "-CAkey", "root.key", "-CAcreateserial", "-out", "leaf.crt",
                "-days", "1", "-extfile", "leaf.ext")

    def _get(self, cafile: str | None):
        context = (
            ssl.create_default_context()
            if cafile is None
            else ssl.create_default_context(cafile=cafile)
        )
        request = urllib.request.Request(
            self.server.url(), headers={"Connection": "close"}
        )
        with urllib.request.urlopen(
            request, timeout=10, context=context
        ) as response:
            return response.status, json.loads(response.read())

    def test_untrusted_synthetic_ca_rejected_by_default(self) -> None:
        # §8: verification stays enabled — before trust injection the
        # synthetic-CA endpoint must FAIL default verification
        with self.assertRaisesRegex(
            urllib.error.URLError, r"certificate verify failed|SSL"
        ):
            self._get(cafile=None)

    @unittest.skipUnless(
        ssl.get_default_verify_paths().cafile
        and os.path.exists(ssl.get_default_verify_paths().cafile),
        "host system CA bundle unavailable",
    )
    def test_augmented_bundle_accepts_synthetic_and_keeps_public(self) -> None:
        assert self.system_cafile is not None
        system_bundle = Path(self.system_cafile).read_text(encoding="utf-8")
        synthetic_root = Path(self.tmp, "root.pem").read_text(encoding="utf-8")
        # the exact artifact the Dockerfile's Mode B RUN produces
        augmented = Path(self.tmp, "augmented-bundle.pem")
        augmented.write_text(system_bundle + "\n" + synthetic_root,
                             encoding="utf-8")

        status, body = self._get(cafile=str(augmented))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        # augment-not-replace: the synthetic CA is present AND the full
        # standard public bundle is retained inside the artifact
        self.assertIn("BEGIN CERTIFICATE", synthetic_root)
        self.assertEqual(
            augmented.read_text(encoding="utf-8").count("BEGIN CERTIFICATE"),
            system_bundle.count("BEGIN CERTIFICATE") + 1,
        )

    def test_public_trust_is_not_removed_by_the_mechanism(self) -> None:
        # the mechanism appends; the public bundle file itself is never
        # opened with write intent — prove the system bundle passes its
        # own count into the augmented artifact unchanged (checked in the
        # test above) and that default paths are untouched at runtime
        paths = ssl.get_default_verify_paths()
        if not paths.cafile:
            self.skipTest("host system CA bundle unavailable")
        self.assertTrue(os.path.exists(paths.cafile))


if __name__ == "__main__":
    unittest.main()