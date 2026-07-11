#!/usr/bin/env python3
"""
Tests for Phase 2B Step 4B — Dispatcher ↔ LocalStore wiring.

These tests use a tmp metadata directory per test, so they do NOT
touch the production metadata tree. They cover:

  - Task metadata creation (every state transition persists)
  - Dry-run dispatch produces a task + a placeholder artifact record
  - Dry-run does NOT call fetch / analyze / render
  - Status lookup works in-memory AND across processes (via store)
  - Rerun creates a child task with parent_task_id set
  - Cancel works for queued tasks; rejected for terminal tasks
  - Artifact metadata shape for dry-run vs execute
  - No external publish behavior (no print() to stdout, no Telegram
    trigger, no DB writes, no cron edits)
  - Execute mode writes a real artifact text file
  - All protected production files remain byte-identical (this
    test re-runs the same sha256 check the framework tests do)

Test isolation:
  - Each test gets its own tmp dir via setUp.
  - The store is configured to use the tmp dir, never the default
    /home/ubuntu/macro-report/metadata.
  - A FakePlugin lives in this test module (same pattern as the
    Step 3+4A framework tests).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

# Make sure tests can import the project root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from reports.base import BaseReport
from reports.hermes.dispatcher import Dispatcher
from reports.hermes.store import LocalStore
from reports.hermes.task import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Task,
)
from reports.registry import Registry


# ----- Fake plugin (deterministic, no I/O) -----

class FakePlugin(BaseReport):
    """A trivial plugin for testing — no yfinance, no DB, no I/O."""

    def __init__(self, manifest=None):
        super().__init__(manifest=manifest or {
            "name": "fake",
            "display_name": "Fake Test Report",
            "schedule": "",
            "version": "0.0.1",
            "entrypoint": "tests.test_phase2b_step4b:FakePlugin",
        })

    def fetch(self) -> dict:
        return {"indicators": {"vix": 12.5, "sp500": 4500.0}, "fake": True}

    def analyze(self, data: dict) -> tuple:
        return ("BULLISH", ["vix_below_20", "sp500_above_4000"], 75)

    def render(self, data, verdict, signals, score) -> str:
        return (
            f"=== Fake Report ===\n"
            f"verdict: {verdict}\n"
            f"signals: {signals}\n"
            f"score: {score}\n"
            f"vix: {data['indicators']['vix']}\n"
        )


# ----- Helpers -----

def _write_manifest(tmpdir: Path, name: str = "fake", entrypoint: str = "tests.test_phase2b_step4b:FakePlugin") -> Path:
    config_dir = tmpdir / "config"
    config_dir.mkdir(exist_ok=True)
    manifest = {
        "name": name,
        "display_name": f"Fake {name}",
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Taipei",
        "version": "0.0.1",
        "entrypoint": entrypoint,
        "timeout_seconds": 60,
        "retry_policy": {"max_attempts": 1, "backoff_seconds": 0},
        "publisher": {"type": "stdout"},
        "archive": {"enabled": False},
    }
    path = config_dir / f"{name}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return path


# ----- Tests -----

class LocalStoreTests(unittest.TestCase):
    """Sanity tests for the LocalStore itself."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="phase2b_step4b_store_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_task_roundtrip(self):
        store = LocalStore(root=self.tmpdir)
        task_dict = {
            "task_id": "abc-123",
            "report_name": "fake",
            "status": "completed",
            "created_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "metadata": {"dry_run": True},
            "result_text": "hi",
            "stages": [],
            "artifact_paths": [],
            "parent_task_id": "",
        }
        store.save_task(task_dict)
        loaded = store.load_task("abc-123")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["report_name"], "fake")
        self.assertEqual(loaded["result_text"], "hi")

    def test_index_updated_on_save(self):
        store = LocalStore(root=self.tmpdir)
        for i in range(3):
            store.save_task({
                "task_id": f"id-{i}",
                "report_name": "fake",
                "status": "completed",
                "created_at": f"2026-01-0{i+1}T00:00:00+00:00",
                "metadata": {},
            })
        tasks = store.list_tasks()
        self.assertEqual(len(tasks), 3)
        # Sorted by created_at asc.
        self.assertEqual(tasks[0]["task_id"], "id-0")
        self.assertEqual(tasks[2]["task_id"], "id-2")

    def test_artifact_metadata_roundtrip(self):
        store = LocalStore(root=self.tmpdir)
        store.save_artifact({
            "artifact_id": "art-xyz",
            "task_id": "id-1",
            "report_name": "fake",
            "created_at": "2026-01-01T00:00:00+00:00",
            "dry_run": False,
            "artifact_path": "/tmp/abc.txt",
        })
        loaded = store.load_artifact("art-xyz")
        self.assertEqual(loaded["artifact_path"], "/tmp/abc.txt")
        arts = store.list_artifacts(task_id="id-1")
        self.assertEqual(len(arts), 1)

    def test_write_and_read_report_text(self):
        store = LocalStore(root=self.tmpdir)
        path = store.write_report_text("art-1", "hello world\nsecond line")
        self.assertTrue(path.exists())
        self.assertEqual(store.read_report_text(path), "hello world\nsecond line")

    def test_atomic_write_does_not_leave_tmp_files(self):
        store = LocalStore(root=self.tmpdir)
        store.save_task({"task_id": "x", "report_name": "fake", "status": "completed"})
        # No .tmp files in the tasks dir.
        tmp_files = list((self.tmpdir / "tasks").glob(".x.*.tmp"))
        self.assertEqual(tmp_files, [])

    def test_corrupt_index_does_not_block_reads(self):
        store = LocalStore(root=self.tmpdir)
        # Corrupt the index.
        (self.tmpdir / "tasks" / "index.json").write_text("{ not json")
        # list_tasks() should not raise — should return empty.
        self.assertEqual(store.list_tasks(), [])

    def test_save_requires_task_id(self):
        store = LocalStore(root=self.tmpdir)
        with self.assertRaises(ValueError):
            store.save_task({"report_name": "fake", "status": "completed"})


