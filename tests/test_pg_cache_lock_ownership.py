#!/usr/bin/env python3
"""Cache acquisition-lock ownership contract — 6.9A-R4-R2-R1 Task E (WO §10).

R4-R2 blocker #4-adjacent (cleanup semantics): ``fie_test_pg_cache_cleanup``
must be able to remove a stale job-owned acquisition lock WITHOUT ever
touching a lock owned by a live holder or one whose ownership cannot be
proven. Contract:

    LK1    acquire writes fie-ownership metadata UNDER the held flock
           (format=fie-6-9a-acquire-lock-v1 + invocation/pid/created_utc)
    LK2    classify() is non-mutating; ABSENT / STALE_JOB_OWNED (fie
           metadata, flock free) / ACTIVE (any live flock holder) /
           UNKNOWN (unparseable content, incl. legacy 0-byte locks)
    LK3    cleanup removes a STALE_JOB_OWNED lock (classification result
           only — the holder proved dead by flock)
    LK4    cleanup PRESERVES an ACTIVE lock byte-for-byte (never removed
           from under a live holder) and the associated cache dir
    LK5    cleanup FAILS CLOSED on UNKNOWN (exit 96) — preserved+
           reported; ambiguous residue is never deleted
    LK6    the acquire loop tolerates a concurrently-replaced lock file
           (the deleted-inode retry via the /proc readlink marker) and
           the null-`exec` stderr-preservation rule holds
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROVISIONER = REPO / "scripts" / "provision-test-postgres.sh"

CLASSIFY_PROBE = ('. ' + repr(str(PROVISIONER)) + '\n'
                  'fie_test_pg_cache_lock_classify "$1"; '
                  'rc=$?; echo "CLS_RC=${rc}"; exit ${rc}\n')

CLEANUP_PROBE = ('. ' + repr(str(PROVISIONER)) + '\n'
                 'fie_test_pg_cache_cleanup 2>&1; echo '
                 '"CLEANUP_RC=$?"\n')

# R4-R5-R1 Task D probes — the canonical acquisition-ownership claim.
# Claim runs in THIS shell; fd 9 stays held until the probe exits. Bounded
# wait via the documented FIE_TEST_PG_LOCK_WAIT_SEC knob (default 300s).
CLAIM_PROBE = ('. ' + repr(str(PROVISIONER)) + '\n'
               'if _fie_cache_lock_acquisition_claim "$1"; then\n'
               '    echo "CLAIM=OK"\n'
               '    printf "FD_TARGET=%s\\n" "$(readlink /proc/self/fd/9 2>/dev/null)"\n'
               'else\n'
               '    rc=$?; echo "CLAIM_RC=${rc}"\n'
               'fi\n'
               'if [ -f "$1/.acquire.lock" ]; then\n'
               '    printf "LOCK_SIZE=%s\\n" "$(stat -c %s "$1/.acquire.lock")"\n'
               '    printf "LOCK_CONTENT:\\n"\n'
               '    cat "$1/.acquire.lock"\n'
               'else\n'
               '    echo "LOCK_ABSENT=yes"\n'
               'fi\n')

RESOLVE_PROBE = ('. ' + repr(str(PROVISIONER)) + '\n'
                 'if _fie_test_pg_cache_resolve; then\n'
                 '    printf "MODE=%s\\n" "${PG_CACHE_MODE}"\n'
                 '    printf "PERSISTENT=%s\\n" "${PG_CACHE_PERSISTENT}"\n'
                 '    printf "TEMP_CREATED=%s\\n" "${PG_CACHE_TEMP_CREATED:+set}"\n'
                 'else\n'
                 '    rc=$?; echo "RESOLVE_RC=${rc}"\n'
                 'fi\n'
                 'if [ -f "$1/.acquire.lock" ]; then\n'
                 '    printf "LOCK_SIZE=%s\\n" "$(stat -c %s "$1/.acquire.lock")"\n'
                 'fi\n')

# R4-R5-R1 Task D — full provisioning-path probe (resolver → claim →
# cold-acquire / fallback), offline via the file:// base override. stdout of
# the ensure call goes to a file (it is the resolved install dir) so the
# PG_CACHE_* variables stay set in THIS shell (a $(...) subshell would lose
# them).
ENSURE_PROBE = ('. ' + repr(str(PROVISIONER)) + '\n'
                ': "${FIE_PROBE_WORK:?FIE_PROBE_WORK required}"\n'
                'ensure_out="${FIE_PROBE_WORK}/ensure.out"\n'
                'ensure_err="${FIE_PROBE_WORK}/ensure.err"\n'
                'if _fie_test_pg_ensure_cache > "${ensure_out}" '
                '2> "${ensure_err}"; then\n'
                '    echo "ENSURE_OK"\n'
                'else\n'
                '    rc=$?; echo "ENSURE_RC=${rc}"\n'
                'fi\n'
                'printf "RESOLVED=%s\\n" "$(cat "${ensure_out}" 2>/dev/null)"\n'
                'printf "MODE=%s\\n" "${PG_CACHE_MODE-}"\n'
                'printf "PERSISTENT=%s\\n" "${PG_CACHE_PERSISTENT-}"\n'
                'printf "TEMP_CREATED=%s\\n" "${PG_CACHE_TEMP_CREATED-}"\n'
                'cat "${ensure_err}" >&2 2>/dev/null\n'
                'exit 0\n')

WRITE_META_PROBE = ('. ' + repr(str(PROVISIONER)) + '\n'
                    'mkdir -p "$1"; exec 9>>"$1/.acquire.lock"; '
                    'flock -w 10 9 || { echo FLOCK_FAILED; exit 1; }\n'
                    '_fie_cache_lock_write_metadata "$1/.acquire.lock" || '
                    '{ echo META_FAILED; exit 1; }\n'
                    'cat "$1/.acquire.lock"\n'
                    'exec 9>&- 9<&-\n')


def _probe_with(probe_template: str, blob: dict | None, cachedir: str,
                timeout: int = 90):
    with tempfile.TemporaryDirectory(prefix="fie-lk-probe-") as td:
        probe_path = Path(td) / "probe.sh"
        probe_path.write_text(probe_template, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": "/tmp"}
        if blob:
            env.update(blob)
        res = subprocess.run(
            ["bash", str(probe_path), cachedir], env=env,
            capture_output=True, text=True, timeout=timeout, cwd=str(REPO))
        return res.returncode, res.stdout, res.stderr


def _empty_cache() -> Path:
    return Path(tempfile.mkdtemp(prefix="fie-lk-cache-"))


class LockAcquisitionMetadataTests(unittest.TestCase):
    """LK1 — ownership metadata written under the held flock."""

    def test_acquire_metadata_shape(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        rc, out, err = _probe_with(WRITE_META_PROBE, None, str(cachedir))
        self.assertEqual(rc, 0, msg=err)
        text = out  # probe's stdout is exactly `cat lockfile`
        self.assertIn("format=fie-6-9a-acquire-lock-v1", text)
        self.assertRegex(text, r"invocation=[0-9a-f]+")
        self.assertRegex(text, r"pid=\d+")
        self.assertRegex(text, r"created_utc=\d{4}-\d{2}-\d{2}T")
        self.assertRegex(text, r"cache_mode=\S*")


class LockClassifyTests(unittest.TestCase):
    """LK2 — non-mutating classification (four outcomes)."""

    def test_classify_absent(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        rc, out, err = _probe_with(CLASSIFY_PROBE, None, str(cachedir))
        self.assertEqual(rc, 0)
        self.assertIn("LOCK_CLASS=ABSENT", out)

    def test_classify_stale_job_owned(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        cachedir.mkdir(parents=True, exist_ok=True)
        (cachedir / ".acquire.lock").write_text(
            "format=fie-6-9a-acquire-lock-v1\ninvocation=deadbeef\n"
            "pid=999999\ncreated_utc=2020-01-01T00:00:00Z\n",
            encoding="utf-8")
        rc, out, err = _probe_with(CLASSIFY_PROBE, None, str(cachedir))
        self.assertEqual(rc, 0)
        self.assertIn("LOCK_CLASS=STALE_JOB_OWNED", out)
        # Non-mutating:
        self.assertTrue((cachedir / ".acquire.lock").is_file())

    def test_classify_active_with_live_flock_holder(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        cachedir.mkdir(parents=True, exist_ok=True)
        lock = cachedir / ".acquire.lock"
        fd = lock.open("a")
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            rc, out, err = _probe_with(CLASSIFY_PROBE, None, str(cachedir))
            self.assertEqual(rc, 0)
            self.assertIn("LOCK_CLASS=ACTIVE", out)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        rc, out, err = _probe_with(CLASSIFY_PROBE, None, str(cachedir))
        self.assertIn("LOCK_CLASS=UNKNOWN", out,
                      "lock without fie metadata (held by a foreign "
                      f"process, now free) classifies UNKNOWN, not stale: "
                      f"content={lock.read_text()!r}")

    def test_classify_unknown_legacy_contents(self):
        for content, label in (("", "legacy-empty"),
                               ("PGPROTECT_ENV_BLOB=x\nother=y\n",
                                "legacy-marked")):
            with self.subTest(label=label):
                cachedir = _empty_cache()
                self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
                (cachedir / ".acquire.lock").write_text(content,
                                                        encoding="utf-8")
                rc, out, err = _probe_with(CLASSIFY_PROBE, None,
                                           str(cachedir))
                self.assertEqual(
                    rc, 0)
                self.assertIn("LOCK_CLASS=UNKNOWN", out)


class CleanupOwnershipTests(unittest.TestCase):
    """LK3/LK4/LK5 — cleanup removes only the provably-dead owned lock."""

    def test_cleanup_removes_stale_job_owned(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        (cachedir / ".acquire.lock").write_text(
            "format=fie-6-9a-acquire-lock-v1\ninvocation=addeadaff\n"
            "pid=999999\ncreated_utc=2020-01-01T00:00:00Z\n",
            encoding="utf-8")
        rc, out, err = _probe_with(CLEANUP_PROBE,
                                   {"FIE_TEST_PG_CACHE_DIR":
                                        str(cachedir)}, str(cachedir))
        self.assertIn("CLEANUP_RC=0", out, msg=err)
        self.assertFalse((cachedir / ".acquire.lock").exists())

    def test_cleanup_preserves_active_holder_lock(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        lock = cachedir / ".acquire.lock"
        lock.write_text(
            "format=fie-6-9a-acquire-lock-v1\ninvocation=liveflock\n"
            "pid=424242\ncreated_utc=2026-10-05T00:00:00Z\n",
            encoding="utf-8")
        fd = lock.open("a")
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            rc, out, err = _probe_with(
                CLEANUP_PROBE, {"FIE_TEST_PG_CACHE_DIR": str(cachedir)},
                str(cachedir))
            self.assertIn("CLEANUP_RC=0", out, msg=err)
            self.assertTrue(lock.is_file(),
                            "ACTIVE lock never removed from under a live "
                            "holder")
            # and the classify-view agrees:
            rc, out, err = _probe_with(CLASSIFY_PROBE, None, str(cachedir))
            self.assertIn("LOCK_CLASS=ACTIVE", out)
            self.assertTrue(lock.is_file())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

    def test_cleanup_fails_closed_on_unknown(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        (cachedir / ".acquire.lock").write_text("", encoding="utf-8")
        rc, out, err = _probe_with(CLEANUP_PROBE,
                                   {"FIE_TEST_PG_CACHE_DIR": str(cachedir)},
                                   str(cachedir))
        self.assertIn("CLEANUP_RC=96", out, msg=err)
        self.assertTrue((cachedir / ".acquire.lock").exists(),
                        "UNKNOWN lock must be PRESERVED (fail closed)")
        self.assertIn("UNKNOWN", out + err,
                      "preserve + report: the fail-closed message is "
                      "operator-visible")


class AcquisitionOwnershipClaimTests(unittest.TestCase):
    """R4-R5-R1 Task D — the canonical acquisition-ownership claim.

    Contract: a pre-existing lock whose ownership cannot be positively
    proven as belonging to the current job is NEVER claimed (no in-place
    metadata write), NEVER deleted; ABSENT locks are created exclusively;
    proven fie-owned stale locks are adopted per the documented stale-owner
    protocol; ACTIVE locks are waited on, then re-classified by content.
    (The FI09 violation was the acquire loop writing fie metadata into a
    legacy 0-byte lock, converting UNKNOWN into apparently-job-owned state
    that cleanup then removed.)"""

    def test_fi09_unknown_empty_lock_claim_fails_closed(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        lock = cachedir / ".acquire.lock"
        lock.write_bytes(b"")  # legacy 0-byte: UNKNOWN
        before = (lock.stat().st_ino, lock.stat().st_size)
        rc, out, err = _probe_with(CLAIM_PROBE, {
            "FIE_TEST_PG_LOCK_WAIT_SEC": "5"}, str(cachedir))
        self.assertIn("CLAIM_RC=97", out, msg=err)
        self.assertIn("OWNERSHIP_UNPROVABLE", err,
                      "fail-closed reason is operator-visible")
        self.assertTrue(lock.exists(), "UNKNOWN lock must be preserved")
        after = (lock.stat().st_ino, lock.stat().st_size)
        self.assertEqual(before, after,
                         "claimed-in-place is forbidden: byte-identical "
                         "lock must survive, no inode churn")
        self.assertEqual(lock.read_bytes(), b"",
                         "no fie metadata may be written into it")
        # and cleanup agrees: preserved + exit 96
        rc, out, err = _probe_with(CLEANUP_PROBE,
                                   {"FIE_TEST_PG_CACHE_DIR": str(cachedir)},
                                   str(cachedir))
        self.assertIn("CLEANUP_RC=96", out, msg=err)
        self.assertTrue(lock.exists())

    def test_fi09_unknown_nonempty_lock_claim_fails_closed(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        lock = cachedir / ".acquire.lock"
        lock.write_text("SOME_FOREIGN_PROTOCOL_MARKER=1\nopaque\n",
                        encoding="utf-8")
        before = lock.read_text()
        rc, out, err = _probe_with(CLAIM_PROBE, {
            "FIE_TEST_PG_LOCK_WAIT_SEC": "5"}, str(cachedir))
        self.assertIn("CLAIM_RC=97", out, msg=err)
        self.assertTrue(lock.exists())
        self.assertEqual(lock.read_text(), before,
                         "unknown non-empty lock must remain byte-identical")

    def test_absent_lock_claimed_exclusively_and_metadata_written(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        rc, out, err = _probe_with(CLAIM_PROBE, {
            "FIE_TEST_PG_LOCK_WAIT_SEC": "5"}, str(cachedir))
        self.assertIn("CLAIM=OK", out, msg=err)
        lock = cachedir / ".acquire.lock"
        self.assertTrue(lock.exists())
        text = lock.read_text()
        self.assertIn("format=fie-6-9a-acquire-lock-v1", text)
        self.assertRegex(text, r"invocation=[0-9a-f]+")
        self.assertIn("FD_TARGET=" + str(lock), out,
                      "the claiming fd targets the live inode")

    def test_stale_proven_job_owned_lock_adopted_per_policy(self):
        # positive proof (fie format + invocation) + flock free = stale
        # fie-owned: adoptable per the documented stale-owner protocol.
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        lock = cachedir / ".acquire.lock"
        lock.write_text(
            "format=fie-6-9a-acquire-lock-v1\ninvocation=deadbeef\n"
            "pid=999999\ncreated_utc=2020-01-01T00:00:00Z\n",
            encoding="utf-8")
        rc, out, err = _probe_with(CLAIM_PROBE, {
            "FIE_TEST_PG_LOCK_WAIT_SEC": "5"}, str(cachedir))
        self.assertIn("CLAIM=OK", out, msg=err)
        self.assertIn("format=fie-6-9a-acquire-lock-v1", lock.read_text())
        # metadata refreshed: a NEW invocation id replaces the stale one
        self.assertNotIn("invocation=deadbeef", lock.read_text())

    def test_foreign_object_in_classify_mutate_window_fails_closed(self):
        # TOCTOU: the claimer pre-classifies (fi: fie-owned), then a
        # concurrent party replaces the object with foreign junk before
        # the claim's mutation; content re-verification UNDER the held
        # flock must fail closed (no refresh into the foreign object).
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        lock = cachedir / ".acquire.lock"
        lock.write_text(
            "format=fie-6-9a-acquire-lock-v1\ninvocation=deadbeef\n"
            "pid=999999\ncreated_utc=2020-01-01T00:00:00Z\n",
            encoding="utf-8")
        probe_path = Path(tempfile.mkdtemp(prefix="fie-lk-race-")) / \
            "probe.sh"
        self.addCleanup(shutil.rmtree, probe_path.parent,
                        ignore_errors=True)
        probe_path.write_text(
            '. ' + repr(str(PROVISIONER)) + '\n'
            'if _fie_cache_lock_acquisition_claim "$1"; then echo '
            '"CLAIM=OK"; else rc=$?; echo "CLAIM_RC=${rc}"; fi\n',
            encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": "/tmp",
               "FIE_TEST_PG_LOCK_WAIT_SEC": "5"}
        proc = subprocess.Popen(
            ["bash", str(probe_path), str(cachedir)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(REPO))
        import fcntl
        # hold the flock so the claimer parks inside its wait loop, then
        # replace the object; release only afterwards
        fd = lock.open("a")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            import time
            time.sleep(0.8)  # claimer is now waiting on our flock
            lock.unlink()
            lock.write_text("FOREIGN_REPLACEMENT=1\n", encoding="utf-8")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        out, err = proc.communicate(timeout=60)
        self.assertIn("CLAIM_RC=97", out,
                      f"replacement must fail closed: out={out!r} "
                      f"err={err!r}")
        self.assertEqual(
            lock.read_text(), "FOREIGN_REPLACEMENT=1\n",
            "the foreign replacement must survive byte-identical (never "
            "claimed, never removed by the acquisition path)")

    def test_concurrent_acquisition_collision_serializes(self):
        # Two racers on an ABSENT cache. Contract invariants: at most one
        # claim lineage materializes (exactly one fie metadata), any racer
        # that cannot positively claim fails CLOSED with the lock
        # preserved, and every successful claim proves ownership (fie
        # format). No garbage, no double lineage, nothing unclaimed ends
        # up claimed in place.
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: _probe_with(
                    CLAIM_PROBE, {"FIE_TEST_PG_LOCK_WAIT_SEC": "10"},
                    str(cachedir)),
                (0, 1)))
        outcomes = []
        for rc, out, err in results:
            if "CLAIM=OK" in out:
                self.assertIn("format=fie-6-9a-acquire-lock-v1",
                              out.split("LOCK_CONTENT:")[-1],
                              f"a successful claim proved ownership: "
                              f"{out!r}")
                outcomes.append("ok")
            else:
                self.assertIn("CLAIM_RC=97", out,
                              f"unknown racer exit: rc={rc} {out!r} {err!r}")
                self.assertIn("OWNERSHIP_UNPROVABLE", err)
                outcomes.append("fail_closed")
        self.assertIn("ok", outcomes,
                      f"the creator must always win: {outcomes}")
        lock = cachedir / ".acquire.lock"
        self.assertTrue(lock.exists())
        text = lock.read_text()
        self.assertEqual(text.count("invocation="), 1,
                         "single coherent ownership metadata lineage")

    def test_acquisition_falls_back_to_isolated_temp_on_unknown_lock(self):
        # End-to-end at the unit boundary (offline: FIE_TEST_PG_TEST_BASE_URL
        # file:// farm built from the host's warm portable cache — same
        # artifact bytes the published sidecar pins): a persistent candidate
        # holding an UNKNOWN pre-existing lock is not claimable; the
        # provisioning path selects the isolated job-local fallback, the
        # unknown lock stays byte-identical, and the portable install lands
        # in the fallback cache.
        import shutil as _sh
        warm_jar = Path("/home/ubuntu/.cache/fie/test-postgres/"
                        "portable-postgres.jar")
        warm_sha = Path("/home/ubuntu/.cache/fie/test-postgres/"
                        "artifact.sha256")
        if not (warm_jar.is_file() and warm_sha.is_file()):
            self.skipTest(
                "SYNTHETIC-ONLY skip: host warm portable-postgres cache "
                "absent — cannot build the offline file:// farm for the "
                "cold-acquire fallback leg (environmental, failure not "
                "implied)")
            return
        with tempfile.TemporaryDirectory(prefix="fie-lk-ens-") as td:
            tdp = Path(td)
            # pinned-shape farm: <base>/embedded-postgres-binaries-<arch>/
            # <ver>/embedded-postgres-binaries-<arch>-<ver>.jar[.sha256]
            rc0, out0, err0 = _probe_with(
                '. ' + repr(str(PROVISIONER)) + '\n'
                'printf "ARCH=%s|VER=%s\\n" "$(_portable_pg_arch)" '
                '"${PORTABLE_PG_VERSION}"\n', None, "")
            line0 = next((_ for _ in out0.splitlines() if "ARCH=" in _), "")
            arch = line0.split("|")[0][len("ARCH="):]
            ver = line0.split("|")[1][len("VER="):]
            farm = tdp / "farm"
            afarm = farm / f"embedded-postgres-binaries-{arch}" / ver
            afarm.mkdir(parents=True)
            self.assertEqual(warm_sha.read_text().strip(),
                             hashlib.sha256(warm_jar.read_bytes()).hexdigest(),
                             "host warm cache sidecar must pin the warm jar "
                             "before it farms the offline test base")
            (afarm / "embedded-postgres-binaries-"
             f"{arch}-{ver}.jar").symlink_to(warm_jar)
            (afarm / "embedded-postgres-binaries-"
             f"{arch}-{ver}.jar.sha256").write_text(
                warm_sha.read_text().strip(), encoding="utf-8")
            cachedir = tdp / "pinned"
            cachedir.mkdir()
            (tdp / "probe-work").mkdir()
            lock = cachedir / ".acquire.lock"
            lock.write_bytes(b"")
            home = tdp / "home"
            home.mkdir()
            rc, out, err = _probe_with(
                ENSURE_PROBE,
                {"FIE_TEST_PG_CACHE_DIR": str(cachedir),
                 "TMPDIR": str(tdp),
                 "FIE_PROBE_WORK": str(tdp / "probe-work"),
                 "FIE_TEST_PG_TEST_BASE_URL": f"file://{farm}"},
                str(cachedir))
            self.assertIn("ENSURE_OK", out, msg=err[-2000:])
            self.assertIn("MODE=temp", out)
            self.assertIn("PERSISTENT=no", out)
            temp_line = next((_ for _ in out.splitlines()
                              if _.startswith("TEMP_CREATED=")), "")
            temp_created = temp_line[len("TEMP_CREATED="):]
            self.assertTrue(temp_created.startswith(str(tdp)),
                            f"fallback cache must be job-local to the test "
                            f"workdir: {temp_created!r}")
            self.assertIn("isolated job-local cache fallback", err)
            self.assertTrue(lock.exists())
            self.assertEqual(lock.stat().st_size, 0,
                             "the unknown lock was never touched")

    def test_resolver_still_returns_explicit_candidate_for_cleanup(self):
        # contract-composition pin: the RESOLVER itself does not reject or
        # hide an unknown-lock persistent candidate — the cleanup contract
        # re-resolves through it and must still reach the fail-closed
        # exit-96 report (LK5). Only the ACQUISITION claim path refuses it.
        with tempfile.TemporaryDirectory(prefix="fie-lk-rslv-") as td:
            tdp = Path(td)
            cachedir = tdp / "pinned"
            cachedir.mkdir()
            lock = cachedir / ".acquire.lock"
            lock.write_bytes(b"")
            home = tdp / "home"
            home.mkdir()
            rc, out, err = _probe_with(
                RESOLVE_PROBE,
                {"FIE_TEST_PG_CACHE_DIR": str(cachedir), "HOME": str(home)},
                str(cachedir))
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("MODE=explicit", out)
            self.assertIn("PERSISTENT=yes", out)
            self.assertTrue(lock.exists())
            self.assertEqual(lock.stat().st_size, 0,
                             "resolution never mutates the unknown lock")


class CleanupRevalidationTests(unittest.TestCase):
    """R4-R5-R1 Task D — cleanup revalidates identity/ownership; a lock
    replaced between classification and cleanup never gets removed unless
    the replacement itself proves fie ownership on its own inode."""

    def test_cleanup_preserves_replaced_foreign_object(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        lock = cachedir / ".acquire.lock"
        lock.write_text(
            "format=fie-6-9a-acquire-lock-v1\ninvocation=deadbeef\n"
            "pid=999999\ncreated_utc=2020-01-01T00:00:00Z\n",
            encoding="utf-8")
        # "classification" would call this stale; a concurrent party
        # replaces the object with foreign junk BEFORE cleanup runs
        lock.unlink()
        lock.write_text("FOREIGN_REPLACEMENT=1\n", encoding="utf-8")
        rc, out, err = _probe_with(CLEANUP_PROBE,
                                   {"FIE_TEST_PG_CACHE_DIR": str(cachedir)},
                                   str(cachedir))
        self.assertTrue(lock.exists(),
                        "replaced foreign object must be preserved")
        self.assertIn("FOREIGN_REPLACEMENT=1", lock.read_text())
        self.assertIn("preserved", out,
                      "fail-closed report must state preservation")
        self.assertNotIn("removed ", out,
                         "cleanup must not report removal for a foreign "
                         "object")

    def test_cleanup_idempotent_second_run_noop(self):
        cachedir = _empty_cache()
        self.addCleanup(shutil.rmtree, cachedir, ignore_errors=True)
        (cachedir / ".acquire.lock").write_text(
            "format=fie-6-9a-acquire-lock-v1\ninvocation=deadbeef\n"
            "pid=999999\ncreated_utc=2020-01-01T00:00:00Z\n",
            encoding="utf-8")
        rc, out, err = _probe_with(CLEANUP_PROBE,
                                   {"FIE_TEST_PG_CACHE_DIR": str(cachedir)},
                                   str(cachedir))
        self.assertIn("CLEANUP_RC=0", out, msg=err)
        self.assertFalse((cachedir / ".acquire.lock").exists())
        rc, out, err = _probe_with(CLEANUP_PROBE,
                                   {"FIE_TEST_PG_CACHE_DIR": str(cachedir)},
                                   str(cachedir))
        self.assertIn("CLEANUP_RC=0", out,
                      f"repeat cleanup is a no-op: {out!r} {err!r}")


class DeletedInodeRetryTests(unittest.TestCase):
    """LK6 — the acquire loop's deleted-inode retry + fd-2 preservation."""

    def test_acquisition_stderr_not_silenced_by_null_exec(self):
        """Regression pin: a null-command `exec 9>>file 2>/dev/null` redirects
        fd 2 PERMANENTLY (POSIX exec rule) — the shipped code must keep its
        stderr probe inside a group/parenthesis so diagnostics survive."""
        src = PROVISIONER.read_text(encoding="utf-8")
        self.assertNotIn("exec 9>>\"${cachedir}/.acquire.lock\" 2>/dev/null",
                         src, "null-exec with 2>/dev/null permanently "
                              "silences the shell's stderr (POSIX exec rule)")
        self.assertIn('exec 9>>"${cachedir}/.acquire.lock"; } 2>/dev/null',
                      src, "the group form keeps fd 2 alive")

    def test_deleted_lock_replacement_detected_by_readlink_marker(self):
        cachedir = _empty_cache()
        shutil.rmtree(cachedir, ignore_errors=True)
        cachedir.mkdir(parents=True)
        probe = ('f="$1/.acquire.lock"; if ! { exec 9>>"$f"; } 2>/dev/null; '
                 'then echo OPEN_FAILED; exit 1; fi; '
                 'rm -f -- "$f"; '  # concurrent maintenance unlinks under us
                 'r="$(readlink /proc/self/fd/9 2>/dev/null || true)"; '
                 'case "$r" in "$f") echo LIVE ;; *deleted*) echo DELETED ;;'
                 '*) echo "OTHER:$r" ;; esac; exec 9>&- 9<&-')
        with tempfile.TemporaryDirectory(prefix="fie-lk-del-") as td:
            probe_path = Path(td) / "probe.sh"
            probe_path.write_text(probe, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(probe_path), str(cachedir)],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": "/tmp"},
                capture_output=True, text=True, timeout=60)
        self.assertIn("DELETED", res.stdout,
                      msg=f"readlink marker expected: {res.stdout!r}")


if __name__ == "__main__":
    unittest.main()