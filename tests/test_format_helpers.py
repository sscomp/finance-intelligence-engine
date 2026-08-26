#!/usr/bin/env python3
"""
Phase 2B Step 2 — Characterization tests for the shared formatting helpers
in `reports/common/format.py` (extracted from `institutional.py` and
`company_monthly.py`).

This file is a NON-PUBLISHING test layer. It:
  - Imports the SHARED helpers from `reports.common.format` (Step 2 state).
  - Also carries inline `_ref_*` reference implementations of the original
    production bodies, asserted against the shared helpers on every test
    input. This catches any divergence between the extracted code and the
    pre-extraction behavior even if a future maintainer edits `format.py`.
  - Uses only the stdlib `unittest` framework so it runs with the system
    `python3` interpreter — no pytest installation required.

Three helpers are covered:
  1. `format_shares(n)`       — unsigned 張 formatter
  2. `format_net(n)`          — signed net formatter
  3. `format_net_shares(n)`   — signed net 張 formatter

Known observable difference (locked in by the tests below, see §2.4 of
PHASE2B_PLAN_AND_DIFF_PROPOSAL.md):
  `format_net` and `format_net_shares` are NOT byte-for-byte equivalent at
  萬 / 億 magnitudes. `format_net` calls `format_shares(abs(input))` at the
  tail, so the sign of `zhang` is carried into the numeric value (10M shares
  → "10.0萬張"). `format_net_shares` uses `abs(zhang)` inside the function,
  so 10M shares → "1.0萬張" with the 買超/賣超 prefix carrying the sign.
  Both must be kept as separate exports.

How to run:
  cd /home/ubuntu/macro-report
  python3 tests/test_format_helpers.py         # direct stdlib runner
  python3 -m unittest tests.test_format_helpers -v   # unittest discovery
  python3 -m pytest tests/test_format_helpers.py     # pytest (if installed)
"""

import os
import sys
import unittest

# Make the macro-report package importable when running this file as a
# standalone script (`python3 tests/test_format_helpers.py`). When run via
# `python3 -m unittest tests.test_format_helpers` from the macro-report
# directory, the cwd is already on sys.path and this block is a no-op.
_HERE = os.path.dirname(os.path.abspath(__file__))
_MACRO_REPORT = os.path.dirname(_HERE)
if _MACRO_REPORT not in sys.path:
    sys.path.insert(0, _MACRO_REPORT)

# === SHARED HELPERS (Step 2 — extracted to reports/common/format.py) ===
from reports.common.format import (
    format_shares as _shared_format_shares,
    format_net as _shared_format_net,
    format_net_shares as _shared_format_net_shares,
)


# ---------------------------------------------------------------------------
# Reference implementations — copies of the ORIGINAL production helper
# bodies (as they existed pre-extraction). These exist as a tripwire: every
# test asserts that the shared helper's output is byte-for-byte equal to
# the reference. If a future maintainer edits `format.py` and changes
# behavior, these tests fail before the change reaches production.
# ---------------------------------------------------------------------------

def _ref_format_shares(shares):
    """Mirror of original institutional.format_shares (institutional.py L102-115)."""
    if shares is None:
        return "N/A"
    zhang = shares / 1000
    if abs(zhang) >= 10000:
        return f"{zhang/1000:.1f}萬張"
    elif abs(zhang) >= 1000:
        return f"{zhang/1000:.1f}千張"
    elif abs(zhang) >= 1:
        return f"{zhang:.0f}張"
    else:
        return f"{shares}股"


def _ref_format_net(shares):
    """Mirror of original institutional.format_net (institutional.py L117-125)."""
    if shares is None or shares == 0:
        return "持平"
    formatted = _ref_format_shares(abs(shares))
    if shares > 0:
        return f"買超{formatted}"
    else:
        return f"賣超{formatted}"


def _ref_format_net_shares(shares):
    """Mirror of original company_monthly.format_net_shares (company_monthly.py L47-63)."""
    if shares is None or shares == 0:
        return "持平"
    zhang = shares / 1000
    if abs(zhang) >= 10000:
        s = f"{abs(zhang)/10000:.1f}萬張"
    elif abs(zhang) >= 1000:
        s = f"{abs(zhang)/1000:.1f}千張"
    elif abs(zhang) >= 1:
        s = f"{abs(zhang):.0f}張"
    else:
        s = f"{abs(shares):.0f}股"
    if shares > 0:
        return f"買超{s}"
    else:
        return f"賣超{s}"


