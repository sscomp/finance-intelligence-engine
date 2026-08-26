#!/usr/bin/env python3
"""
Shared formatting helpers for macro-report.

Phase 2B Step 2 — extracted from:
  - institutional.py (format_shares, format_net)
  - company_monthly.py (format_net_shares)

Stdlib only. No I/O. No logging. No external state. Pure functions.

IMPORTANT (per PHASE2B_PLAN_AND_DIFF_PROPOSAL.md §2.4 and the
characterization tests in tests/test_format_helpers.py):

`format_net` and `format_net_shares` are NOT byte-for-byte equivalent at
萬 / 億 magnitudes. Both are preserved as separate exports so a future
change to one cannot silently change the other.

  - `format_net`          — uses format_shares(abs(input)) at the tail,
                            so the sign of `zhang` is carried into the
                            numeric value. 10M shares → "10.0萬張".
  - `format_net_shares`   — uses abs(zhang) inside the function, so
                            10M shares → "1.0萬張" with the 買超/賣超
                            prefix carrying the sign.

Both must be kept as separate exports. Do not collapse them.
"""


def format_shares(shares):
    """Format share count in 張 (1張 = 1000股).

    Unsigned 張 formatter. None → "N/A". Zero is not special-cased
    (returns "0股"). Sign is preserved in the 股 branch only
    (e.g. -1 → "-1股"), but the 張 / 千張 / 萬張 branches operate on
    the raw (signed) zhang value.

    Extracted from institutional.py (L102-115) — preserved verbatim.
    """
    if shares is None:
        return "N/A"
    # Convert to 張
    zhang = shares / 1000
    if abs(zhang) >= 10000:
        return f"{zhang/1000:.1f}萬張"
    elif abs(zhang) >= 1000:
        return f"{zhang/1000:.1f}千張"
    elif abs(zhang) >= 1:
        return f"{zhang:.0f}張"
    else:
        return f"{shares}股"


def format_net(shares):
    """Format net buy/sell with 買超/賣超 prefix.

    Special-cases None and zero to "持平" (NOT "N/A" like format_shares).
    Calls format_shares(abs(shares)) at the tail — sign of `zhang` is
    therefore carried into the numeric value at 萬 / 億 magnitudes.

    Extracted from institutional.py (L117-125) — preserved verbatim.
    """
    if shares is None or shares == 0:
        return "持平"
    formatted = format_shares(abs(shares))
    if shares > 0:
        return f"買超{formatted}"
    else:
        return f"賣超{formatted}"


def format_net_shares(shares):
    """Format net buy/sell in 張 (1張=1000股).

    Structurally similar to `format_net` but computes magnitude from
    `abs(zhang)` (NOT from `format_shares(abs(input))`), so the sign of
    `zhang` is never carried into the numeric value. The 買超/賣超
    prefix carries the sign instead.

    Extracted from company_monthly.py (L47-63) — preserved verbatim.
    """
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


__all__ = ["format_shares", "format_net", "format_net_shares"]
