"""User bootstrap and lookup helpers."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import hash_password
from ..config import get_settings
from ..db import get_session_factory
from ..models import BOOTSTRAP_ADMIN_ID, User

log = logging.getLogger(__name__)

ROB_USERNAME = "rob@rob"


async def upsert_admin_user(session: AsyncSession) -> User:
    """Ensure the env-configured admin user exists with the current password hash."""
    settings = get_settings()
    row = await session.get(User, str(BOOTSTRAP_ADMIN_ID))
    if row is None:
        row = User(
            id=str(BOOTSTRAP_ADMIN_ID),
            username=settings.admin_username,
            password_hash=settings.admin_password_hash,
            role="admin",
        )
        session.add(row)
    else:
        row.username = settings.admin_username
        row.password_hash = settings.admin_password_hash
        row.role = "admin"
    await session.commit()
    return row


async def ensure_rob_user(session: AsyncSession) -> User | None:
    """Create rob@rob if missing and ROB_INITIAL_PASSWORD is set."""
    settings = get_settings()
    if not settings.rob_initial_password:
        return None

    existing = (
        await session.execute(select(User).where(User.username == ROB_USERNAME))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    user = User(
        username=ROB_USERNAME,
        password_hash=hash_password(settings.rob_initial_password),
        role="user",
    )
    session.add(user)
    await session.commit()
    log.info("bootstrap_users.rob_created", extra={"username": ROB_USERNAME})
    return user


async def bootstrap_users() -> None:
    """Sync admin from env and optionally seed rob@rob."""
    try:
        factory = get_session_factory()
        async with factory() as session:
            await upsert_admin_user(session)
            await ensure_rob_user(session)
    except Exception as exc:
        # Tests and fresh SQLite dev DBs may not have run migration 0003 yet;
        # the app should still start so the health endpoint can report status.
        if "no such table" in str(exc).lower():
            log.debug("bootstrap_users.skipped_no_schema")
            return
        raise


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    """Lookup a user by username."""
    return (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: UUID) -> User | None:
    """Lookup a user by primary key."""
    return await session.get(User, str(user_id))


__all__ = [
    "ROB_USERNAME",
    "bootstrap_users",
    "ensure_rob_user",
    "get_user_by_id",
    "get_user_by_username",
    "upsert_admin_user",
]
