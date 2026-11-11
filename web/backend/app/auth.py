"""Authentication primitives: bcrypt + JWT + cookie dependency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status

from .config import get_settings
from .schemas import AuthUser

COOKIE_ACCESS_TOKEN = "access_token"
COOKIE_CSRF_TOKEN = "csrf_token"

JWT_ALGORITHM = "HS256"

# bcrypt reads only the first 72 bytes of a password. Longer ones are
# refused rather than truncated, so two different strings can never
# authenticate the same account.
BCRYPT_MAX_PASSWORD_BYTES = 72
BCRYPT_ROUNDS = 12


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt (``$2b$``, 12 rounds)."""
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password is {len(encoded)} bytes; bcrypt accepts at most "
            f"{BCRYPT_MAX_PASSWORD_BYTES} bytes"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time bcrypt password check.

    Accepts every hash passlib produced (``$2a$``/``$2b$``), so stored
    hashes keep working. Any malformed input is a failed check, not an error.
    """
    if not password or not hashed:
        return False
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# 403 detail for a session whose password is temporary. The SPA matches it.
PASSWORD_CHANGE_REQUIRED = "password_change_required"


def create_access_token(
    *, user_id: UUID, username: str, role: str, must_change_password: bool = False
) -> str:
    """Mint a JWT for the given user.

    ``pwc`` ("password change") is true when the account's password is a
    temporary one set by an admin; ``get_current_user`` then refuses the
    session everywhere except the routes that let the user change it.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "pwc": bool(must_change_password),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode + verify a JWT. Raise 401 on any failure."""
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        ) from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from e


async def get_current_user_allow_password_change(request: Request) -> AuthUser:
    """FastAPI dependency: extract + validate the JWT cookie.

    Does NOT refuse a session with a temporary password. Only ``/auth/me``,
    ``/auth/logout`` and ``/auth/change-password`` may use it directly;
    everything else goes through ``get_current_user``.
    """
    token = request.cookies.get(COOKIE_ACCESS_TOKEN)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    payload = decode_access_token(token)
    sub = payload.get("sub")
    username = payload.get("username")
    role = payload.get("role")
    if not isinstance(sub, str) or not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject",
        )
    if not isinstance(username, str) or not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token username",
        )
    if role not in ("admin", "user"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token role",
        )
    # Tokens issued before the claim existed have no "pwc": read as False.
    # Any present value that is not a bool is an invalid token (fail closed).
    pwc = payload.get("pwc", False)
    if not isinstance(pwc, bool):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token claims",
        )
    try:
        user_id = UUID(sub)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject",
        ) from e
    return AuthUser(id=user_id, username=username, role=role, must_change_password=pwc)


async def get_current_user(
    user: AuthUser = Depends(get_current_user_allow_password_change),
) -> AuthUser:
    """FastAPI dependency: a signed-in user whose password is not temporary.

    Every route depends on this (directly or via ``require_admin``), so a
    session with a temporary password is refused everywhere by default.
    """
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=PASSWORD_CHANGE_REQUIRED,
        )
    return user


async def require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """FastAPI dependency: admin role required."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


__all__ = [
    "COOKIE_ACCESS_TOKEN",
    "COOKIE_CSRF_TOKEN",
    "JWT_ALGORITHM",
    "PASSWORD_CHANGE_REQUIRED",
    "hash_password",
    "verify_password",
    "create_access_token",
    "decode_access_token",
    "get_current_user",
    "get_current_user_allow_password_change",
    "require_admin",
]