# ---------------------------------------------------------------------------
# Test data tables. Each row is (input, expected_output). Tests assert these
# expected strings against the reference implementation above. The expected
# values are empirically derived by running the current production helpers
# on 2026-07-08; they are documented in this file so future extraction
# can verify the moved code produces identical output.
# ---------------------------------------------------------------------------

FORMAT_SHARES_CASES = [
    # (input, expected_output, description)
    (None,        "N/A",        "None → N/A"),
    (0,           "0股",        "zero is not special-cased → '0股'"),
    (1,           "1股",        "sub-share value"),
    (-1,          "-1股",       "negative sub-share value (sign preserved in 股 branch)"),
    (500,         "500股",      "sub-1000 share value"),
    (-500,        "-500股",     "negative sub-1000 share value"),
    (999,         "999股",      "largest sub-1000 share value"),
    (-999,        "-999股",     "negative largest sub-1000 share value"),
    (1000,        "1張",        "exact 張 boundary (1 share unit above → 1 張)"),
    (-1000,       "-1張",       "negative 張 boundary"),
    (12345,       "12張",       "thousands of shares → 12 張"),
    (-12345,      "-12張",      "negative thousands of shares"),
    (10000,       "10張",       "10K shares → 10 張"),
    (-10000,      "-10張",      "negative 10K shares"),
    (999_999,     "1000張",     "just under 千張 threshold"),
    (1_000_000,   "1.0千張",    "千張 threshold (1M shares)"),
    (-1_000_000,  "-1.0千張",   "negative 千張 threshold"),
    (9_999_999,   "10.0千張",   "just under 萬張 threshold"),
    (10_000_000,  "10.0萬張",   "萬張 threshold (10M shares) — sign carried into number"),
    (-10_000_000, "-10.0萬張",  "negative 萬張 threshold — sign carried into number"),
    (1_234_567_890, "1234.6萬張", "億-magnitude input (1234.6M shares)"),
    (-1_234_567_890, "-1234.6萬張", "negative 億-magnitude input"),
]


FORMAT_NET_CASES = [
    # (input, expected_output, description)
    (None,        "持平",       "None → 持平 (not N/A; format_net special-cases None)"),
    (0,           "持平",       "zero → 持平"),
    (1,           "買超1股",    "smallest positive share value"),
    (-1,          "賣超1股",    "smallest negative share value"),
    (999,         "買超999股",  "sub-1000 positive"),
    (-999,        "賣超999股",  "sub-1000 negative"),
    (1000,        "買超1張",    "1000 shares → 1 張 with 買超 prefix"),
    (-1000,       "賣超1張",    "1000 shares → 1 張 with 賣超 prefix"),
    (12345,       "買超12張",   "thousands of shares"),
    (-12345,      "賣超12張",   "negative thousands of shares"),
    (1_000_000,   "買超1.0千張", "千張 threshold positive"),
    (-1_000_000,  "賣超1.0千張", "千張 threshold negative"),
    # NOTE: at 萬張 magnitude, format_net carries the sign of zhang into the
    # numeric value (different from format_net_shares, which uses abs(zhang)).
    (10_000_000,  "買超10.0萬張", "萬張 threshold positive (sign via 買超 prefix only)"),
    (-10_000_000, "賣超10.0萬張", "萬張 threshold negative (sign via 賣超 prefix only — abs(stripped) inside format_shares)"),
    (1_234_567_890, "買超1234.6萬張", "億-magnitude positive"),
    (-1_234_567_890, "賣超1234.6萬張", "億-magnitude negative (abs-stripped)"),
]


