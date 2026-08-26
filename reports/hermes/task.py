#!/usr/bin/env python3
"""
Task — the in-memory record of a single report dispatch attempt.

Phase 2B Step 4A + 4B. This module defines the data model for tasks.
A task is created when something asks the Dispatcher to run a report;
it transitions through a state machine (created → queued → running
→ completed | failed | cancelled) and accumulates timing + error
info along the way.

In Step 4A the model was in-memory only (the Dispatcher kept tasks
in a dict). In Step 4B we add:

  - `artifact_paths: list[str]`  — list of local artifact file paths
                                   written by the Dispatcher. Empty
                                   for dry-run tasks. The Dispatcher
                                   populates this on execute mode.
  - `parent_task_id: str`        — when this task is the child of a
                                   rerun(), the parent task_id. Empty
                                   for top-level tasks.
  - `duration_ms` in to_dict()   — convenience field (computed from
                                   started_at / finished_at) so the
                                   CLI can show run duration without
                                   re-parsing timestamps.

The Dispatcher is the source of truth for persistence — Task stays
a pure in-memory model. The Store (reports/hermes/store.py) reads
Task.to_dict() and writes it to disk; Task itself does not know
about the filesystem.

State machine (transitions enforced by Task.transition()):

    created
      └──> queued
            └──> running
                  ├──> completed      (terminal)
                  ├──> failed         (terminal)
                  └──> cancelled      (terminal)

`cancelled` is reachable from queued OR running (not from a terminal
state). The Dispatcher.cancel() interface uses this transition.

Why a class (not a dataclass): the state machine needs invariants
("you can't go from completed back to running") that a frozen
dataclass would make awkward. A regular class with a private
`_status` attribute + a transition() method is clearer.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any, Optional

# Status constants — exported as module-level names so callers can
# write `Task.STATUS_QUEUED` etc. without typo risk.
STATUS_CREATED = "created"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# Allowed transitions. Anything not in this map is rejected.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    STATUS_CREATED: {STATUS_QUEUED, STATUS_CANCELLED},
    STATUS_QUEUED: {STATUS_RUNNING, STATUS_CANCELLED},
    STATUS_RUNNING: {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED},
    STATUS_COMPLETED: set(),
    STATUS_FAILED: set(),
    STATUS_CANCELLED: set(),
}

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}


class Task:
    """
    In-memory record of one dispatch attempt.

    Fields:
        task_id        — UUID4 string, unique per task
        report_name    — name in the Registry (e.g. "macro_daily")
        status         — one of the STATUS_* constants
        created_at     — ISO 8601 UTC timestamp
        started_at     — ISO 8601 UTC timestamp (set on running)
        finished_at    — ISO 8601 UTC timestamp (set on terminal)
        error          — populated on failed / cancelled
        metadata       — free-form dict for the caller's bookkeeping
        result_text    — populated on completed (the rendered report)
        stages         — list of stage records from ReportPipeline
        artifact_paths — list of local artifact file paths (Step 4B)
        parent_task_id — parent task_id if this is a rerun() child
    """

    def __init__(
        self,
        report_name: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ):
        self.task_id: str = task_id or str(uuid.uuid4())
        self.report_name: str = report_name
        self.status: str = STATUS_CREATED
        self.created_at: str = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.started_at: str = ""
        self.finished_at: str = ""
        self.error: str = ""
        self.metadata: dict[str, Any] = dict(metadata) if metadata else {}
        self.result_text: str = ""
        self.stages: list[dict[str, Any]] = []
        # Step 4B: explicit artifact path list (one entry per
        # local artifact file written). May be empty for dry-run
        # tasks. The Dispatcher populates this when it writes a
        # rendered report body to the local store.
        self.artifact_paths: list[str] = []
        # Step 4B: parent_task_id is set when this task is the
        # child of a rerun(). Allows the operator to trace the
        # lineage of a task chain (audit trail).
        self.parent_task_id: str = metadata.get("rerun_of", "") if metadata else ""

    # ----- transitions -----

    def transition(self, new_status: str, error: str = "") -> "Task":
        """
        Move the task to `new_status`. Validates the transition against
        the state machine. On success, mutates self.status and any
        timing fields, then returns self (for chaining). On an
        invalid transition, raises ValueError — caller is expected
        to catch it (the Dispatcher does, the CLI surfaces it).
        """
        if new_status not in _ALLOWED_TRANSITIONS:
            raise ValueError(
                f"Unknown status '{new_status}'. "
                f"Valid: {sorted(_ALLOWED_TRANSITIONS.keys())}"
            )
        allowed = _ALLOWED_TRANSITIONS[self.status]
        if new_status not in allowed:
            raise ValueError(
                f"Illegal transition {self.status} -> {new_status} "
                f"for task {self.task_id}. Allowed: {sorted(allowed)}"
            )
        self.status = new_status
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        if new_status == STATUS_RUNNING:
            self.started_at = now
        if new_status in TERMINAL_STATUSES:
            self.finished_at = now
            if error:
                self.error = error
        return self

    # ----- queries -----

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict. Used by the CLI / status() output."""
        # Step 4B: compute a duration_ms field for terminal tasks.
        # Non-terminal tasks return 0 (no finished_at yet).
        duration_ms = 0
        if self.started_at and self.finished_at:
            try:
                t0 = _dt.datetime.fromisoformat(self.started_at)
                t1 = _dt.datetime.fromisoformat(self.finished_at)
                duration_ms = int((t1 - t0).total_seconds() * 1000)
            except ValueError:
                # If timestamps are malformed for any reason, just
                # leave duration_ms at 0 rather than crash the
                # serialization. The dispatcher's own
                # `total_duration_ms` (if present) is preferred.
                duration_ms = 0
        return {
            "task_id": self.task_id,
            "report_name": self.report_name,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": duration_ms,
            "error": self.error,
            "metadata": self.metadata,
            "result_text": self.result_text,
            "result_text_len": len(self.result_text),
            "stages": list(self.stages),
            "artifact_paths": list(self.artifact_paths),
            "parent_task_id": self.parent_task_id,
        }


__all__ = [
    "Task",
    "STATUS_CREATED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
    "TERMINAL_STATUSES",
]
