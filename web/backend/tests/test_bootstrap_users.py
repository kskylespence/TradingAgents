"""Startup user bootstrap: sync the env admin, and create nobody else.

Regression pinned here: bootstrap used to (re)create a hardcoded
``rob@rob`` account whenever ``ROB_INITIAL_PASSWORD`` was set and the row
was missing. Deleting that account in the Users UI therefore only lasted
until the next restart, when it came back with the old bootstrap password.
Accounts are managed through ``/api/users`` now; startup must never create
a non-admin user.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app import models  # noqa: F401 — register tables on Base.metadata
from app.config import get_settings
from app.db import Base
from app.models import User
from app.services import users as users_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(users_service, "get_session_factory", lambda: session_factory)
    # The deploy that shipped this fix still had the variable set.
    monkeypatch.setenv("ROB_INITIAL_PASSWORD", "legacy-bootstrap-password")
    get_settings.cache_clear()
    yield session_factory
    get_settings.cache_clear()
    await engine.dispose()


async def _usernames(session_factory) -> list[str]:
    async with session_factory() as session:
        return sorted((await session.execute(select(User.username))).scalars())


async def test_bootstrap_creates_only_the_env_admin(factory):
    await users_service.bootstrap_users()

    assert await _usernames(factory) == [get_settings().admin_username]


async def test_a_deleted_account_stays_deleted_across_restarts(factory):
    await users_service.bootstrap_users()
    async with factory() as session:
        session.add(User(id=str(uuid4()), username="rob@rob", password_hash="$2b$12$" + "x" * 53, role="user"))
        await session.commit()
    async with factory() as session:
        row = (await session.execute(select(User).where(User.username == "rob@rob"))).scalar_one()
        await session.delete(row)
        await session.commit()

    await users_service.bootstrap_users()  # the next container start

    assert "rob@rob" not in await _usernames(factory)


async def test_an_existing_account_is_left_untouched(factory):
    async with factory() as session:
        session.add(User(id=str(uuid4()), username="rob@rob", password_hash="$2b$12$" + "y" * 53, role="user"))
        await session.commit()

    await users_service.bootstrap_users()

    async with factory() as session:
        row = (await session.execute(select(User).where(User.username == "rob@rob"))).scalar_one()
    assert row.password_hash == "$2b$12$" + "y" * 53
