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
    """Autouse dummy provider keys — same pattern as the parent repo."""
    for env_var in _API_KEY_ENV_VARS:
        monkeypatch.setenv(env_var, os.environ.get(env_var, "placeholder"))


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
