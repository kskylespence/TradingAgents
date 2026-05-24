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
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import Base
    from app import models  # noqa: F401 — register all tables on Base.metadata

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
# Ollama-discovery fixtures — used by every test that exercises               #
# /api/catalog/models?provider=ollama, /api/settings/defaults,                #
# /api/runs (validation path), or /api/health (Ollama probe block).           #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_ollama_cache():
    """Clear the live-discovery cache + lock around every test.

    Required because pytest-asyncio gives each test its own event loop;
    a stale ``asyncio.Lock`` from a previous test bound to a dead loop
    raises ``RuntimeError: <Lock> is bound to a different event loop``.
    Also prevents cross-test cache pollution.
    """
    from app.services import ollama_models

    ollama_models._reset_for_tests()
    yield
    ollama_models._reset_for_tests()


def install_fake_httpx_ollama(
    monkeypatch,
    *,
    ids: list[str] | None = None,
    status: int = 200,
    raise_exc: Exception | None = None,
) -> dict:
    """Install a fake ``httpx.AsyncClient`` for the ollama_models service.

    Returns a ``dict`` recording calls — ``{"calls": int, "last_url": str|None,
    "last_headers": dict|None}`` — so tests can assert that the right URL
    and auth headers were sent. Single shared helper keeps the contract
    between tests consistent: if the service ever changes how it
    constructs the request, every test exercising the catalog/runs/health
    flows fails at once.

    Covers BOTH endpoints exposed by the service:

    * ``GET /v1/models`` — the catalog listing path (driven by ``ids``).
    * ``POST /v1/chat/completions`` — the model liveness probe path
      (Phase 2 Layer 1). The default behaviour is "every model is
      healthy" so existing tests that don't care about probing keep
      working unchanged. Tests that *do* want a probe failure should
      use the more granular helper in ``test_runs_preflight_probe.py``.
    """
    import httpx

    record: dict = {"calls": 0, "last_url": None, "last_headers": None}

    class _FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"HTTP {self.status_code}", request=None, response=self
                )

        def json(self) -> dict:
            return self._payload

        @property
        def text(self) -> str:
            import json as _json

            try:
                return _json.dumps(self._payload)
            except Exception:
                return str(self._payload)

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def get(self, url, headers=None):
            record["calls"] += 1
            record["last_url"] = url
            record["last_headers"] = dict(headers or {})
            if raise_exc is not None:
                raise raise_exc
            return _FakeResponse(
                status,
                {"object": "list", "data": [{"id": x} for x in (ids or [])]},
            )

        async def post(self, url, *, json=None, headers=None):
            # Default-healthy probe response for the pre-flight probe
            # path (Layer 1). Tests that want a probe failure should
            # install their own client via ``_install_probe_fake`` in
            # ``test_runs_preflight_probe.py``.
            model_id = (json or {}).get("model", "")
            return _FakeResponse(
                200,
                {
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
                },
            )

    monkeypatch.setattr(
        "app.services.ollama_models.httpx.AsyncClient", _FakeClient
    )
    return record
