"""Automated PII/secret prevention gate (Phase 6.7A-R1; hardened in R3).

Scans **tracked repository content** (``git ls-files``) for prohibited
identifier/secret shapes and fails with redacted, path-anchored output
— the raw matched value is never printed. Purpose and resolution
policy: ADR-016's security exception clause applies to *content*
integrity; this gate is the prevention half of the OI-07a remediation
(Phase 6.7A-R1), hardened against the four R2 acceptance bypass
families.

Design constraints (work order §6):

* deterministic, offline, stdlib-only, no network/API/production data;
* content-keyed (renaming/moving a file does not evade it);
* narrow, reviewable allowlist (empty by default);
* detection is **content-oriented**: key/value prohibitions use a
  bounded multi-line window, not physical line layout (DEFECT P1:
  ``"chat_id"`` and its numeric value split across lines used to
  evade same-line matching).

R3 hardening semantics (each replaces an R2 bypass):

* **P2 — exact reserve boundary.** A reserved example domain exempts a
  matched email token only when the token's *whole domain* is the
  reserved name or a subdomain of it. A host that merely *contains* a
  reserved name to the left of extra labels (e.g. a reserved name
  glued in front of an unresolvable attacker-style suffix) is NOT
  exempt. Substring tests such as ``"example.com" in value`` are not
  used. Case-insensitive; a sole leading-dot form is normalized away.
* **P3 — no attacker-controlled safety prefix.** Synthetic/example
  status comes only from the RFC-2606/6761 reserved names, exact
  service no-reply local parts, and the repository's own fragment-
  assembled fixtures — never from a generic prefix such as
  ``synthetic-``/``placeholder-``/``test-`` prepended to an arbitrary
  value. A prefix cannot make a prohibited token trusted.
* **P4 — token-scoped URI handling.** URIs are parsed; only an email-
  shaped token that lies wholly inside a parsed URI's *authority
  region* (``scheme://[userinfo@]host[:port]``, incl. a bounded
  insignificant blank run after the scheme, which the DSN test fixture
  uses) is treated as credential material governed by secret policy
  instead of contact policy. The mere presence of ``://`` on a line
  does **not** suppress detection of unrelated matches on that line.
* Multi-line key/value pass: a chat-id-shaped key and a 6–13 digit
  value separated by a bounded whitespace run (≤80 characters,
  including newlines) and optional quotes/colon-or-equals/colon-like
  separators is flagged, whatever line the value sits on.

R5 fail-closed scan-completeness contract (defect R4-SEC-01):

> **SCAN COMPLETE + ZERO UNAPPROVED PII = PASS**
> **SCAN INCOMPLETE / READ ERROR / CLASSIFICATION ERROR = FAIL CLOSED**

An inability to *completely* scan an in-scope file was previously
collapsed into the same empty-findings result as a clean scan
(``collect_findings`` used ``except OSError: continue``), so an
enumerated-but-unreadable file was accepted as clean. This module now
distinguishes three explicit scan states:

* ``CLEAN`` — every in-scope file scanned, no unapproved match;
* ``FINDINGS_PRESENT`` — scanned completely, unapproved match(es);
* ``SCAN_INCOMPLETE`` — at least one in-scope file could not be
  conclusively scanned (read/open ``OSError``, strict-UTF-8 decode
  failure, or an internal classification error); the gate **fails
  closed**. An empty findings collection is NOT proof of success.

* **Decode contract (no error-driven silence).** Files are decoded
  strictly as UTF-8; a decode failure is a scan-completeness failure
  (``PII_SCAN_DECODE_ERROR``), not a silent content-masking skip.
  There is deliberately NO errors-``replace`` fallback and no
  extension-based binary exclusion: any in-scope file whose bytes
  this contract cannot decode fails the gate deterministically. The
  tracked tree is required to be pure UTF-8 test text.
* **Reason codes** (safe, machine-readable; no file contents or
  matched values are ever dumped):
  ``PII_SCAN_READ_ERROR`` / ``PII_SCAN_DECODE_ERROR`` /
  ``PII_SCAN_INTERNAL_ERROR``; overall incomplete state
  ``PII_SCAN_INCOMPLETE`` with ``gate_result=FAIL``.

R7 diagnostic-path redaction contract (defect R6-SEC-01):

> A fail-closed decision is necessary but not sufficient: the
> diagnostics that EXPLAIN it must not re-disclose the prohibited
> value. Fail closed **and** stay silent about the unsafe value.

R6 independently demonstrated that with an in-scope file whose
*path itself* carries a prohibited identifier (e.g. an email-shaped
basename that disappears between enumeration and read), the correct
``FAIL`` decision used to be accompanied by diagnostics echoing the
raw path — in the structured ``scan_errors``/``findings`` records, in
``json.dumps`` output, and in the compatibility wrapper text of
:class:`PIIScanIncompleteError` (``path=...``). R7 closes that
channel with one canonical policy (:func:`_path_diag`) applied at
the single point where diagnostic path records are constructed:

* every path component is classified by the SAME detector rules the
  scanner applies to file content — a component carrying a
  prohibited identifier/secret shape is never fit for output;
* prohibited components are dropped whole (replaced by
  ``<redacted>``): no partial echo, suffix carving, or local-part
  surgery that could re-assemble the value; clean components stay,
  so repository-relative paths remain human-correlatable;
* every record additionally carries ``path_id`` — a stable,
  domain-separated ``sha256`` digest of the full path string — so
  redacted paths remain correlatable across runs without disclosing
  the value (see :func:`_path_id` for the threat model);
* the policy itself fails SAFE: if the sanitizer (or the classifier
  on a component, or the digest) raises for any reason, the path is
  emitted completely opaque and correlation degrades gracefully —
  a sanitizer failure can never cause raw-path emission, and it
  never causes a file to be skipped (the scan state still fails
  closed).

Documented, narrow exemptions (kept in repo, reviewable, with reasons):

* message-id left context: numbers directly preceded by a *message id*
  field are per-message receipts, not account identifiers;
* reserved example domains (exact boundary): the RFC-2606/6761
  reserved names ``example.com``/``.org``/``.net``/``.invalid``/
  ``.test`` and their subdomains are not personal contacts; this also
  covers the gate's own tracked test fixtures (self-scan);
* exact no-reply service local parts (``noreply``/``do-not-reply``
  modulo ``-``/``_``/``.`` separators): service addresses, not persons;
* GitHub ``users.noreply.github.com`` host family: commit-attribution
  service addresses.

The detector's own tests assemble synthetic prohibited values at
runtime from separated string fragments so this tracked source stays
clean under the gate itself, including all adversarial (bypass-shaped)
vectors — the gate must self-scan its own source without any
self-exemption rule. The work-order §8 negative control (temporarily
injecting a synthetic identifier into a tracked file, expecting FAIL,
then restoring byte-identically) is exercised as a separate evidence
step against this same gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from bisect import bisect_right
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

REDACT = "▒"  # ▒ — the redaction mask used in failure output


# ----------------------------------------------------------- allowlist
# Narrow, content-anchored, reviewed exceptions. Each entry must carry a
# fingerprint prefix and an engineering rationale; path/line anchors are
# NOT used so that moving a line cannot bypass the scan, and a moved
# allowlisted block is caught whenever its content changes.
# Keep EMPTY unless a genuinely benign identifier-shaped value must ship.
ALLOWLIST: list[dict[str, str]] = [
    # {"fingerprint_prefix": "sha256:<16-hex>", "reason": "…"},
]


# ------------------------------------------------------------ patterns
# Per-line display-style context (prose forms). Same-line requirement is
# retained for these *prose* shapes; the structured key/value family is
# handled by the bounded multi-line rule below.
_CHAT_CONTEXT = re.compile(
    r"(?i)("
    r"chat_id"                                  # JSON/YAML field or prose
    r"|telegram\s*\("                           # "Telegram (…)" display
    r"|(?:target|recipient|deliver\w*|send_to)[\"\']?\s*[:=][^\n]{0,60}telegram"
    r"|hermes send"                             # hermes CLI targets
    r"|delivered to"
    r")"
)
_CHAT_ID = re.compile(r"\b(\d{6,13})\b")
_MESSAGE_ID_LEFT = re.compile(
    r"(?i)(message[_\s-]?id|msg_?id|telegram_msg_id)\s*[=:]?\s*[\"']?\d*$"
)
_BOT_TOKEN = re.compile(r"\b(\d{8,10}:[A-Za-z0-9_-]{33,})\b")
# assembled from fragments: the *marker string itself* must not appear in
# this tracked source (the repo CA-trust audit forbids committed key
# markers; see its module self-exclusion precedent)
_PRIVATE_KEY = re.compile("-----" "BEGIN [A-Z0-9 ]*" "PRIVATE" " KEY" "-----")
_PHONE = re.compile(r"\b(09\d{8})\b")
_API_KEYISH = re.compile(
    r"\b(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|sk-"
    r"ant-|sk-[A-Za-z0-9]{20,}"
    r"|xox[bpas]-[A-Za-z0-9-]{10,})\b")
_EMAIL = re.compile(
    r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")

# --- P1: bounded multi-line key/value pass -----------------------------
# A chat-id-shaped key and its numeric value may be split across lines by
# any whitespace formatting (JSON/YAML/config). The window between key
# (plus optional closing quote) and separator, and between separator and
# value, is bounded to 80 whitespace characters total (covers indentation
# and blank lines of real config files without jumping blocks). Quotes
# around the value are tolerated. The full-width colon is tolerated next
# to ASCII ':'/'=' for CJK-formatted keys.
_CHAT_KEYVAL_ML = re.compile(
    r"(?i)\b(?:telegram[_-]?)?chat[_-]?id\b"
    r"[\s\"']{0,80}[:=：][\s\"']{0,80}"
    r"(\d{6,13})\b"
)

# --- P2: exact reserved-domain boundary --------------------------------
# Reserved names are compared as whole domains (identity or subdomain),
# never by substring containment.
_RESERVED_EXAMPLE_DOMAINS = frozenset({
    "example.com",
    "example.org",
    "example.net",
    "example.invalid",
    "example.test",
    # GitHub commit-attribution service host family (documented above)
    "users.noreply.github.com",
})

# --- P3: exact no-reply service local parts ----------------------------
_NOREPLY_LOCAL = frozenset({"noreply", "donotreply"})


def _is_reserved_example_domain(domain: str) -> bool:
    """True only if the token's whole domain is a reserved name or a
    subdomain of one (left-extension such as
    RESERVED+'.'+anything is NOT treated as reserved)."""
    candidate = domain.lower().lstrip(".").rstrip(".")
    for reserved in _RESERVED_EXAMPLE_DOMAINS:
        if candidate == reserved or candidate.endswith("." + reserved):
            return True
    return False


def _is_noreply_local(local: str) -> bool:
    """Exact service no-reply local part (separator-insensitive)."""
    return re.sub(r"[-_.]", "", local.lower()) in _NOREPLY_LOCAL


# --- P4: token-scoped URI authority regions ----------------------------
# Scheme head, tolerating a bounded insignificant blank run right after
# the separator (the DSN test fixture carries one); the authority region
# then runs to the first terminator (whitespace, path/query/fragment
# start, or a quote/bracket used as a delimiter in prose).
_URI_HEAD = re.compile(r"(?i)[a-z][a-z0-9+.-]*://[ \t]{0,3}")
_URI_AUTHORITY_STOP = re.compile(r"[\s/?#\"'<>]")


def _uri_authority_regions(line: str) -> list[tuple[int, int]]:
    """Spans of ``[userinfo@]host[:port]`` regions of parsed URIs."""
    regions: list[tuple[int, int]] = []
    for head in _URI_HEAD.finditer(line):
        start = head.end()
        end = start
        while end < len(line) and not _URI_AUTHORITY_STOP.match(line[end]):
            end += 1
        if end > start:
            regions.append((start, end))
    return regions


def _left_context(line: str, start: int, width: int = 20) -> str:
    return line[max(0, start - width):start]


def _is_excluded_number(line: str, start: int) -> bool:
    left = _left_context(line, start)
    return bool(_MESSAGE_ID_LEFT.search(left))


def _in_uri(span: tuple[int, int], regions: list[tuple[int, int]]) -> bool:
    return any(start >= a and end <= b
               for (start, end) in [span] for (a, b) in regions)


def _scan_line(line: str) -> list[tuple[str, str]]:
    """Return (category, fingerprint-prefix) pairs for one line."""
    out: list[tuple[str, str]] = []

    def _fp(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]

    if _PRIVATE_KEY.search(line):
        out.append(("private_key_marker", ""))
    for match in _BOT_TOKEN.finditer(line):
        out.append(("telegram_bot_token", _fp(match.group(1))))
    for match in _API_KEYISH.finditer(line):
        out.append(("api_key_shape", _fp(match.group(1))))
    if _CHAT_CONTEXT.search(line):
        for match in _CHAT_ID.finditer(line):
            if _is_excluded_number(line, match.start(1)):
                continue
            out.append(("telegram_chat_id", _fp(match.group(1))))
    regions = _uri_authority_regions(line)
    for match in _EMAIL.finditer(line):
        local, domain = match.group(1), match.group(2)
        if _in_uri(match.span(0), regions):
            # credential material inside a parsed URI's authority region
            # (userinfo@host) — governed by secret policy, not contact
            # policy. Token-scoped: only this matched token is exempt.
            continue
        if _is_reserved_example_domain(domain):
            continue
        if _is_noreply_local(local):
            continue
        out.append(("personal_email", _fp(match.group(0))))
    for match in _PHONE.finditer(line):
        out.append(("phone_number_shape", _fp(match.group(1))))
    return out


def _line_start_offsets(text: str) -> list[int]:
    starts = [0]
    for match in re.finditer(r"\n", text):
        starts.append(match.end())
    return starts


def _scan_text(text: str) -> list[tuple[int, str, str]]:
    """(line, category, fingerprint-prefix) findings for document text.

    Combines the per-line rules with the bounded multi-line key/value
    pass, deduplicated per (line, category, fingerprint).
    """
    rows: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for category, fp in _scan_line(line):
            rows.append((lineno, category, fp))
    starts = _line_start_offsets(text)
    for match in _CHAT_KEYVAL_ML.finditer(text):
        lineno = bisect_right(starts, match.start(1))
        rows.append((lineno, "telegram_chat_id",
                     "sha256:" + hashlib.sha256(
                         match.group(1).encode()).hexdigest()[:16]))
    seen: set[tuple[int, str, str]] = set()
    unique: list[tuple[int, str, str]] = []
    for row in rows:
        if row not in seen:
            seen.add(row)
            unique.append(row)
    return unique


class PIIScanIncompleteError(RuntimeError):
    """Fail-closed signal: an in-scope file could not be conclusively
    scanned. The overall gate result is FAIL — never a clean-equivalent."""


# --- R7: canonical safe-path diagnostic policy (R6-SEC-01) -------------
# The single rule for EVERY diagnostic surface that carries a path:
# structured records, JSON dumps, wrapper text and exception messages.
# Path records are built only through `_path_diag`, so all surfaces
# inherit identical redaction (no sanitize-one-field-leak-another gap).

# Whole-component replacement marker. Deliberately distinct from the
# content snippet mask `REDACT`. No suffix/extension carving and no
# local-part surgery of a flagged component: the value must not be
# re-assemblable from whatever is emitted.
_PATH_REDACTED = "<redacted>"
# Correlation digest domain separator (see _path_id threat model).
_PATH_ID_TAG = "fie-pii-gate/path-id/v1\0"


def _path_id(path: Path) -> str:
    """Stable, domain-separated correlation digest of a full path.

    ``sha256(digest-tag || path-string)``, truncated to 16 hex chars.
    Threat model for publishing these digests in diagnostics: the path
    string is preimage-resistant so the id leaks nothing about names or
    structure; the attacker can only re-compute ids for path strings
    they already possess — i.e. guessing is limited to replaying known
    candidate paths, never extracting unknown ones. The id must never
    be presented alongside any raw path material (records carry one
    construction or the other), and the domain tag prevents cross-use
    of path ids as content fingerprints or of fingerprints as path ids.

    Encoding uses ``surrogatepass`` so undecodable byte-level names
    (see `_tracked_files`) still produce a stable deterministic id.
    """
    raw = _PATH_ID_TAG.encode("utf-8") + str(path).encode(
        "utf-8", "surrogatepass")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def _location_scope(path: Path) -> str:
    """``repository`` when inside the tracked working tree, else ``external``."""
    try:
        path.relative_to(REPO_ROOT)
        return "repository"
    except ValueError:
        return "external"


def _path_diag(path: Path) -> dict[str, str]:
    """Canonical safe-path diagnostics for one enumerated path (R7).

    One policy for all surfaces that carry a path (Task B of the R7
    work order):

    * **identity**: repository-relative string preferred (correlation
      without host clutter), full string otherwise; identity is split
      on path-component boundaries;
    * **classification**: every component runs through the SAME
      detector rules the scanner applies to file content
      (:func:`_scan_line`); a component whose text carries a
      prohibited identifier/secret shape is not fit for output,
      whatever its role in the path (basename, parent, or deeper);
    * **emission**: flagged components are replaced WHOLE by
      ``<redacted>``; clean components are kept so a safe
      repository-relative path stays readable and correlatable;
    * **correlation**: every record also carries ``path_id``
      (:func:`_path_id`) so operators can match redacted failures
      across runs without disclosing the value;
    * **failure-safe**: if any step of this policy (including the
      per-component classifier or the digest) raises, the emitted
      path degrades to the fully opaque form and the id to an
      explicit-unavailable token — a sanitizer error can NEVER emit
      raw path material, and it never skips the scanned file (the
      caller's scan state keeps failing closed independently).
    """
    record: dict[str, str] = {
        "path": _PATH_REDACTED,
        "path_id": "<path_id_unavailable>",
        "location_scope": "external",
    }
    try:
        record["location_scope"] = _location_scope(path)
    except Exception:  # noqa: BLE001 — scope hint is best-effort
        pass
    try:
        if record["location_scope"] == "repository":
            parts = list(path.relative_to(REPO_ROOT).parts)
        else:
            parts = list(path.parts)
    except Exception:  # noqa: BLE001 — cannot state structure => opaque
        parts = None
    if parts:
        cleaned: list[str] = []
        for part in parts:
            try:
                flagged = bool(_scan_line(part))
            except Exception:  # noqa: BLE001 — an unclassifiable
                # component is never fit for output: redact it whole.
                flagged = True
            cleaned.append(_PATH_REDACTED if flagged else part)
        try:
            record["path"] = str(Path(*cleaned))
        except Exception:  # noqa: BLE001 — reconstruction failure =>
            # opaque form only (never the pre-redaction string)
            record["path"] = _PATH_REDACTED
    try:
        record["path_id"] = _path_id(path)
    except Exception:  # noqa: BLE001 — losing correlation is
        # acceptable; echoing the path instead is not.
        pass
    return record


def collect_scan_report(files: list[Path]) -> dict[str, object]:
    """Scan the given files and return the fail-closed scan report.

    States (work order §5):

    * ``state``: ``SCAN_COMPLETE`` or ``SCAN_INCOMPLETE``;
    * ``scan_errors``: per-file failure records with safe path, reason
      code and exception class (never file contents or matched values);
    * ``findings``: unapproved redacted findings (allowlist filtered);
    * ``gate_result``: ``PASS`` only when scanning completed AND there
      are zero unapproved findings — any in-scope file that cannot be
      conclusively scanned forces ``FAIL``.
    """
    raw: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")  # strict: F4 fail closed
        except UnicodeDecodeError as exc:  # F4 — decode contract failure
            errors.append({
                **_path_diag(path),  # R7 — one canonical path policy
                "reason_code": "PII_SCAN_DECODE_ERROR",
                "error_class": type(exc).__name__,
            })
            continue
        except OSError as exc:  # F1/F2/F3 — incompleteness, not cleanliness
            errors.append({
                **_path_diag(path),  # R7 — one canonical path policy
                "reason_code": "PII_SCAN_READ_ERROR",
                "error_class": type(exc).__name__,
            })
            continue
        try:
            found_rows = _scan_text(text)  # F5 — internal failure fails closed
        except Exception as exc:  # noqa: BLE001 — converted to FAIL, never passed
            errors.append({
                **_path_diag(path),  # R7 — one canonical path policy
                "reason_code": "PII_SCAN_INTERNAL_ERROR",
                "error_class": type(exc).__name__,
            })
            continue
        for lineno, category, fp in found_rows:
            raw.append({
                **_path_diag(path),  # R7 — findings paths are redacted too
                "line": lineno,
                "category": category,
                "fingerprint": fp,
                "raw_value_recorded": False,
            })
    allowed = {entry["fingerprint_prefix"] for entry in ALLOWLIST}
    findings = [f for f in raw
                if not any(f["fingerprint"] == fp or f["fingerprint"].startswith(fp)
                           for fp in allowed)]
    incomplete = bool(errors)
    return {
        "state": "SCAN_INCOMPLETE" if incomplete else "SCAN_COMPLETE",
        "scan_complete": not incomplete,
        "scan_errors": errors,
        "findings": findings,
        "raw_match_count": len(raw),
        "allowlisted_count": len(raw) - len(findings),
        "unapproved_pii_count": len(findings),
        "gate_result": "FAIL" if (incomplete or findings) else "PASS",
    }


def _incomplete_message(report: dict[str, object]) -> str:
    """Safe structured failure text — reason codes + error classes +
    redacted paths + correlation ids only (R7: the wrapper face of the
    failure must inherit the same canonical path policy as the JSON
    records; `path_id` keeps failures correlatable across runs)."""
    lines = ["PII_SCAN_INCOMPLETE: gate_result=FAIL "
             f"unscanned_files={len(report['scan_errors'])}"]
    for err in report["scan_errors"]:  # type: ignore[union-attr]
        lines.append(f"  reason_code={err['reason_code']} "
                     f"error_class={err['error_class']} "
                     f"location_scope={err['location_scope']} "
                     f"path={err['path']} path_id={err['path_id']}")
    return "\n".join(lines)


def collect_findings(files: list[Path]) -> list[dict[str, object]]:
    """Scan the given files; return redacted findings records.

    Fail closed (R4-SEC-01 repair): if ANY in-scope file cannot be
    conclusively scanned, raises :class:`PIIScanIncompleteError` instead
    of returning a clean-equivalent empty result. Callers that need the
    distinction as data should use :func:`collect_scan_report`.
    """
    report = collect_scan_report(files)
    if not report["scan_complete"]:
        raise PIIScanIncompleteError(_incomplete_message(report))
    return report["findings"]  # type: ignore[return-value]


def _tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                         capture_output=True, check=True)
    # §9 (R7) — bijective filename decode: `surrogateescape` maps every
    # distinct byte-level Git name to a DISTINCT str and back, so
    # replacement decoding cannot alias two tracked files onto one
    # path identity (the wrong-file/bypass shape is excluded
    # structurally, not case by case). A surrogate-bearing path still
    # resolves at the OS layer; its bytes are then subject to the same
    # strict-UTF-8 read contract as every other in-scope file, so this
    # cannot silently widen what counts as scannable content.
    names = out.stdout.decode("utf-8", "surrogateescape").split("\0")
    return [REPO_ROOT / name for name in names if name]


def _redact_snippet(text: str) -> str:
    """A snippet safe for failure output: digits of length ≥5 masked,
    email local-parts masked, bot tokens masked."""
    return re.sub(
        r"(\d{5,})",
        REDACT * 2,
        re.sub(r"[A-Za-z0-9._%+-]{4,}@[A-Za-z0-9.-]+", REDACT * 3, text),
    ).strip()


# Runtime-assembled synthetic prohibited shapes (piecewise concatenation —
# never appear as literals in tracked source; the gate's own scan of this
# file stays clean while the unit tests receive whole, identifier-shaped
# values at runtime). This includes every bypass-shaped vector so the
# adversarial tests can exercise real detection without embedding attack
# strings — or any exempted literal — in this tracked source.
SYNTH_CHAT = "7" "6543" "21098"
SYNTH_BOT = "123" "4567890:AA" "E" + ("c" * 30)
SYNTH_AWS = "AKIA" + "Q3M4VQYR" + "ZT8W" + "2NE1"
SYNTH_GH = "ghp_" + ("x" * 20) + "9"
SYNTH_OPENAI = "sk-" + ("a" * 20) + "7"
SYNTH_SLACK = "xoxb-" + "123456-" + "abcdef"
SYNTH_PHONE = "09871" + "23456"
# R3 adversarial fixtures (fragment-assembled for the same reason)
# attacker-style relay host: contains a reserved name but NOT at the
# domain boundary (P2), and an unresolvable-style suffix (P2-form)
SYNTH_RELAY_DOMAIN = "atta" + "cker.example.com" + ".relay.invalid"
# prefix-disguised contact host (P3): a generic prefix on the host must
# not grant safety to the local-part identifier
SYNTH_PREFIX_HOST = "syn" + "thetic-mail.invalid"
SYNTH_CONTACT_LOCAL = "call" + "center.agent"
SYNTH_REMOTE_ADDR = SYNTH_CONTACT_LOCAL + "@" + SYNTH_RELAY_DOMAIN
SYNTH_PREFIX_ADDR = SYNTH_CONTACT_LOCAL + "@" + SYNTH_PREFIX_HOST
# R7 diagnostic-path fixtures: must be a value the detector FLAGS (the
# reserved example domains are exempt, so they cannot exercise the
# redaction policy) — an attacker-style relay address assembled from
# fragments, never a literal in tracked source.
SYNTH_R7_LOCAL = "r7-" + "operator"
SYNTH_R7_DOMAIN = "relay." + "attacker" + ".invalid"
SYNTH_R7_ADDR = SYNTH_R7_LOCAL + "@" + SYNTH_R7_DOMAIN


class TestPIIGateDetector(unittest.TestCase):
    """Detector unit tests — synthetic values assembled at runtime so
    this source file itself stays clean under the gate."""

    def _scan_one(self, line: str) -> list[tuple[str, str]]:
        return _scan_line(line)

    def test_chat_id_with_context_is_caught(self) -> None:
        for line in (
            f'"chat_id": "{SYNTH_CHAT}",',
            f"Telegram (user {SYNTH_CHAT})",
            f"hermes send --to telegram:{SYNTH_CHAT} --subject x",
            f'{{"deliver": "telegram:{SYNTH_CHAT}"}}',
        ):
            with self.subTest(line=line):
                cats = {c for c, _ in self._scan_one(line)}
                self.assertIn("telegram_chat_id", cats)

    def test_chat_id_without_context_is_not_caught(self) -> None:
        found = [f for f in self._scan_one(f"value {SYNTH_CHAT} bytes total")
                 if f[0] == "telegram_chat_id"]
        self.assertEqual(found, [])

    def test_message_ids_are_not_flagged(self) -> None:
        bare = "7" "6543" "2098"
        line = f"message_id={SYNTH_CHAT}, delivered to user (user {bare})"
        cats = [c for c, _ in self._scan_one(line)]
        # the message-id number is excluded; the bare recipient number is caught
        self.assertEqual(cats.count("telegram_chat_id"), 1)

    def test_uri_emails_are_out_of_pii_scope(self) -> None:
        line = "postgresql://user:pass@db.internal:5432/fie_db"
        self.assertEqual([f for f in self._scan_one(line) if f[0] == "personal_email"], [])

    def test_synthetic_email_domains_are_not_flagged(self) -> None:
        for line in ("maintainer@example.invalid",
                     "ops@example.com",
                     "noreply@example.org",
                     # RFC-2606 reserved-domain subdomains are also exempt;
                     # these literals make the gate's own self-scan verify it
                     "someone@corp.example.com",
                     "team@sub.example.test"):
            with self.subTest(line=line):
                self.assertEqual(
                    [f for f in self._scan_one(line) if f[0] == "personal_email"], [])

    def test_secret_shapes_are_caught(self) -> None:
        for line, cat in (
            ("-----" "BEGIN RSA " "PRIVATE" " KEY" "-----", "private_key_marker"),
            (f'token="{SYNTH_BOT}"', "telegram_bot_token"),
            (f"key: {SYNTH_AWS}", "api_key_shape"),
            (f"github: {SYNTH_GH}", "api_key_shape"),
            (f"openai: {SYNTH_OPENAI}", "api_key_shape"),
            (f"slack: {SYNTH_SLACK}", "api_key_shape"),
            (f"mobile={SYNTH_PHONE}", "phone_number_shape"),
        ):
            with self.subTest(line=line):
                cats = {c for c, _ in self._scan_one(line)}
                self.assertIn(cat, cats)


class TestAdversarialMatrix(unittest.TestCase):
    """Durable adversarial matrix (work order §7).

    Every case drives the real production detector through
    :func:`collect_findings` (file-content path, like the tracked-tree
    scan) — no duplicate toy regex. Synthetic payloads only; fragments
    assembled at runtime so this tracked source stays clean.
    """

    maxDiff = None

    def _findings(self, content: str) -> list[dict[str, object]]:
        # Exercise the real production path: content on disk →
        # collect_findings (the same reader the tracked-tree scan uses).
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            probe = Path(tmp) / "payload.txt"
            probe.write_text(content, encoding="utf-8")
            return collect_findings([probe])

    def _categories(self, content: str) -> set[str]:
        return {f["category"] for f in self._findings(content)}

    # --- chat-id key/value family (P1) ---------------------------------

    def test_A01_same_line_chat_id(self) -> None:
        self.assertIn("telegram_chat_id",
                      self._categories(f'"chat_id": {SYNTH_CHAT},'))

    def test_A02_value_split_by_newline(self) -> None:
        content = f'"chat_id":\n{SYNTH_CHAT}\n'
        self.assertIn("telegram_chat_id",
                      self._categories(content))

    def test_A03_value_split_by_whitespace_run(self) -> None:
        content = f'"chat_id":\n\n                {SYNTH_CHAT}'
        self.assertIn("telegram_chat_id",
                      self._categories(content))

    def test_A04_json_quoted_key_multiline(self) -> None:
        content = 'config = {\n    "chat' + '_id":\n        "' \
                  + SYNTH_CHAT + '",\n}'
        self.assertIn("telegram_chat_id",
                      self._categories(content))

    def test_A04b_key_quote_split_from_colon(self) -> None:
        content = '"chat' + '_id"\n  : "' + SYNTH_CHAT + '"'
        self.assertIn("telegram_chat_id",
                      self._categories(content))

    def test_A05_yaml_style_multiline(self) -> None:
        content = ("send:\n"
                   "  chat" + "_id:\n"
                   "    " + SYNTH_CHAT + "\n")
        self.assertIn("telegram_chat_id",
                      self._categories(content))

    def test_A05b_equals_and_fullwidth_colon_forms(self) -> None:
        self.assertIn("telegram_chat_id",
                      self._categories(f"chat" + "_id\n=\n" + SYNTH_CHAT))
        self.assertIn("telegram_chat_id",
                      self._categories("chat" + "_id：" + SYNTH_CHAT))

    def test_A06_benign_unrelated_long_number(self) -> None:
        content = ("Q3 settlement run booked 87654321 in ledger entries\n"
                   "and message_id=87654322 receipt\n")
        self.assertEqual(self._categories(content), set())

    # --- reserved-domain boundary (P2) ---------------------------------

    def test_A07_canonical_reserved_fixture(self) -> None:
        content = "contact user@example.com for docs\n"
        self.assertNotIn("personal_email", self._categories(content))

    def test_A08_reserved_name_non_terminal(self) -> None:
        content = "escalate immediately: " \
                  + ("contact." + "desk@" + "example.com" + ".atta" + "cker.invalid")
        cats = self._categories(content)
        self.assertIn("personal_email", cats)

    def test_A09_mixed_case_reserved_exact_is_exempt(self) -> None:
        content = "ping USER@EXAMPLE.COM and User@Sub.Example.OrG\n"
        self.assertNotIn("personal_email", self._categories(content))

    def test_A09b_mixed_case_nonreserved_still_flagged(self) -> None:
        content = "write to " + "Support.Desk@" + "Example.invalid.Relay" \
                  + ".example.com" + ".relay"
        self.assertIn("personal_email", self._categories(content))

    # --- URI handling (P4) ---------------------------------------------

    def test_A10_legitimate_uri_only_fixture(self) -> None:
        content = ("docs: https://docs.example.org/routing-guide\n"
                   "dsn: postgresql://user:pass@db.internal:5432/fie_db\n")
        self.assertEqual(self._categories(content), set())

    def test_A11_uri_plus_separate_prohibited_email_same_line(self) -> None:
        content = ("read https://docs.example.org/routing-guide then page "
                   + SYNTH_REMOTE_ADDR + " at once")
        self.assertIn("personal_email", self._categories(content))

    def test_A12_uri_plus_prohibited_identifier_adjacent_line(self) -> None:
        content = ("upstream: https://docs.example.org/routing-guide\n"
                   'notify:\n  "chat' + '_id": ' + SYNTH_CHAT + "\n")
        self.assertEqual(self._categories(content),
                         {"telegram_chat_id"})

    # --- prefix exemption removal (P3) ---------------------------------

    def test_A13_repository_reserved_fixture_is_approved(self) -> None:
        # the approved synthetic fixture family is the RFC-reserved
        # reserved-domain address used by this repo's own tests
        content = "sample owner: team@sub.example.test\n"
        cats = self._categories(content)
        self.assertEqual(cats, set())

    def test_A14_arbitrary_synthetic_prefix(self) -> None:
        content = "reference only: " \
                  + ("syn" + "thetic-" + SYNTH_CONTACT_LOCAL + "@"
                     + "relay" + ".attacker.invalid")
        self.assertIn("personal_email", self._categories(content))

    def test_A15_arbitrary_placeholder_prefix(self) -> None:
        content = "reference only: " \
                  + ("place" + "holder-" + SYNTH_CONTACT_LOCAL + "@"
                     + "relay" + ".attacker.invalid")
        self.assertIn("personal_email", self._categories(content))

    def test_A16_arbitrary_test_prefix(self) -> None:
        content = "reference only: " \
                  + ("te" + "st-" + SYNTH_CONTACT_LOCAL + "@"
                     + "relay" + ".attacker.invalid")
        self.assertIn("personal_email", self._categories(content))

    def test_A16b_prefix_disguised_host_does_not_trust_local_part(self) -> None:
        self.assertIn("personal_email",
                      self._categories("note: " + SYNTH_PREFIX_ADDR))

    def test_A17_prefix_never_sanctifies_secret_shapes(self) -> None:
        cases = (
            (f"test-token: {SYNTH_BOT}", "telegram_bot_token"),
            (f"synthetic key {SYNTH_AWS}", "api_key_shape"),
            (f"placeholder mobile={SYNTH_PHONE}", "phone_number_shape"),
        )
        for line, cat in cases:
            with self.subTest(line=line):
                self.assertIn(cat, {c for c, _ in _scan_line(line)})

    # --- diagnostics & evidence ----------------------------------------

    def test_A18_redacted_evidence_representation_is_safe(self) -> None:
        raw = f'"chat' + '_id": ' + SYNTH_CHAT + ", mail " + SYNTH_REMOTE_ADDR
        snippet = _redact_snippet(raw)
        findings = self._findings(raw)
        self.assertTrue(findings)
        dumped = json.dumps(findings, ensure_ascii=False)
        self.assertNotIn(SYNTH_CHAT, dumped)
        self.assertNotIn(SYNTH_REMOTE_ADDR, dumped)
        self.assertNotIn(SYNTH_CHAT, snippet)
        self.assertNotIn("call" + "center.agent", snippet)
        # fingerprints are stable, digest-shaped, and value-free
        self.assertTrue(all(f["fingerprint"].startswith("sha256:")
                            and f["raw_value_recorded"] is False
                            for f in findings))

    # --- rename/path robustness & self-scan -----------------------------

    def test_A19_A20_moved_prohibited_content_is_flagged(self) -> None:
        import tempfile
        payload = 'deliver: "chat' + '_id": ' + SYNTH_CHAT + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tmp_path = Path(tmp)
            doc_dir = tmp_path / "notes" / "docs"
            test_dir = tmp_path / "suite" / "test_sub"
            doc_dir.mkdir(parents=True)
            test_dir.mkdir(parents=True)
            for target in (doc_dir / "engineering_note.md",
                           test_dir / "helper_regression.py"):
                target.write_text(payload, encoding="utf-8")
                with self.subTest(path=target.name):
                    findings = collect_findings([target])
                    cats = {f["category"] for f in findings}
                    self.assertIn("telegram_chat_id", cats)

    def test_A21_gate_scans_itself_without_unsafe_exemption(self) -> None:
        # The gate's own tracked source must produce zero findings through
        # the production scan — i.e. self-scan works with NO special
        # exemption for the scanner itself. (Deliberately no literal-string
        # assertions for hypothetical skip markers here: such an assertion
        # would embed its own target in this source and be self-defeating;
        # the structural property — zero findings, no path-based filtering —
        # is covered by this check plus test_A19_A20's path-independence.)
        source = Path(__file__).resolve()
        self.assertEqual(collect_findings([source]), [])

    def test_A22_multiple_matches_fail_safely(self) -> None:
        content = ('"chat' + '_id": ' + SYNTH_CHAT + ','
                   ' page ' + SYNTH_REMOTE_ADDR
                   + ', mobile ' + SYNTH_PHONE + '\n')
        findings = self._findings(content)
        self.assertGreaterEqual(len(findings), 3)
        dumped = json.dumps(findings, ensure_ascii=False)
        for raw in (SYNTH_CHAT, SYNTH_REMOTE_ADDR, SYNTH_PHONE,
                    "call" + "center.agent"):
            self.assertNotIn(raw, dumped)

    # --- formatting variants --------------------------------------------

    def test_A23_CRLF_equals_LF_result(self) -> None:
        lf = f'"chat' + '_id":\n    ' + SYNTH_CHAT + "\nmail " \
             + SYNTH_REMOTE_ADDR + "\n"
        crlf = lf.replace("\n", "\r\n")
        lf_rows = sorted((f["line"], f["category"])
                         for f in self._findings(lf))
        crlf_rows = sorted((f["line"], f["category"])
                           for f in self._findings(crlf))
        self.assertEqual(lf_rows, crlf_rows)
        self.assertIn("telegram_chat_id", {cat for _, cat in lf_rows})
        self.assertIn("personal_email", {cat for _, cat in lf_rows})

    def test_A24_unicode_surrounding_text(self) -> None:
        content = ("警報通知：" + SYNTH_CONTACT_LOCAL + "@" + SYNTH_RELAY_DOMAIN
                   + "，設定 chat" + "_id：" + SYNTH_CHAT + " 完成後立即聯絡\n")
        cats = self._categories(content)
        self.assertIn("personal_email", cats)
        self.assertIn("telegram_chat_id", cats)


class TestR5FailClosedScanCompleteness(unittest.TestCase):
    """Permanent regression suite for defect R4-SEC-01 (work order §8).

    Baseline behavior (repair SHA 772da949): any read failure on an
    enumerated in-scope file was swallowed with
    ``except OSError: continue`` and the gate returned the SAME
    zero-findings result as a clean scan — a prohibited payload was
    accepted as clean whenever its carrier file could not be read.

    Every test here must FAIL on the baseline implementation and PASS
    only after the fail-closed repair. All fixtures are synthetic,
    runtime-assembled payloads; no real PII, no secrets.
    """

    def _payload(self) -> str:
        # synthetic prohibited payload (chat id + attacker-style email),
        # detectable by the positive control
        return ("deliver: \"chat" + "_id\": " + SYNTH_CHAT + "\npage "
                + SYNTH_CONTACT_LOCAL + "@" + SYNTH_RELAY_DOMAIN + "\n")

    def _readable_finding(self, fixture: Path) -> dict[str, object]:
        # positive control: the fixture MUST be scanned & detected now
        report = collect_scan_report([fixture])
        self.assertIs(report["scan_complete"], True)
        self.assertGreaterEqual(report["unapproved_pii_count"], 1)  # type: ignore[operator]
        self.assertEqual("FAIL", report["gate_result"])
        return report  # type: ignore[return-value]

    def _assert_report_fail_closed(self, fixture: Path,
                                   report: dict[str, object],
                                   reason_code: str,
                                   error_class: str) -> None:
        """Report-level fail-closed assertions (structure + semantics)."""
        self.assertFalse(
            report["scan_complete"],
            "a scan/read failure must NOT be reported as a complete scan")
        self.assertEqual("SCAN_INCOMPLETE", report["state"])
        self.assertEqual("FAIL", report["gate_result"])
        self.assertEqual(
            [{"reason_code": reason_code, "error_class": error_class}],
            [{"reason_code": e["reason_code"], "error_class": e["error_class"]
              } for e in report["scan_errors"]],  # type: ignore[union-attr]
            "structured failure record must carry reason code + error class")
        self.assertEqual(0, report["unapproved_pii_count"])
        # safe (repository-relative-or-external) path record present
        self.assertEqual(str(fixture), report["scan_errors"][0]["path"])  # type: ignore[index]

    def _assert_entrypoint_fail_closed(self, fixture: Path,
                                       reason_code: str) -> None:
        """The canonical entrypoint must hard-fail in the SAME state the
        report was produced in — never return a clean-equivalent."""
        with self.assertRaises(PIIScanIncompleteError) as caught:
            collect_findings([fixture])
        message = str(caught.exception)
        self.assertIn("PII_SCAN_INCOMPLETE", message)
        self.assertIn(reason_code, message)
        self.assertIn("gate_result=FAIL", message)
        # failure text is value-free: no file contents / matched values
        self.assertNotIn(self._payload(), message)

    # --- T1: FileNotFoundError / disappeared file -----------------------

    def test_T1_file_not_found_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = Path(tmp) / "carrier.md"
            fixture.write_text(self._payload(), encoding="utf-8")
            self._readable_finding(fixture)  # positive control (baseline T7)
            enumerated = [fixture]
            fixture.unlink()  # disappears between enumeration and read
            report = collect_scan_report(enumerated)
            self._assert_report_fail_closed(fixture, report,
                                            "PII_SCAN_READ_ERROR",
                                            "FileNotFoundError")
            self._assert_entrypoint_fail_closed(fixture, "PII_SCAN_READ_ERROR")

    # --- T2: PermissionError / unreadable file ---------------------------

    def test_T2_permission_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = Path(tmp) / "carrier.md"
            fixture.write_text(self._payload(), encoding="utf-8")
            self._readable_finding(fixture)
            privileged_env = False
            fixture.chmod(0)
            try:
                report = collect_scan_report([fixture])
                if report["scan_complete"]:
                    # hosts that ignore permission bits (e.g. running as
                    # root) cannot produce PermissionError from chmod —
                    # deterministic controlled-mock fallback, same class
                    privileged_env = True
                else:
                    self._assert_report_fail_closed(
                        fixture, report, "PII_SCAN_READ_ERROR",
                        "PermissionError")
                    self._assert_entrypoint_fail_closed(
                        fixture, "PII_SCAN_READ_ERROR")
            finally:
                fixture.chmod(0o644)  # restore readability/state
            if privileged_env:
                exc = PermissionError(13, "simulated-locked read seam")
                with self._read_failure_seam(fixture, exc):
                    report = collect_scan_report([fixture])
                    self._assert_report_fail_closed(
                        fixture, report, "PII_SCAN_READ_ERROR",
                        "PermissionError")
                    self._assert_entrypoint_fail_closed(
                        fixture, "PII_SCAN_READ_ERROR")
            # readability restored → the same prohibited payload detects
            restored = self._readable_finding(fixture)
            self.assertGreaterEqual(restored["unapproved_pii_count"], 1)  # type: ignore[operator]

    # --- T3: generic OSError / I/O failure -------------------------------

    def _read_failure_seam(self, fixture: Path, exc: Exception):
        """Narrowest safe seam: only this fixture's read raises; every
        other file reads normally through the real implementation."""
        original = Path.read_text

        def raiser(path_self: Path, *args: object, **kwargs: object):
            if path_self == fixture:
                raise exc
            return original(path_self, *args, **kwargs)  # type: ignore[arg-type]

        return mock.patch.object(Path, "read_text", new=raiser)

    def test_T3_generic_oserror_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = Path(tmp) / "carrier.md"
            fixture.write_text(self._payload(), encoding="utf-8")
            self._readable_finding(fixture)
            with self._read_failure_seam(
                    fixture, OSError(5, "simulated generic I/O failure")):
                report = collect_scan_report([fixture])
                self._assert_report_fail_closed(fixture, report,
                                                "PII_SCAN_READ_ERROR",
                                                "OSError")
                self._assert_entrypoint_fail_closed(fixture,
                                                    "PII_SCAN_READ_ERROR")

    # --- T4: decode failure (strict-UTF-8 contract, no masking) ----------

    def test_T4_decode_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = Path(tmp) / "carrier.md"
            # non-UTF-8 bytes carrying the same prohibited payload shape:
            # the old errors="replace" contract silently decoded this to
            # mask characters and would have hidden any non-UTF-8 encoded
            # prohibited value; the strict contract fails closed instead.
            payload_bytes = ("deliver: \"chat" + "_id\": " + SYNTH_CHAT + "\n"
                             ).encode("utf-8") + b"\xff\xfe\x00payload\n"
            fixture.write_bytes(payload_bytes)
            report = collect_scan_report([fixture])
            self._assert_report_fail_closed(fixture, report,
                                            "PII_SCAN_DECODE_ERROR",
                                            "UnicodeDecodeError")
            self._assert_entrypoint_fail_closed(fixture, "PII_SCAN_DECODE_ERROR")

    # --- T5: scanner internal/classification error -----------------------

    def test_T5_scanner_internal_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = Path(tmp) / "carrier.md"
            fixture.write_text(self._payload(), encoding="utf-8")
            self._readable_finding(fixture)
            with mock.patch(f"{__name__}._scan_text",
                            side_effect=RuntimeError(
                                "simulated classification fault")):
                report = collect_scan_report([fixture])
                self._assert_report_fail_closed(fixture, report,
                                                "PII_SCAN_INTERNAL_ERROR",
                                                "RuntimeError")
                self._assert_entrypoint_fail_closed(
                    fixture, "PII_SCAN_INTERNAL_ERROR")

    # --- T6/T7/T8: gate-decision controls ---------------------------------

    def test_T6_clean_scan_still_passes(self) -> None:
        report = collect_scan_report(_tracked_files())
        self.assertIs(report["scan_complete"], True)
        self.assertEqual(0, report["unapproved_pii_count"])
        self.assertEqual("PASS", report["gate_result"])

    def test_T7_readable_prohibited_fixture_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = Path(tmp) / "carrier.md"
            fixture.write_text(self._payload(), encoding="utf-8")
            report = self._readable_finding(fixture)
            self.assertEqual("FAIL", report["gate_result"])
            self.assertEqual({"telegram_chat_id", "personal_email"},
                             {f["category"] for f in report["findings"]})

    def test_T8_approved_synthetic_fixture_remains_allowed(self) -> None:
        # documented approved/synthetic family: reserved example domains
        # and the URI DSN form are exempt content — scan completes and
        # the gate passes with zero unapproved findings
        content = ("owner: team@sub.example.test docs https://docs.example.org/x\n"
                   "dsn: postgresql://user:pass@db.internal:5432/fie_db\n")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = Path(tmp) / "approved_fixture.md"
            fixture.write_text(content, encoding="utf-8")
            report = collect_scan_report([fixture])
            self.assertIs(report["scan_complete"], True)
            self.assertEqual(0, report["unapproved_pii_count"])
            self.assertEqual("PASS", report["gate_result"])


class TestRepoCleanOfOI07A(unittest.TestCase):
    """The gate itself: tracked content must be free of prohibited PII."""

    def test_tracked_tree_has_no_prohibited_identifiers(self) -> None:
        # Fail-closed contract (R5): BOTH conditions must hold — the scan
        # must be complete AND unapproved PII must be zero. An incomplete
        # scan (e.g. a read/decode/internal error) is a gate failure by
        # itself and is never interpreted as a clean result.
        report = collect_scan_report(_tracked_files())
        self.assertEqual(
            [], report["findings"],
            "unapproved PII/secret findings (redacted): "
            + json.dumps(report["findings"], ensure_ascii=False, indent=1))
        self.assertIs(report["scan_complete"], True,
                      "scan incompleteness must fail the gate, "
                      "details: " + json.dumps(report["scan_errors"],
                                               ensure_ascii=False, indent=1))
        self.assertEqual("PASS", report["gate_result"],
                         "gate decision details: "
                         + json.dumps(report["scan_errors"],
                                      ensure_ascii=False, indent=1))

    def test_failure_output_redacts_values(self) -> None:
        # Fragment-assembled address: it stays invisible to this file's own
        # tracked-source scan regardless of any future domain-rule change.
        address = "someone@" + "corp." + "example.com"
        snippet = _redact_snippet(
            f'"chat_id": "{SYNTH_CHAT}", "mail": ' + f'"{address}"')
        self.assertNotIn(SYNTH_CHAT, snippet)
        self.assertNotIn("someone@corp", snippet)
        self.assertIn("chat_id", snippet)


class TestR7DiagnosticPathRedaction(unittest.TestCase):
    """Permanent regression suite for defect R6-SEC-01 (R7 §6.E).

    Baseline (repair SHA 70c7bed): the fail-closed DECISION was correct,
    but the diagnostics explaining it — structured ``scan_errors`` /
    ``findings`` records, their JSON serialization, and the
    compatibility wrapper text of :class:`PIIScanIncompleteError` —
    echoed the raw path of an in-scope file whenever a prohibited
    identifier appeared in a path component (basename, parent
    directory, or external absolute path).

    Every test here asserts BOTH required halves:

    * the security decision is correct (fail closed on scan
      incompleteness, real findings preserved), AND
    * the raw prohibited path token is absent from EVERY observable
      output surface (structured records, JSON dumps, wrapper text,
      exception text, nested string fields).

    Fixtures: synthetic, fragment-assembled; the address family is a
    detector-FLAGGED attacker-style relay address (reserved example
    domains are exempt and therefore cannot exercise redaction).
    """

    maxDiff = None

    # fragment-assembled prohibited path token
    _TOKEN = SYNTH_R7_ADDR
    _BASENAME = SYNTH_R7_ADDR + ".md"
    _PAYLOAD = ('deliver: "chat' + '_id": ' + SYNTH_CHAT + "\npage "
                + SYNTH_CONTACT_LOCAL + "@" + SYNTH_RELAY_DOMAIN + "\n")
    _CLEAN_CONTENT = "plain operational content\n"
    _PII_CONTENT_VALUES = (SYNTH_CHAT, SYNTH_REMOTE_ADDR,
                           SYNTH_CONTACT_LOCAL)

    # ------------------------------------------------------------ helpers

    def _all_strings(self, obj: object) -> list[str]:
        """Every string reachable in a report (records, fields, nested)."""
        if isinstance(obj, str):
            return [obj]
        if isinstance(obj, dict):
            return [s for v in obj.values() for s in self._all_strings(v)]
        if isinstance(obj, (list, tuple)):
            return [s for v in obj for s in self._all_strings(v)]
        return [str(obj)]

    def _assert_no_token_anywhere(self, *surfaces: object) -> None:
        """I2 + I4: the prohibited path token must be absent from every
        string reachable in every given output surface (records, nested
        fields, wrapper text, exception text, dumps)."""
        for surface in surfaces:
            for text in self._all_strings(surface):
                self.assertNotIn(self._TOKEN, text,
                                 "raw prohibited path material reached an "
                                 f"observable diagnostic surface: {text!r}")

    def _wrapper_text(self, files: list[Path]) -> str:
        """The compatibility face: the raised fail-closed exception text."""
        try:
            collect_findings(files)
        except PIIScanIncompleteError as exc:
            return str(exc)
        self.fail("unreadable fixture did NOT fail closed through the "
                  "compatibility entrypoint")

    def _pii_name_file(self, tmp: Path) -> Path:
        return tmp / self._BASENAME

    def _assert_fail_closed_shape(self, report: dict[str, object],
                                  reason_code: str = "PII_SCAN_READ_ERROR",
                                  ) -> dict[str, str]:
        """Decision half of the invariant; returns the single error record."""
        self.assertEqual("SCAN_INCOMPLETE", report["state"])
        self.assertEqual("FAIL", report["gate_result"],
                         "redaction must never soften the failing decision")
        errors = report["scan_errors"]  # type: ignore[union-attr]
        self.assertEqual(1, len(errors))  # type: ignore[arg-type]
        record = errors[0]  # type: ignore[index]
        self.assertEqual(reason_code, record["reason_code"])
        return record  # type: ignore[return-value]

    def _correlation_shape(self, record: dict[str, str],
                           want_scope: str = "external") -> None:
        """I3: redaction must not destroy correlation."""
        # flagged component dropped whole; clean components stay
        self.assertIn("<redacted>", record["path"])
        self.assertEqual(want_scope, record["location_scope"])
        pid = record["path_id"]
        self.assertTrue(pid.startswith("sha256:") and len(pid) == len(
            "sha256:") + 16,
            f"path_id must be a 16-hex digest, got {pid!r}")
        self.assertNotIn(self._TOKEN, pid)

    # --- 1/5. PII in basename + FileNotFoundError ------------------------
    # --- 10. structured JSON output ---------------------------------------

    def test_R01_basename_filenotfound_redacted_json_and_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            enumerated = [fixture]
            fixture.unlink()  # disappears between enumeration and read
            report = collect_scan_report(enumerated)
            record = self._assert_fail_closed_shape(report)
            self.assertEqual("FileNotFoundError", record["error_class"])
            self._correlation_shape(record)
            dumped = json.dumps(report, ensure_ascii=False)
            self._assert_no_token_anywhere(report, dumped,
                                           self._wrapper_text(enumerated))

    # --- 6. genuine PermissionError ---------------------------------------

    def test_R02_basename_permissionerror_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            privileged_env = False
            fixture.chmod(0)
            try:
                report = collect_scan_report([fixture])
                if report["scan_complete"]:
                    privileged_env = True  # permission bits ignored (root)
                else:
                    record = self._assert_fail_closed_shape(report)
                    self.assertEqual("PermissionError", record["error_class"])
                    self._correlation_shape(record)
                    self._assert_no_token_anywhere(
                        report, json.dumps(report, ensure_ascii=False),
                        self._wrapper_text([fixture]))
            finally:
                fixture.chmod(0o644)
            if privileged_env:
                exc = PermissionError(13, "simulated-locked read seam")
                with self._read_failure_seam(fixture, exc):
                    report = collect_scan_report([fixture])
                    record = self._assert_fail_closed_shape(report)
                    self.assertEqual("PermissionError", record["error_class"])
                    self._correlation_shape(record)
                    self._assert_no_token_anywhere(
                        report, json.dumps(report, ensure_ascii=False),
                        self._wrapper_text([fixture]))

    # seam shared with the R5 suite's T-tests (same shape, local scope)
    def _read_failure_seam(self, fixture: Path, exc: Exception):
        original = Path.read_text

        def raiser(path_self: Path, *args: object, **kwargs: object):
            if path_self == fixture:
                raise exc
            return original(path_self, *args, **kwargs)  # type: ignore[arg-type]

        return mock.patch.object(Path, "read_text", new=raiser)

    # --- 2. PII in parent directory ---------------------------------------

    def test_R03_parent_dir_pii_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pii_dir = tmp / self._TOKEN
            pii_dir.mkdir()
            # (a) unreadable file under the pii-named directory
            inner = pii_dir / "notes.md"
            inner.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            inner.chmod(0)
            try:
                report = collect_scan_report([inner])
                if not report["scan_complete"]:
                    record = self._assert_fail_closed_shape(report)
                    self._correlation_shape(record)
                    self._assert_no_token_anywhere(
                        report, json.dumps(report, ensure_ascii=False),
                        self._wrapper_text([inner]))
            finally:
                inner.chmod(0o644)
            # (b) readable file under the pii-named directory: the
            # findings[].path surface must be redacted just as strictly
            inner.write_text(self._PAYLOAD, encoding="utf-8")
            report = collect_scan_report([inner])
            self.assertEqual("FAIL", report["gate_result"],
                             "prohibited content must still be reported")
            self.assertGreaterEqual(report["unapproved_pii_count"], 1)  # type: ignore[operator]
            finding = report["findings"][0]  # type: ignore[index]
            self.assertIn("<redacted>", finding["path"])
            self._correlation_shape(finding)
            self._assert_no_token_anywhere(
                report, json.dumps(report, ensure_ascii=False))

    # --- 3. PII in absolute external path (scope + structure) -------------

    def test_R03b_external_absolute_path_scope_declared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)  # absolute external path
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            enumerated = [fixture]
            fixture.unlink()
            report = collect_scan_report(enumerated)
            record = self._assert_fail_closed_shape(report)
            self.assertEqual("external", record["location_scope"])
            # the emitted string keeps only unflagged structural
            # components and the whole opaque marker for the flagged one
            self.assertNotIn(self._TOKEN, record["path"])
            self.assertIn("<redacted>", record["path"])

    # --- 4. safe repository-relative path stays correlatable --------------

    def test_R04_safe_repo_relative_path_is_retained(self) -> None:
        # I3 forbids solving the defect by deleting all diagnostic
        # identity: a path with NO prohibited component inside the
        # working tree keeps its readable relative form + stable id.
        with mock.patch(f"{__name__}.REPO_ROOT", tempfile.mkdtemp()) as tmp_s:
            tmp = Path(tmp_s)
            fixture = tmp / "carrier_clean.md"
            fixture.write_text(self._PAYLOAD, encoding="utf-8")
            report = collect_scan_report([fixture])
        self.assertEqual("FAIL", report["gate_result"])
        finding = report["findings"]  # type: ignore[index]
        self.assertGreaterEqual(len(finding), 1)
        self.assertEqual("carrier_clean.md",
                         finding[0]["path"])  # type: ignore[index]
        self.assertEqual("repository",
                         finding[0]["location_scope"])  # type: ignore[index]
        self._assert_no_token_anywhere(
            report, json.dumps(report, ensure_ascii=False))

    # --- 7. generic OSError ------------------------------------------------

    def test_R05_generic_oserror_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            with self._read_failure_seam(
                    fixture, OSError(5, "simulated generic I/O failure")):
                report = collect_scan_report([fixture])
                record = self._assert_fail_closed_shape(report)
                self.assertEqual("OSError", record["error_class"])
                self._correlation_shape(record)
                self._assert_no_token_anywhere(
                    report, json.dumps(report, ensure_ascii=False),
                    self._wrapper_text([fixture]))

    # --- 8. decode failure --------------------------------------------------

    def test_R06_decode_failure_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_bytes(("text " + "x" * 10 + "\n").encode("utf-8")
                                + b"\xff\xfe\x00payload\n")
            report = collect_scan_report([fixture])
            record = self._assert_fail_closed_shape(
                report, reason_code="PII_SCAN_DECODE_ERROR")
            self.assertEqual("UnicodeDecodeError", record["error_class"])
            self._correlation_shape(record)
            self._assert_no_token_anywhere(
                report, json.dumps(report, ensure_ascii=False),
                self._wrapper_text([fixture]))

    # --- 9. scanner internal error ------------------------------------------

    def test_R07_internal_error_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            with mock.patch(f"{__name__}._scan_text",
                            side_effect=RuntimeError(
                                "simulated classification fault")):
                report = collect_scan_report([fixture])
                record = self._assert_fail_closed_shape(
                    report, reason_code="PII_SCAN_INTERNAL_ERROR")
                self.assertEqual("RuntimeError", record["error_class"])
                self._correlation_shape(record)
                self._assert_no_token_anywhere(
                    report, json.dumps(report, ensure_ascii=False),
                    self._wrapper_text([fixture]))

    # --- 13. mixed prohibited content + unreadable file ---------------------

    def test_R10_mixed_readable_prohibited_and_unreadable_pii_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # readable carrier: prohibited CONTENT, safe basename
            readable = tmp / "carrier_plain.md"
            readable.write_text(self._PAYLOAD, encoding="utf-8")
            # unreadable carrier: prohibited identity in its path
            hidden = self._pii_name_file(tmp)
            hidden.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            hidden.unlink()
            report = collect_scan_report([readable, hidden])
            self.assertEqual("SCAN_INCOMPLETE", report["state"])
            self.assertEqual("FAIL", report["gate_result"])
            self.assertGreaterEqual(report["unapproved_pii_count"], 1)  # type: ignore[operator]
            # readable finding: safe path correlated, content not dumped
            f0 = report["findings"][0]  # type: ignore[index]
            self.assertEqual(str(readable), f0["path"])
            for value in self._PII_CONTENT_VALUES:
                self.assertNotIn(value, json.dumps(report,
                                                   ensure_ascii=False))
            # unreadable record: redacted identity
            errors = report["scan_errors"]  # type: ignore[union-attr]
            self.assertEqual(1, len(errors))  # type: ignore[arg-type]
            self.assertIn("<redacted>", errors[0]["path"])  # type: ignore[index]
            self._assert_no_token_anywhere(
                report, json.dumps(report, ensure_ascii=False),
                self._wrapper_text([readable, hidden]))

    # --- 11. compatibility wrapper output ------------------------------------

    def test_R09_wrapper_text_uses_same_policy_as_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            fixture.unlink()
            report = collect_scan_report([fixture])
            wrapper = self._wrapper_text([fixture])
            record = report["scan_errors"][0]  # type: ignore[index]
            # the wrapper line and the JSON record must carry the SAME
            # redacted path + the same correlation id (I4 consistency)
            self.assertIn(f"path={record['path']}", wrapper)
            self.assertIn(f"path_id={record['path_id']}", wrapper)
            self.assertNotIn(self._TOKEN, wrapper)
            self.assertIn("PII_SCAN_INCOMPLETE", wrapper)
            self.assertIn("gate_result=FAIL", wrapper)

    # --- 12. nested reason/message/details leakage (I4) ----------------------

    def test_R11_all_nested_fields_and_dumps_are_token_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_bytes(b"\xff\xfe\x00broken\n")
            report = collect_scan_report([fixture])
            message = str(PIIScanIncompleteError(str(report)))
            incomplete = _incomplete_message(report)
            # every string reachable anywhere: record fields (reason /
            # error_class / path / path_id / location_scope), nested
            # dumps in both ASCII-escaping modes, exception renderings,
            # and the wrapper formatter output — no partial echo
            # survived anywhere
            for dump in (json.dumps(report, ensure_ascii=False),
                         json.dumps(report, ensure_ascii=True),
                         repr(report), message, incomplete,
                         _redact_snippet(json.dumps(report,
                                                    ensure_ascii=False))):
                self.assertNotIn(self._TOKEN, dump)
            for text in self._all_strings(report):
                self.assertNotIn(self._TOKEN, text)
            # and the entrypoint exception built from the report
            try:
                collect_findings([fixture])
                self.fail("expected PIIScanIncompleteError")
            except PIIScanIncompleteError as exc:
                self.assertNotIn(self._TOKEN, str(exc))

    # --- 14. sanitizer failure / defensive fallback (fail safe) -------------

    def test_R12_classifier_failure_redacts_whole_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            fixture.unlink()
            with mock.patch(f"{__name__}._scan_line",
                            side_effect=RuntimeError(
                                "simulated policy-side classifier fault")):
                report = collect_scan_report([fixture])
            # the policy must fail SAFE: the raw path never escapes even
            # when the sanitizer itself is broken, and the file is not
            # skipped — the scan state still fails closed
            record = report["scan_errors"][0]  # type: ignore[index]
            self.assertIn("<redacted>", record["path"])
            self.assertNotIn(self._TOKEN, record["path"])
            self.assertEqual("PII_SCAN_READ_ERROR", record["reason_code"])
            self.assertEqual("FAIL", report["gate_result"])
            self._assert_no_token_anywhere(
                report, json.dumps(report, ensure_ascii=False),
                self._wrapper_text([fixture]))

    def test_R12b_digest_failure_degrades_without_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            fixture.unlink()
            with mock.patch(f"{__name__}._path_id",
                            side_effect=RuntimeError("digest fault")):
                report = collect_scan_report([fixture])
            record = report["scan_errors"][0]  # type: ignore[index]
            self.assertIn("<redacted>", record["path"])
            self.assertEqual("<path_id_unavailable>", record["path_id"])
            self.assertEqual("FAIL", report["gate_result"])
            self._assert_no_token_anywhere(
                report, json.dumps(report, ensure_ascii=False),
                self._wrapper_text([fixture]))

    def test_R12c_unclassifiable_component_is_not_emitted(self) -> None:
        # belt-and-braces: even a component the classifier cannot judge
        # (raised mid-scan) is dropped whole — never emitted raw
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = tmp / self._TOKEN / "notes.md"
            fixture.parent.mkdir()
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            fixture.unlink()  # unreadable after enumeration -> error record
            # only the PARENT component classification is broken
            real_scan_line = _scan_line

            def picky(line: str):
                if line == self._TOKEN:
                    raise RuntimeError("simulated component fault")
                return real_scan_line(line)

            with mock.patch(f"{__name__}._scan_line", side_effect=picky):
                report = collect_scan_report([fixture])
            errors = report["scan_errors"]  # type: ignore[union-attr]
            self.assertEqual(1, len(errors))  # type: ignore[arg-type]
            record = errors[0]  # type: ignore[index]
            # the flagged/unverifiable component is gone; the readable
            # basename of an in-scope-but-unreadable file may remain,
            # but the prohibited directory identity itself cannot
            self.assertNotIn(self._TOKEN, record["path"])
            self._assert_no_token_anywhere(
                report, json.dumps(report, ensure_ascii=False),
                self._wrapper_text([fixture]))

    # --- 15. deterministic correlation identity ------------------------------

    def test_R14_path_id_is_deterministic_and_value_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            fixture.unlink()
            other = tmp / "other" / self._BASENAME
            other.parent.mkdir()
            other.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            other.unlink()
            ids = [collect_scan_report([fixture])["scan_errors"][0][  # type: ignore[index]
                       "path_id"],
                   collect_scan_report([fixture])["scan_errors"][0][  # type: ignore[index]
                       "path_id"]]
            other_id = collect_scan_report([other])[  # type: ignore[index]
                "scan_errors"][0]["path_id"]  # type: ignore[index]
            # stable: the same path correlates to the same id across runs
            self.assertEqual(ids[0], ids[1])
            # discriminating: different paths never share an id
            self.assertNotEqual(ids[0], other_id)
            # digest-shaped and value-free
            for pid in (*ids, other_id):
                self.assertRegex(pid, r"^sha256:[0-9a-f]{16}$")
                self.assertNotIn(self._TOKEN, pid)
                self.assertNotIn(tmp.name, pid)

    # --- 16. no false PASS after redaction ------------------------------------

    def test_R15_redaction_never_softens_decisions(self) -> None:
        # (a) redaction of an unreadable pii path keeps FAIL/INCOMPLETE
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._CLEAN_CONTENT, encoding="utf-8")
            fixture.unlink()
            report = collect_scan_report([fixture])
            self.assertEqual("FAIL", report["gate_result"])
            self.assertFalse(report["scan_complete"])  # type: ignore[arg-type]
        # (b) a readable pii-named carrier still reports its prohibited
        # content — redaction changes the diagnostics, not the verdict
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fixture = self._pii_name_file(tmp)
            fixture.write_text(self._PAYLOAD, encoding="utf-8")
            report = collect_scan_report([fixture])
            self.assertEqual("FAIL", report["gate_result"])
            self.assertEqual({"telegram_chat_id", "personal_email"},
                             {f["category"] for f in  # type: ignore[union-attr]
                              report["findings"]})  # type: ignore[union-attr]
            self._assert_no_token_anywhere(
                report, json.dumps(report, ensure_ascii=False))
        # (c) a CLEAN tree still passes — redaction must not invert
        # the decision into an unconditional FAIL either
        clean_report = collect_scan_report(_tracked_files())
        self.assertEqual("PASS", clean_report["gate_result"])
        self.assertEqual(0, clean_report["unapproved_pii_count"])  # type: ignore[arg-type]


class TestR7GitFilenameDecodeIdentity(unittest.TestCase):
    """§9 bounded analysis regression: replacement decoding of Git
    tracked-file names must not alias distinct byte-level names onto
    one path identity.

    Baseline (repair SHA 70c7bed): `git ls-files -z` output was decoded
    with errors="replace", so a tracked name carrying an invalid UTF-8
    byte enumerated as the SAME string as a tracked name carrying the
    U+FFFD character itself. Deterministic consequence (defect proven by
    the R7 §9 analysis): the invalid-byte name's CONTENT was never read;
    if the U+FFFD twin is clean, the gate reported SCAN_COMPLETE and
    PASS while prohibited content sat in the unscanned file — a false
    PASS bypass. The enumerated identity must be bijective instead
    (`surrogateescape`): distinct raw names stay distinct, OS-level
    opens still resolve, and per-file content then follows the ordinary
    strict-UTF-8 contract.
    """

    maxDiff = None

    def _scratch_tracked_repo(self, files: dict[bytes, bytes]) -> Path:
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(scratch, ignore_errors=True))
        for raw_name, content in files.items():
            target = os.path.join(os.fsencode(scratch), raw_name)
            with open(target, "wb") as fh:
                fh.write(content)
        subprocess.run(["git", "init", "-q"], cwd=scratch,
                       capture_output=True, check=True)
        subprocess.run(["git", "add", "-f", "-A", "."], cwd=scratch,
                       capture_output=True, check=True)
        return scratch

    _PROHIBITED = ('deliver: "chat' + '_id": ' + SYNTH_CHAT + "\n"
                   + "page " + SYNTH_CONTACT_LOCAL + "@" + SYNTH_RELAY_DOMAIN
                   + "\n").encode("utf-8")

    def test_names_with_invalid_utf8_bytes_are_scanned_not_aliased(self) -> None:
        scratch = self._scratch_tracked_repo({
            # byte-level distinct names that replacement-decoding would
            # collapse into one string
            b"carrier\x80.md": self._PROHIBITED,          # invalid UTF-8
            ("carrier\N{REPLACEMENT CHARACTER}" + ".md"
             ).encode("utf-8"): b"clean\n",               # literal U+FFFD
        })
        with mock.patch(f"{__name__}.REPO_ROOT", scratch):
            enumerated = _tracked_files()
        # identity is bijective: two tracked names -> two distinct paths
        relative = sorted(str(p.relative_to(scratch)) for p in enumerated)
        self.assertEqual(2, len(relative))
        self.assertEqual(2, len(set(relative)))
        # and the REAL file contents are now reachable and scanned
        with mock.patch(f"{__name__}.REPO_ROOT", scratch):
            report = collect_scan_report(_tracked_files())
            self.assertIs(report["scan_complete"], True)
            self.assertGreaterEqual(report["unapproved_pii_count"], 1)  # type: ignore[operator]
            self.assertEqual("FAIL", report["gate_result"])
            categories = {f["category"]  # type: ignore[union-attr]
                          for f in report["findings"]}  # type: ignore[union-attr]
            self.assertIn("telegram_chat_id", categories)

    def test_invalid_utf8_name_single_file_is_scanned_fail_closed(self) -> None:
        # singleton case: pre-fix this name decoded to a nonexistent
        # replacement-char path and vanished into a read error; the
        # bijective decode now reads the real bytes — the content
        # contract then rules as usual (clean here -> PASS)
        scratch = self._scratch_tracked_repo({
            b"carrier\x80.md": b"clean content\n",
        })
        with mock.patch(f"{__name__}.REPO_ROOT", scratch):
            enumerated = _tracked_files()
            self.assertEqual(1, len(enumerated))
            report = collect_scan_report(enumerated)
        self.assertIs(report["scan_complete"], True)
        self.assertEqual("PASS", report["gate_result"])