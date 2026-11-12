#!/usr/bin/env python3
"""
Shared TWSE fetcher — path-level cooldown + security-page detection.

Implements the validated cures of docs/operations/TWSE_RATELIMIT_LOG.md §2:

  - §1.2 security-page detection: the Chinese / English "FOR SECURITY
    REASONS" page marks a throttled response. A bare 307 (or any redirect /
    non-JSON body without the security signature) is NOT treated as a
    throttle; it becomes a plain fetch miss (None) and must never be
    retried in a storm.
  - §2.1 merged requests first: callers should prefer MI_INDEX-type whole
    market endpoints; this module governs whatever single-day path they
    do request.
  - §2.2 standard reaction: on security-page detection STOP the remaining
    requests for that path (raise FetchCooldownActive to the caller),
    record the cooldown in cross-process state, allow one controlled probe
    after the cooldown expires, and never extend the cooldown from a failed
    probe (the "30+ minutes" release time is a lower bound, not a promise).

Design contracts:

  - Every blocking action is injectable for tests: `opener` (any function
    returning (status, body_bytes)), `now`, `sleeper`. Tests run against a
    local fake endpoint and never touch twse.com.tw.
  - State is a small JSON file guarded by flock, so cooldowns survive
    across processes (cron pipeline, replays) on this Linux/macOS host.
  - The module is stdlib-only and never touches the network at import time.

Integration status (§3 of the governance file): institutional.py and
phase3/backfill.py T86 loops are wired; the instances/t2 ingestion loop
keeps its own reliability contract and is a separate follow-up.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

# --- Governance core strings (docs/operations/TWSE_RATELIMIT_LOG.md §1.2) ---

SECURITY_PAGE_MARKERS = (
    # Core substrings only: punctuation variants exist between events.
    "安全性考量，您所執行的頁面無法呈現",
    "FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED",
)

# --- Tunables (defaults follow §2.2; callers may override for tests) ---

DEFAULT_COOLDOWN_S = 1800            # at least 30 minutes after a hit
DEFAULT_PROBE_INTERVAL_S = 1800      # pause between failed probes
DEFAULT_REQUEST_INTERVAL_S = 0.3     # per-path min gap (existing policy sleep)

# --- Errors callers must handle (§2.2: stop the loop, do not storm) ---


class TwseFetcherError(Exception):
    """Base class for fetcher contract errors."""


class SecurityPageError(TwseFetcherError):
    """The response body carried the TWSE security-page signature."""


class FetchCooldownActive(TwseFetcherError):
    """The path is cooling down; remaining requests for it must stop.

    Attributes:
        path: the governed TWSE path key.
        retry_after: seconds remaining until the next controlled probe.
    """

    def __init__(self, path: str, retry_after: float):
        self.path = path
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(
            f"TWSE path cooling down: {path} (retry after ~{int(self.retry_after)}s); "
            "do not retry in a loop — see docs/operations/TWSE_RATELIMIT_LOG.md §2.2"
        )


# --- Security-page detector (shared, exported for reuse) ---


def check_security_page(body: bytes) -> bool:
    """True when the body carries the TWSE security-page signature.

    Decoding is lossy-safe (utf-8 with replacement) and the English marker
    is case-folded so punctuation-casing variants still match. Status codes
    are deliberately ignored here: 200/307/302 with the signature are all
    throttled pages, while any other body is NOT.
    """
    if not body:
        return False
    text = body.decode("utf-8", errors="replace")
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in SECURITY_PAGE_MARKERS)


# --- Cross-process state (flock-guarded JSON; gitignored runtime file) ---

DEFAULT_STATE_PATH = Path(__file__).resolve().parent / "state" / "twse_fetcher_state.json"

STATE_SCHEMA_VERSION = 1


def _empty_state() -> dict:
    return {"schema": STATE_SCHEMA_VERSION, "paths": {}}


def load_state(state_path: Path = DEFAULT_STATE_PATH) -> dict:
    path = Path(state_path)
    if not path.exists():
        return _empty_state()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "r+", encoding="utf-8") as fh:
        _flock(fh)
        try:
            fh.seek(0)
            raw = fh.read()
        finally:
            _funlock(fh)
    if not raw.strip():
        return _empty_state()
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_state()
    if state.get("schema") != STATE_SCHEMA_VERSION:
        return _empty_state()
    return state


def save_state(state: dict, state_path: Path = DEFAULT_STATE_PATH) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        _flock(fh)
        try:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            _funlock(fh)
    os.replace(tmp, path)


def _flock(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        pass  # single-writer fallback; atomic replace above still applies


def _funlock(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _path_entry(state: dict, path: str) -> dict:
    return state.setdefault("paths", {}).setdefault(path, {})


# --- Production opener (the only place that touches the real network) ---


def _production_opener(url: str, timeout: float) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            return resp.status, resp.read()
        finally:
            resp.close()
    except urllib.error.HTTPError as err:  # 3xx/4xx/5xx bodies still matter
        try:
            body = err.read()
        finally:
            err.close()
        return err.code, body


# --- The governed fetch ---


def fetch_json(
    url: str,
    *,
    opener: Optional[Callable[[str, float], Tuple[int, bytes]]] = None,
    timeout: float = 15.0,
    now: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    state_path: Path = DEFAULT_STATE_PATH,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    probe_interval_s: float = DEFAULT_PROBE_INTERVAL_S,
    request_interval_s: float = DEFAULT_REQUEST_INTERVAL_S,
) -> Optional[dict]:
    """Fetch a TWSE JSON endpoint under the §2 governance contract.

    Returns:
        The parsed JSON object when `stat == "OK"` (existing contract with
        institutional.py), otherwise None for schema/HTTP misses.

    Raises:
        FetchCooldownActive: the path is cooling down — callers must stop
        the remaining requests for this path this round (§2.2). Also raised
        *without any network call* while a cooldown is unexpired.
        SecurityPageError: surfaced after the cooldown was recorded, for
        callers that want the immediate cause.
    """
    open_fn = opener or _production_opener
    key = _policy_key(url)
    state = load_state(state_path)
    stamp = now()

    entry = _path_entry(state, key)
    blocked_until = float(entry.get("blocked_until", 0.0) or 0.0)
    if blocked_until > stamp:
        raise FetchCooldownActive(key, blocked_until - stamp)

    # Per-path inter-request pacing (§2.4: batch inner throttle; the legacy
    # 0.3 s sleep is kept as the floor, not as a safety contract).
    last_seen = float(entry.get("last_request_ts", 0.0) or 0.0)
    gap = stamp - last_seen
    if gap < request_interval_s:
        sleeper(request_interval_s - gap)
        stamp = now()

    status, body = open_fn(url, timeout)
    stamp = now()

    # Fresh state after I/O: any real request updates the pacing clock, so
    # plain misses are still bookkept (but never mistaken for throttles).
    state = load_state(state_path)
    entry = _path_entry(state, key)
    entry["last_request_ts"] = stamp

    if check_security_page(body):
        # §2.2 steps 1-2: stop, record, then leave the path alone.
        entry["blocked_until"] = stamp + cooldown_s
        entry["last_block_ts"] = stamp
        entry["last_block_status"] = int(status)
        entry["last_probe_ts"] = 0.0
        save_state(state, state_path)
        raise SecurityPageError(f"security page on {key} (status {status}); cooldown {int(cooldown_s)}s recorded")

    if blocked_until:
        # Unexpired blocked_until was already handled above; reaching here
        # after a past expiry means this call was the controlled probe.
        probe_ok = False
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
            probe_ok = isinstance(parsed, dict) and parsed.get("stat") == "OK"
        except json.JSONDecodeError:
            probe_ok = False
        entry["last_probe_ts"] = stamp
        if probe_ok:
            entry.pop("blocked_until", None)
            entry.pop("last_block_status", None)
        else:
            # §2.2/§4: a failed probe must NOT extend the block; it only
            # schedules the next controlled probe.
            entry["blocked_until"] = max(entry.get("blocked_until", 0.0), stamp + probe_interval_s)
            save_state(state, state_path)
            raise FetchCooldownActive(key, probe_interval_s)

    save_state(state, state_path)

    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        # 307/302/HTML without the security signature: a data miss, never a
        # throttle signal (§1.2) — and never an immediate retry.
        return None
    if not isinstance(parsed, dict) or parsed.get("stat") != "OK" or "data" not in parsed:
        return None
    return parsed


def _policy_key(url: str) -> str:
    """Path-only policy key: the path lock is per endpoint, not per query."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"