class DispatcherStoreIntegrationTests(unittest.TestCase):
    """End-to-end tests for Dispatcher wired up to a LocalStore."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="phase2b_step4b_disp_"))
        self.meta_dir = self.tmpdir / "meta"
        self.config_dir = self.tmpdir / "config"
        _write_manifest(self.tmpdir)
        self.registry = Registry.from_directory(self.config_dir)
        self.store = LocalStore(root=self.meta_dir)
        self.dispatcher = Dispatcher(registry=self.registry, store=self.store)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- task metadata creation ----

    def test_dry_run_creates_task_with_full_state(self):
        task = self.dispatcher.dispatch("fake", dry_run=True)
        d = task.to_dict()
        self.assertEqual(d["status"], STATUS_COMPLETED)
        self.assertEqual(d["report_name"], "fake")
        self.assertTrue(d["metadata"]["dry_run"])
        self.assertEqual(d["parent_task_id"], "")
        self.assertEqual(d["artifact_paths"], [])
        self.assertIn("entrypoint_resolved", d["metadata"])

    def test_dry_run_persists_to_store(self):
        task = self.dispatcher.dispatch("fake", dry_run=True)
        # Re-load from store in a fresh Dispatcher (simulating cross-process).
        fresh = Dispatcher(registry=self.registry, store=self.store)
        loaded = fresh.status(task.task_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task_id, task.task_id)
        self.assertEqual(loaded.status, STATUS_COMPLETED)

    def test_state_transitions_each_persist(self):
        # Each transition writes a separate task file; the latest is the truth.
        task = self.dispatcher.dispatch("fake", dry_run=True)
        # Read the JSON on disk to confirm it captured the final state.
        path = self.meta_dir / "tasks" / f"{task.task_id}.json"
        rec = json.loads(path.read_text())
        self.assertEqual(rec["status"], STATUS_COMPLETED)
        # created_at < started_at < finished_at (sanity)
        self.assertLess(rec["created_at"], rec["finished_at"])

    # ---- artifact tracking ----

    def test_dry_run_creates_placeholder_artifact(self):
        task = self.dispatcher.dispatch("fake", dry_run=True)
        art = self.dispatcher.get_artifact(task.task_id)
        self.assertIsNotNone(art)
        self.assertTrue(art["dry_run"])
        # Dry-run artifact has NO real file path.
        self.assertEqual(art["artifact_path"], "")

    def test_dry_run_does_not_write_report_text_file(self):
        task = self.dispatcher.dispatch("fake", dry_run=True)
        # No .txt file should exist for this task.
        txt_files = list(self.meta_dir.glob(f"reports/artifacts/art-{task.task_id}.txt"))
        self.assertEqual(txt_files, [])

    def test_dry_run_does_not_call_fetch(self):
        # Wrap FakePlugin.fetch to track invocations.
        call_count = {"n": 0}
        original_fetch = FakePlugin.fetch
        def tracked_fetch(self_):
            call_count["n"] += 1
            return original_fetch(self_)
        with patch.object(FakePlugin, "fetch", tracked_fetch):
            self.dispatcher.dispatch("fake", dry_run=True)
        self.assertEqual(call_count["n"], 0, "dry-run must NOT call fetch()")

    # ---- no external publish behavior ----

    def test_dry_run_does_not_print_to_stdout(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.dispatcher.dispatch("fake", dry_run=True)
        out = buf.getvalue()
        # The dispatcher must not have produced any stdout output.
        # (CLI wrappers print task.to_dict(), but Dispatcher.dispatch
        # itself must stay silent.)
        self.assertEqual(out, "")

    def test_dry_run_does_not_print_to_stderr(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.dispatcher.dispatch("fake", dry_run=True)
        # We tolerate warnings; we don't tolerate uncaught exceptions
        # showing as tracebacks.
        self.assertNotIn("Traceback", buf.getvalue())

    def test_no_cron_or_db_side_effects(self):
        # Snapshot the relevant files. Dispatcher must not touch them.
        cron_path = Path.home() / ".hermes/cron/jobs.json"
        db_path = _PROJECT_ROOT / "macro_history.db"
        cron_before = cron_path.read_bytes() if cron_path.exists() else None
        db_before = db_path.read_bytes() if db_path.exists() else None
        self.dispatcher.dispatch("fake", dry_run=True)
        cron_after = cron_path.read_bytes() if cron_path.exists() else None
        db_after = db_path.read_bytes() if db_path.exists() else None
        self.assertEqual(cron_before, cron_after, "cron jobs.json must not change")
        self.assertEqual(db_before, db_after, "macro_history.db must not change")

    # ---- status lookup ----

    def test_status_lookup_in_memory(self):
        task = self.dispatcher.dispatch("fake", dry_run=True)
        looked = self.dispatcher.status(task.task_id)
        self.assertIs(looked, task, "in-memory lookup should return same object")

    def test_status_lookup_via_store(self):
        task = self.dispatcher.dispatch("fake", dry_run=True)
        # New Dispatcher = new process. Status must come from store.
        fresh = Dispatcher(registry=self.registry, store=self.store)
        loaded = fresh.status(task.task_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task_id, task.task_id)
        self.assertEqual(loaded.status, STATUS_COMPLETED)

    def test_status_unknown_returns_none(self):
        self.assertIsNone(self.dispatcher.status("nonexistent-id"))

    # ---- list_tasks ----

    def test_list_tasks_returns_persisted(self):
        t1 = self.dispatcher.dispatch("fake", dry_run=True)
        t2 = self.dispatcher.dispatch("fake", dry_run=True)
        fresh = Dispatcher(registry=self.registry, store=self.store)
        tasks = fresh.list_tasks()
        ids = {t.task_id for t in tasks}
        self.assertIn(t1.task_id, ids)
        self.assertIn(t2.task_id, ids)
        self.assertEqual(len(tasks), 2)

    def test_list_tasks_filtered_by_report_name(self):
        # Register a second report.
        _write_manifest(self.tmpdir, name="other", entrypoint="tests.test_phase2b_step4b:FakePlugin")
        registry2 = Registry.from_directory(self.config_dir)
        dispatcher2 = Dispatcher(registry=registry2, store=self.store)
        dispatcher2.dispatch("fake", dry_run=True)
        dispatcher2.dispatch("other", dry_run=True)
        fresh = Dispatcher(registry=registry2, store=self.store)
        fakes = fresh.list_tasks(report_name="fake")
        others = fresh.list_tasks(report_name="other")
        self.assertEqual(len(fakes), 1)
        self.assertEqual(len(others), 1)
        self.assertEqual(fakes[0].report_name, "fake")
        self.assertEqual(others[0].report_name, "other")

    # ---- rerun ----

    def test_rerun_creates_child_with_parent_task_id(self):
        original = self.dispatcher.dispatch("fake", dry_run=True)
        child = self.dispatcher.rerun(original.task_id)
        self.assertNotEqual(child.task_id, original.task_id)
        self.assertEqual(child.parent_task_id, original.task_id)
        self.assertEqual(child.report_name, original.report_name)

    def test_rerun_persists_parent_link_in_store(self):
        original = self.dispatcher.dispatch("fake", dry_run=True)
        child = self.dispatcher.rerun(original.task_id)
        # Reload child from store and confirm parent_task_id survived.
        fresh = Dispatcher(registry=self.registry, store=self.store)
        loaded_child = fresh.status(child.task_id)
        self.assertEqual(loaded_child.parent_task_id, original.task_id)

    def test_rerun_unknown_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.dispatcher.rerun("does-not-exist")

    def test_rerun_default_is_dry_run(self):
        # Rerun defaults to dry-run regardless of the original's mode.
        # This is the safety property: a misclick on `rerun --execute`
        # in the CLI is a deliberate opt-in, not the default.
        for original_mode in (True, False):
            original = self.dispatcher.dispatch("fake", dry_run=original_mode)
            child = self.dispatcher.rerun(original.task_id)
            self.assertTrue(
                child.metadata.get("dry_run"),
                f"rerun of a dry_run={original_mode} task should default to dry_run=True",
            )

    # ---- cancel ----

    def test_cancel_queued_task_succeeds(self):
        # We can't easily inject a queued task mid-dispatch (sync).
        # Instead, create a task manually with status=queued.
        task = Task(report_name="fake")
        task.transition(STATUS_QUEUED)
        self.dispatcher._tasks[task.task_id] = task
        self.dispatcher._persist(task)
        cancelled = self.dispatcher.cancel(task.task_id)
        self.assertEqual(cancelled.status, STATUS_CANCELLED)

    def test_cancel_completed_task_raises(self):
        task = self.dispatcher.dispatch("fake", dry_run=True)
        with self.assertRaises(ValueError):
            self.dispatcher.cancel(task.task_id)

    def test_cancel_unknown_task_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.dispatcher.cancel("does-not-exist")

    def test_cancel_persists_to_store(self):
        task = Task(report_name="fake")
        task.transition(STATUS_QUEUED)
        self.dispatcher._tasks[task.task_id] = task
        self.dispatcher._persist(task)
        self.dispatcher.cancel(task.task_id)
        # Reload in fresh dispatcher; status should be cancelled.
        fresh = Dispatcher(registry=self.registry, store=self.store)
        loaded = fresh.status(task.task_id)
        self.assertEqual(loaded.status, STATUS_CANCELLED)

    # ---- execute mode (local only) ----

    def test_execute_mode_writes_artifact_file(self):
        task = self.dispatcher.dispatch("fake", dry_run=False)
        self.assertEqual(task.status, STATUS_COMPLETED)
        # The rendered report text should be captured.
        self.assertIn("Fake Report", task.result_text)
        # An artifact file should exist on disk.
        self.assertEqual(len(task.artifact_paths), 1)
        artifact_path = Path(task.artifact_paths[0])
        self.assertTrue(artifact_path.exists())
        self.assertIn("Fake Report", artifact_path.read_text())

    def test_execute_mode_artifact_record_has_real_path(self):
        task = self.dispatcher.dispatch("fake", dry_run=False)
        art = self.dispatcher.get_artifact(task.task_id)
        self.assertIsNotNone(art)
        self.assertFalse(art["dry_run"])
        self.assertTrue(art["artifact_path"])
        self.assertGreater(art["result_text_len"], 0)

    def test_execute_mode_does_not_print_to_stdout(self):
        # The dispatcher must NOT print the report text to stdout
        # (which is the cron-agent's Telegram trigger). The CLI is
        # the right place to surface the text, not the Dispatcher.
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.dispatcher.dispatch("fake", dry_run=False)
        out = buf.getvalue()
        self.assertNotIn("Fake Report", out)

    def test_execute_mode_no_cron_or_db_side_effects(self):
        cron_path = Path.home() / ".hermes/cron/jobs.json"
        db_path = _PROJECT_ROOT / "macro_history.db"
        cron_before = cron_path.read_bytes() if cron_path.exists() else None
        db_before = db_path.read_bytes() if db_path.exists() else None
        self.dispatcher.dispatch("fake", dry_run=False)
        cron_after = cron_path.read_bytes() if cron_path.exists() else None
        db_after = db_path.read_bytes() if db_path.exists() else None
        self.assertEqual(cron_before, cron_after)
        self.assertEqual(db_before, db_after)

    def test_execute_mode_failure_records_failed_status(self):
        # Register a broken plugin.
        class BrokenPlugin(BaseReport):
            def fetch(self): return {}
            def analyze(self, data): return ("x", [], 0)
            def render(self, data, verdict, signals, score):
                raise RuntimeError("intentional failure for test")
        _write_manifest(self.tmpdir, name="broken",
                        entrypoint="tests.test_phase2b_step4b:_BrokenPluginHolder")
        # We need a holder class that actually has the broken impl.
        # The simplest approach: replace the FakePlugin entry in the
        # registry's cache after first instantiation, OR write a
        # different manifest with a different entrypoint.
        # Here we just patch the manifest to a broken module path
        # that raises on import — and verify the dispatcher
        # surfaces a failed status (not a crash).
        # Update the manifest to a path that does not exist.
        broken_manifest = self.config_dir / "broken.json"
        broken_manifest.write_text(json.dumps({
            "name": "broken",
            "display_name": "Broken",
            "entrypoint": "tests.test_phase2b_step4b:NonExistentClass",
            "version": "0.0.1",
        }))
        registry2 = Registry.from_directory(self.config_dir)
        dispatcher2 = Dispatcher(registry=registry2, store=self.store)
        # Clean the cache so the broken manifest is reloaded.
        registry2.clear_cache()
        task = dispatcher2.dispatch("broken", dry_run=False)
        # The import will fail, so the dispatcher should mark it failed.
        self.assertEqual(task.status, STATUS_FAILED)
        self.assertIn("NonExistentClass", task.error)


# Need a module-level class for the broken plugin's entrypoint to be
# resolvable (so the test exercises the import path, not a missing module).
class _BrokenPluginHolder(FakePlugin):
    pass


class InMemoryDispatcherStillWorksTests(unittest.TestCase):
    """
    Step 4A's in-memory behavior must be preserved. A Dispatcher
    with no store attached should still work exactly as before —
    no file writes, no errors.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="phase2b_step4b_inmem_"))
        _write_manifest(self.tmpdir)
        self.registry = Registry.from_directory(self.tmpdir / "config")
        self.dispatcher = Dispatcher(registry=self.registry)  # no store

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dry_run_in_memory(self):
        task = self.dispatcher.dispatch("fake", dry_run=True)
        self.assertEqual(task.status, STATUS_COMPLETED)

    def test_status_unknown_in_memory(self):
        self.assertIsNone(self.dispatcher.status("nope"))

    def test_get_artifact_returns_none_without_store(self):
        task = self.dispatcher.dispatch("fake", dry_run=True)
        self.assertIsNone(self.dispatcher.get_artifact(task.task_id))