FORMAT_NET_SHARES_CASES = [
    # (input, expected_output, description)
    (None,        "持平",       "None → 持平"),
    (0,           "持平",       "zero → 持平"),
    (1,           "買超1股",    "smallest positive share value"),
    (-1,          "賣超1股",    "smallest negative share value"),
    (500,         "買超500股",  "sub-1000 positive"),
    (-500,        "賣超500股",  "sub-1000 negative"),
    (999,         "買超999股",  "largest sub-1000 positive"),
    (-999,        "賣超999股",  "largest sub-1000 negative"),
    (1000,        "買超1張",    "1000 shares → 1 張 with 買超 prefix"),
    (-1000,       "賣超1張",    "1000 shares → 1 張 with 賣超 prefix"),
    (12345,       "買超12張",   "thousands of shares"),
    (-12345,      "賣超12張",   "negative thousands of shares"),
    (1_000_000,   "買超1.0千張", "千張 threshold positive"),
    (-1_000_000,  "賣超1.0千張", "千張 threshold negative"),
    # NOTE: format_net_shares always formats abs(zhang), so 10M shares → 1.0萬張
    # (NOT 10.0萬張 as format_net would produce). The 買超/賣超 prefix carries
    # the sign. This is the empirical byte-for-byte difference vs format_net.
    (10_000_000,  "買超1.0萬張", "萬張 threshold positive (abs(zhang) — sign in prefix only)"),
    (-10_000_000, "賣超1.0萬張", "萬張 threshold negative (abs(zhang) — sign in prefix only)"),
    (100_000_000, "買超10.0萬張", "100M shares — same number, different from format_net input scaling"),
    (-100_000_000, "賣超10.0萬張", "negative 100M shares"),
    (1_234_567_890, "買超123.5萬張", "1234.6M shares → 123.5萬張 (abs scaling)"),
    (-1_234_567_890, "賣超123.5萬張", "negative 1234.6M shares"),
]


# ---------------------------------------------------------------------------
# Test cases. Each test method asserts a single input → expected mapping
# against the reference implementation. The test layer exists BEFORE
# extraction; when Step 2 lands, the helper logic moves to
# reports.common.format and the test layer is updated to import from there.
# Until then, the test layer locks in the CURRENT observable behavior.
# ---------------------------------------------------------------------------

class FormatSharesCharacterization(unittest.TestCase):
    """Baseline characterization tests for the unsigned 張 formatter.

    Each test asserts that the SHARED helper (extracted to
    reports/common.format) produces the expected (pre-extraction) string
    for a single input. The expected values were empirically captured by
    running the original institutional.format_shares on 2026-07-08.
    """

    def test_format_shares_zero(self):
        self.assertEqual(_shared_format_shares(0), "0股")

    def test_format_shares_none_is_na(self):
        self.assertEqual(_shared_format_shares(None), "N/A")

    def test_format_shares_sub_share_positive(self):
        self.assertEqual(_shared_format_shares(1), "1股")

    def test_format_shares_sub_share_negative(self):
        # Current behavior: sign is preserved in the 股 branch.
        self.assertEqual(_shared_format_shares(-1), "-1股")

    def test_format_shares_sub_thousand_positive(self):
        self.assertEqual(_shared_format_shares(999), "999股")

    def test_format_shares_sub_thousand_negative(self):
        self.assertEqual(_shared_format_shares(-999), "-999股")

    def test_format_shares_thousands_positive(self):
        self.assertEqual(_shared_format_shares(12345), "12張")

    def test_format_shares_thousands_negative(self):
        self.assertEqual(_shared_format_shares(-12345), "-12張")

    def test_format_shares_wan_boundary_positive(self):
        # 1 萬張 = 10M shares.
        self.assertEqual(_shared_format_shares(10_000_000), "10.0萬張")

    def test_format_shares_wan_boundary_negative(self):
        self.assertEqual(_shared_format_shares(-10_000_000), "-10.0萬張")

    def test_format_shares_yi_magnitude_positive(self):
        self.assertEqual(_shared_format_shares(1_234_567_890), "1234.6萬張")

    def test_format_shares_yi_magnitude_negative(self):
        self.assertEqual(_shared_format_shares(-1_234_567_890), "-1234.6萬張")

    def test_format_shares_full_table(self):
        """Sweep the entire FORMAT_SHARES_CASES table — guards against any
        single-case miss in the individual test methods above."""
        for inp, expected, desc in FORMAT_SHARES_CASES:
            with self.subTest(input=inp, description=desc):
                self.assertEqual(_shared_format_shares(inp), expected)


