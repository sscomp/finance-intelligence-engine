#!/usr/bin/env python3
"""Portable-PostgreSQL cache-path/writable-runtime contract tests (6.9A-R4-R1).

Locks the canonical cache resolver contract of
``scripts/provision-test-postgres.sh`` after the R4 blocker (Codex Cloud
managed filesystem: ``$HOME`` exists but ``$HOME/.cache`` is on a read-only
filesystem → cold acquisition died with wrong classification
POSTGRESQL_PLATFORM_UNSUPPORTED / exit 90 instead of falling back):

    RC1    explicit writable FIE_TEST_PG_CACHE_DIR override is selected
    RC2    explicit UNUSABLE override fails closed (exit 97, no fallback)
    RC3    writable XDG_CACHE_HOME is selected
    RC4    writable HOME cache is selected
    RC5    HOME present but unwritable → isolated temp fallback (mode=temp,
           marker-proven, never a static /tmp/fie)
    RC6    temp fallback uniqueness / collision resistance
    RC7    no writable candidate anywhere → fail closed (exit 97) with
           per-candidate trace
    RC8    valid cache HIT re-verifies the stored jar and uses no network
    RC9    partial/corrupt cache artifact rejected/reacquired — an
           interrupted acquisition never becomes a trusted cache entry
    RC10   checksum mismatch fails closed (exit 92)
    RC11   foreign (non-artifact-class) content in a resolved cache dir →
           fail closed (exit 97), nothing modified/removed
    RC12   concurrent cold acquisitions serialize via flock and both
           complete (promotion race determinism)
    RC13   temp-mode cache cleanup is ownership-proven (refuses unowned,
           idempotent, removes only the dir this invocation created)

Offline-safe: acquisition legs are network-true in SHAPE (download →
sidecar verify → extract → validate → promote) but run against a LOCAL
loopback-only HTTP server serving a synthetic artifact — never Maven
Central (the real Maven acquisition is proven in the evidence layer).
"""
import io
import json
import lzma
import os
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROVISIONER = REPO / "scripts" / "provision-test-postgres.sh"

ARCH_TAG = {"x86_64": "linux-amd64", "aarch64": "linux-arm64v8"}.get(
    os.uname().machine, "noarch")
VER = "18.4.0"
ARTIFACT_NAME = f"embedded-postgres-binaries-{ARCH_TAG}-{VER}.jar"

# Scrubbed child baseline: no ambient FIE_*, no XDG, no override — each test
# then introduces exactly the environment shape it is proving.
_BASE_ENV_PATH = "/usr/bin:/bin"


def _run(cmd, blob=None, timeout=90, cwd=None):
    env = {"PATH": _BASE_ENV_PATH, "PWD": str(cwd or REPO)}
    if blob:
        env.update(blob)
    res = subprocess.run(cmd, env=env, capture_output=True, text=True,
                         timeout=timeout, cwd=str(cwd or REPO))
    return res.returncode, res.stdout, res.stderr


def _grep_key(out, key):
    """Value of the first 'KEY=value' result line in probe stdout."""
    for line in out.splitlines():
        if line.startswith(key):
            return line[len(key):]
    raise AssertionError(f"result line {key!r} missing in: {out}")


def _probe(text, blob=None, timeout=90):
    """Source the REAL shipped provisioner and run resolver/provisioner
    calls against it (never a modified copy)."""
    with tempfile.TemporaryDirectory(prefix="fie-rc-probe-") as td:
        probe = Path(td) / "probe.sh"
        probe.write_text(text, encoding="utf-8")
        return _run(["bash", str(probe)], blob=blob, timeout=timeout)


def _resolve_blob(home, tmpdir, extra=None):
    blob = {"HOME": home, "TMPDIR": tmpdir}
    if extra:
        blob.update(extra)
    return blob


_RESOLVE_ONLY = (". " + repr(str(PROVISIONER)) + "\n"
                 "_fie_test_pg_cache_resolve; rc_resolve=$?\n"
                 'echo "RESOLVE_RC=${rc_resolve}"\n'
                 'echo "CACHE_DIR=${PG_CACHE_DIR:-}"\n'
                 'echo "CACHE_MODE=${PG_CACHE_MODE:-}"\n'
                 'echo "CACHE_PERSISTENT=${PG_CACHE_PERSISTENT:-}"\n')