class TaskModelNewFieldsTests(unittest.TestCase):
    """Test the Step 4B additions to the Task model."""

    def test_task_has_empty_artifact_paths_by_default(self):
        t = Task(report_name="x")
        self.assertEqual(t.artifact_paths, [])

    def test_task_has_empty_parent_task_id_by_default(self):
        t = Task(report_name="x")
        self.assertEqual(t.parent_task_id, "")

    def test_task_parent_task_id_from_metadata(self):
        t = Task(report_name="x", metadata={"rerun_of": "parent-id"})
        self.assertEqual(t.parent_task_id, "parent-id")

    def test_to_dict_includes_duration_ms(self):
        import time as _time
        t = Task(report_name="x")
        t.transition(STATUS_QUEUED)
        t.transition(STATUS_RUNNING)
        _time.sleep(0.02)
        t.transition(STATUS_COMPLETED)
        d = t.to_dict()
        self.assertIn("duration_ms", d)
        self.assertGreaterEqual(d["duration_ms"], 15)

    def test_to_dict_includes_artifact_paths_and_parent(self):
        t = Task(report_name="x")
        t.artifact_paths = ["/tmp/a.txt", "/tmp/b.txt"]
        t.parent_task_id = "parent-1"
        d = t.to_dict()
        self.assertEqual(d["artifact_paths"], ["/tmp/a.txt", "/tmp/b.txt"])
        self.assertEqual(d["parent_task_id"], "parent-1")


