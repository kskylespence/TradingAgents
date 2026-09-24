"""User bootstrap and lookup helpers."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session_factory
from ..models import BOOTSTRAP_ADMIN_ID, User

log = logging.getLogger(__name__)


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


async def bootstrap_users() -> None:
    """Sync the admin from env. Other accounts are managed via /api/users;
    startup never creates one, so a deleted account stays deleted."""
    try:
        factory = get_session_factory()
        async with factory() as session:
            await upsert_admin_user(session)
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
    "bootstrap_users",
    "get_user_by_id",
    "get_user_by_username",
    "upsert_admin_user",
]
