"""PUT /api/settings/defaults rejects stale model names with 400.

Mirror of the auto-heal logic: GET nulls out stale values so the form
stays useful; PUT validates so the user can't save a stale value going
forward. Clearing a field (`null`) is explicitly allowed — that's how
auto-heal stores its "I don't know" state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from app.middleware.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .conftest import install_fake_httpx_ollama as _install_fake_httpx

CSRF_TOKEN = "test-csrf-token-put-validate"


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def put_client(monkeypatch):
    from app.db import get_session
    from app.main import app
    from app.routers.settings import get_current_user
    from app.schemas import AuthUser
    from sqlalchemy.pool import StaticPool

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async def _init():
        from app import models  # noqa: F401
        from app.db import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_init())

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _yield_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    from tests.helpers import make_auth_user

    app.dependency_overrides[get_current_user] = lambda: make_auth_user(username="tester")

    client = TestClient(app)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_TOKEN)

    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_user, None)
        _run(engine.dispose())


def _put(client: TestClient, body: dict) -> object:
    return client.put(
        "/api/settings/defaults",
        json=body,
        headers={CSRF_HEADER_NAME: CSRF_TOKEN},
    )


def test_put_stale_model_returns_400(
    put_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b", "qwen3-coder:480b"])

    resp = _put(
        put_client,
        {"llm_provider": "ollama", "quick_think_llm": "qwen3:latest"},
    )
    assert resp.status_code == 400, resp.text

    detail = resp.json().get("detail", "")
    assert "qwen3:latest" in detail
    assert "ollama" in detail.lower()
    # The error must list at least one available model so the user
    # knows what to pick.
    assert "gpt-oss:120b" in detail or "Available" in detail


def test_put_valid_model_succeeds(
    put_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b", "qwen3-coder:480b"])

    resp = _put(
        put_client,
        {
            "llm_provider": "ollama",
            "quick_think_llm": "gpt-oss:120b",
            "deep_think_llm": "qwen3-coder:480b",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quick_think_llm"] == "gpt-oss:120b"
    assert body["deep_think_llm"] == "qwen3-coder:480b"


def test_put_null_quick_model_succeeds(
    put_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing a field with null is explicitly allowed (auto-heal uses this)."""
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b"])

    resp = _put(
        put_client,
        {"llm_provider": "ollama", "quick_think_llm": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["quick_think_llm"] is None


def test_put_stale_deep_model_returns_400(
    put_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b"])

    resp = _put(
        put_client,
        {"llm_provider": "ollama", "deep_think_llm": "glm-4.7-flash:latest"},
    )
    assert resp.status_code == 400, resp.text
    assert "glm-4.7-flash:latest" in resp.json().get("detail", "")
