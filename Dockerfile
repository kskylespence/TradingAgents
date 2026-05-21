# syntax=docker/dockerfile:1.7
#
# TradingAgents — Coolify-deployable single-image build.
#
# Stage 1 (fe): Node 20 builds the Vite/React frontend.
# Stage 2 (be): Python 3.12-slim runtime that hosts FastAPI + the bundled SPA.
#
# Licensed under the Apache License, Version 2.0. See LICENSE for terms.

# ---- frontend build ----
FROM node:20-alpine AS fe
WORKDIR /fe
COPY web/frontend/package*.json ./
RUN npm ci
COPY web/frontend ./
RUN npm run build                          # outputs to /fe/dist

# ---- python runtime ----
FROM python:3.12-slim AS be
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq5 curl \
    && rm -rf /var/lib/apt/lists/*         # curl: needed for Coolify's UI health check
COPY pyproject.toml .
COPY tradingagents ./tradingagents
COPY cli ./cli
COPY web/backend ./web/backend
RUN pip install --no-cache-dir ./web/backend
COPY --from=fe /fe/dist /app/web/backend/app/static
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
ENTRYPOINT ["/entrypoint.sh"]
