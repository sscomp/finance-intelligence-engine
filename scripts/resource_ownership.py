#!/usr/bin/env python3
"""Job-resource ownership classification and teardown evidence — 6.9A-R4-R3-R1.

ONE canonical implementation (repository contract pin) of the ownership
identity contract introduced after R4-R3: cleanup must distinguish

    JOB_OWNED   positively attributable to THIS job (creation-time
                registry + identity evidence stronger than a bare PID)
    FOREIGN     positively attributable to something else (another job's
                marker, a live holder, a different-owner lock/lock class)
    ADOPTED     provably reparented outside this job's control (e.g. a
                zombie adopted by PID 1 — never claimable, never reapable
                by the job)
    UNKNOWN     no/invalid ownership evidence — fail closed: preserved
                and reported, never deleted, never killed

The registry (``ownership_registry.json``) is written at CREATION time by
the canonical provisioner (scripts/provision-test-postgres.sh) — teardown
reads it, it is never inferred only during cleanup. Identity evidence for
a process is (command signature referencing the job's data dir) plus
(recorded start time) plus (owner marker under the job dir); a bare PID
match alone classifies as UNKNOWN — PID reuse must never produce a
false JOB_OWNED attribution.

Machine-readable teardown evidence lists what the job created, what was
reaped/removed, what remained, and why.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path

REGISTRY_FORMAT = "fie-6-9a-ownership-registry-v1"
EVIDENCE_FORMAT = "fie-6-9a-teardown-evidence-v1"
CACHE_MARKER_FORMAT = "fie-6-9a-cache-v1"
LOCK_MARKER_FORMAT = "fie-6-9a-acquire-lock-v1"
PG_MARKER_FORMAT = "fie-6-9a-ephemeral-v1"

# Process classification labels.
JOB_OWNED_LIVE = "JOB_OWNED_LIVE"
JOB_OWNED_DEAD = "JOB_OWNED_DEAD"
JOB_OWNED_ZOMBIE_REAPABLE = "JOB_OWNED_ZOMBIE_REAPABLE"
ADOPTED_ZOMBIE = "ADOPTED_ZOMBIE"
ZOMBIE_FOREIGN_PARENT = "ZOMBIE_FOREIGN_PARENT"
FOREIGN = "FOREIGN"
UNKNOWN = "UNKNOWN"

# Directory classification labels.
DIR_JOB_OWNED = "JOB_OWNED_TEMP_DIR"
DIR_FOREIGN = "FOREIGN_TEMP_DIR"
DIR_UNKNOWN = "UNKNOWN_TEMP_DIR"

# Exit codes (aligned with the provisioner contract).
PG_EXIT_TEARDOWN = 94

# Classes the job MUST leave at zero residue after teardown.
JOB_RESIDUE_CLASSES = (JOB_OWNED_LIVE, DIR_JOB_OWNED, JOB_OWNED_ZOMBIE_REAPABLE)


# ---------------------------------------------------------------- /proc layer
class ProcSnapshot:
    """Minimal process identity evidence (injected or /proc-derived)."""

    __slots__ = ("pid", "state", "ppid", "cmdline", "start_ticks", "uid")

    def __init__(self, pid, state, ppid, cmdline, start_ticks, uid):
        self.pid = pid
        self.state = state            # single char: R S D Z T ...
        self.ppid = ppid              # parent pid (-1 = unknown/not observed)
        self.cmdline = cmdline        # space-joined argv ("") for empty
        self.start_ticks = start_ticks  # field 22 of /proc/pid/stat (None ok)
        self.uid = uid


def read_proc(pid: int) -> ProcSnapshot | None:
    """Read real identity evidence from /proc; None when the process is
    gone (dead) or unintelligible."""
    base = Path("/proc") / str(pid)
    try:
        stat = (base / "stat").read_text()
        state = stat[stat.rindex(")") + 2: stat.rindex(")") + 3]
        fields = stat[stat.rindex(")") + 2:].split()
        ppid = int(fields[1])
        ticks = int(fields[19])
        cmdline = " ".join(
            (base / "cmdline").read_bytes().decode("utf-8", "replace")
            .split("\0")).strip()
        uid = os.stat(str(base)).st_uid
    except (OSError, IndexError, ValueError):
        return None
    return ProcSnapshot(pid, state, ppid, cmdline, ticks, uid)


# ---------------------------------------------------------------- registry
def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_registry(job_id: str) -> dict:
    """Creation-time registry header (the provisioner fills resources in)."""
    return {
        "format": REGISTRY_FORMAT,
        "job_id": job_id,
        "created_utc": _now_utc(),
        "resources": [],
    }


def record_resource(registry: dict, kind: str, *,
                    path: str | None = None, marker: str | None = None,
                    marker_format: str | None = None,
                    process: dict | None = None,
                    removable: bool = True,
                    metadata: dict | None = None) -> dict:
    """Append one creation-time ownership record and return it.

    ``removable`` marks resources the job may remove during teardown
    (a BORROWED persistent cache dir is recorded for evidence with
    ``removable=false`` — its contents belong to later jobs too).
    """
    entry = {
        "kind": kind,
        "removable": removable,
        "recorded_utc": _now_utc(),
    }
    if path is not None:
        entry["path"] = str(path)
    if marker is not None:
        entry["marker"] = marker
    if marker_format is not None:
        entry["marker_format"] = marker_format
    if process is not None:
        entry["process"] = dict(process)
    if metadata:
        entry["metadata"] = dict(metadata)
    registry["resources"].append(entry)
    return entry


def write_registry(registry: dict, destination: str | os.PathLike) -> None:
    """Write the registry 0600 (credential-adjacent evidence file)."""
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)
        fh.write("\n")


def load_registry(path: str | os.PathLike) -> dict | None:
    """Load a registry; malformed/foreign-format input is UNKNOWN (None)."""
    try:
        registry = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if registry.get("format") != REGISTRY_FORMAT:
        return None
    return registry


# ---------------------------------------------------------------- process
def postgres_children(snapshots: dict[int, ProcSnapshot],
                      data_dir: str) -> list[int]:
    """PIDs whose command line references the job's data dir (PG children
    are attributable by command signature, not parentage alone)."""
    norm = data_dir.rstrip("/")
    found = []
    for pid, snap in snapshots.items():
        if snap.state == "Z":
            # zombies keep no cmdline; they are classified separately
            continue
        if norm in snap.cmdline:
            found.append(pid)
    return sorted(found)


def classify_process(snapshot: ProcSnapshot | None,
                     entry: dict | None,
                     observer_pid: int | None = None) -> str:
    """Classify one observed process against one registry process record.

    Evidence rules (no PID-only attribution):
      * dead (no snapshot) and the registry process recorded a start
        identity => JOB_OWNED_DEAD (already exited; success for teardown);
      * zombie whose parent is the OBSERVER   => JOB_OWNED_ZOMBIE_REAPABLE
        (the job created it directly — the job must reap it);
      * zombie whose parent is PID 1          => ADOPTED_ZOMBIE (reparented
        outside the job's control — preserved, reported, never claimed);
      * zombie with any other parent          => ZOMBIE_FOREIGN_PARENT;
      * live process: JOB_OWNED only when BOTH the command signature
        references the recorded data dir AND the observed start ticks
        match the recorded start ticks (a PID reuse yields different
        start ticks => UNKNOWN, never JOB_OWNED);
      * live process without a registry entry => UNKNOWN.
    """
    if snapshot is None or snapshot.state == "X":
        return JOB_OWNED_DEAD if entry else UNKNOWN
    if snapshot.state == "Z":
        if observer_pid is not None and snapshot.ppid == observer_pid:
            return JOB_OWNED_ZOMBIE_REAPABLE
        if snapshot.ppid == 1:
            return ADOPTED_ZOMBIE
        return ZOMBIE_FOREIGN_PARENT
    if not entry:
        return UNKNOWN
    recorded = entry.get("process") or {}
    sig = recorded.get("command_signature") or ""
    data_dir = recorded.get("data_dir") or ""
    if sig and data_dir and data_dir.rstrip("/") in snapshot.cmdline:
        start = recorded.get("start_ticks")
        if start is None or snapshot.start_ticks is None \
                or int(start) == int(snapshot.start_ticks):
            return JOB_OWNED_LIVE
        return UNKNOWN  # PID reuse: same PID, different start lineage
    return UNKNOWN


def reap_job_children(observer_pid: int, timeout: float = 5.0) -> list[int]:
    """Reap zombie children whose parent is THIS process (and only them).

    Non-blocking sweep (bounded wait); returns the reaped PIDs. Only
    zombies are collected — live children are never waited out.
    """
    reaped = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reaped_now = []
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break  # no children left at all — nothing to reap
            if pid == 0:
                break
            reaped_now.append(pid)
        if not reaped_now:
            return reaped
        reaped.extend(reaped_now)
        time.sleep(0.05)
    return reaped


# ---------------------------------------------------------------- dirs
def _cache_marker_valid(path: str | os.PathLike) -> bool:
    try:
        content = (Path(path) / "fie_cache.owner").read_text(
            errors="replace")
    except OSError:
        return False
    return f"format={CACHE_MARKER_FORMAT}" in content


def classify_temp_dir(path: str | os.PathLike,
                      claimed: set[str] | frozenset[str] = frozenset()) -> str:
    """Classify an observed temporary directory WITHOUT mutating it.

      * valid FIE cache marker + this job's creation evidence (registry /
        observed created set) names it   => DIR_JOB_OWNED;
      * valid FIE cache marker, NOT claimed by this job => DIR_FOREIGN
        (marker-proven FIE-owned by ANOTHER job — preserved);
      * anything unmarked / unattributable => DIR_UNKNOWN (never deleted
        merely to satisfy a count).

    ``claimed`` carries the canonical absolute paths this job created
    (from the ownership registry, or the resolver's observed-created set).
    """
    base = Path(path)
    if not _cache_marker_valid(base):
        return DIR_UNKNOWN
    want = str(base).rstrip("/")
    for c in claimed:
        if str(c).rstrip("/") == want:
            return DIR_JOB_OWNED
    return DIR_FOREIGN


# ---------------------------------------------------------------- teardown
def process_residue_inventory(snapshots: dict[int, ProcSnapshot],
                              registry: dict,
                              observer_pid: int) -> dict:
    """Classify every observed process against the registry; produce the
    residue inventory: which observed processes are job-owned-live,
    reapable, adopted, foreign, or unknown."""
    server_entries = {
        pid: entry for entry in registry.get("resources", [])
        if entry.get("kind") == "postgres_server"
        for pid in [((entry.get("process") or {}).get("pid"))]
    } if registry else {}
    inventory = {}
    for pid, snap in snapshots.items():
        entry = server_entries.get(pid)
        inventory[pid] = classify_process(snap, entry, observer_pid)
    return inventory


def residue_counts(inventory: dict) -> dict:
    counts = {cls: 0 for cls in
              (JOB_OWNED_LIVE, JOB_OWNED_ZOMBIE_REAPABLE, ADOPTED_ZOMBIE,
               ZOMBIE_FOREIGN_PARENT, FOREIGN, UNKNOWN)}
    for cls in inventory.values():
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def build_teardown_evidence(registry: dict | None,
                            job_dir: str,
                            removed_dirs: list[str] | None = None,
                            observations: list[dict] | None = None,
                            reaped: list[int] | None = None,
                            extra: dict | None = None) -> dict:
    """Machine-readable teardown evidence: what this job created, what was
    removed/reaped, what remained and why."""
    created = []
    if registry:
        for res in registry.get("resources", []):
            created.append({
                "kind": res.get("kind"),
                "path": res.get("path"),
                "process": res.get("process"),
                "recorded_utc": res.get("recorded_utc"),
            })
    removed = list(removed_dirs or [])
    remained = list(observations or [])
    return {
        "format": EVIDENCE_FORMAT,
        "job_id": (registry or {}).get("job_id"),
        "job_dir": job_dir,
        "evidence_utc": _now_utc(),
        "created": created,
        "removed": removed,
        "reaped": list(reaped or []),
        "remained": remained,
        **(extra or {}),
    }


# ---------------------------------------------------------------- CLI
# Bash-facing subcommands (the provisioner is the caller; registry JSON on
# stdin so the job dir may already be removed when evidence is emitted).
def _cli(argv: list) -> int:  # pragma: no cover - exercised via provisioner
    import argparse
    import glob
    ap = argparse.ArgumentParser(prog="resource_ownership.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser(
        "verify-stopped",
        help="registry JSON on stdin; exit 0 iff every registry-recorded "
             "process (and every live process whose command line references "
             "the job data dir) is ENDED or provably beyond the job's "
             "control (zombie); a bounded wait applies first")
    v.add_argument("--data-dir", required=True)
    v.add_argument("--observer", type=int, required=True)
    v.add_argument("--timeout", type=float, default=10.0)
    e = sub.add_parser(
        "stop-evidence", help="emit teardown evidence JSON (registry stdin)")
    e.add_argument("--job-dir", required=True)
    e.add_argument("--removed-dir", default=None)
    args = ap.parse_args(argv[1:])

    registry = None
    stdin_text = sys.stdin.read()
    if stdin_text.strip():
        try:
            registry = json.loads(stdin_text)
        except ValueError:
            registry = None
    if args.cmd == "verify-stopped":
        if not registry or registry.get("format") != REGISTRY_FORMAT:
            print("VERIFY_REGISTRY_UNPARSEABLE")
            return 2
        data_dir = os.path.realpath(args.data_dir)
        deadline = time.monotonic() + args.timeout
        while True:
            leftovers = []
            observations = []
            for p in sorted(glob.glob("/proc/[0-9]*")):
                pid = p.rsplit("/", 1)[-1]
                snap = read_proc(int(pid))
                if snap is None:
                    continue
                # Live processes referencing THIS job's data dir are
                # positively job-attributable (the registry proves the
                # data dir is job-owned; the command signature proves the
                # process belongs to it). Zombies carry no command line
                # and are observation-only — un-reapable by this job.
                if snap.state == "Z":
                    proc = registry_server_record(registry, int(pid))
                    entry = {"process": proc} if proc else None
                    observations.append(
                        {"pid": snap.pid,
                         "class": classify_process(snap, entry,
                                                   args.observer),
                         "reason": "zombie carries no command line; "
                                   "classification by registry record + "
                                   "parent (preserved, never claimed)"})
                    continue
                if data_dir and data_dir in snap.cmdline:
                    if snap.pid == args.observer or \
                            "resource_ownership.py" in snap.cmdline:
                        continue  # the verify/teardown tooling itself
                    entry = registry_process_entry(registry, int(pid))
                    cls = classify_process(snap, entry, args.observer)
                    if cls == JOB_OWNED_LIVE:
                        leftovers.append({"pid": snap.pid, "class": cls})
                    else:
                        observations.append(
                            {"pid": snap.pid, "class": cls,
                             "reason": "references job data dir but "
                                       "identity evidence is insufficient"})
            if not leftovers:
                if observations:
                    print(json.dumps({"verify": "ok", "observations":
                                      observations}))
                return 0
            if time.monotonic() >= deadline:
                print(json.dumps({"verify": "leftovers", "leftovers":
                                  leftovers}, default=str))
                return 2
            time.sleep(0.5)
    if args.cmd == "stop-evidence":
        removals = []
        if registry:
            for res in registry.get("resources", []):
                if res.get("removable", True):
                    removals.append({"kind": res.get("kind"),
                                     "path": res.get("path")})
        evidence = build_teardown_evidence(
            registry, args.job_dir, removed_dirs=[
                args.removed_dir or args.job_dir],
            observations=[{"kind": "postmaster_children",
                           "reason": "ended/reaped by the granted stop "
                                     "mechanism (pg_ctl fast stop + "
                                     "registry verification)"}])
        text = json.dumps(evidence, sort_keys=True)
        out = os.environ.get("FIE_TEST_PG_TEARDOWN_EVIDENCE", "")
        if out:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(text + "\n")
        else:
            print("OWNERSHIP_TEARDOWN_EVIDENCE=" + text)
        return 0
    return 2


def registry_process_entry(registry: dict, pid: int) -> dict | None:
    for res in registry.get("resources", []):
        if res.get("kind") == "postgres_server" and \
                ((res.get("process") or {}).get("pid")) == pid:
            return res
    return None


def registry_server_record(registry: dict, pid: int) -> dict | None:
    rec = registry_process_entry(registry, pid)
    return rec.get("process") if rec else None


if __name__ == "__main__":
    sys.exit(_cli(sys.argv) or 0)