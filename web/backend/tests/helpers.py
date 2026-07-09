"""Shared test helpers for backend multi-user support."""

from __future__ import annotations

from uuid import UUID

from passlib.hash import bcrypt

TEST_ADMIN_ID = "00000000-0000-0000-0000-000000000001"
TEST_USER_ID = "00000000-0000-0000-0000-000000000002"


async def seed_admin_user(session, *, username: str = "test-admin", password_hash: str) -> None:
    from app.models import User

    row = await session.get(User, TEST_ADMIN_ID)
    if row is None:
        session.add(
            User(
                id=TEST_ADMIN_ID,
                username=username,
                password_hash=password_hash,
                role="admin",
            )
        )
    else:
        row.username = username
        row.password_hash = password_hash
        row.role = "admin"
    await session.commit()


async def seed_regular_user(
    session,
    *,
    user_id: str = TEST_USER_ID,
    username: str = "rob@rob",
    password: str = "user-password",
) -> None:
    from app.models import User

    existing = await session.get(User, user_id)
    if existing is not None:
        return
    session.add(
        User(
            id=user_id,
            username=username,
            password_hash=bcrypt.hash(password),
            role="user",
        )
    )
    await session.commit()


def make_auth_user(
    *,
    user_id: str = TEST_ADMIN_ID,
    username: str = "test-admin",
    role: str = "admin",
):
    from app.schemas import AuthUser

    return AuthUser(id=UUID(user_id), username=username, role=role)  # type: ignore[arg-type]
