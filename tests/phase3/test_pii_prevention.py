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
import re
import subprocess
import tempfile
import unittest
from bisect import bisect_right
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


def collect_findings(files: list[Path]) -> list[dict[str, object]]:
    """Scan the given files; return redacted findings records."""
    findings: list[dict[str, object]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, category, fp in _scan_text(text):
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


class TestRepoCleanOfOI07A(unittest.TestCase):
    """The gate itself: tracked content must be free of prohibited PII."""

    def test_tracked_tree_has_no_prohibited_identifiers(self) -> None:
        findings = collect_findings(_tracked_files())
        self.assertEqual([], findings, "PII/secret findings (redacted): "
                          + json.dumps(findings, ensure_ascii=False, indent=1))

    def test_failure_output_redacts_values(self) -> None:
        # Fragment-assembled address: it stays invisible to this file's own
        # tracked-source scan regardless of any future domain-rule change.
        address = "someone@" + "corp." + "example.com"
        snippet = _redact_snippet(
            f'"chat_id": "{SYNTH_CHAT}", "mail": ' + f'"{address}"')
        self.assertNotIn(SYNTH_CHAT, snippet)
        self.assertNotIn("someone@corp", snippet)
        self.assertIn("chat_id", snippet)


if __name__ == "__main__":
    unittest.main()