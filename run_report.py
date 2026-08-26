#!/usr/bin/env python3
"""
run_report.py — CLI entrypoint for the Phase 2B report framework.

Two operating modes (per task brief):
  1. LIST mode    — `python3 run_report.py list`
                     Show every registered report (name, schedule,
                     version, entrypoint). NEVER executes a fetcher.

  2. DISPATCH mode — `python3 run_report.py dispatch <name>`
                     Resolves the manifest, verifies the entrypoint
                     is importable, records a "completed" task in
                     the in-memory dispatcher + the local store,
                     and prints the task summary. By default this
                     is DRY-RUN (no fetch/analyze/render).
                     Pass `--execute` to actually run the report
                     locally — the rendered text is written to the
                     artifact directory; nothing is published
                     externally.

  3. STATUS mode  — `python3 run_report.py status <task_id>`
                     Print the dict form of a task. Reads from the
                     in-memory dict first, then the local store
                     (so cross-process queries work).

  4. RERUN mode   — `python3 run_report.py rerun <task_id>`
                     Re-dispatch the same report. Default is
                     dry-run; `--execute` actually runs it.

  5. CANCEL mode  — `python3 run_report.py cancel <task_id>`
                     Mark a non-terminal task as cancelled.

  6. ARTIFACT mode — `python3 run_report.py artifact <task_id>`
                     Show the artifact metadata for a task. With
                     `--text` flag, also print the rendered report
                     body to stdout (Telegram-bound output is
                     explicitly NOT triggered).

The CLI never publishes to Telegram. The CLI never writes to the
SQLite macro_history.db. The CLI never edits cron jobs. The CLI
never modifies the production .py files.

Why a single file (vs. an `argparse` subcommand pattern split into
many modules): the task brief says "new CLI entrypoint (e.g.
hermes.py or run_report.py)" — singular. Keeping it as one file
matches the brief and keeps the surface easy to audit.

Usage examples:

    python3 run_report.py list
    python3 run_report.py dispatch macro_daily
    python3 run_report.py dispatch macro_daily --execute
    python3 run_report.py status <task_id>
    python3 run_report.py rerun <task_id> --execute
    python3 run_report.py cancel <task_id>
    python3 run_report.py artifact <task_id>
    python3 run_report.py artifact <task_id> --text

If PyYAML is not installed, .yaml manifests are silently skipped and
only .json manifests are loaded. The default config dir is
`/home/ubuntu/macro-report/config/reports`, but `--config-dir` can
override for testing. The default metadata dir is
`/home/ubuntu/macro-report/metadata`, overridable via `--metadata-dir`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from reports.hermes.dispatcher import Dispatcher
from reports.hermes.store import LocalStore
from reports.registry import Registry


# Path constants. These default to the production locations but can
# be overridden for tests via CLI flags.
DEFAULT_CONFIG_DIR = "/home/ubuntu/macro-report/config/reports"
# Default metadata dir is the empty string, which means "do NOT
# persist task / artifact metadata". Operators who want persistence
# (e.g. for ad-hoc inspection after a manual run) should pass
# --metadata-dir /home/ubuntu/macro-report/metadata explicitly.
#
# Why the default is no-persist: the CLI's primary use is dry-run
# validation and ad-hoc dispatch. Most invocations do NOT need a
# persistent audit trail — and accidentally writing to the
# production metadata tree from a test or a misclick would be a
# silent side-effect that would confuse future audits. Requiring
# --metadata-dir to be explicit keeps the persistence opt-in.
DEFAULT_METADATA_DIR = ""


def _build_dispatcher(config_dir: str, metadata_dir: Optional[str] = None) -> Dispatcher:
    """
    Build a Dispatcher wired up with a LocalStore. The metadata dir
    defaults to no-persistence (empty string → store=None). Pass
    an explicit path to enable persistence.

    If metadata_dir is the empty string, no store is attached (the
    CLI is running in pure in-memory mode — useful for ad-hoc
    validation against an unknown config).
    """
    registry = Registry.from_directory(config_dir)
    if metadata_dir is None or metadata_dir == "":
        return Dispatcher(registry=registry)
    store = LocalStore(root=metadata_dir) if metadata_dir else LocalStore()
    return Dispatcher(registry=registry, store=store)


def _print_table(rows: list[dict], columns: list[str]) -> None:
    """
    Minimal column-aligned printer. No external deps (no tabulate,
    no rich) — this is stdlib only so the CLI works whether or not
    the user has the macro-venv extras installed.
    """
    widths = {
        c: max(len(c), *(len(str(r.get(c, ""))) for r in rows))
        for c in columns
    }
    sep = "  "
    header = sep.join(c.ljust(widths[c]) for c in columns)
    print(header)
    print(sep.join("-" * widths[c] for c in columns))
    for r in rows:
        print(sep.join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def cmd_list(args: argparse.Namespace) -> int:
    dispatcher = _build_dispatcher(args.config_dir, args.metadata_dir)
    registry = dispatcher.registry
    names = registry.list_names()
    if not names:
        print(f"No reports found in {args.config_dir}.")
        return 0
    print(f"Registered reports ({len(names)}):")
    print()
    rows = []
    for name in names:
        manifest = registry.get_manifest(name)
        rows.append({
            "name": name,
            "display_name": manifest.get("display_name", ""),
            "schedule": manifest.get("schedule", ""),
            "version": manifest.get("version", ""),
            "entrypoint": manifest.get("entrypoint", ""),
        })
    _print_table(
        rows,
        columns=["name", "display_name", "schedule", "version", "entrypoint"],
    )
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    try:
        dispatcher = _build_dispatcher(args.config_dir, args.metadata_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to build dispatcher: {exc}", file=sys.stderr)
        return 1
    dry_run = args.dry_run  # default True for safety
    try:
        task = dispatcher.dispatch(args.name, dry_run=dry_run)
    except KeyError as exc:
        print(f"Dispatch failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(task.to_dict(), ensure_ascii=False, indent=2))
    if task.status == "failed":
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    dispatcher = _build_dispatcher(args.config_dir, args.metadata_dir)
    task = dispatcher.status(args.task_id)
    if task is None:
        print(f"No task with id {args.task_id}", file=sys.stderr)
        return 1
    print(json.dumps(task.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_rerun(args: argparse.Namespace) -> int:
    dispatcher = _build_dispatcher(args.config_dir, args.metadata_dir)
    original = dispatcher.status(args.task_id)
    if original is None:
        print(f"No task with id {args.task_id}", file=sys.stderr)
        return 1
    # Pass `rerun_execute` in metadata to force execute mode when
    # the user asked for it. The Dispatcher defaults to dry-run
    # otherwise.
    if args.execute:
        task = dispatcher.dispatch(
            original.report_name,
            metadata={"rerun_of": args.task_id, "rerun_execute": True},
            dry_run=False,
        )
    else:
        task = dispatcher.dispatch(
            original.report_name,
            metadata={"rerun_of": args.task_id},
            dry_run=True,
        )
    print(json.dumps(task.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    dispatcher = _build_dispatcher(args.config_dir, args.metadata_dir)
    try:
        task = dispatcher.cancel(args.task_id)
    except KeyError as exc:
        print(f"Cancel failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Cancel failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(task.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_artifact(args: argparse.Namespace) -> int:
    """
    Show the artifact metadata for a task. With --text, also print
    the rendered report body to stdout.

    Note: printing the text to stdout is a local output, NOT a
    Telegram publish. The cron agent's stdout→Telegram contract
    is NOT in effect for this CLI invocation — `run_report.py`
    is invoked directly by the operator or by a separate test,
    not by the cron agent.
    """
    dispatcher = _build_dispatcher(args.config_dir, args.metadata_dir)
    art = dispatcher.get_artifact(args.task_id)
    if art is None:
        print(f"No artifact for task id {args.task_id}", file=sys.stderr)
        return 1
    print(json.dumps(art, ensure_ascii=False, indent=2))
    if args.text:
        text = dispatcher.get_artifact_text(art)
        if text is None:
            print(
                f"\n[artifact text unavailable: dry_run={art.get('dry_run')}, "
                f"path={art.get('artifact_path') or '(empty)'}]",
                file=sys.stderr,
            )
            return 1
        print("\n----- BEGIN ARTIFACT TEXT -----")
        print(text)
        print("----- END ARTIFACT TEXT -----")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """
    List all known tasks (in-memory + persisted). For Step 4B
    observability. Optional --report filter.
    """
    dispatcher = _build_dispatcher(args.config_dir, args.metadata_dir)
    tasks = dispatcher.list_tasks(report_name=args.report)
    if not tasks:
        print("No tasks found.")
        return 0
    print(f"Tasks ({len(tasks)}):")
    print()
    rows = []
    for t in tasks:
        rows.append({
            "task_id": t.task_id,
            "report_name": t.report_name,
            "status": t.status,
            "dry_run": str(t.metadata.get("dry_run", False)),
            "created_at": t.created_at,
            "finished_at": t.finished_at,
        })
    _print_table(
        rows,
        columns=["task_id", "report_name", "status", "dry_run", "created_at", "finished_at"],
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_report",
        description=(
            "Phase 2B report framework CLI. By default this CLI "
            "operates in dry-run / list mode and does NOT execute "
            "production reports. Use --execute on dispatch/rerun "
            "to run a report locally (still no Telegram publish)."
        ),
    )
    parser.add_argument(
        "--config-dir",
        default=DEFAULT_CONFIG_DIR,
        help=f"Manifest directory (default: {DEFAULT_CONFIG_DIR})",
    )
    parser.add_argument(
        "--metadata-dir",
        default=DEFAULT_METADATA_DIR,
        help=(
            f"Local metadata root for task + artifact JSON. "
            f"Pass empty string to disable persistence "
            f"(default: {DEFAULT_METADATA_DIR})"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    sub.add_parser(
        "list",
        help="List all registered reports (never executes anything).",
    )

    # tasks
    p_tasks = sub.add_parser(
        "tasks",
        help="List known tasks (in-memory + persisted).",
    )
    p_tasks.add_argument(
        "--report",
        default=None,
        help="Filter tasks to a single report name.",
    )

    # dispatch
    p_dispatch = sub.add_parser(
        "dispatch",
        help="Dispatch a report. Defaults to dry-run (safe).",
    )
    p_dispatch.add_argument("name", help="Report name (e.g. macro_daily)")
    p_dispatch.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Default. Do not actually run fetch/analyze/render.",
    )
    p_dispatch.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help=(
            "DANGER: actually execute the report locally. The "
            "framework will call fetch/analyze/render and write "
            "a local artifact. Does NOT publish to Telegram. "
            "The existing cron schedule is still the recommended "
            "production trigger."
        ),
    )

    # status
    p_status = sub.add_parser("status", help="Show a task's status.")
    p_status.add_argument("task_id")

    # rerun
    p_rerun = sub.add_parser("rerun", help="Re-dispatch a previous task.")
    p_rerun.add_argument("task_id")
    p_rerun.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="DANGER: actually re-execute (default is dry-run).",
    )

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a non-terminal task.")
    p_cancel.add_argument("task_id")

    # artifact
    p_artifact = sub.add_parser(
        "artifact",
        help="Show artifact metadata for a task. Use --text to also print body.",
    )
    p_artifact.add_argument("task_id")
    p_artifact.add_argument(
        "--text",
        action="store_true",
        default=False,
        help="Also print the rendered report body to stdout (local only).",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "list": cmd_list,
        "dispatch": cmd_dispatch,
        "status": cmd_status,
        "rerun": cmd_rerun,
        "cancel": cmd_cancel,
        "artifact": cmd_artifact,
        "tasks": cmd_tasks,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