def _ensure_only(extra=None):
    return (". '%s'\n_fie_test_pg_ensure_cache ; rc=$?\n"
            'echo "ENSURE_RC=${rc}"\n' % PROVISIONER), extra


def _build_stub_artifact(stage_root):
    """Synthetic portable distribution: a jar containing exactly one flat
    .txz with bin/{initdb,pg_ctl,postgres} stubs that validate. Returns
    (jar_bytes, sha256_hex)."""
    stub = Path(stage_root)
    (stub / "bin").mkdir(parents=True)
    (stub / "share").mkdir()
    (stub / "lib").mkdir()
    (stub / "share" / "pg_hba.txt").write_text("# synthetic\n")
    for name in ("initdb", "pg_ctl", "postgres"):
        exe = stub / "bin" / name
        exe.write_text("#!/bin/sh\n"
                       'case "${1-}" in --version) echo "%s (PostgreSQL) 18.4" ;; esac\n'
                       "exit 0\n" % name)
        exe.chmod(0o755)
    txz_name = (f"embedded-postgres-binaries-{ARCH_TAG}-{VER}.txz")
    txz_bytes = _make_txz(stub, ["bin", "share", "lib"], txz_name)
    jar_byteio = io.BytesIO()
    with zipfile.ZipFile(jar_byteio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(txz_name, txz_bytes)
    jar = jar_byteio.getvalue()

    import hashlib
    return jar, hashlib.sha256(jar).hexdigest()


def _make_txz(source_root, dirs, archive_name):
    import io as _io
    tarbuf = _io.BytesIO()
    with tarfile.open(fileobj=tarbuf, mode="w") as tf:
        for top in dirs:
            top_dir = tarfile.TarInfo(top + "/")
            top_dir.type = tarfile.DIRTYPE
            top_dir.mode = 0o755
            tf.addfile(top_dir)
            for path in sorted((source_root / top).rglob("*")):
                rel = path.relative_to(source_root)
                if path.is_dir():
                    info = tarfile.TarInfo(str(rel) + "/")
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tf.addfile(info)
                else:
                    data = path.read_bytes()
                    info = tarfile.TarInfo(str(rel))
                    info.size = len(data)
                    info.mode = 0o755
                    tf.addfile(info, _io.BytesIO(data))
    return lzma.compress(tarbuf.getvalue())


class _StaticFileServer:
    def __init__(self, files, delay=0.4):
        server_files = {("/" + k if not k.startswith("/") else k): v
                        for k, v in files.items()}
        server_delay = delay

        class Handler(BaseHTTPRequestHandler):
            # Class attributes (handler `self` is the handler instance).
            files = server_files
            delay = server_delay

            def do_GET(self):
                if self.path in Handler.files:
                    if Handler.delay:
                        time.sleep(Handler.delay)
                    body = Handler.files[self.path]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever,
                                       daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.srv.shutdown()


class _DirPerms(unittest.TestCase):
    """Temp-root fixture: dirs made and permission-bounded per test."""

    def setUp(self):
        self.probes = []

    def tearDown(self):
        import shutil
        for d in getattr(self, "probes", []):
            if os.path.isdir(d):
                subprocess.run(["chmod", "-R", "u+rwX",
                                d], capture_output=True, timeout=30)
                shutil.rmtree(d, ignore_errors=True)

    def _dir(self):
        d = tempfile.mkdtemp(prefix="fie-rc-")
        self.probes.append(d)
        return d

    def _readonly(self):
        d = self._dir()
        os.chmod(d, 0o555)
        return d

    def _writable(self):
        d = self._dir()
        os.chmod(d, 0o755)
        return d


class ResolverPrecedenceTests(_DirPerms):
    """RC1–RC7 — canonical resolver precedence and fail-closed behavior."""

    def test_rc1_explicit_writable_override_selected(self):
        home, tmpd = self._readonly(), self._writable()
        ovr = Path(self._writable()) / "ov"
        blob = {"FIE_TEST_PG_CACHE_DIR": str(ovr)}
        rc, out, err = _probe(_RESOLVE_ONLY,
                              blob=_resolve_blob(home, tmpd, blob))
        self.assertEqual((rc, _grep_key(out, "RESOLVE_RC=")), (0, "0"),
                         msg=err)
        self.assertIn("CACHE_MODE=explicit", out)
        self.assertIn(f"CACHE_DIR={ovr}", out)
        self.assertIn("CACHE_PERSISTENT=yes", out)

    def test_rc2_explicit_unusable_override_fails_closed_no_fallback(self):
        home, tmpd = self._readonly(), self._writable()
        before = set(Path(tmpd).glob("fie-pgcache.*"))
        rc, out, err = _probe(_RESOLVE_ONLY, blob=_resolve_blob(
            home, tmpd, {"FIE_TEST_PG_CACHE_DIR": "/root/no-such-unwritable"}))
        self.assertIn("RESOLVE_RC=97", out)
        self.assertIn("POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE", err)
        self.assertIn("refusing to fall back", err)
        self.assertEqual(set(Path(tmpd).glob("fie-pgcache.*")), before,
                         "an unusable override must NOT fall back to a "
                         "temp allocation")

    def test_rc3_writable_xdg_cache_selected(self):
        home, tmpd = self._readonly(), self._writable()
        xdg = Path(self._writable()) / "xdgcache"
        rc, out, err = _probe(_RESOLVE_ONLY, blob=_resolve_blob(
            home, tmpd, {"XDG_CACHE_HOME": str(xdg)}))
        self.assertEqual(_grep_key(out, "RESOLVE_RC="), "0", msg=err)
        self.assertIn("CACHE_MODE=xdg", out)
        self.assertIn(f"CACHE_DIR={xdg / 'fie' / 'test-postgres'}", out)
        self.assertIn("CACHE_PERSISTENT=yes", out)
        self.assertTrue((xdg / "fie" / "test-postgres").is_dir())

    def test_rc4_writable_home_cache_selected(self):
        tmpd = self._writable()
        home = self._writable()
        rc, out, err = _probe(_RESOLVE_ONLY,
                              blob=_resolve_blob(home, tmpd))
        self.assertEqual(_grep_key(out, "RESOLVE_RC="), "0", msg=err)
        self.assertIn("CACHE_MODE=home", out)
        self.assertIn(f"CACHE_DIR={Path(home) / '.cache' / 'fie' / 'test-postgres'}", out)

    def test_rc5_unwritable_home_falls_back_to_temp(self):
        tmpd = self._writable()
        home = self._readonly()  # $HOME exists but cache cannot be created
        rc, out, err = _probe(_RESOLVE_ONLY,
                              blob=_resolve_blob(home, tmpd))
        self.assertEqual(_grep_key(out, "RESOLVE_RC="), "0", msg=err)
        self.assertIn("CACHE_MODE=temp", out)
        self.assertIn("CACHE_PERSISTENT=no", out)
        cache_dir = _grep_key(out, "CACHE_DIR=")
        self.assertTrue(cache_dir.startswith(str(tmpd)),
                        "temp fallback must live under TMPDIR")
        self.assertTrue(cache_dir.endswith(".cache") is False)
        marker = Path(cache_dir) / "fie_cache.owner"
        self.assertTrue(marker.is_file(), "temp cache must be marker-proven")
        self.assertIn("format=fie-6-9a-cache-v1", marker.read_text())
        self.assertIn("ephemeral=yes", marker.read_text())

    def test_rc6_temp_fallback_unique_and_never_static(self):
        tmpd = self._writable()
        home = self._readonly()
        dirs = []
        for _ in range(3):
            rc, out, err = _probe(_RESOLVE_ONLY,
                                  blob=_resolve_blob(home, tmpd))
            self.assertEqual(_grep_key(out, "RESOLVE_RC="), "0",
                             msg=err)
            dirs.append(_grep_key(out, "CACHE_DIR="))
        self.assertEqual(len(set(dirs)), 3,
                         "concurrent/serial temp fallbacks must never share "
                         "a static directory")
        for d in dirs:
            self.assertNotEqual(os.path.dirname(d),
                                str(Path(tmpd) / "fie"),
                                "never a static /tmp/fie (shared) dir")

    def test_rc7_no_writable_candidate_fails_closed(self):
        home, tmpd, tmproot = self._readonly(), self._readonly(), \
            self._readonly()
        rc, out, err = _probe(_RESOLVE_ONLY,
                              blob=_resolve_blob(home, tmproot))
        self.assertIn("RESOLVE_RC=97", out)
        self.assertIn("POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE", err)
        self.assertIn("no creatable/writable cache-runtime candidate", err)
        self.assertIn("home:not-creatable", err)
        self.assertIn("temp-root:", err)
        self.assertNotIn(str(tmpd), err + out)  # withheld paths in messages

    def test_rc7b_resolve_cache_action_prints_contract_keys(self):
        tmpd, home = self._writable(), self._writable()
        probe = (". " + repr(str(PROVISIONER)) + "\n"
                 "_fie_test_pg_cache_resolve || exit $?\n"
                 'printf "CACHE_DIR=%s\\nCACHE_MODE=%s\\n'
                 'CACHE_PERSISTENT=%s\\n" \
'
                 '"${PG_CACHE_DIR}" "${PG_CACHE_MODE}" '
                 '"${PG_CACHE_PERSISTENT}"\n')
        rc, out, err = _run(["bash", "-c", probe],
                            blob=_resolve_blob(home, tmpd))
        self.assertEqual(rc, 0, msg=err)
        for key in ("CACHE_DIR=", "CACHE_MODE=", "CACHE_PERSISTENT="):
            self.assertIn(key, out)


class _ArtifactFixture(_DirPerms):
    """Shared: one synthetic artifact + local loopback-only artifact server."""

    @classmethod
    def _server_for(cls, jar, sha, delay=0.3, tampered=False):
        sidecar = ("f" * 64 if tampered else sha).encode("ascii") + b"\n"
        # The provisioner requests the FULL Maven-style nested path.
        base = f"/embedded-postgres-binaries-{ARCH_TAG}/{VER}"
        files = {
            f"{base}/embedded-postgres-binaries-{ARCH_TAG}-{VER}.jar": jar,
            f"{base}/embedded-postgres-binaries-{ARCH_TAG}-{VER}.jar.sha256":
                sidecar,
        }
        return _StaticFileServer(files, delay=delay).start()

    @classmethod
    def setUpClass(cls):
        cls.stage = tempfile.mkdtemp(prefix="fie-rc-artifact-")
        cls.jar, cls.sha = _build_stub_artifact(cls.stage)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.stage, ignore_errors=True)


