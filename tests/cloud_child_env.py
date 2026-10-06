#!/usr/bin/env python3
"""Canonical test-child environment construction (6.9A-R4-R2-R1, Tasks C/D).

ONE helper for the suite's scrubbed-child environments. Two contracts:

1. SCRAPE/SCRUB (Task C): a test child NEVER receives the whole parent
   environment. Environment construction is explicit: a fixed seed
   (PATH/PWD/HOME/TMPDIR) plus the NETWORK-RUNTIME allowlist — only the
   names the repository's supported HTTP/TLS stack (curl/wget, PyYAML,
   yfinance/requests, python-ssl) can consume for artifact acquisition:

       HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY (+ lowercase spellings)
       SSL_CERT_FILE  SSL_CERT_DIR  REQUESTS_CA_BUNDLE  CURL_CA_BUNDLE

   Values may be credential-bearing inside a proxy URL: they are copied
   into the child environment but are NEVER logged, printed or compared by
   value; tests assert names/redacted metadata only (WO §8 C3).
   Secrets (FIE_* contracts, PGPASSWORD, API/GitHub/cloud tokens) NEVER
   propagate — the allowlist is closed over the network-runtime names only.

2. CACHE VISIBILITY (Task D): the ACTIVE cache state is resolved by
   executing the shipped canonical resolver/provisioner
   (``scripts/provision-test-postgres.sh --cache-state``) — the test
   helpers are NOT a second resolution engine and never assume
   ``$HOME/.cache/...`` when the runtime selected another location.

Not a test module (name does not match the unittest discovery pattern);
it is imported by the network-context / cache-visibility test files.
"""
from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROVISIONER = REPO / "scripts" / "provision-test-postgres.sh"

# WO §6 A2 candidate categories — names proven/intentionally supported by
# the acquisition stack (curl/wget honor the four proxy spellings and the
# CA pair; python-ssl honors SSL_CERT_*/REQUESTS_CA_BUNDLE).
NETWORK_RUNTIME_ALLOWLIST: tuple[str, ...] = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)

# Names deliberately REMOVED from every scrubbed child regardless of any
# parent state (secret boundary, WO §8 C3).
SECRET_BOUNDARY_DENYLIST: tuple[str, ...] = (
    "PGPASSWORD", "POSTGRES_PASSWORD", "DATABASE_URL",
    "FIE_INTELLIGENCE_DB", "FIE_DB_PATH", "FIE_DATABASE_URL",
    "FIE_DB_TARGET_INTELLIGENCE", "FIE_DB_TARGET_RAW",
    "FIE_TEST_PG_DSN", "GITHUB_TOKEN", "GH_TOKEN", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS",
)

DEFAULT_SEED = {
    "PATH": "/usr/bin:/bin",
}


def scrubbed_child_seed(home: str | None, tmpdir: str | None,
                        blob: dict | None = None) -> dict:
    """Deterministic scrubbed child seed: PATH + caller-named HOME/TMPDIR."""
    env = dict(DEFAULT_SEED)
    env["PWD"] = str(REPO)
    if home:
        env["HOME"] = str(home)
    if tmpdir:
        env["TMPDIR"] = str(tmpdir)
    if blob:
        env.update(blob)
    return env


def network_context(parent_env: dict | None = None) -> dict:
    """The parent's network-runtime context, filtered to the explicit
    allowlist. Values are carried opaquely (never logged)."""
    env = os.environ if parent_env is None else {k: v for k, v in
                                                 parent_env.items()}
    return {name: env[name] for name in NETWORK_RUNTIME_ALLOWLIST
            if name in env}


def child_env(home: str | None = None, tmpdir: str | None = None,
              blob: dict | None = None,
              *, with_network_context: bool = True,
              network_context_map: dict | None = None,
              parent_env: dict | None = None) -> dict:
    """Build a scrubbed test-child environment.

    Base = explicit seed only. When ``with_network_context`` is true, the
    allowlisted network-runtime names are PROPAGATED (from the live parent
    or from an explicitly supplied map). Nothing else leaks: no FIE_*
    contract variable and no secret is ever implicitly inherited — only an
    explicit ``blob`` entry can name one.
    """
    env = scrubbed_child_seed(home, tmpdir, blob)
    if with_network_context:
        ctx = network_context_map if network_context_map is not None \
            else network_context(parent_env)
        for name in ctx:
            env[name] = ctx[name]
    return env


def redact_network_env(netenv: dict) -> dict:
    """Redacted metadata view for tests/evidence: per name, PRESENT plus the
    credential classification — never a value (WO §8 C3)."""
    cred_re = re.compile(r"://[^/@:]+:")
    return {name: ("present;credential-bearing-url"
                   if cred_re.search(value)
                   else "present;no-embedded-credential")
            for name, value in sorted(netenv.items())}


# --------------------------------------------------------------------------
# Canonical cache-state bridge (Task D)
# --------------------------------------------------------------------------

_CACHED_STATE = None


