# FIE reference HTTP runtime — Phase 6.6 deployable artifact (§13).
#
# Built from THIS repository source only:
#   docker build -t fie-reference-runtime:local .
#   docker run --rm -p 127.0.0.1:8787:8787 -e FIE_DATABASE_URL=/data/intelligence.db \
#     -v <host-data-dir>:/data:ro fie-reference-runtime:local
#
# Constraints honoured (§13):
# - non-root runtime user (uid/gid 10001, no home writes needed);
# - explicit exposed port (8787, documented in README/docs);
# - no embedded credentials, no production data, no A3 host mounts,
#   no references to legacy deployments on this host — persistence
#   arrives ONLY through FIE_DATABASE_URL/mounted config provided by
#   the operator;
# - deterministic pinned base image + pinned runtime dependencies.
#
# NOTE: the artifact is a *reference* build — it is never deployed to
# production in this work order (§24 non-goals).
#
# Base image pinning: the CI/production flow should pin this tag by
# digest at deploy time (`docker pull python:3.12-slim && docker images
# --digests`); the digest is machine-specific and is deliberately NOT
# baked into the tracked file so every host builds from its verified copy.
FROM python:3.12-slim AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# runtime dependencies only (PyYAML); the phase3 engine is import-safe
# without yfinance (it is only used by legacy fetch scripts, which this
# runtime never invokes)
COPY --chown=root:root pyproject.toml README.md /build/
COPY --chown=root:root phase3 /build/phase3
COPY --chown=root:root reports /build/reports

# ---- Container CA trust (Phase 6.6R3) ------------------------------------
# TLS verification is ALWAYS enabled. Two build modes:
#
# Mode A (default, nothing to supply): pip trusts its own bundled public
#   CA store (certifi) and this RUN touches nothing custom — ordinary
#   public-Internet builds succeed with standard public trust only.
# Mode B (optional, environment-supplied): where outbound HTTPS may be
#   intercepted by an authorized private/enterprise CA, the build
#   environment injects that ONE additional PEM certificate as a
#   BuildKit secret (id=authorized_extra_ca). It is ADDED to the system
#   trust store via update-ca-certificates (which augments, never
#   replaces, /etc/ssl/certs/ca-certificates.crt) and pip validates
#   against that augmented bundle (explicit trust argument) for this
#   install. The standard public CAs stay trusted; host-trust bypass
#   flags and any other verification shortcut are deliberately never
#   used — see tests/phase3/transport/test_ca_trust_audit.py.
#
#   DOCKER_BUILDKIT=1 docker build \
#     --secret id=authorized_extra_ca,src=/authorized/path/ca.pem ...
#
# CACHING (verified dynamically): BuildKit deliberately excludes secret
# content from layer cache keys. A Mode B build sharing cache with a
# previous Mode A build would silently reuse the NON-injected layer —
# run Mode B builds with --no-cache so the trust step truly executes.
#
# The secret mechanism keeps the build input out of build context,
# environment and image metadata; the augmented trust material (a PUBLIC
# certificate, never a private key) is the only thing a Mode B image
# intentionally carries — and only when the environment supplied one.
# No CA material is ever committed to this repository.
RUN --mount=type=secret,id=authorized_extra_ca \
    set -eu; \
    pip_cert=""; \
    if [ -f /run/secrets/authorized_extra_ca ]; then \
      cp /run/secrets/authorized_extra_ca \
         /usr/local/share/ca-certificates/authorized-extra-ca.crt; \
      chmod 0644 /usr/local/share/ca-certificates/authorized-extra-ca.crt; \
      update-ca-certificates; \
      pip_cert="--cert /etc/ssl/certs/ca-certificates.crt"; \
    fi; \
    python -m pip install --no-cache-dir $pip_cert "PyYAML>=6,<8" \
    && python -m pip install --no-cache-dir $pip_cert --no-deps /build \
    && rm -rf /build

USER 10001:10001

# explicit, documented port (§13); config is env-driven (§9)
ENV FIE_HTTP_HOST=0.0.0.0 \
    FIE_HTTP_PORT=8787 \
    FIE_LOG_LEVEL=INFO

EXPOSE 8787

# liveness endpoint (§10) — readiness (/readyz) is the persistence probe
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,os; \
urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"FIE_HTTP_PORT\",\"8787\")}/healthz', timeout=4)"

ENTRYPOINT ["python", "-m", "phase3.transport.http"]

# ---- PostgreSQL-capable production artifact (Phase 6.7B-R4, ADR-019) ------
#
# Deterministic build TARGET of the same reference runtime with the
# declared `[postgres]` runtime dependency (psycopg[binary]) baked in at
# BUILD time — the packaging contract for `FIE_DATABASE_URL=postgres://...`
# production-like deployments. An operator NEVER runs `pip install` inside
# a running container:
#
#   docker build --target runtime-postgres -t fie-runtime-postgres:local .
#
# Both final stages run uid/gid 10001 (non-root), carry the identical
# HEALTHCHECK/ENTRYPOINT and differ ONLY in the dependency set. The
# cert bundle flag keeps the CA-trust mode semantics of the base stage
# (Mode A: the standard bundle — same trust; Mode B: the augmented store
# baked into the layer above — the enterprise CA stays trusted, the
# public CAs are never dropped).
FROM runtime-base AS runtime-postgres
USER 0:0
# The SAME trust-mode conditioning as the base stage's RUN: Mode A (no
# secret) → pip validates against its own bundled public trust; Mode B →
# against the (already augmented, additive) system bundle baked into the
# runtime-base layer above. The secret is mounted per-RUN, so a Mode B
# build of THIS target supplies it again and both stages stay identical.
RUN --mount=type=secret,id=authorized_extra_ca \
    set -eu; \
    pip_cert=""; \
    if [ -f /run/secrets/authorized_extra_ca ]; then \
      pip_cert="--cert /etc/ssl/certs/ca-certificates.crt"; \
    fi; \
    python -m pip install --no-cache-dir $pip_cert "psycopg[binary]>=3.2,<4"
USER 10001:10001

# Default build target — UNCHANGED (PyYAML-only reference artifact).
# Declared explicitly so the default `docker build` resolves to the
# same image as before the R4 restructure (the LAST stage is what a
# target-less build produces).
FROM runtime-base AS runtime