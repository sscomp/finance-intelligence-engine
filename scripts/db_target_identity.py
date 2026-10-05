#!/usr/bin/env python3
"""DB target identity helper for the rehearsal guard (2026-10-05 incident closure).

Reads whitespace-free lines from stdin::

    CANDIDATE <dsn>
    PROD <dsn>          (repeated; the production contract identities)

and reports on stdout whether the CANDIDATE PostgreSQL DSN is semantically
equivalent to any PROD identity, per the target-safety contract (WO §7):
the comparison is over the resolved backend identity — scheme class, host
(local aliases normalized), port, database — NEVER the password (S3).

DSN forms supported (all whitespace-free; the guard rejects whitespace
upstream)::

    postgresql://user:pass@host:5432/dbname?sslmode=...
    postgres://user@host/dbname
    postgresql:///dbname?host=/path/to/socket&port=5432   (unix-socket dir
      host; query params may carry host/port/dbname in any order)
    "host=127.0.0.1 port=5432 dbname=fie_prod"            (libpq keyword form;
      dbname/database keys both accepted)

Normalization: localhost/127.0.0.0-255/::1/0.0.0.0 coalesce to ``loopback``;
IPv6 brackets are stripped; %-escapes are decoded; an absent port defaults
to 5432 for URL and keyword forms; a socket-dir host is compared as its
normalized absolute path. A DSN whose production identity cannot be parsed
is treated as EQUIVALENT by the caller (fail closed, S6) via its exit code.

Exit codes: 0 = parsed (see stdout), 2 = CANDIDATE unparseable,
3 = PROD line unparseable, 4 = no CANDIDATE line given.

stdout lines::

    STATUS ok
    EQUIVALENT 0|1
    CAND_FP <sha256[:16]>           # fingerprint of the candidate identity
    PROD_FP <sha256[:16]>           # one per PROD line

No DSN text, credential, or raw component is echoed — fingerprints only
(the caller's own stderr discipline keeps secrets out of logs).
"""
import hashlib
import shlex
import sys
from urllib.parse import unquote, urlsplit

_LOOPBACK_HOSTS = frozenset({
    "localhost", "::1", "0.0.0.0", "[::1]",
    "::ffff:127.0.0.1",
})

DEFAULT_PORT = "5432"


class Unparseable(Exception):
    """The text does not fit any supported PostgreSQL DSN form."""


def _normalize_host(host: str) -> str:
    host = unquote(host).strip("[]").strip()
    if not host:
        raise Unparseable("empty host")
    if host in _LOOPBACK_HOSTS or host.startswith("127."):
        return "loopback"
    if host.startswith("/"):
        # unix-socket directory: identity is the canonical directory path
        return "socket:" + (host.rstrip("/") or "/")
    return host.lower()


def _parse_url_form(text: str) -> dict:
    if not text.lower().startswith(("postgres://", "postgresql://")):
        raise Unparseable("not a postgres URL")
    parts = urlsplit(text)
    params: dict = {}
    if parts.query:
        for chunk in parts.query.split("&"):
            if not chunk:
                continue
            key, _, value = chunk.partition("=")
            params[unquote(key).lower()] = unquote(value)
    userinfo, _, hostport = parts.netloc.rpartition("@")
    host = hostport
    port_from_url = ""
    if hostport.startswith("[") and "]" in hostport:  # IPv6 literal
        host, _, rest = hostport.rpartition("]")
        host = "[" + host[1:] + "]"
        port_from_url = rest[1:] if rest.startswith(":") else ""
    elif hostport.count(":") == 1:
        host, _, port_from_url = hostport.partition(":")
    elif hostport.count(":") > 1:
        # An unbracketed bare IPv6 literal does not fit the URL grammar;
        # mis-reading it would corrupt the host identity — fail closed.
        raise Unparseable("bare IPv6 host without brackets")
    dbname = unquote(parts.path)[1:] if parts.path else ""
    dbname = params.get("dbname") or params.get("database") or dbname
    host = params.get("host") or host
    port = params.get("port") or port_from_url or DEFAULT_PORT
    if not dbname:
        raise Unparseable("no database selected")
    if not host:
        raise Unparseable("no host or socket directory given")
    try:
        port = str(int(port))
    except ValueError:
        raise Unparseable("non-numeric port") from None
    return {
        "kind": "postgres",
        "host": _normalize_host(host),
        "port": port,
        "dbname": dbname,
    }


def _parse_keyword_form(text: str) -> dict:
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise Unparseable(f"bad keyword DSN ({exc})") from None
    if not tokens:
        raise Unparseable("empty DSN")
    pairs: dict = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep or not key or not value:
            raise Unparseable("not in key=value form")
        pairs[key.lower()] = value
    if not pairs:
        raise Unparseable("no recognized keys")
    if not any(key in pairs for key in ("host", "dbname", "database",
                                        "port")):
        raise Unparseable("no recognized postgres keys")
    dbname = pairs.get("dbname") or pairs.get("database") or ""
    host = pairs.get("host", "")
    port = pairs.get("port", DEFAULT_PORT)
    if not dbname:
        raise Unparseable("no database selected")
    if not host:
        raise Unparseable("no host or socket directory given")
    try:
        port = str(int(port))
    except ValueError:
        raise Unparseable("non-numeric port") from None
    return {
        "kind": "postgres",
        "host": _normalize_host(host),
        "port": port,
        "dbname": dbname,
    }


def parse_postgres_dsn(text: str) -> dict:
    """Parse a PostgreSQL DSN in URL or keyword form into its identity."""
    text = text.strip()
    if not text:
        raise Unparseable("empty")
    if text.lower().startswith(("postgres://", "postgresql://")):
        return _parse_url_form(text)
    if "=" in text and "://" not in text:
        return _parse_keyword_form(text)
    raise Unparseable("unrecognized DSN form")


def identity_text(identity: dict) -> str:
    return "{kind}@{host}@{port}@{dbname}".format(**identity)


def identity_fingerprint(identity: dict) -> str:
    return hashlib.sha256(identity_text(identity).encode()).hexdigest()[:16]


def equivalent(a: dict, b: dict) -> bool:
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