def canonical_cache_state(force_refresh: bool = False) -> dict:
    """Resolve the ACTIVE cache by executing the SHIPPED resolver.

    Returns {'dir','mode','persistent','warm','lock_class'} (or raises
    RuntimeError with the classifier's failure text). In temp mode the
    resolved dir is per-invocation — the helper only relies on
    mode/persistent/warm semantics; it never re-derives a path from HOME.
    """
    global _CACHED_STATE
    if _CACHED_STATE is not None and not force_refresh:
        return dict(_CACHED_STATE)
    seed = scrubbed_child_seed(None, None)
    for name in CACHE_VISIBILITY_PASSTHRU:  # parent cache context, verbatim
        if name in os.environ and os.environ[name]:
            seed[name] = os.environ[name]
    res = subprocess.run(
        ["bash", str(PROVISIONER), "--cache-state"], env=seed,
        capture_output=True, text=True, timeout=120, cwd=str(REPO))
    keys = {key: val.replace("\n", "") for key, val in
            (line.split("=", 1) for line in res.stdout.splitlines()
             if "=" in line)}
    if res.returncode != 0 or "CACHE_DIR" not in keys:
        raise RuntimeError(
            f"canonical cache resolution failed (rc={res.returncode}): "
            f"{res.stderr.strip()}")
    state = {
        "dir": keys.get("CACHE_DIR", ""),
        "mode": keys.get("CACHE_MODE", ""),
        "persistent": keys.get("CACHE_PERSISTENT", "") == "yes",
        "warm": keys.get("CACHE_WARM", "false") == "true",
        "lock_class": keys.get("LOCK_CLASS", "UNKNOWN"),
    }
    _CACHED_STATE = state
    return dict(state)


class WarmPortableCache:
    """One usable warm portable cache for portable-legged children.

    Resolution order (canonical resolver is the only engine):
      * parent FIE_TEST_PG_CACHE_DIR override (usable hard contract);
      * active persistent cache, warmed once when allowed;
      * a helper-allocated job-local cache, warmed once under an explicit
        override (temp mode has no cross-invocation durability). This is
        also used when the active persistent cache is warm-with-binless or
        its pre-existing acquisition lock is unclaimable (LOCK_CLASS=UNKNOWN
        — the R4-R5-R1 Task D fail-closed contract): the provisioner would
        warm an isolated fallback that no scope adopts, so the helper
        allocates the job-local cache itself and owns its removal.

    A real acquisition is attempted only when
    ``FIE_TEST_PG_ALLOW_REAL_ACQUISITION=1`` (test-cloud sets this; direct
    suite runs default offline). ``acquired`` reports what was used: skip
    attribution is derived from ``classification``.
    """

    def __init__(self) -> None:
        self.dir: str | None = None
        self.classification: str = ""
        self.acquired: str = ""
        self._tmpdir: str | None = None

    def __enter__(self):
        allow = os.environ.get("FIE_TEST_PG_ALLOW_REAL_ACQUISITION", "0")
        override = os.environ.get("FIE_TEST_PG_CACHE_DIR") or ""
        try:
            state = canonical_cache_state(force_refresh=True)
        except RuntimeError as exc:
            self.classification = f"cache-resolution-failed ({exc})"
            return self
        override_dir = Path(override) if override else Path(state["dir"])
        if override and _cache_ready(override_dir):
            self.dir = override
            self.acquired = "override"
            return self
        if _cache_ready(override_dir):
            self.dir = str(override_dir)
            self.acquired = "active-warm"
            return self
        if allow != "1":
            self.classification = (
                "portable acquisition cache cold — warm it with "
                "'scripts/provision-test-postgres.sh --ensure-cache' "
                "(or allow real acquisition via test-cloud.sh / "
                "FIE_TEST_PG_ALLOW_REAL_ACQUISITION=1)")
            return self
        # One warm attempt. In temp mode the per-invocation temp dir cannot
        # be shared by children — allocate an explicit job-local cache dir
        # instead (same contract mode: explicit override). Same treatment
        # when the persistent cache's pre-existing acquisition lock is
        # unclaimable (UNKNOWN): ensure-cache would fail closed into an
        # isolated fallback temp that is orphaned on child exit (no
        # cross-invocation durability) — allocate the job-local cache here
        # so this scope owns both the warmed content and its removal.
        target = state["dir"]
        if state["mode"] == "temp" or state.get("lock_class") == "UNKNOWN":
            self._tmpdir = os.path.join(
                os.environ.get("TMPDIR", "/tmp"),
                f"fie-warm-cache.{uuid.uuid4().hex[:12]}")
            target = self._tmpdir
        blob = {"FIE_TEST_PG_CACHE_DIR": str(target)}
        env = child_env(None, None, blob, parent_env=os.environ)
        res = subprocess.run(
            ["bash", str(PROVISIONER), "--ensure-cache"], env=env,
            capture_output=True, text=True, timeout=900, cwd=str(REPO))
        if _cache_ready(target):
            self.dir = str(target)
            self.acquired = "warmed"
            return self
        err_tail = res.stderr.strip().splitlines()[-1] if res.stderr.strip() \
            else "no-output"
        self.classification = (
            "portable acquisition cache cold — warm attempt failed "
            f"(rc={res.returncode}, {err_tail}); acquisition refused, "
            "no fallback")
        if self._tmpdir:
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        return self

    def __exit__(self, *exc):
        import shutil
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        return False


# Non-secret cache-visibility variables the canonical resolver consumes —
# propagated verbatim (Task D: the helper must resolve what the runtime
# resolves in the same parent, not invent a second engine).
CACHE_VISIBILITY_PASSTHRU: tuple[str, ...] = (
    "FIE_TEST_PG_CACHE_DIR", "XDG_CACHE_HOME", "HOME", "TMPDIR",
)


def cache_ready(d) -> bool:
    """Public capability probe (jar + sidecar + extracted server tooling)."""
    return _cache_ready(d)


def _cache_ready(d) -> bool:
    """Capability probe (jar + sidecar + extracted server tooling)."""
    jar = Path(d) / "portable-postgres.jar"
    sidecar = Path(d) / "artifact.sha256"
    if not (Path(d) / "bin" / "initdb").is_file():
        return False
    if not (Path(d) / "bin" / "pg_ctl").is_file():
        return False
    return jar.is_file() and sidecar.is_file() and jar.stat().st_size > 0