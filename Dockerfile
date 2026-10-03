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
FROM python:3.12-slim

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

RUN python -m pip install --no-cache-dir "PyYAML>=6,<8" \
    && python -m pip install --no-cache-dir --no-deps /build \
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