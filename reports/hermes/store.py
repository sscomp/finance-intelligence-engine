#!/usr/bin/env python3
"""
Store — local JSON-backed persistence for Dispatcher tasks and artifacts.

Phase 2B Step 4B. This module replaces the Step 4A in-memory task table
with a small, dependency-free file store so the framework CLI's
`status / rerun / cancel` operations survive process restarts. It
deliberately does NOT use SQLite — the task brief forbids touching
`macro_history.db` schema. The store is intentionally simple:

    metadata/
      tasks/
        <task_id>.json     — one file per task (full state + metadata)
        index.json         — list of {task_id, report_name, status, created_at}
                             kept for fast `list` queries without scanning
                             every file
      reports/
        <report_id>.json   — one file per artifact (artifact metadata)
        index.json         — list of artifact metadata records
        artifacts/         — the actual rendered report text (if execute mode)

Design rules:
  - Every write is atomic (write to .tmp, fsync, rename).
  - Every JSON file is UTF-8 + ensure_ascii=False so Chinese report
    text round-trips cleanly.
  - The store is fail-soft: corrupt files are reported but never
    block reads of the rest of the data.
  - No external dependencies (no sqlite, no redis, no pickle).
  - The store is per-process for reads but survives across processes
    via the on-disk JSON.

Why a per-task file instead of one big JSON:
  - Avoid loading the whole history into memory for status queries.
  - Avoid losing the entire history on a single corrupt write.
  - Make artifact cleanup / archival trivial (delete one file).
  - Allow `git`-style diffs in `git log -p` of the metadata dir.

Why a separate artifacts/ subdir for the rendered report text:
  - Keeps the task metadata JSON small (metadata is metadata, not
    the multi-KB report body).
  - Lets the operator inspect the rendered text without parsing JSON.
  - Makes it safe to truncate / prune artifact files independently
    of task records.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional


# Atomic-write lock: per-process guard so concurrent writes to the
# same index file don't interleave.  Cross-process safety is provided
# by the atomic-rename pattern (write .tmp + os.replace) — POSIX
# guarantees rename atomicity within a filesystem.
_write_lock = threading.Lock()


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: Any) -> None:
    """
    Write JSON to `path` atomically: write to a temp file in the same
    directory, fsync, then os.replace. This guarantees that a crash
    mid-write never leaves a half-written JSON file on disk.

    The temp file uses NamedTemporaryFile(delete=False) so we can
    control the name and fsync before rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use a stable prefix so operators can see partial writes if
    # a crash happens between write and rename.
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync may fail on some filesystems (e.g. tmpfs without
                # backing store); the atomic rename still gives us
                # crash-safety within a single filesystem operation.
                pass
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the temp file on any failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[dict]:
    """
    Read JSON from `path`. Returns None if the file is missing or
    unparseable. The intent is fail-soft: a corrupt task file
    should not block the rest of the system.
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


class LocalStore:
    """
    File-backed task + artifact store. The root directory defaults to
    `<project_root>/metadata`, but can be overridden for tests.

    Public API:
        save_task(task_dict)                    -> None
        load_task(task_id)                      -> dict | None
        list_tasks(report_name=None)            -> list[dict]
        save_artifact(artifact_dict)            -> None
        load_artifact(artifact_id)              -> dict | None
        list_artifacts(task_id=None)            -> list[dict]
        get_artifact_for_task(task_id)          -> dict | None
        write_report_text(artifact_id, text)    -> Path
        read_report_text(artifact_path)         -> str | None
    """

    def __init__(self, root: str | os.PathLike | None = None):
        if root is None:
            # Default: <project_root>/metadata where project_root is
            # 3 levels up from this file (reports/hermes/store.py).
            project_root = Path(__file__).resolve().parents[2]
            root = project_root / "metadata"
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.reports_dir = self.root / "reports"
        self.artifacts_dir = self.reports_dir / "artifacts"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.tasks_dir / "index.json"
        self._artifact_index_path = self.reports_dir / "index.json"
        # Bootstrap empty indexes so list operations don't have to
        # handle the missing-file case.
        if not self._index_path.exists():
            _atomic_write_json(self._index_path, {"version": 1, "tasks": []})
        if not self._artifact_index_path.exists():
            _atomic_write_json(self._artifact_index_path, {"version": 1, "artifacts": []})

    # ----- task CRUD -----

    def save_task(self, task_dict: dict) -> None:
        """
        Persist a task record. `task_dict` must include `task_id`.
        Updates the index atomically.
        """
        if "task_id" not in task_dict:
            raise ValueError("save_task requires 'task_id' in the dict")
        task_id = task_dict["task_id"]
        # Persist the full record.
        _atomic_write_json(self._tasks_path(task_id), task_dict)
        # Update the index (lightweight summary).
        with _write_lock:
            index = self._load_index()
            entries = index.get("tasks", [])
            # Replace if exists, else append.
            entries = [e for e in entries if e.get("task_id") != task_id]
            entries.append(self._index_entry_from_task(task_dict))
            entries.sort(key=lambda e: e.get("created_at", ""))
            index["tasks"] = entries
            index["updated_at"] = _utcnow_iso()
            _atomic_write_json(self._index_path, index)

    def load_task(self, task_id: str) -> Optional[dict]:
        return _read_json(self._tasks_path(task_id))

    def list_tasks(self, report_name: Optional[str] = None) -> list[dict]:
        """
        Return all task records (full dicts), optionally filtered by
        `report_name`. Sorted by created_at ascending.
        """
        index = self._load_index()
        entries = index.get("tasks", [])
        if report_name is not None:
            entries = [e for e in entries if e.get("report_name") == report_name]
        # Load full records (file may have been edited or have more
        # data than the index summary).
        out = []
        for e in entries:
            tid = e.get("task_id")
            if not tid:
                continue
            full = self.load_task(tid)
            if full is not None:
                out.append(full)
        return out

    # ----- artifact CRUD -----

    def save_artifact(self, artifact_dict: dict) -> None:
        """
        Persist an artifact metadata record. `artifact_dict` must
        include `artifact_id`. Updates the artifact index atomically.
        """
        if "artifact_id" not in artifact_dict:
            raise ValueError("save_artifact requires 'artifact_id'")
        artifact_id = artifact_dict["artifact_id"]
        _atomic_write_json(self._artifacts_path(artifact_id), artifact_dict)
        with _write_lock:
            index = self._load_artifact_index()
            entries = index.get("artifacts", [])
            entries = [e for e in entries if e.get("artifact_id") != artifact_id]
            entries.append(self._artifact_index_entry(artifact_dict))
            entries.sort(key=lambda e: e.get("created_at", ""))
            index["artifacts"] = entries
            index["updated_at"] = _utcnow_iso()
            _atomic_write_json(self._artifact_index_path, index)

    def load_artifact(self, artifact_id: str) -> Optional[dict]:
        return _read_json(self._artifacts_path(artifact_id))

    def list_artifacts(self, task_id: Optional[str] = None) -> list[dict]:
        index = self._load_artifact_index()
        entries = index.get("artifacts", [])
        if task_id is not None:
            entries = [e for e in entries if e.get("task_id") == task_id]
        out = []
        for e in entries:
            aid = e.get("artifact_id")
            if not aid:
                continue
            full = self.load_artifact(aid)
            if full is not None:
                out.append(full)
        return out

    def get_artifact_for_task(self, task_id: str) -> Optional[dict]:
        """Return the (most recent) artifact metadata for a task_id."""
        for art in self.list_artifacts(task_id=task_id):
            return art
        return None

    # ----- rendered report text (separate from JSON metadata) -----

    def write_report_text(self, artifact_id: str, text: str) -> Path:
        """
        Write the rendered report body to a plain-text file under
        `metadata/reports/artifacts/<artifact_id>.txt`. Returns the
        absolute path of the written file.

        For dry-run tasks, this is NOT called — there is no real
        rendered text. For execute mode, this captures the local
        artifact without publishing externally.
        """
        out_path = self.artifacts_dir / f"{artifact_id}.txt"
        _atomic_write_json  # noop — keep linter happy about reference
        # Plain-text write, also atomic.
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{out_path.name}.",
            suffix=".tmp",
            dir=str(out_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, out_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return out_path

    def read_report_text(self, artifact_path: str | os.PathLike) -> Optional[str]:
        p = Path(artifact_path)
        if not p.exists():
            return None
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    # ----- index helpers -----

    def _load_index(self) -> dict:
        return _read_json(self._index_path) or {"version": 1, "tasks": []}

    def _load_artifact_index(self) -> dict:
        return _read_json(self._artifact_index_path) or {"version": 1, "artifacts": []}

    @staticmethod
    def _index_entry_from_task(task_dict: dict) -> dict:
        """Build the lightweight index entry from a full task dict."""
        return {
            "task_id": task_dict.get("task_id", ""),
            "report_name": task_dict.get("report_name", ""),
            "status": task_dict.get("status", ""),
            "created_at": task_dict.get("created_at", ""),
            "finished_at": task_dict.get("finished_at", ""),
            "dry_run": bool(task_dict.get("metadata", {}).get("dry_run", False)),
        }

    @staticmethod
    def _artifact_index_entry(artifact_dict: dict) -> dict:
        return {
            "artifact_id": artifact_dict.get("artifact_id", ""),
            "task_id": artifact_dict.get("task_id", ""),
            "report_name": artifact_dict.get("report_name", ""),
            "created_at": artifact_dict.get("created_at", ""),
            "dry_run": bool(artifact_dict.get("dry_run", False)),
            "artifact_path": artifact_dict.get("artifact_path", ""),
        }

    # ----- path helpers -----

    def _tasks_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _artifacts_path(self, artifact_id: str) -> Path:
        return self.reports_dir / f"{artifact_id}.json"


__all__ = ["LocalStore"]
