#!/usr/bin/env sh
# TradingAgents container entrypoint.
#
# Coolify does not expose a pre-deploy hook for Dockerfile apps; we chain
# `alembic upgrade head` ahead of uvicorn here so migrations run on every
# container start. `exec` hands PID 1 to uvicorn so it receives SIGTERM
# directly from Docker on shutdown.
#
# Licensed under the Apache License, Version 2.0. See LICENSE for terms.

set -e

mkdir -p /data/tradingagents/logs \
         /data/tradingagents/cache \
         /data/tradingagents/memory \
         /data/tradingagents/reports

cd /app/web/backend && alembic upgrade head

exec uvicorn app.main:app \
    --app-dir /app/web/backend \
    --host 0.0.0.0 \
    --port 8000
