"""Automated PII/secret prevention gate (Phase 6.7A-R1, OI-07a).

Scans **tracked repository content** (``git ls-files``) for prohibited
identifier/secret shapes and fails with redacted, path-anchored output
— the raw matched value is never printed. Purpose and resolution
policy: ADR-016's security exception clause applies to *content*
integrity; this gate is the prevention half of the OI-07a remediation
(Phase 6.7A-R1).

Design constraints (work order §7):

* deterministic, offline, stdlib-only;
* content-keyed (renaming/moving a file does not evade it);
* narrow, reviewable allowlist (empty by default);
* detects at minimum the OI-07a trigger category (Telegram chat IDs),
  plus high-confidence secret shapes within the same policy:
  bot-token forms, private-key markers, cloud/GitHub/OpenAI/Slack
  API-key shapes, phone-number-shaped personal contacts, and
  personal email addresses.

Documented, narrow exclusions (kept in repo, reviewable):

* numbers directly preceded by a *message-id* field (Telegram message
  IDs are per-message receipts, not account identifiers);
* email-shaped text inside URIs (``scheme://…user:pass@host`` is
  credential material governed by secret policy, not a contact email);
* synthetic/example contact domains (``example.invalid/`` etc.) and
  no-reply addresses.

The detector's own unit tests assemble synthetic prohibited values at
runtime from separated string fragments so this tracked source stays
clean under the gate itself. The work-order §8 negative control
(temporarily injecting a synthetic identifier into a tracked file,
expecting FAIL, then restoring byte-identically) is exercised as a
separate evidence step against this same gate.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path

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
# Chat-ID context must exist on the SAME line (precision over recall;
# near-line context produced 8 false positives on historical reports).
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
    r"(?i)(message[_\s-]?id|msg_?id|telegram_msg_id)\s*[=:]?\s*[\"']?\d*$")
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
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
_SYNTHETIC_EMAIL_DOMAIN = re.compile(
    r"(?i)(@example\.(com|invalid|org|net|test)"
    r"|@(synthetic|placeholder|test)[-.]"
    r"|noreply@|no-reply@|users\.noreply\.github\.com$)")


def _left_context(line: str, start: int, width: int = 20) -> str:
    return line[max(0, start - width):start]


def _is_excluded_number(line: str, start: int) -> bool:
    left = _left_context(line, start)
    return bool(_MESSAGE_ID_LEFT.search(left))


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
    if not re.search(r"://", line):
        for match in _EMAIL.finditer(line):
            local, domain = match.group(1), match.group(2)
            if _SYNTHETIC_EMAIL_DOMAIN.search("@" + domain + "\n") or \
               _SYNTHETIC_EMAIL_DOMAIN.search(_left_context(line, match.start(2)) + "@" + domain):
                continue
            if re.search(r"(?i)(noreply|no[-_]?reply|@example)", local + "@" + domain):
                continue
            out.append(("personal_email", _fp(match.group(0))))
    for match in _PHONE.finditer(line):
        out.append(("phone_number_shape", _fp(match.group(1))))
    return out


def collect_findings(files: list[Path]) -> list[dict[str, object]]:
    """Scan the given files; return redacted findings records."""
    findings: list[dict[str, object]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for category, fp in _scan_line(line):
                findings.append({
                    "path": str(path),
                    "line": lineno,
                    "category": category,
                    "fingerprint": fp,
                    "raw_value_recorded": False,
                })
    allowed = {entry["fingerprint_prefix"] for entry in ALLOWLIST}
    return [f for f in findings
            if not any(f["fingerprint"] == fp or f["fingerprint"].startswith(fp)
                       for fp in allowed)]


def _tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                         capture_output=True, check=True)
    names = out.stdout.decode("utf-8", "replace").split("\0")
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
# values at runtime).
SYNTH_CHAT = "7" "6543" "21098"
SYNTH_BOT = "123" "4567890:AA" "E" + ("c" * 30)
SYNTH_AWS = "AKIA" + "Q3M4VQYR" + "ZT8W" + "2NE1"
SYNTH_GH = "ghp_" + ("x" * 20) + "9"
SYNTH_OPENAI = "sk-" + ("a" * 20) + "7"
SYNTH_SLACK = "xoxb-" + "123456-" + "abcdef"
SYNTH_PHONE = "09871" + "23456"


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
                     "noreply@example.org"):
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


class TestRepoCleanOfOI07A(unittest.TestCase):
    """The gate itself: tracked content must be free of prohibited PII."""

    def test_tracked_tree_has_no_prohibited_identifiers(self) -> None:
        findings = collect_findings(_tracked_files())
        self.assertEqual([], findings, "PII/secret findings (redacted): "
                          + json.dumps(findings, ensure_ascii=False, indent=1))

    def test_failure_output_redacts_values(self) -> None:
        snippet = _redact_snippet(
            f'"chat_id": "{SYNTH_CHAT}", "mail": "someone@corp.example.com"')
        self.assertNotIn(SYNTH_CHAT, snippet)
        self.assertNotIn("someone@corp", snippet)
        self.assertIn("chat_id", snippet)


if __name__ == "__main__":
    unittest.main()