#!/usr/bin/env python3
"""
Dispatcher — task lifecycle manager for report plugins.

Phase 2B Step 4A + 4B. The Dispatcher is the orchestrator that:
  1. Receives a `dispatch(report_name, ...)` call
  2. Creates a Task (status=created, then queued)
  3. Resolves the report via the Registry
  4. Runs it through the ReportPipeline (status=running)
  5. Records the outcome (completed / failed)
  6. Surfaces the result via `status(task_id)`, `list_tasks()`, etc.
  7. (Step 4B) Persists task + artifact metadata to a LocalStore so
     the state survives process restarts and can be inspected by
     later CLI invocations.

Public API (4 methods, matching the task brief):
  - dispatch(report_name, metadata=None, dry_run=False) -> Task
  - status(task_id) -> Task | None
  - rerun(task_id) -> Task
  - cancel(task_id) -> Task

`dry_run=True` is the critical safety feature for the CLI. When
True, the dispatcher:
  1. Resolves the manifest from the Registry
  2. Constructs the BaseReport instance
  3. Returns a task with status=completed and metadata describing
     what WOULD have run — without ever calling fetch / analyze /
     render. This satisfies the requirement "CLI must NOT execute
     production reports."

`dry_run=False` (--execute mode) is the local execution path. It
runs the pipeline and writes the rendered report body to the local
artifact directory. It does NOT publish to Telegram and does NOT
modify any production state outside the framework's own metadata
tree.

Persistence model (Step 4B):
  - On every state transition the Dispatcher calls store.save_task()
    so a crash mid-run still leaves a consistent task record.
  - On successful execute, the rendered report text is written to
    store.write_report_text() and an artifact metadata record is
    saved via store.save_artifact(). The path is appended to
    task.artifact_paths and the task is saved again.
  - status() / list_tasks() consult the in-memory _tasks dict first
    (fast path for in-process operations) and fall back to
    store.load_task() / store.list_tasks() so cross-process queries
    (the CLI is one process per invocation) see persisted state.

The store is OPTIONAL — passing `store=None` falls back to the
Step 4A purely in-memory behavior, so existing tests that don't
care about persistence keep working unchanged.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from reports.base import BaseReport, ReportResult
from reports.hermes.store import LocalStore
from reports.hermes.task import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Task,
)
from reports.pipeline import PipelineResult, ReportPipeline
from reports.registry import Registry


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class Dispatcher:
    """
    Task lifecycle manager. One instance per process. Tasks are
    kept in memory and (optionally) persisted to a LocalStore.

    Construction:
        Dispatcher(registry)
        Dispatcher(registry, store=LocalStore())
        Dispatcher(registry, store=LocalStore(root="/tmp/test-meta"))

    The store can be `None` (no persistence) — this is the Step 4A
    behavior. When a store is provided, every state transition
    triggers a save_task() call, and execute mode writes a local
    artifact file.
    """

    def __init__(
        self,
        registry: Registry,
        store: Optional[LocalStore] = None,
    ):
        if registry is None:
            raise ValueError("Dispatcher requires a Registry instance")
        self.registry = registry
        self.store = store
        self._tasks: dict[str, Task] = {}

    # ----- core API -----

    def dispatch(
        self,
        report_name: str,
        metadata: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> Task:
        """
        Create a task, transition it through the state machine, run
        the report, and return the final Task.

        dry_run=True short-circuits before the fetch stage: it
        resolves the manifest, constructs the BaseReport, and
        returns a task marked completed with a `dry_run: true`
        metadata flag. This is what the CLI uses to validate that
        a report is plumbed correctly without ever calling yfinance.
        """
        if report_name not in self.registry:
            raise KeyError(
                f"Report '{report_name}' not in registry. "
                f"Known: {self.registry.list_names()}"
            )

        # Step 1: created
        task = Task(
            report_name=report_name,
            metadata=dict(metadata) if metadata else {},
        )
        task.metadata.setdefault("dry_run", dry_run)
        self._tasks[task.task_id] = task
        self._persist(task)

        # Step 2: queued
        task.transition(STATUS_QUEUED)
        self._persist(task)

        if dry_run:
            return self._dry_run_complete(task)

        # Step 3: running
        task.transition(STATUS_RUNNING)
        self._persist(task)
        try:
            report = self.registry.instantiate(report_name)
            pipeline = ReportPipeline(report)
            # publish=False is enforced regardless of caller intent.
            # This guarantees the dispatcher never prints to stdout
            # (which the cron agent would forward to Telegram). The
            # rendered text is captured in outcome.result.report_text
            # and persisted as a local artifact.
            outcome: PipelineResult = pipeline.run(publish=False)
        except Exception as exc:  # noqa: BLE001
            task.transition(STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")
            self._persist(task)
            return task

        # Step 4: terminal
        if outcome.result.error:
            task.transition(STATUS_FAILED, error=outcome.result.error)
            self._persist(task)
        else:
            task.result_text = outcome.result.report_text
            task.stages = [s.__dict__ for s in outcome.stages]
            task.metadata["total_duration_ms"] = outcome.total_duration_ms
            task.transition(STATUS_COMPLETED)
            # Persist the local artifact (rendered report text).
            self._persist_execute_artifact(task)
            self._persist(task)
        return task

    def status(self, task_id: str) -> Optional[Task]:
        """
        Return the Task for `task_id`, or None if not found.

        Order of resolution:
          1. In-memory dict (fast path for in-process operations).
          2. LocalStore (cross-process / post-restart recovery).
        """
        in_mem = self._tasks.get(task_id)
        if in_mem is not None:
            return in_mem
        if self.store is None:
            return None
        record = self.store.load_task(task_id)
        if record is None:
            return None
        return self._task_from_record(record)

    def rerun(self, task_id: str) -> Task:
        """
        Re-dispatch the same report that produced `task_id`. The
        new task gets a fresh task_id; the old one is left in its
        terminal state for audit. The new task's `parent_task_id`
        is set to the original task_id so the lineage is traceable.

        The rerun is ALWAYS dry-run by default — this is the safety
        property. To actually run the pipeline, the caller must
        explicitly call `dispatch(report_name, dry_run=False)`
        with `metadata={"rerun_of": task_id}` (or use the CLI's
        `rerun --execute` flag). This matches the principle that
        the existing cron schedule is the right trigger for
        production runs.

        If the original task is missing (e.g. never existed, or was
        lost to a metadata dir wipe), raises KeyError.
        """
        old = self.status(task_id)
        if old is None:
            raise KeyError(f"No task with id {task_id}")
        new_task = self.dispatch(
            old.report_name,
            metadata={"rerun_of": task_id},
            dry_run=True,
        )
        new_task.parent_task_id = task_id
        self._persist(new_task)
        return new_task

    def cancel(self, task_id: str) -> Task:
        """
        Mark a non-terminal task as cancelled. If the task is
        already terminal, raises ValueError (you can't cancel a
        completed task — that's not what cancel means).

        For in-memory tasks: works for created / queued / running.
        For tasks loaded from the store but not in this process's
        memory: works for created / queued only. A running task
        loaded from the store is in another process; we mark it
        cancelled in the local store so future status() returns
        cancelled, but the running process will finish its current
        work (sync pipeline cannot be interrupted). This is a
        documented limitation — async / threaded execution is
        Phase 2D scope.
        """
        task = self.status(task_id)
        if task is None:
            raise KeyError(f"No task with id {task_id}")
        if task.is_terminal:
            raise ValueError(
                f"Task {task_id} is already in terminal state "
                f"'{task.status}'; cannot cancel."
            )
        # If the task was loaded from the store (not in this
        # process's memory) and is currently running, we document
        # the limitation in the metadata before marking cancelled.
        in_memory = task_id in self._tasks
        if not in_memory and task.status == STATUS_RUNNING:
            task.metadata["cancel_note"] = (
                "Task was running in another process; status flipped "
                "to cancelled in the store but the running process "
                "may still finish. Sync pipeline cannot be interrupted."
            )
        task.transition(STATUS_CANCELLED, error="cancelled by dispatcher")
        self._persist(task)
        return task

    # ----- list API -----

    def list_tasks(self, report_name: Optional[str] = None) -> list[Task]:
        """
        Return all known tasks, sorted by created_at ascending.

        Merges in-memory tasks with persisted tasks. In-memory
        tasks win on conflict (they are the freshest copy).
        """
        seen: dict[str, Task] = {}
        # Persisted tasks first (older, more numerous).
        if self.store is not None:
            for rec in self.store.list_tasks(report_name=report_name):
                tid = rec.get("task_id")
                if tid:
                    seen[tid] = self._task_from_record(rec)
        # In-memory tasks override (newer / in-progress).
        for tid, t in self._tasks.items():
            if report_name is not None and t.report_name != report_name:
                continue
            seen[tid] = t
        return sorted(seen.values(), key=lambda t: t.created_at)

    def list_active(self) -> list[Task]:
        """Return non-terminal tasks only."""
        return [t for t in self.list_tasks() if not t.is_terminal]

    # ----- artifact API (Step 4B) -----

    def get_artifact(self, task_id: str) -> Optional[dict]:
        """
        Return the artifact metadata dict for `task_id`, or None.
        Loads from the store (artifacts are never kept in memory).
        """
        if self.store is None:
            return None
        return self.store.get_artifact_for_task(task_id)

    def get_artifact_text(self, artifact_meta: dict) -> Optional[str]:
        """
        Read the rendered report text from the local artifact file
        whose path is recorded in `artifact_meta`. Returns None if
        the store is unavailable, the path is empty, or the file
        no longer exists.
        """
        if self.store is None:
            return None
        path = artifact_meta.get("artifact_path", "")
        if not path:
            return None
        return self.store.read_report_text(path)

    # ----- internals -----

    def _dry_run_complete(self, task: Task) -> Task:
        """
        Mark a dry-run task as completed without invoking the
        pipeline. Records the resolved manifest name + entrypoint
        in metadata so the CLI can show what WOULD have run.

        State machine: we walk created → queued → running → completed
        (the same path a real run takes). The pipeline stages are
        simply not invoked during the running phase. This keeps the
        state machine strict and uniform across both paths.
        """
        task.transition(STATUS_RUNNING)
        self._persist(task)
        try:
            manifest = self.registry.get_manifest(task.report_name)
            # Verify the entrypoint is importable, but don't call .run()
            # on it. We deliberately do NOT call instantiate() here
            # because that might trigger plugin __init__ side effects
            # in future plugin classes. We only verify the import
            # path is resolvable.
            entrypoint = manifest.get("entrypoint", "")
            module_path = entrypoint.split(":", 1)[0] if entrypoint else ""
            if module_path:
                import importlib
                importlib.import_module(module_path)
                task.metadata["entrypoint_resolved"] = module_path
            task.metadata["manifest_keys"] = sorted(manifest.keys())
            task.metadata["dry_run_completed_at"] = _utcnow_iso()
            task.result_text = (
                f"[dry-run] Would execute report '{task.report_name}' "
                f"via entrypoint '{entrypoint}'. No fetch/analyze/render "
                f"was performed."
            )
            task.transition(STATUS_COMPLETED)
        except Exception as exc:  # noqa: BLE001
            task.transition(STATUS_FAILED, error=f"dry-run failed: {exc}")
        # Step 4B: also write a placeholder artifact record so the
        # artifact CLI subcommand can find SOMETHING for every task,
        # even dry-run ones. The artifact_path is empty (no real
        # rendered text was produced) and dry_run=true is set.
        self._persist_dry_run_artifact(task)
        self._persist(task)
        return task

    def _persist(self, task: Task) -> None:
        """
        Write the task record to the store if a store is configured.
        Silent no-op when store is None (Step 4A in-memory mode).
        Failures here are logged to stderr but do NOT raise — the
        task is the source of truth, and a disk failure should not
        make the in-memory operation look like a failure to the
        caller.
        """
        if self.store is None:
            return
        try:
            self.store.save_task(task.to_dict())
        except Exception as exc:  # noqa: BLE001
            import sys
            print(
                f"⚠️  store.save_task failed for {task.task_id}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def _persist_execute_artifact(self, task: Task) -> None:
        """
        Write the rendered report body to the local artifact file
        and record an artifact metadata entry. No-op when:
          - the store is None (in-memory mode)
          - the task has no result_text (i.e. a failed execute)
        """
        if self.store is None:
            return
        if not task.result_text:
            return
        artifact_id = f"art-{task.task_id}"
        try:
            artifact_path = self.store.write_report_text(artifact_id, task.result_text)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(
                f"⚠️  store.write_report_text failed for {task.task_id}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return
        # Record the artifact metadata.
        artifact_meta = {
            "artifact_id": artifact_id,
            "task_id": task.task_id,
            "report_name": task.report_name,
            "created_at": task.finished_at or _utcnow_iso(),
            "dry_run": False,
            "artifact_path": str(artifact_path),
            "result_text_len": len(task.result_text),
            "summary": {
                "total_duration_ms": task.metadata.get("total_duration_ms", 0),
                "stages": task.stages,
            },
        }
        try:
            self.store.save_artifact(artifact_meta)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(
                f"⚠️  store.save_artifact failed for {task.task_id}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return
        # Record the path on the task and re-persist.
        task.artifact_paths.append(str(artifact_path))

    def _persist_dry_run_artifact(self, task: Task) -> None:
        """
        Record a placeholder artifact for a dry-run task. The
        artifact_path is empty (no real text), but the metadata is
        queryable so the CLI's `artifact <task_id>` subcommand
        returns a useful "dry_run: true, no artifact written" record
        instead of "no artifact found".
        """
        if self.store is None:
            return
        artifact_id = f"art-{task.task_id}"
        artifact_meta = {
            "artifact_id": artifact_id,
            "task_id": task.task_id,
            "report_name": task.report_name,
            "created_at": task.finished_at or _utcnow_iso(),
            "dry_run": True,
            "artifact_path": "",  # no real file for dry-run
            "result_text_len": len(task.result_text),
            "summary": {
                "dry_run_completed_at": task.metadata.get(
                    "dry_run_completed_at", ""
                ),
                "entrypoint_resolved": task.metadata.get(
                    "entrypoint_resolved", ""
                ),
            },
        }
        try:
            self.store.save_artifact(artifact_meta)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(
                f"⚠️  store.save_artifact (dry-run) failed for {task.task_id}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    @staticmethod
    def _task_from_record(record: dict) -> Task:
        """
        Reconstruct a Task from a dict produced by Task.to_dict()
        and persisted by the store. This is a "thin" reconstruction
        — the returned object supports read-only access (.task_id,
        .status, .to_dict()) but calling .transition() on it will
        not propagate to the store; the caller should treat it as
        a view.

        This split keeps Task free of any I/O dependencies while
        letting the Dispatcher surface persisted state as Task
        objects.
        """
        t = Task(
            report_name=record.get("report_name", ""),
            task_id=record.get("task_id"),
            metadata=record.get("metadata") or {},
        )
        t.status = record.get("status", STATUS_QUEUED)
        t.created_at = record.get("created_at", "")
        t.started_at = record.get("started_at", "")
        t.finished_at = record.get("finished_at", "")
        t.error = record.get("error", "")
        t.result_text = record.get("result_text", "")
        t.stages = list(record.get("stages") or [])
        t.artifact_paths = list(record.get("artifact_paths") or [])
        t.parent_task_id = record.get("parent_task_id", "")
        return t


__all__ = ["Dispatcher"]
