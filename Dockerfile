# syntax=docker/dockerfile:1.7
#
# TradingAgents — Coolify-deployable single-image build.
#
# Stage 1 (fe): Node 24 LTS builds the Vite/React frontend.
# Stage 2 (be): Python 3.12-slim runtime that hosts FastAPI + the bundled SPA.
#
# Licensed under the Apache License, Version 2.0. See LICENSE for terms.

# ---- frontend build ----
FROM node:24-alpine AS fe
WORKDIR /fe
COPY web/frontend/package*.json ./
RUN npm ci
COPY web/frontend ./
# Vite writes to /backend/app/static/, NOT /fe/dist. That's because
# vite.config.ts:25 uses `outDir: path.resolve(__dirname, "../backend/app/static")`
# — a path that's convenient for local dev (the build lands directly
# in FastAPI's static dir, no copy needed) but resolves to an absolute
# path inside the fe stage in Docker. Don't change the vite config;
# just track its output location here. The `test` step asserts the
# build actually produced index.html so a future regression fails
# loud rather than silently breaking the next COPY --from=fe step.
RUN npm run build && test -s /backend/app/static/index.html

# ---- python runtime ----
FROM python:3.12-slim AS be
WORKDIR /app
# No compiler: every locked package ships a prebuilt wheel, and a runtime
# image without one is smaller and gives an attacker less to work with.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 curl \
    && rm -rf /var/lib/apt/lists/*         # curl: needed for Coolify's UI health check
# Third-party packages come from requirements.lock only, each wheel checked
# against its sha256 — a changed or hijacked release fails the build instead
# of shipping. Regenerate the lock per docs/RELEASING.md §2.3.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml .
COPY tradingagents ./tradingagents
COPY cli ./cli
COPY web/backend ./web/backend
# The two local packages, without letting pip resolve or download anything:
# --no-build-isolation builds them with the setuptools the lock installed.
# The parent `tradingagents` is named first so the backend's
# path-dependency on it is already satisfied.
RUN pip install --no-cache-dir --no-deps --no-build-isolation . ./web/backend \
 && pip check
COPY --from=fe /backend/app/static /app/web/backend/app/static
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data/tradingagents
ENV TRADINGAGENTS_RESULTS_DIR=/data/tradingagents/logs
ENV TRADINGAGENTS_CACHE_DIR=/data/tradingagents/cache
ENV TRADINGAGENTS_MEMORY_LOG_PATH=/data/tradingagents/memory/trading_memory.md
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin tradingagents \
 && mkdir -p /data/tradingagents \
 && chown -R tradingagents:tradingagents /data/tradingagents /app
USER tradingagents
ENTRYPOINT ["/entrypoint.sh"]
