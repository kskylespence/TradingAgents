"""Authentication primitives: bcrypt + JWT + cookie dependency.

This is the top-level auth helper module (NOT a router). Routers import
from here; middleware can import the cookie name constants too.

Public surface:
- ``COOKIE_ACCESS_TOKEN`` — name of the HttpOnly JWT cookie.
- ``COOKIE_CSRF_TOKEN`` — name of the CSRF double-submit cookie.
- ``verify_password(password, hashed)`` — bcrypt compare.
- ``create_access_token(username)`` — issue a short-lived HS256 JWT.
- ``decode_access_token(token)`` — verify + decode; raise 401 on invalid.
- ``get_current_user(request)`` — FastAPI dep returning ``AuthUser``.

Security posture:
- JWT signed with HS256 using ``settings.jwt_secret``; TTL controlled by
  ``settings.jwt_ttl_seconds``.
- Cookie is set HttpOnly + SameSite=Lax. ``Secure`` is enabled when the
  request is HTTPS, as reported by ``request.url.scheme``. In production
  this works behind Coolify's Traefik reverse proxy because uvicorn is
  started with ``--proxy-headers``, which lets Starlette's
  ProxyHeadersMiddleware rewrite the scheme from the trusted proxy's
  ``X-Forwarded-Proto`` header before any app code runs.
- bcrypt verification is constant-time via ``passlib.hash.bcrypt.verify``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import HTTPException, Request, status
from passlib.hash import bcrypt

from .config import get_settings
from .schemas import AuthUser

# Cookie names — exported so middleware (CSRF) and tests can reference
# the exact same strings without redefining.
COOKIE_ACCESS_TOKEN = "access_token"
COOKIE_CSRF_TOKEN = "csrf_token"

# JWT algorithm — HS256 because we share a single symmetric secret across
# the app (single backend, no public key distribution needed).
JWT_ALGORITHM = "HS256"


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time bcrypt password check.

    Returns False on any error (malformed hash, etc.) — we never raise
    out of this function so callers can treat it as a pure predicate.
    """
    if not password or not hashed:
        return False
    try:
        return bool(bcrypt.verify(password, hashed))
    except (ValueError, TypeError):
        # ValueError covers malformed bcrypt strings; TypeError covers
        # passing non-strings. Either way: not authenticated.
        return False


def create_access_token(username: str) -> str:
    """Mint a JWT for ``username`` with TTL from settings.

    A negative TTL is allowed (used in tests for instant-expiry checks);
    PyJWT happily encodes a past ``exp`` and ``decode_access_token`` will
    then reject it with 401 as expected.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode + verify a JWT. Raise 401 on any failure (expired, bad sig)."""
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
    """FastAPI dependency: extract + validate the JWT cookie.

    Reads the HttpOnly ``access_token`` cookie, decodes it, and returns
    an ``AuthUser`` containing the username. Missing or invalid cookie =>
    HTTP 401.
    """
    token = request.cookies.get(COOKIE_ACCESS_TOKEN)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    payload = decode_access_token(token)
    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject",
        )
    return AuthUser(username=username)


__all__ = [
    "COOKIE_ACCESS_TOKEN",
    "COOKIE_CSRF_TOKEN",
    "JWT_ALGORITHM",
    "verify_password",
    "create_access_token",
    "decode_access_token",
    "get_current_user",
]