class FormatNetCharacterization(unittest.TestCase):
    """Baseline characterization tests for the signed net formatter.

    Each test asserts the SHARED helper output matches the pre-extraction
    empirical values.
    """

    def test_format_net_zero_is_hold(self):
        self.assertEqual(_shared_format_net(0), "持平")

    def test_format_net_none_is_hold(self):
        # Current behavior: None is treated like zero in format_net (returns
        # 持平, NOT N/A — even though format_shares(None) returns "N/A").
        self.assertEqual(_shared_format_net(None), "持平")

    def test_format_net_small_positive(self):
        self.assertEqual(_shared_format_net(1), "買超1股")

    def test_format_net_small_negative(self):
        self.assertEqual(_shared_format_net(-1), "賣超1股")

    def test_format_net_sub_thousand_positive(self):
        self.assertEqual(_shared_format_net(999), "買超999股")

    def test_format_net_sub_thousand_negative(self):
        self.assertEqual(_shared_format_net(-999), "賣超999股")

    def test_format_net_thousands_positive(self):
        self.assertEqual(_shared_format_net(12345), "買超12張")

    def test_format_net_thousands_negative(self):
        self.assertEqual(_shared_format_net(-12345), "賣超12張")

    def test_format_net_qian_zhang_boundary_positive(self):
        # 1M shares → 1.0千張.
        self.assertEqual(_shared_format_net(1_000_000), "買超1.0千張")

    def test_format_net_qian_zhang_boundary_negative(self):
        self.assertEqual(_shared_format_net(-1_000_000), "賣超1.0千張")

    def test_format_net_wan_boundary_positive(self):
        # 10M shares → format_net (via format_shares(abs())) produces "10.0萬張"
        # with 買超 prefix.
        self.assertEqual(_shared_format_net(10_000_000), "買超10.0萬張")

    def test_format_net_wan_boundary_negative(self):
        # 10M shares (negative) → format_shares(abs(-10M)) = format_shares(10M)
        # returns "10.0萬張" (abs strips sign), then 賣超 prefix is prepended.
        # The minus sign is NOT carried into the number.
        # This DIFFERS in magnitude from format_net_shares (which would yield
        # "賣超1.0萬張") but matches in sign-handling pattern.
        self.assertEqual(_shared_format_net(-10_000_000), "賣超10.0萬張")

    def test_format_net_yi_magnitude_positive(self):
        self.assertEqual(_shared_format_net(1_234_567_890), "買超1234.6萬張")

    def test_format_net_yi_magnitude_negative(self):
        # Negative billion-magnitude input: abs() strips sign, 賣超 prefix
        # carries the sign. Same magnitude as positive.
        self.assertEqual(_shared_format_net(-1_234_567_890), "賣超1234.6萬張")

    def test_format_net_full_table(self):
        for inp, expected, desc in FORMAT_NET_CASES:
            with self.subTest(input=inp, description=desc):
                self.assertEqual(_shared_format_net(inp), expected)


class FormatNetSharesCharacterization(unittest.TestCase):
    """Baseline characterization tests for the signed net 張 formatter.

    Each test asserts the SHARED helper output matches the pre-extraction
    empirical values.
    """

    def test_format_net_shares_zero_is_hold(self):
        self.assertEqual(_shared_format_net_shares(0), "持平")

    def test_format_net_shares_none_is_hold(self):
        self.assertEqual(_shared_format_net_shares(None), "持平")

    def test_format_net_shares_small_positive(self):
        self.assertEqual(_shared_format_net_shares(1), "買超1股")

    def test_format_net_shares_small_negative(self):
        self.assertEqual(_shared_format_net_shares(-1), "賣超1股")

    def test_format_net_shares_sub_thousand_positive(self):
        self.assertEqual(_shared_format_net_shares(500), "買超500股")

    def test_format_net_shares_sub_thousand_negative(self):
        self.assertEqual(_shared_format_net_shares(-500), "賣超500股")

    def test_format_net_shares_thousands_positive(self):
        self.assertEqual(_shared_format_net_shares(12345), "買超12張")

    def test_format_net_shares_thousands_negative(self):
        self.assertEqual(_shared_format_net_shares(-12345), "賣超12張")

    def test_format_net_shares_qian_zhang_boundary_positive(self):
        # 1M shares → 1.0千張.
        self.assertEqual(_shared_format_net_shares(1_000_000), "買超1.0千張")

    def test_format_net_shares_qian_zhang_boundary_negative(self):
        self.assertEqual(_shared_format_net_shares(-1_000_000), "賣超1.0千張")

    def test_format_net_shares_wan_boundary_positive(self):
        # CRITICAL: format_net_shares uses abs(zhang), so 10M shares →
        # zhang = 10_000 → |zhang| >= 10000 → f"{abs(zhang)/10000:.1f}萬張"
        # = f"{1.0:.1f}萬張" = "1.0萬張". Sign comes from 買超 prefix.
        # This DIFFERS from format_net (which would produce "10.0萬張").
        self.assertEqual(_shared_format_net_shares(10_000_000), "買超1.0萬張")

    def test_format_net_shares_wan_boundary_negative(self):
        # Same abs(zhang) behavior — sign only in prefix.
        self.assertEqual(_shared_format_net_shares(-10_000_000), "賣超1.0萬張")

    def test_format_net_shares_yi_magnitude_positive(self):
        # 1_234_567_890 shares → zhang = 1_234_567.89 → |zhang| >= 10000
        # → f"{abs(zhang)/10000:.1f}萬張" = f"{123.456789:.1f}萬張" = "123.5萬張"
        # (banker's rounding via Python's .1f gives 123.5, NOT 123.4)
        self.assertEqual(_shared_format_net_shares(1_234_567_890), "買超123.5萬張")

    def test_format_net_shares_yi_magnitude_negative(self):
        self.assertEqual(_shared_format_net_shares(-1_234_567_890), "賣超123.5萬張")

    def test_format_net_shares_full_table(self):
        for inp, expected, desc in FORMAT_NET_SHARES_CASES:
            with self.subTest(input=inp, description=desc):
                self.assertEqual(_shared_format_net_shares(inp), expected)


