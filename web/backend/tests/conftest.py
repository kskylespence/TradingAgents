"""Shared pytest fixtures for the backend.

Mirrors the parent repo's `tests/conftest.py` pattern (autouse dummy
provider API keys so nothing accidentally hits a real provider). Also
sets the backend-specific env (FERNET_KEY, DATABASE_URL → in-memory
SQLite) before the first import of `app.config.Settings`, plus offers a
session-scoped engine fixture for tests that need a live SQLAlchemy
connection.

Tests that need real Postgres should use the `pytest-postgresql` plugin
directly — those will live alongside the routers/services they exercise
(tasks #3-6), not in the foundation smoke suite.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from cryptography.fernet import Fernet

# Don't load pytest-postgresql by default — on Windows it tries to import
# psycopg which requires libpq, and the foundation tests don't need real
# Postgres anyway. Downstream tasks that DO need pytest-postgresql can
# override this by re-enabling the plugin in their own conftest.
collect_ignore_glob: list[str] = []


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """No-op hook — placeholder kept so downstream conftests can append."""

# --- Pre-import env setup -------------------------------------------------- #
# These MUST be set before any `from app.config import ...` import that
# pydantic-settings will snapshot. They are deliberately set at import
# time (not in a fixture) because `get_settings()` is `@lru_cache`d.

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ADMIN_USERNAME", "test-admin")
# bcrypt hash for the empty string — only present so settings validate;
# auth tests will use a real hash via the auth router's helpers (task #3).
os.environ.setdefault(
    "ADMIN_PASSWORD_HASH",
    "$2b$12$placeholderplaceholderplaceholderplaceholderplaceholderp",
)


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    """Autouse dummy provider keys — same pattern as the parent repo.

    Treat an empty-string env var the same as unset. Some shells/CIs
    export ``OPENAI_API_KEY=""`` to signal "not configured"; the catalog
    provider filter (see ``tradingagents.providers.available_providers``)
    correctly treats empty as missing, so tests that expect a placeholder
    would otherwise see the provider filtered out.
    """
    for env_var in _API_KEY_ENV_VARS:
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


# --------------------------------------------------------------------------- #
# Async DB fixtures (in-memory SQLite — the foundation smoke tests don't      #
# need Postgres-specific behavior; downstream tasks can opt in to             #
# pytest-postgresql for things like JSONB indexing).                          #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="function")
async def db_engine() -> AsyncIterator:
    """A fresh in-memory SQLite engine per test, with all tables created."""
    from app import models  # noqa: F401 — register all tables on Base.metadata
    from app.db import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="function")
async def db_session(db_engine) -> AsyncIterator:
    """An async session bound to the per-test SQLite engine."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
def anyio_backend():
    """Pin pytest-anyio to asyncio (avoids trio dep in default env)."""
    return "asyncio"


# --------------------------------------------------------------------------- #
# Multi-user helpers — see tests/helpers.py                                   #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Ollama-discovery fixtures — used by every test that exercises               #
# /api/catalog/models?provider=ollama, /api/settings/defaults,                #
# /api/runs (validation path), or /api/health (Ollama probe block).           #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
async def _reset_ollama_cache():
    """Clear the live-discovery cache + lock around every test.

    Required because pytest-asyncio gives each test its own event loop;
    a stale ``asyncio.Lock`` from a previous test bound to a dead loop
    raises ``RuntimeError: <Lock> is bound to a different event loop``.
    Also prevents cross-test cache pollution.

    This fixture is ``async`` specifically so teardown can *await*
    ``drain_in_flight_refreshes()``. ``_reset_for_tests()`` is sync and can
    only call ``task.cancel()``, which merely requests cancellation — the
    stale-while-revalidate refresh stays in state ``cancelling`` until the
    loop runs it again, and pytest-asyncio closes the loop first. That is
    what produced "Task was destroyed but it is pending!" followed by
    ``RuntimeError: Event loop is closed`` at teardown.
    """
    from app.services import ollama_models

    ollama_models._reset_for_tests()
    yield
    await ollama_models.drain_in_flight_refreshes()
    ollama_models._reset_for_tests()


def install_fake_httpx_ollama(
    monkeypatch,
    *,
    ids: list[str] | None = None,
    status: int = 200,
    raise_exc: Exception | None = None,
) -> dict:
    """Install a fake httpx transport for the shared ``upstream_http`` client.

    Returns a ``dict`` recording calls — ``{"calls": int, "last_url": str|None,
    "last_headers": dict|None}`` — so tests can assert that the right URL
    and auth headers were sent. Single shared helper keeps the contract
    between tests consistent: if the service ever changes how it
    constructs the request, every test exercising the catalog/runs/health
    flows fails at once.

    Covers BOTH endpoints reachable via ``upstream_http.request``:

    * ``GET /v1/models`` — the catalog listing path (driven by ``ids``).
    * ``POST /v1/chat/completions`` — the model liveness probe path
      (Phase 2 Layer 1). The default behaviour is "every model is
      healthy" so existing tests that don't care about probing keep
      working unchanged. Tests that *do* want a probe failure should
      use the more granular helper in ``test_runs_preflight_probe.py``.

    Implementation note (v0.2.5+hf.4): we no longer monkeypatch
    ``httpx.AsyncClient`` directly. ``ollama_models`` routes through
    ``upstream_http`` which owns a singleton client; we replace that
    client with one wired to an ``httpx.MockTransport`` so all the
    retry/breaker/timeout production wiring stays exercised but the
    transport responses are deterministic.
    """
    import httpx
    from app.services import ollama_models, upstream_http

    record: dict = {"calls": 0, "last_url": None, "last_headers": None}

    def _healthy_probe_payload(model_id: str) -> dict:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "pong",
                        "tool_calls": None,
                    },
                    "finish_reason": "stop",
                }
            ],
        }

    def _handler(request: httpx.Request) -> httpx.Response:
        record["calls"] += 1
        record["last_url"] = str(request.url)
        record["last_headers"] = dict(request.headers)
        if raise_exc is not None:
            raise raise_exc

        url_path = request.url.path
        if url_path.endswith("/models"):
            return httpx.Response(
                status,
                json={
                    "object": "list",
                    "data": [{"id": x} for x in (ids or [])],
                },
                request=request,
            )
        if "chat/completions" in url_path:
            import json as _json

            try:
                body = _json.loads(request.content or b"{}")
            except Exception:
                body = {}
            model_id = body.get("model", "")
            return httpx.Response(
                200, json=_healthy_probe_payload(model_id), request=request
            )
        return httpx.Response(404, json={"error": "unknown"}, request=request)

    # Reset the singleton first so we drop any stale state from a prior
    # test, then plant a mock-transport client.
    ollama_models._reset_for_tests()
    upstream_http._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        timeout=httpx.Timeout(5.0),
    )
    return record
