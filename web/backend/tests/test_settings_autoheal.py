"""GET /api/settings/defaults auto-heals stale model names.

Before this PR the catalog hardcoded local-Ollama tags (`qwen3:latest`)
that didn't exist on Ollama Cloud. The deployed admin's saved defaults
still point at those stale names; pre-filling the form with them would
just reproduce the 404. Instead, on GET we null out any saved model
that's not in the live catalog. The user sees an empty model picker
and is forced to pick a real model on save.

Notably: the DB row is NOT mutated — the auto-heal is purely on the
read path. The next PUT (with the user's new pick) overwrites it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.middleware.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.models import UserDefaults as UserDefaultsModel

from .conftest import install_fake_httpx_ollama as _install_fake_httpx


CSRF_TOKEN = "test-csrf-token-autoheal"


def _run(coro):
    """Run an async coroutine from sync code (mirrors test_settings.py)."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def autoheal_client(monkeypatch):
    """TestClient with a per-test in-memory DB and Ollama configured."""
    from sqlalchemy.pool import StaticPool

    from app.db import get_session
    from app.main import app
    from app.routers.settings import get_current_user
    from app.schemas import AuthUser

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async def _init():
        from app.db import Base
        from app import models  # noqa: F401 — register tables

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_init())

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _yield_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="tester"
    )

    client = TestClient(app)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_TOKEN)

    try:
        yield client, factory
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_user, None)
        _run(engine.dispose())


def _seed_defaults(factory, **fields) -> None:
    async def _do():
        async with factory() as session:
            row = UserDefaultsModel(id=1, **fields)
            session.add(row)
            await session.commit()

    _run(_do())


def _read_row(factory) -> UserDefaultsModel | None:
    async def _do():
        async with factory() as session:
            result = await session.execute(select(UserDefaultsModel))
            return result.scalars().first()

    return _run(_do())


def test_stale_quick_model_returned_as_null(
    autoheal_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory = autoheal_client
    _install_fake_httpx(
        monkeypatch, ids=["gpt-oss:120b", "qwen3-coder:480b", "glm-4.7"]
    )
    _seed_defaults(
        factory,
        llm_provider="ollama",
        quick_think_llm="qwen3:latest",
        deep_think_llm="gpt-oss:120b",
    )

    resp = client.get("/api/settings/defaults")
    assert resp.status_code == 200
    body = resp.json()

    assert body["quick_think_llm"] is None, (
        "stale quick model should be nulled out"
    )
    assert body["deep_think_llm"] == "gpt-oss:120b", (
        "valid deep model should be preserved"
    )
    assert body["llm_provider"] == "ollama"


def test_autoheal_does_not_mutate_db_row(
    autoheal_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory = autoheal_client
    _install_fake_httpx(
        monkeypatch, ids=["gpt-oss:120b", "qwen3-coder:480b"]
    )
    _seed_defaults(
        factory,
        llm_provider="ollama",
        quick_think_llm="qwen3:latest",
        deep_think_llm="glm-4.7-flash:latest",
    )

    # GET several times — the row must stay stale, only the response is healed.
    for _ in range(3):
        resp = client.get("/api/settings/defaults")
        assert resp.status_code == 200

    row = _read_row(factory)
    assert row is not None
    assert row.quick_think_llm == "qwen3:latest"
    assert row.deep_think_llm == "glm-4.7-flash:latest"


def test_no_provider_means_no_autoheal_attempted(
    autoheal_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If no llm_provider is saved we can't validate models — return as-is."""
    client, factory = autoheal_client
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b"])
    _seed_defaults(
        factory,
        quick_think_llm="something-random",
        deep_think_llm="anything-else",
    )

    resp = client.get("/api/settings/defaults")
    assert resp.status_code == 200
    body = resp.json()

    # With no provider, we can't validate — keep whatever the user had.
    assert body["quick_think_llm"] == "something-random"
    assert body["deep_think_llm"] == "anything-else"