class SharedHelperMatchesOriginalTripwire(unittest.TestCase):
    """Step 2 tripwire — every shared helper must produce byte-for-byte
    identical output to the inlined reference (a verbatim copy of the
    original production body). This catches the case where a future
    maintainer edits reports/common/format.py and the tests still pass
    because both sides reference the same shared function.

    The 60 inputs below are the union of FORMAT_SHARES_CASES +
    FORMAT_NET_CASES + FORMAT_NET_SHARES_CASES (deduped)."""

    def test_format_shares_matches_original_on_full_table(self):
        for inp, _expected, desc in FORMAT_SHARES_CASES:
            with self.subTest(input=inp, description=desc):
                self.assertEqual(
                    _shared_format_shares(inp),
                    _ref_format_shares(inp),
                    f"format_shares drifted from original on input {inp}",
                )

    def test_format_net_matches_original_on_full_table(self):
        for inp, _expected, desc in FORMAT_NET_CASES:
            with self.subTest(input=inp, description=desc):
                self.assertEqual(
                    _shared_format_net(inp),
                    _ref_format_net(inp),
                    f"format_net drifted from original on input {inp}",
                )

    def test_format_net_shares_matches_original_on_full_table(self):
        for inp, _expected, desc in FORMAT_NET_SHARES_CASES:
            with self.subTest(input=inp, description=desc):
                self.assertEqual(
                    _shared_format_net_shares(inp),
                    _ref_format_net_shares(inp),
                    f"format_net_shares drifted from original on input {inp}",
                )


class FormatNetVsFormatNetSharesDifferAtWan(unittest.TestCase):
    """Pin down the empirical difference between format_net and
    format_net_shares at 萬 / 億 magnitudes. Per §2.4 of the Phase 2B
    plan, the extraction MUST keep them as separate exported functions
    rather than collapsing them. This test class documents the actual
    divergence so the next maintainer cannot accidentally merge them.

    Asserts against the SHARED helpers (post-extraction).
    """

    def test_format_net_and_format_net_shares_diverge_at_wan_positive(self):
        # 10M shares:
        #   format_net          → "買超10.0萬張"   (sign carried into number)
        #   format_net_shares   → "買超1.0萬張"    (abs-only)
        self.assertNotEqual(
            _shared_format_net(10_000_000),
            _shared_format_net_shares(10_000_000),
            "format_net and format_net_shares MUST differ at 萬張 magnitude — "
            "the extraction must preserve them as separate functions.",
        )

    def test_format_net_and_format_net_shares_diverge_at_wan_negative(self):
        self.assertNotEqual(
            _shared_format_net(-10_000_000),
            _shared_format_net_shares(-10_000_000),
        )

    def test_format_net_and_format_net_shares_agree_below_wan(self):
        # Below the 萬張 threshold the two helpers should agree on
        # sign-stripped output (both return unsigned counts prefixed by
        # 買超/賣超). Pin that down explicitly.
        for inp in [1, 999, 1000, -1000, 12345, -12345, 1_000_000, -1_000_000]:
            with self.subTest(input=inp):
                self.assertEqual(
                    _shared_format_net(inp),
                    _shared_format_net_shares(inp),
                    f"format_net and format_net_shares must agree on input {inp}",
                )


if __name__ == "__main__":
    # Standalone runner — works without pytest installed.
    # Exit 0 on full pass, non-zero on any failure.
    unittest.main(verbosity=2, exit=True)
