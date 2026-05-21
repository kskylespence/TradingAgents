"""Authentication endpoints: /auth/login, /auth/logout, /auth/me.

Drops into the router registry via ``register(router)`` at module scope.
``main.py`` mounts every registered router under ``/api`` — so the final
public paths are ``/api/auth/login`` etc.

Login flow:
    1. Rate limiter checks IP — 401 if locked out.
    2. Username compared against ``settings.admin_username``.
    3. Password verified against ``settings.admin_password_hash`` (bcrypt).
    4. Attempt persisted (success or failure) to ``login_attempts``.
    5. On success: HttpOnly ``access_token`` cookie + non-HttpOnly
       ``csrf_token`` cookie are set. Body is 204 No Content.

Logout simply clears both cookies. ``/auth/me`` reuses
``get_current_user`` and is the canonical "am I logged in" probe.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import register
from ..auth import (
    COOKIE_ACCESS_TOKEN,
    COOKIE_CSRF_TOKEN,
    create_access_token,
    get_current_user,
    verify_password,
)
from ..config import get_settings
from ..db import get_session
from ..schemas import AuthUser, LoginRequest
from ..services.rate_limit import login_rate_limiter


router = APIRouter(prefix="/auth", tags=["auth"])


# Generic-by-design: same response shape for "wrong username" and "wrong
# password" so an attacker can't enumerate which one is incorrect.
_INVALID_CREDS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials",
)


def _set_auth_cookies(
    response: Response, *, token: str, csrf_token: str, secure: bool, max_age: int
) -> None:
    """Set both auth cookies on the response.

    ``access_token``: HttpOnly so JS cannot read it (XSS-resistant).
    ``csrf_token``: NOT HttpOnly so the SPA can echo it as a header for
    the double-submit CSRF check on state-changing methods.

    Both use ``SameSite=Lax``: blocks classic CSRF for top-level
    navigation while still allowing standard same-site form posts. The
    csrf_token cookie + ``X-CSRF-Token`` header double-submit pattern
    covers sibling-subdomain edge cases SameSite alone misses.
    """
    common = {
        "max_age": max_age,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(
        key=COOKIE_ACCESS_TOKEN,
        value=token,
        httponly=True,
        **common,
    )
    response.set_cookie(
        key=COOKIE_CSRF_TOKEN,
        value=csrf_token,
        httponly=False,
        **common,
    )


def _clear_auth_cookies(response: Response, *, secure: bool) -> None:
    """Wipe both cookies. Used by logout."""
    response.delete_cookie(
        COOKIE_ACCESS_TOKEN, path="/", secure=secure, samesite="lax"
    )
    response.delete_cookie(
        COOKIE_CSRF_TOKEN, path="/", secure=secure, samesite="lax"
    )


@router.post(
    "/login",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Authenticate the admin user and set the auth cookies.

    Always records the attempt — success or failure — so the rate
    limiter and audit history stay accurate.
    """
    # 1) Rate-limit check FIRST so brute-forcers don't get to consume
    # bcrypt verification cycles (which are deliberately expensive).
    await login_rate_limiter.check(request, db)

    settings = get_settings()

    # 2) + 3) credential check — `verify_password` is constant-time and
    # `secrets.compare_digest` on the username avoids timing differences
    # between a wrong-length and same-length username miss.
    username_ok = secrets.compare_digest(body.username, settings.admin_username)
    password_ok = verify_password(body.password, settings.admin_password_hash)

    # 4) record attempt regardless of outcome
    succeeded = username_ok and password_ok
    await login_rate_limiter.record(request, db, succeeded=succeeded)

    if not succeeded:
        raise _INVALID_CREDS

    # 5) mint cookies
    token = create_access_token(settings.admin_username)
    csrf_token = secrets.token_hex(32)
    # JWT TTL can be negative in tests; cookie max_age can't, so clamp.
    cookie_max_age = max(0, int(settings.jwt_ttl_seconds))
    _set_auth_cookies(
        response,
        token=token,
        csrf_token=csrf_token,
        secure=request.url.scheme == "https",
        max_age=cookie_max_age,
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def logout(
    request: Request,
    response: Response,
    _user: AuthUser = Depends(get_current_user),
) -> Response:
    """Clear both auth cookies. No body. Requires JWT (per plan)."""
    _clear_auth_cookies(response, secure=request.url.scheme == "https")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=AuthUser)
async def me(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """Return the currently-authenticated user, or 401."""
    return user


register(router)


__all__ = ["router"]
