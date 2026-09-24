"""Authentication primitives: bcrypt + JWT + cookie dependency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def create_access_token(*, user_id: UUID, username: str, role: str) -> str:
    """Mint a JWT for the given user."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role,
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


async def get_current_user(request: Request) -> AuthUser:
    """FastAPI dependency: extract + validate the JWT cookie."""
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
    try:
        user_id = UUID(sub)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject",
        ) from e
    return AuthUser(id=user_id, username=username, role=role)


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
    "hash_password",
    "verify_password",
    "create_access_token",
    "decode_access_token",
    "get_current_user",
    "require_admin",
]
