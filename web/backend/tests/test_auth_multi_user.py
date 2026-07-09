"""Tests for multi-user auth and history isolation."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from passlib.hash import bcrypt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers import (
    TEST_ADMIN_ID,
    TEST_USER_ID,
    make_auth_user,
    seed_admin_user,
    seed_regular_user,
)

PASSWORD = "password"
PASSWORD_HASH = bcrypt.hash(PASSWORD)
USER_PASSWORD = "user-password"


@pytest.fixture
def multi_user_engine(tmp_path):
    from app import models  # noqa: F401
    from app.db import Base

    db_path = tmp_path / "multi.sqlite"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            await seed_admin_user(session, password_hash=PASSWORD_HASH)
            await seed_regular_user(session, password=USER_PASSWORD)

    asyncio.get_event_loop().run_until_complete(_setup())
    try:
        yield engine, factory
    finally:
        asyncio.get_event_loop().run_until_complete(engine.dispose())


@pytest.fixture
def multi_client(monkeypatch, multi_user_engine):
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-not-for-production")

    from app.config import get_settings
    get_settings.cache_clear()

    from app.db import get_session
    from app.main import create_app
    from app.services.rate_limit import login_rate_limiter

    engine, factory = multi_user_engine
    app = create_app()

    async def _override_get_session() -> AsyncIterator:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    login_rate_limiter.reset()

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_login_regular_user_returns_role(multi_client: TestClient) -> None:
    resp = multi_client.post(
        "/api/auth/login",
        json={"username": "rob@rob", "password": USER_PASSWORD},
    )
    assert resp.status_code == 204, resp.text
    me = multi_client.get("/api/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["username"] == "rob@rob"
    assert body["role"] == "user"
    assert body["id"] == TEST_USER_ID


@pytest.mark.asyncio
async def test_history_isolation(multi_user_engine) -> None:
    from app.models import Run

    _engine, factory = multi_user_engine
    admin_run = uuid.uuid4()
    user_run = uuid.uuid4()
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)

    async with factory() as session:
        session.add(
            Run(
                id=str(admin_run),
                user_id=TEST_ADMIN_ID,
                ticker="AAPL",
                asset_type="stock",
                analysis_date=date(2026, 5, 1),
                analysts=["market"],
                research_depth=1,
                llm_provider="ollama",
                quick_think_llm="glm-5.2",
                deep_think_llm="glm-5.2",
                output_language="English",
                checkpoint_enabled=False,
                status="completed",
                created_at=now,
            )
        )
        session.add(
            Run(
                id=str(user_run),
                user_id=TEST_USER_ID,
                ticker="NVDA",
                asset_type="stock",
                analysis_date=date(2026, 5, 1),
                analysts=["market"],
                research_depth=1,
                llm_provider="ollama",
                quick_think_llm="glm-5.2",
                deep_think_llm="glm-5.2",
                output_language="English",
                checkpoint_enabled=False,
                status="completed",
                created_at=now,
            )
        )
        await session.commit()

    from app.auth import get_current_user
    from app.db import get_session
    from app.main import create_app

    app = create_app()

    async def _override_get_session() -> AsyncIterator:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_current_user] = lambda: make_auth_user(
        user_id=TEST_USER_ID, username="rob@rob", role="user"
    )

    with TestClient(app) as client:
        resp = client.get("/api/history")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["ticker"] == "NVDA"

    app.dependency_overrides.clear()


def test_settings_forbidden_for_regular_user(multi_client: TestClient) -> None:
    login = multi_client.post(
        "/api/auth/login",
        json={"username": "rob@rob", "password": USER_PASSWORD},
    )
    assert login.status_code == 204
    csrf = multi_client.cookies.get("csrf_token")
    resp = multi_client.get("/api/settings/defaults")
    assert resp.status_code == 403

    resp = multi_client.put(
        "/api/settings/defaults",
        headers={"X-CSRF-Token": csrf or ""},
        json={"llm_provider": "ollama"},
    )
    assert resp.status_code == 403