class ProductionFilesUnchangedTests(unittest.TestCase):
    """
    The same sha256 protection that test_phase2b_framework.py does.
    Runs against the freshly-captured Step 4B baseline.

    If this test fails, something in the implementation accidentally
    modified a production file. Investigate and revert immediately.

    Sentinel cleanup (Phase 4 Task 3A): the previous version
    included ``macro_history.db`` in the protected-file baseline
    (sha256 ``828ce117...``). That assertion is brittle because
    the 08:30 cron legitimately mutates the file, so any test
    run that races with the cron fails for a non-regression
    reason. The safety guarantee for ``macro_history.db`` is
    independently enforced by:

    * ``tests/phase3/test_safety_guards.py``
      ``ProductionDbUntouchedTests.test_macro_history_db_hash_unchanged``
      (canary pattern with a deterministic temp fixture)
    * ``tests/phase3/test_batched_query_integration.py``
      ``TestProductionSafety.test_macro_history_db_unchanged``
      (before/after stat + sha)
    * ``tests/phase3/test_pipeline_api.py``
      ``TestAPIProductionSafety.test_macro_history_db_unchanged``
      (before/after stat + sha, after Phase 4 Task 3A fix)
    * ``tests/phase3/test_pipeline_cli.py``
      ``TestProductionSafety.test_macro_history_db_unchanged``
      (before/after stat + sha, after Phase 4 Task 3A fix)
    * ``tests/phase3/test_query_optimization.py``
      ``TestProductionSafety.test_macro_history_db_unchanged``
      (before/after stat + sha, after Phase 4 Task 3A fix)
    * ``tests/phase3/test_p4t1b_graph_factory_integration.py``
      ``TestProductionSafety.test_macro_history_db_unchanged``
      (before/after stat + sha, after Phase 4 Task 3A fix)
    * The two ``test_no_cron_or_db_side_effects`` and
      ``test_execute_mode_no_cron_or_db_side_effects`` tests in
      this same file (lines 294 / 448), which already use a
      before/after byte comparison for ``macro_history.db``.

    We therefore exclude ``macro_history.db`` from the protected
    baseline here. If the production code accidentally writes to
    any of the OTHER 11 files in the baseline, this test still
    fails.
    """

    BASELINE_PATH = Path("/tmp/phase2b_step4b_baseline.json")
    # Cron-managed files are excluded from the static baseline
    # check. The safety guarantee for these files is enforced by
    # other before/after tests (see class docstring).
    EXCLUDED_FROM_BASELINE = frozenset({"macro_history.db"})

    def test_production_files_match_baseline(self):
        if not self.BASELINE_PATH.exists():
            self.skipTest("baseline not captured yet")
        import hashlib
        baseline = json.loads(self.BASELINE_PATH.read_text())
        protected = baseline["protected_files"]
        mismatches = []
        for rel, info in protected.items():
            if rel in self.EXCLUDED_FROM_BASELINE:
                continue
            path = _PROJECT_ROOT / rel
            if not path.exists():
                mismatches.append(f"{rel}: MISSING")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = info["sha256"]
            if actual != expected:
                mismatches.append(f"{rel}: expected {expected[:12]} got {actual[:12]}")
        if mismatches:
            self.fail("Protected production files changed:\n  " + "\n  ".join(mismatches))


if __name__ == "__main__":
    unittest.main(verbosity=2)