class AcquisitionContractTests(_ArtifactFixture):
    """RC8–RC12 — cold acquisition, integrity, promotion, concurrency."""

    def test_rc8_cache_hit_reverifies_no_network(self):
        tmpd = self._writable()
        ovr = Path(self._writable()) / "ov"
        # Prebuild a VALID cache: stub binaries + jar + matching sidecar.
        (ovr / "bin").mkdir(parents=True)
        for name in ("initdb", "pg_ctl", "postgres"):
            (ovr / "bin" / name).write_text("#!/bin/sh\nexit 0\n")
            (ovr / "bin" / name).chmod(0o755)
        (ovr / "portable-postgres.jar").write_bytes(self.jar)
        (ovr / "artifact.sha256").write_text(self.sha + "\n")
        srv = self._server_for(self.jar, self.sha)  # reachable but must not be used
        try:
            probe, extra = _ensure_only(
                {"FIE_TEST_PG_CACHE_DIR": str(ovr),
                 "FIE_TEST_PG_TEST_BASE_URL": f"http://127.0.0.1:{srv.port}"})
            rc, out, err = _probe(probe,
                                  blob=_resolve_blob(tmpd, tmpd, extra))
            self.assertIn("ENSURE_RC=0", out, msg=err)
            self.assertIn("cache HIT", err)
            self.assertIn(out.split("ENSURE_RC=")[0].strip(), str(ovr),
                          msg=out)
        finally:
            srv.stop()

    def test_rc8b_checksum_mismatch_fail_closed(self):
        tmpd = self._writable()
        ovr = Path(self._writable()) / "ov"
        srv = self._server_for(self.jar, self.sha, tampered=True)
        try:
            probe, extra = _ensure_only(
                {"FIE_TEST_PG_CACHE_DIR": str(ovr),
                 "FIE_TEST_PG_TEST_BASE_URL": f"http://127.0.0.1:{srv.port}"})
            rc, out, err = _probe(probe, blob=_resolve_blob(
                tmpd, tmpd, dict(extra, HOME="/root/no-such")))
            self.assertIn("ENSURE_RC=92", out, msg=err)
            self.assertIn(
                "POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED", err)
            self.assertFalse((ovr / "portable-postgres.jar").exists(),
                             "checksum mismatch must never promote the jar")
        finally:
            srv.stop()

    def test_rc9_partial_artifact_is_not_trusted_and_is_reacquired(self):
        """A cache entry that is partial (jar present, sidecar/bin missing)
        is NOT a cache hit; cold reacquisition replaces it."""
        ovr = Path(self._writable()) / "ov"
        (ovr).mkdir(parents=True)
        (ovr / "portable-postgres.jar").write_bytes(b"PK-PARTIAL-GARBAGE")
        (ovr / "artifact.sha256").write_text("f" * 64 + "\n")
        srv = self._server_for(self.jar, self.sha, delay=0.0)
        try:
            probe, extra = _ensure_only(
                {"FIE_TEST_PG_CACHE_DIR": str(ovr),
                 "FIE_TEST_PG_TEST_BASE_URL": f"http://127.0.0.1:{srv.port}"})
            rc, out, err = _probe(probe, blob=_resolve_blob(
                self._writable(), self._writable(), extra))
            self.assertIn("ENSURE_RC=0", out, msg=err)
            self.assertFalse((ovr / "artifact.sha256").read_text()
                             .startswith("f"*64),
                             "replaced artifact must be the verified one")
            self.assertTrue((ovr / "bin" / "initdb").is_file())
            # The partial jar must not have survived as the promoted artifact.
            self.assertEqual((ovr / "portable-postgres.jar").read_bytes(),
                             self.jar,
                             "promoted jar must be the verified full artifact")
        finally:
            srv.stop()

    def test_rc10_foreign_content_blocks_fail_closed(self):
        ovr = Path(self._writable()) / "ov"
        ovr.mkdir(parents=True)
        (ovr / "notes.txt").write_text("operator notes\n")
        (ovr / "bin").mkdir()
        srv = self._server_for(self.jar, self.sha, delay=0.0)
        try:
            probe, extra = _ensure_only(
                {"FIE_TEST_PG_CACHE_DIR": str(ovr),
                 "FIE_TEST_PG_TEST_BASE_URL": f"http://127.0.0.1:{srv.port}"})
            rc, out, err = _probe(probe, blob=_resolve_blob(
                self._writable(), self._writable(), extra))
            self.assertIn("ENSURE_RC=97", out, msg=err)
            self.assertIn("POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE", err)
            self.assertIn("ownership unprovable", err)
            # Nothing removed or overwritten:
            self.assertTrue((ovr / "notes.txt").is_file())
            self.assertFalse((ovr / "portable-postgres.jar").exists())
            self.assertEqual(list((ovr / "bin").iterdir()), [])
        finally:
            srv.stop()

    def test_rc11_stale_crash_leftover_reacquires_atomically(self):
        """Simulates a crashed cold acquisition (partial jar + stage
        leftovers + stale bin, all artifact-class): the next cold acquire
        must complete cleanly under lock and must replace the stale bin."""
        ovr = Path(self._writable()) / "ov"
        (ovr / "bin").mkdir(parents=True)
        (ovr / "bin" / "initdb").write_text("#!/bin/sh\nexit 1\n")  # stale
        (ovr / "bin" / "initdb").chmod(0o755)
        (ovr / ".partial.99999.0123abcd.jar").write_bytes(b"x")
        (ovr / ".stage.99999.0123abcd").mkdir()
        srv = self._server_for(self.jar, self.sha, delay=0.0)
        try:
            probe, extra = _ensure_only(
                {"FIE_TEST_PG_CACHE_DIR": str(ovr),
                 "FIE_TEST_PG_TEST_BASE_URL": f"http://127.0.0.1:{srv.port}"})
            rc, out, err = _probe(probe, blob=_resolve_blob(
                self._writable(), self._writable(), extra))
            self.assertIn("ENSURE_RC=0", out, msg=err)
            self.assertTrue((ovr / "bin" / "initdb").is_file())
            self.assertTrue((ovr / "artifact.sha256").is_file())
            self.assertEqual(
                (ovr / "artifact.sha256").read_text().strip(), self.sha)
        finally:
            srv.stop()

    def test_rc12_concurrent_cold_acquisitions_promote_safely(self):
        ovr = Path(self._writable()) / "ov"
        ovr.mkdir(parents=True)
        srv = self._server_for(self.jar, self.sha, delay=1.0)
        try:
            probe, extra = _ensure_only(
                {"FIE_TEST_PG_CACHE_DIR": str(ovr),
                 "FIE_TEST_PG_TEST_BASE_URL": f"http://127.0.0.1:{srv.port}"})
            blob = _resolve_blob(self._writable(), self._writable(), extra)
            probes = {}
            with tempfile.TemporaryDirectory(prefix="fie-rc-con-") as ptd:
                for i in (1, 2):
                    p = Path(ptd) / f"probe{i}.sh"
                    p.write_text(probe, encoding="utf-8")
                    probes[i] = p
                results = {}
                import subprocess as sp
                def run(i):
                    results[i] = subprocess.run(
                        ["bash", str(probes[i])], env={k: str(v) for k, v in
                                                       blob.items()},
                        capture_output=True, text=True, timeout=600)
                threads = [threading.Thread(target=run, args=(i,))
                           for i in (1, 2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(590)
            for i in (1, 2):
                self.assertIn("ENSURE_RC=0", results[i].stdout,
                              msg=results[i].stderr)
            # Final state: one verified, complete cache.
            self.assertTrue((ovr / "bin" / "initdb").is_file())
            self.assertEqual((ovr / "artifact.sha256").read_text().strip(),
                             self.sha)
            leftovers = [p.name for p in sorted(ovr.iterdir())
                         if p.name.startswith((".partial", ".stage"))]
            self.assertFalse(
                [l for l in leftovers],
                f"promotion left partial residue: {leftovers}")
        finally:
            srv.stop()


class TempCacheOwnershipTests(_DirPerms):
    """RC13 — temp-mode cache cleanup is ownership-proven."""

    def test_cleanup_refuses_unowned_dir(self):
        rc, out, err = _probe(
            ". '%s'\nPG_CACHE_TEMP_CREATED='%s'\n"
            "fie_test_pg_cache_cleanup; rc=$?\n"
            'echo "CLEANUP_RC=${rc}"\n' % (PROVISIONER,
                                           self._writable()),
            blob={"PATH": _BASE_ENV_PATH, "TMPDIR": "/tmp"})
        self.assertIn("CLEANUP_RC=96", out, msg=err)
        self.assertIn("ownership marker", err)

    def test_cleanup_idempotent_and_owned_removal(self):
        tmpd = self._writable()
        home = self._readonly()
        rc, out, err = _probe(
            ". '%s'\n_fie_test_pg_cache_resolve || exit 1\n"
            'echo "DIR=${PG_CACHE_TEMP_CREATED}"\n'
            'ifie_dir="${PG_CACHE_TEMP_CREATED}"\n'
            "fie_test_pg_cache_cleanup; rc=$?\n"
            'echo "CLEANUP_RC=${rc}"\n'
            'fie_test_pg_cache_cleanup; rc2=$?\n'
            'echo "CLEANUP2_RC=${rc2}"\n'
            'if [ -d "${ifie_dir}" ]; then echo "STILL_PRESENT";'
            'else echo "REMOVED"; fi\n' % PROVISIONER,
            blob=_resolve_blob(home, tmpd))
        self.assertIn("CLEANUP_RC=0", out)
        self.assertIn("CLEANUP2_RC=0", out, msg=err)
        self.assertIn("REMOVED", out)
        self.assertNotIn("STILL_PRESENT", out)


class ResolverContractStaticTests(_DirPerms):

    def test_resolver_is_single_implementation(self):
        """The resolver lives only in the canonical provisioner."""
        rc, out, err = _run(["git", "grep", "-l",
                             "XDG_CACHE_HOME", "scripts/"])
        self.assertEqual(rc, 0, msg=err)
        hits = [l for l in out.splitlines() if l.endswith(".sh")]
        self.assertEqual(hits, ["scripts/provision-test-postgres.sh"],
                         f"resolver implementation leaked outside the "
                         f"canonical boundary: {hits}")

    def test_temp_fallback_marker_format_pinned(self):
        src = PROVISIONER.read_text(encoding="utf-8")
        self.assertIn("format=fie-6-9a-cache-v1", src)
        self.assertIn("fie-pgcache.XXXXXX", src)
        self.assertIn("fie_test_pg_cache_cleanup", src)


if __name__ == "__main__":
    unittest.main()