#!/usr/bin/env python3
"""DB target identity helper for the rehearsal guard (thin adapter, 6.8A).

The canonical DSN-identity engine now lives in
``phase3/runtime_contract.py`` (single authoritative implementation —
WO Task C prohibits two independent precedence engines). This script is
only the stdin-protocol subprocess boundary the bash guard uses:

    CANDIDATE <dsn>
    PROD <dsn>          (repeated; the production contract identities)

stdout lines: STATUS ok | EQUIVALENT 0|1 | CAND_FP <sha256[:16]> |
PROD_FP <sha256[:16]>. Exit codes: 0 parsed, 2 CANDIDATE unparseable,
3 PROD unparseable, 4 no CANDIDATE. No DSN text or credential is ever
echoed — fingerprints only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from phase3.runtime_contract import (  # noqa: E402,F401  (re-exports)
    BACKEND_POSTGRES,
    BACKEND_SQLITE,
    DEFAULT_PORT,
    PG_URL_PREFIXES,
    Unparseable,
    identity_fingerprint,
    identity_text,
    parse_postgres_dsn,
)


def equivalent(a: dict, b: dict) -> bool:
    """Semantic equivalence of two parsed identity dicts (no secrets)."""
    return identity_text(a) == identity_text(b)


def main() -> int:
    candidate = None
    prods = []
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if line.startswith("CANDIDATE "):
            candidate = line[len("CANDIDATE "):]
        elif line.startswith("PROD "):
            prods.append(line[len("PROD "):])
    if candidate is None:
        print("STATUS no_candidate")
        return 4
    try:
        cand = parse_postgres_dsn(candidate)
    except Unparseable:
        print("STATUS unparseable_candidate")
        return 2
    parsed_prods = []
    for prod in prods:
        try:
            parsed_prods.append(parse_postgres_dsn(prod))
        except Unparseable:
            print("STATUS unparseable_prod")
            return 3
    eq = any(equivalent(cand, p) for p in parsed_prods)
    print("STATUS ok")
    print("EQUIVALENT", 1 if eq else 0)
    print("CAND_FP", identity_fingerprint(cand))
    for p in parsed_prods:
        print("PROD_FP", identity_fingerprint(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())