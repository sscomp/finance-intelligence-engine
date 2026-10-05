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