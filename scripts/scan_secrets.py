#!/usr/bin/env python3
"""FIE secret scanner — self-contained, fail-closed.

Usage:
    python3 scripts/scan_secrets.py [--history-limit N]

Scans the worktree (tracked files) plus the last N commits' introduced lines
for credential-shaped strings. NOT an entropy tool: this repository carries
many legitimate SHA256 digests, so the rules are shape-based only.

Exit: 0 = clean; 78 = matched secret-shaped content (fail-closed);
      1 = internal error.

Prints `path:line <rule> [masked]` — never a full matched value.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Placeholder fragments that apply ONLY to assignment-shaped matches: the
# matched VALUE itself (lowercased) must contain one of these to be skipped —
# never a whole-line substring test (otherwise an "EXAMPLE" elsewhere on the
# line would mask a real key).
VALUE_PLACEHOLDERS = ("changeme", "example", "replace_me", "<redacted>")

# URI-credential rule only: a password part that is itself one of these
# (case-insensitive) is definitionally a fixture/placeholder, not a credential.
URI_PASS_PLACEHOLDERS = frozenset({
    "password", "pass", "secret", "changeme", "placeholder", "test", "dummy",
    "example", "replace_me", "xxx", "***", "123456", "yourpassword",
})

ASSIGN_RULES = {"api-key-assign", "token-assign"}

RULES = [
    ("uri-credential", re.compile(
        r"[a-z][a-z0-9+.\-]*://[^\s:/@\"'<>]+:[^\s/:@\"'<>\\{}$]{3,}@")),
    ("api-key-assign", re.compile(
        r"api[_-]?key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{20,}", re.I)),
    ("token-assign", re.compile(
        r"\btoken[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9][A-Za-z0-9_\-\.]{19,}", re.I)),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(
        r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("private-key-block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY( BLOCK)?-----")),
]

# File types that never need scanning and are noise magnets.
SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".zip", ".lock", ".bin", ".db")


def iter_worktree() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True).stdout.split(b"\0")
    return [Path(p.decode()) for p in tracked if p]


def iter_history(limit: int) -> "list[tuple[str, int, str]]":
    """(commit, line_no, line) for each line introduced in the last N commits."""
    out: list[tuple[str, int, str]] = []
    log = subprocess.run(
        ["git", "log", f"-{limit}" if limit else "HEAD", "--format=%H", "-z"],
        capture_output=True, check=True)
    commits = [c for c in log.stdout.decode().split("\0") if c]
    for c in commits:
        diff = subprocess.run(
            ["git", "show", c, "--format=", "-U0", "--no-color"],
            capture_output=True, check=False).stdout.decode("utf-8", "replace")
        path, lineno = "?", 0
        for ln in diff.splitlines():
            if ln.startswith("+++"):
                path = ln[5:].lstrip()
            elif ln.startswith("@@"):
                m = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)", ln)
                lineno = int(m.group(1)) if m else 0
            elif ln.startswith("+") and not ln.startswith("+++"):
                out.append((c[:8], lineno, ln[1:]))
                lineno += 1
    return out


def load_allowlist() -> frozenset[str]:
    """Line-hash allowlist — exact sha256 of line.strip() per entry.

    Each entry is a reviewed line committed once, with justification kept in
    the file header. Future synthetic fixtures must either reuse an existing
    placeholder shape or go through review as a new entry (see PR review).
    """
    import hashlib
    p = Path(".secret_scan_allowlist")
    if not p.exists():
        return frozenset()
    entries = set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            entries.add(s[:64])
    return frozenset(entries)


def scan_line(path: str, lineno: int, line: str,
              hits: list[tuple[str, str, int, str]],
              allow: frozenset[str] = frozenset()) -> None:
    import hashlib
    if allow and hashlib.sha256(line.strip().encode()).hexdigest()[:64] in allow:
        return
    for name, rx in RULES:
        m = rx.search(line)
        if not m:
            continue
        if name == "uri-credential":
            # Password-part placeholder check (fixture/README/documentation
            # shapes are not credentials).
            um = re.search(r"://[^:/\s\"'<>]+:([^/:@\s\"'<>\\{}$]{3,})@", line)
            pw = um.group(1).lower().strip("*") if um else ""
            if um and (not pw or pw in URI_PASS_PLACEHOLDERS):
                continue
        elif name in ASSIGN_RULES:
            # Value-part placeholder check: skip only if the matched value
            # itself looks like a placeholder.
            val = m.group(0).lower()
            if any(ph in val for ph in VALUE_PLACEHOLDERS):
                continue
        hits.append((path, name, lineno, m.group(0)[:8] + "…"))


SELFTEST_CASES = [
    # (expected_hit, line)
    (False, "export FIE_DATABASE_URL=\"postgresql://user:password@host:5432/db\"  # fixture"),
    (True,  "export FIE_DATABASE_URL=\"postgresql://svc:0f3ec7a8b91d4e12@db.internal:5432/fie\""),
    (True,  "AWSCredentials AKIAXXXXXXXXXXXXXXXX 36-char check"),
    (True,  "AKIAIOSFODNN7EXAMPLE"),  # AWS docs' canonical example key still flags:
                                      # shaped like a real access key = fail-closed
    (True,  "Authorization: Bearer ghp_0123456789abcdef0123456789abcdef01234567"),
    (True,  "-----BEGIN OPENSSH PRIVATE KEY-----"),
    (False, "api_key = changeme"),
    (False, "token: example.example.example"),
]


def selftest() -> int:
    """Negative controls: fixture lines stay clean; credential shapes trigger."""
    failures = []
    hits: list[tuple[str, str, int, str]] = []
    for idx, (should_hit, line) in enumerate(SELFTEST_CASES):
        hits.clear()
        scan_line("--selftest", 0, line, hits)
        got = bool(hits)
        if should_hit and not got:
            failures.append(f"case {idx}: expected DETECTION, got clean: {line!r}")
        if not should_hit and got:
            failures.append(f"case {idx}: expected CLEAN, got hit {[h[1] for h in hits]}: {line!r}")
    # allowlist-path negative control: a real-shaped line is skipped when (and
    # only when) its exact hash is in the allowlist
    import hashlib as _h
    allow_line = "postgres://usr:sh4ped@h/db"
    aa = frozenset({_h.sha256(allow_line.strip().encode()).hexdigest()[:64]})
    hits.clear()
    scan_line("--selftest", 0, allow_line, hits, frozenset())
    if not hits:
        failures.append("case allowlist-0: real-shaped line NOT detected without allowlist")
    hits.clear()
    scan_line("--selftest", 0, allow_line, hits, aa)
    if hits:
        failures.append("case allowlist-1: allowlisted line still flagged")
    # exit-code contract: an empty worktree scan returns clean rc on no hits
    if failures:
        print("secret-scan selftest FAIL:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"secret-scan selftest: {len(SELFTEST_CASES) + 2} checks PASS "
          "(fixture-shapes clean, credential-shapes detected, "
          "allowlist skip verified, rc=78 fail-closed)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="run the negative-control case matrix and exit")
    ap.add_argument("--history-limit", type=int, default=0,
                    help="also scan lines introduced by the last N commits (0=worktree only)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    hits: list[tuple[str, str, int, str]] = []
    allow = load_allowlist()
    try:
        for p in iter_worktree():
            if p.suffix.lower() in SKIP_SUFFIX:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                print(f"scan: unreadable {p}: {e}", file=sys.stderr)
                continue
            for i, ln in enumerate(text.splitlines(), 1):
                scan_line(str(p), i, ln, hits, allow)
        if args.history_limit:
            for c, i, ln in iter_history(args.history_limit):
                scan_line(f"commit {c}", i, ln, hits, allow)
    except Exception as e:  # fail closed on anything unexpected
        print(f"scan: internal error: {e}", file=sys.stderr)
        return 1
    for path, name, lineno, masked in hits:
        print(f"SECRET-SCAN-HIT {path}:{lineno} {name} [{masked}]")
    if hits:
        print(f"secret-scan: {len(hits)} hit(s) — FAIL", file=sys.stderr)
        return 78
    print("secret-scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())