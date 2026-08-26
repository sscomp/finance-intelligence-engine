#!/usr/bin/env python3
"""
Framework tests for Phase 2B Step 3 + Step 4A.

This test layer validates:
  - BaseReport abstract interface (cannot be instantiated directly;
    subclasses must implement fetch / analyze / render).
  - BaseReport.run() happy path + exception path.
  - ReportPipeline stage timing + stage-level error capture.
  - Registry loading from disk (JSON, since PyYAML is not installed).
  - Registry.instantiate() entrypoint resolution + caching.
  - Task state machine (legal + illegal transitions).
  - Dispatcher.dispatch() in dry-run mode (does NOT call fetch).
  - Dispatcher.status / rerun / cancel.
  - CLI list mode (does NOT execute anything).
  - CLI dispatch dry-run mode (does NOT call fetch).
  - Production files are byte-for-byte unchanged (sha256 against
    the captured Step 1 / Step 2 baselines).

NO production report is ever executed. NO network I/O. NO DB writes.

Test runner: `python3 tests/test_phase2b_framework.py` (the file
also works as `python3 -m unittest tests.test_phase2b_framework`).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Bootstrap sys.path so the test can `import reports` regardless of cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# =============================================================================
# Test fixtures
# =============================================================================


from reports.base import BaseReport as _RealBaseReport
from reports.base import ReportResult  # noqa: F401  (re-exported for tests)


class FakePlugin(_RealBaseReport):
    """Concrete BaseReport that returns canned data. No I/O."""

    def __init__(self, manifest=None, fail_at: str | None = None):
        super().__init__(manifest=manifest)
        self.fail_at = fail_at
        self.fetch_called = 0
        self.analyze_called = 0
        self.render_called = 0
        self.archive_called = 0

    def fetch(self) -> dict:
        self.fetch_called += 1
        if self.fail_at == "fetch":
            raise RuntimeError("fetch boom")
        return {"ticker": "FAKE", "value": 42.0}

    def analyze(self, data: dict) -> tuple:
        self.analyze_called += 1
        if self.fail_at == "analyze":
            raise RuntimeError("analyze boom")
        return ("neutral", ["sig-1", "sig-2"], 0)

    def render(self, data, verdict, signals, score) -> str:
        self.render_called += 1
        if self.fail_at == "render":
            raise RuntimeError("render boom")
        return f"FAKE-REPORT verdict={verdict} score={score} signals={signals}"

    def archive(self, result):
        self.archive_called += 1
        if self.fail_at == "archive":
            raise RuntimeError("archive boom")
        result.artifacts["archived"] = True


# =============================================================================
# BaseReport
# =============================================================================


class BaseReportContractTests(unittest.TestCase):
    def test_cannot_instantiate_base_directly(self):
        with self.assertRaises(TypeError):
            _RealBaseReport()  # type: ignore[abstract]

    def test_subclass_without_methods_cannot_instantiate(self):
        class Incomplete(_RealBaseReport):
            pass
        with self.assertRaises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_happy_path_run(self):
        report = FakePlugin()
        result = report.run()
        self.assertEqual(result.error, "")
        self.assertIn("FAKE-REPORT", result.report_text)
        self.assertEqual(report.fetch_called, 1)
        self.assertEqual(report.analyze_called, 1)
        self.assertEqual(report.render_called, 1)
        self.assertEqual(report.archive_called, 1)

    def test_run_with_publish_does_not_break(self):
        report = FakePlugin()
        # publish=True just adds a print; the result is still valid.
        result = report.run(publish=True)
        self.assertEqual(result.error, "")

    def test_run_catches_exceptions(self):
        report = FakePlugin(fail_at="fetch")
        result = report.run()
        self.assertNotEqual(result.error, "")
        self.assertIn("RuntimeError", result.error)
        self.assertIn("fetch boom", result.error)

    def test_manifest_accessor_returns_copy(self):
        report = FakePlugin(manifest={"name": "x", "version": "1"})
        m1 = report.manifest()
        m1["name"] = "mutated"
        m2 = report.manifest()
        # Internal state not corrupted.
        self.assertEqual(m2["name"], "x")

    def test_default_manifest_keys(self):
        report = FakePlugin(manifest={})
        for key in ("name", "display_name", "schedule", "version"):
            self.assertIn(key, report.manifest())


# =============================================================================
# ReportPipeline
# =============================================================================


class ReportPipelineTests(unittest.TestCase):
    def test_pipeline_stages_happy(self):
        from reports.pipeline import ReportPipeline
        report = FakePlugin()
        outcome = ReportPipeline(report).run()
        self.assertEqual(outcome.result.error, "")
        self.assertEqual(len(outcome.stages), 4)
        stage_names = [s.stage for s in outcome.stages]
        self.assertEqual(stage_names, ["fetch", "analyze", "render", "archive"])
        for s in outcome.stages:
            self.assertEqual(s.status, "ok")
            self.assertGreaterEqual(s.duration_ms, 0)

    def test_pipeline_fetch_failure(self):
        from reports.pipeline import ReportPipeline
        report = FakePlugin(fail_at="fetch")
        outcome = ReportPipeline(report).run()
        self.assertNotEqual(outcome.result.error, "")
        self.assertEqual(outcome.stages[0].status, "failed")
        # Subsequent stages must NOT have run.
        self.assertEqual(len(outcome.stages), 1)

    def test_pipeline_analyze_failure(self):
        from reports.pipeline import ReportPipeline
        report = FakePlugin(fail_at="analyze")
        outcome = ReportPipeline(report).run()
        self.assertNotEqual(outcome.result.error, "")
        self.assertEqual(outcome.stages[0].status, "ok")
        self.assertEqual(outcome.stages[1].status, "failed")
        self.assertEqual(len(outcome.stages), 2)

    def test_pipeline_render_failure(self):
        from reports.pipeline import ReportPipeline
        report = FakePlugin(fail_at="render")
        outcome = ReportPipeline(report).run()
        self.assertNotEqual(outcome.result.error, "")
        self.assertEqual(len(outcome.stages), 3)

    def test_pipeline_archive_failure_does_not_fail_pipeline(self):
        from reports.pipeline import ReportPipeline
        report = FakePlugin(fail_at="archive")
        outcome = ReportPipeline(report).run()
        # Archive is best-effort: pipeline still reports success.
        self.assertEqual(outcome.result.error, "")
        self.assertEqual(outcome.stages[3].status, "failed")
        # And the other stages succeeded.
        self.assertEqual(outcome.stages[0].status, "ok")
        self.assertEqual(outcome.stages[1].status, "ok")
        self.assertEqual(outcome.stages[2].status, "ok")


# =============================================================================
# Registry
# =============================================================================


class RegistryTests(unittest.TestCase):
    def _write_manifest(self, tmpdir: str, name: str = "alpha",
                        extra: dict | None = None) -> str:
        data = {
            "name": name,
            "display_name": f"Report {name}",
            "schedule": "0 9 * * *",
            "version": "1.2.3",
            "entrypoint": "tests.test_phase2b_framework:FakePlugin",
        }
        if extra:
            data.update(extra)
        path = os.path.join(tmpdir, f"{name}.json")
        Path(path).write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_empty_directory_yields_empty_registry(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Registry.from_directory(td)
            self.assertEqual(len(reg), 0)
            self.assertEqual(reg.list_names(), [])

    def test_missing_directory_yields_empty_registry(self):
        reg = Registry.from_directory("/nonexistent/path/xyz")
        self.assertEqual(len(reg), 0)

    def test_loads_json_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_manifest(td, "alpha")
            self._write_manifest(td, "beta")
            reg = Registry.from_directory(td)
            self.assertEqual(set(reg.list_names()), {"alpha", "beta"})

    def test_get_manifest_returns_copy(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_manifest(td, "alpha")
            reg = Registry.from_directory(td)
            m = reg.get_manifest("alpha")
            m["version"] = "9.9.9"
            m2 = reg.get_manifest("alpha")
            self.assertEqual(m2["version"], "1.2.3")

    def test_instantiate_resolves_entrypoint(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_manifest(td, "alpha")
            reg = Registry.from_directory(td)
            instance = reg.instantiate("alpha")
            # The class identity check is "the resolved class is the
            # FakePlugin defined in tests.test_phase2b_framework".
            # We compare by module + qualname to avoid issues when
            # this test is run as a script (under __main__) AND the
            # registry imports it as a module.
            self.assertEqual(instance.__class__.__name__, "FakePlugin")
            self.assertIn(
                "test_phase2b_framework",
                instance.__class__.__module__,
            )
            # Cache: second call returns the SAME object.
            self.assertIs(reg.instantiate("alpha"), instance)

    def test_instantiate_missing_raises_keyerror(self):
        with tempfile.TemporaryDirectory() as td:
            reg = Registry.from_directory(td)
            with self.assertRaises(KeyError):
                reg.instantiate("nope")

    def test_unknown_name_raises_keyerror(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_manifest(td, "alpha")
            reg = Registry.from_directory(td)
            with self.assertRaises(KeyError):
                reg.get_manifest("nope")

    def test_clear_cache(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_manifest(td, "alpha")
            reg = Registry.from_directory(td)
            i1 = reg.instantiate("alpha")
            reg.clear_cache()
            i2 = reg.instantiate("alpha")
            self.assertIsNot(i1, i2)

    def test_yaml_files_are_skipped_when_pyyaml_missing(self):
        # We do NOT assert that yaml files are loaded; we only assert
        # they don't crash the loader. (PyYAML isn't installed here.)
        with tempfile.TemporaryDirectory() as td:
            Path(td, "junk.yaml").write_text("name: junk\n", encoding="utf-8")
            self._write_manifest(td, "alpha")
            reg = Registry.from_directory(td)
            # alpha is loaded; junk.yaml was either loaded or silently
            # skipped. The only assertion we make is that no exception
            # was raised and "alpha" is present.
            self.assertIn("alpha", reg.list_names())


from reports.registry import Registry


# =============================================================================
# Task state machine
# =============================================================================


class TaskStateMachineTests(unittest.TestCase):
    def test_initial_status_is_created(self):
        from reports.hermes.task import Task, STATUS_CREATED
        t = Task("macro_daily")
        self.assertEqual(t.status, STATUS_CREATED)
        self.assertEqual(t.report_name, "macro_daily")
        self.assertTrue(t.task_id)

    def test_happy_path_transitions(self):
        from reports.hermes.task import (
            Task, STATUS_CREATED, STATUS_QUEUED, STATUS_RUNNING,
            STATUS_COMPLETED,
        )
        t = Task("macro_daily")
        t.transition(STATUS_QUEUED)
        self.assertEqual(t.status, STATUS_QUEUED)
        t.transition(STATUS_RUNNING)
        self.assertEqual(t.status, STATUS_RUNNING)
        self.assertNotEqual(t.started_at, "")
        t.transition(STATUS_COMPLETED)
        self.assertEqual(t.status, STATUS_COMPLETED)
        self.assertNotEqual(t.finished_at, "")

    def test_illegal_transition_raises(self):
        from reports.hermes.task import Task, STATUS_RUNNING
        t = Task("macro_daily")
        # Cannot jump from created → running.
        with self.assertRaises(ValueError):
            t.transition(STATUS_RUNNING)

    def test_cannot_transition_from_terminal(self):
        from reports.hermes.task import (
            Task, STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED,
        )
        t = Task("macro_daily")
        t.transition(STATUS_QUEUED).transition(STATUS_RUNNING).transition(STATUS_COMPLETED)
        with self.assertRaises(ValueError):
            t.transition(STATUS_QUEUED)  # back from completed: illegal

    def test_cancel_from_queued_or_running(self):
        from reports.hermes.task import (
            Task, STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLED,
        )
        t = Task("x")
        t.transition(STATUS_QUEUED).transition(STATUS_CANCELLED)
        self.assertEqual(t.status, STATUS_CANCELLED)
        # And from running:
        t2 = Task("x")
        t2.transition(STATUS_QUEUED).transition(STATUS_RUNNING).transition(STATUS_CANCELLED)
        self.assertEqual(t2.status, STATUS_CANCELLED)

    def test_to_dict_is_json_safe(self):
        from reports.hermes.task import Task, STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED
        t = Task("x")
        t.transition(STATUS_QUEUED).transition(STATUS_RUNNING).transition(STATUS_COMPLETED)
        d = t.to_dict()
        # Must be JSON-serializable.
        s = json.dumps(d)
        self.assertIsInstance(json.loads(s), dict)
        self.assertEqual(d["status"], "completed")
        self.assertIn("task_id", d)
        self.assertIn("report_name", d)


# =============================================================================
# Dispatcher (no real fetches — uses FakePlugin via custom manifest)
# =============================================================================


class DispatcherTests(unittest.TestCase):
    def _build_dispatcher(self) -> "Dispatcher":
        with tempfile.TemporaryDirectory() as td:
            self._tmp_manifest_dir = td  # keep alive
            Path(td, "fake.json").write_text(json.dumps({
                "name": "fake",
                "display_name": "Fake",
                "schedule": "0 9 * * *",
                "version": "1.0.0",
                "entrypoint": "tests.test_phase2b_framework:FakePlugin",
            }), encoding="utf-8")
            reg = Registry.from_directory(td)
            return Dispatcher(registry=reg)

    def test_dry_run_does_not_call_fetch(self):
        from reports.hermes.dispatcher import Dispatcher
        d = self._build_dispatcher()
        task = d.dispatch("fake", dry_run=True)
        self.assertEqual(task.status, "completed")
        self.assertTrue(task.metadata.get("dry_run"))
        # The result_text should mention dry-run, not "FAKE-REPORT".
        self.assertIn("dry-run", task.result_text.lower())

    def test_dry_run_records_entrypoint_resolution(self):
        d = self._build_dispatcher()
        task = d.dispatch("fake", dry_run=True)
        self.assertIn("entrypoint_resolved", task.metadata)
        self.assertEqual(
            task.metadata["entrypoint_resolved"],
            "tests.test_phase2b_framework",
        )

    def test_dispatch_unknown_report_raises(self):
        d = self._build_dispatcher()
        with self.assertRaises(KeyError):
            d.dispatch("nope", dry_run=True)

    def test_status_lookup(self):
        d = self._build_dispatcher()
        task = d.dispatch("fake", dry_run=True)
        self.assertIs(d.status(task.task_id), task)
        self.assertIsNone(d.status("missing-id"))

    def test_rerun_creates_new_task(self):
        d = self._build_dispatcher()
        t1 = d.dispatch("fake", dry_run=True)
        t2 = d.rerun(t1.task_id)
        self.assertNotEqual(t1.task_id, t2.task_id)
        self.assertEqual(t2.report_name, "fake")
        self.assertEqual(t2.metadata.get("rerun_of"), t1.task_id)

    def test_cancel_non_terminal(self):
        # The state machine goes created → queued → (cancel here).
        # We can't have a non-terminal task without running it,
        # so we craft a task manually.
        from reports.hermes.dispatcher import Dispatcher
        from reports.hermes.task import (
            Task, STATUS_QUEUED, STATUS_CANCELLED,
        )
        d = self._build_dispatcher()
        t = Task("fake")
        d._tasks[t.task_id] = t
        t.transition(STATUS_QUEUED)
        cancelled = d.cancel(t.task_id)
        self.assertEqual(cancelled.status, STATUS_CANCELLED)

    def test_cancel_terminal_raises(self):
        from reports.hermes.dispatcher import Dispatcher
        from reports.hermes.task import Task, STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETED
        d = self._build_dispatcher()
        t = Task("fake")
        t.transition(STATUS_QUEUED).transition(STATUS_RUNNING).transition(STATUS_COMPLETED)
        d._tasks[t.task_id] = t
        with self.assertRaises(ValueError):
            d.cancel(t.task_id)

    def test_cancel_unknown_raises(self):
        d = self._build_dispatcher()
        with self.assertRaises(KeyError):
            d.cancel("nope")

    def test_list_tasks(self):
        d = self._build_dispatcher()
        t1 = d.dispatch("fake", dry_run=True)
        t2 = d.dispatch("fake", dry_run=True)
        all_tasks = d.list_tasks()
        self.assertEqual(len(all_tasks), 2)
        self.assertEqual({t.task_id for t in all_tasks}, {t1.task_id, t2.task_id})


from reports.hermes.dispatcher import Dispatcher


# =============================================================================
# CLI smoke tests
# =============================================================================


class CLITests(unittest.TestCase):
    def test_list_command_lists_reports(self):
        import run_report
        with tempfile.TemporaryDirectory() as td:
            Path(td, "alpha.json").write_text(json.dumps({
                "name": "alpha",
                "display_name": "Alpha",
                "schedule": "0 9 * * *",
                "version": "1.0.0",
                "entrypoint": "x.y:Z",
            }), encoding="utf-8")
            Path(td, "beta.json").write_text(json.dumps({
                "name": "beta",
                "display_name": "Beta",
                "schedule": "0 10 * * *",
                "version": "2.0.0",
                "entrypoint": "a.b:C",
            }), encoding="utf-8")
            rc = run_report.main(["--config-dir", td, "list"])
            self.assertEqual(rc, 0)

    def test_dispatch_dry_run_does_not_execute(self):
        import run_report
        with tempfile.TemporaryDirectory() as td:
            Path(td, "fake.json").write_text(json.dumps({
                "name": "fake",
                "display_name": "Fake",
                "schedule": "0 9 * * *",
                "version": "1.0.0",
                "entrypoint": "tests.test_phase2b_framework:FakePlugin",
            }), encoding="utf-8")
            rc = run_report.main(["--config-dir", td, "dispatch", "fake"])
            self.assertEqual(rc, 0)

    def test_dispatch_unknown_returns_error_code(self):
        import run_report
        with tempfile.TemporaryDirectory() as td:
            Path(td, "fake.json").write_text(json.dumps({
                "name": "fake",
                "display_name": "Fake",
                "schedule": "0 9 * * *",
                "version": "1.0.0",
                "entrypoint": "x.y:Z",
            }), encoding="utf-8")
            rc = run_report.main(["--config-dir", td, "dispatch", "missing"])
            self.assertEqual(rc, 1)

    def test_execute_flag_must_be_explicit(self):
        """The default is dry-run; --execute is required to actually run."""
        import run_report
        with tempfile.TemporaryDirectory() as td:
            Path(td, "fake.json").write_text(json.dumps({
                "name": "fake",
                "display_name": "Fake",
                "schedule": "0 9 * * *",
                "version": "1.0.0",
                "entrypoint": "tests.test_phase2b_framework:FakePlugin",
            }), encoding="utf-8")
            # No --execute: must be dry-run. We can verify by checking
            # the task dict printed.
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = run_report.main(["--config-dir", td, "dispatch", "fake"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("dry-run", out.lower())


# =============================================================================
# Production file protection
# =============================================================================


class ProductionFileProtectionTests(unittest.TestCase):
    """
    Verify the production files in the project root are byte-for-byte
    unchanged after the framework lands. This is a defense against
    accidental edits in Step 3 / Step 4A.
    """

    PROTECTED_FILES = [
        "macro_daily.py",
        "industry_weekly.py",
        "company_monthly.py",
        "institutional.py",
        "db.py",
        "run.sh",
        "run_weekly.sh",
        "run_monthly.sh",
        "industry_config.json",
        "taiwan50_config.json",
    ]

    def test_sha256_against_step2_baseline(self):
        baseline_path = Path("/tmp/phase2b_step34_baseline.json")
        if not baseline_path.exists():
            self.skipTest(f"Baseline not found at {baseline_path}")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        for fname, expected in baseline.items():
            fpath = PROJECT_ROOT / fname
            self.assertTrue(
                fpath.exists(),
                f"Production file missing: {fname}",
            )
            actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
            self.assertEqual(
                actual, expected["sha256"],
                f"{fname} has been modified! "
                f"Expected {expected['sha256']}, got {actual}. "
                f"Production files must NOT be touched by Phase 2B.",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
