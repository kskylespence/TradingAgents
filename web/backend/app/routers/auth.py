"""Authentication endpoints: /auth/login, /auth/logout, /auth/me."""

from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

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
from ..services.users import get_user_by_username
from . import register

router = APIRouter(prefix="/auth", tags=["auth"])


_INVALID_CREDS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials",
)


def _set_auth_cookies(
    response: Response, *, token: str, csrf_token: str, secure: bool, max_age: int
) -> None:
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
    """Authenticate a user and set auth cookies."""
    await login_rate_limiter.check(request, db)

    user = await get_user_by_username(db, body.username)
    password_ok = user is not None and verify_password(
        body.password, user.password_hash
    )
    succeeded = password_ok
    await login_rate_limiter.record(request, db, succeeded=succeeded)

    if not succeeded:
        raise _INVALID_CREDS

    assert user is not None
    user_uuid = user.id if isinstance(user.id, UUID) else UUID(str(user.id))
    token = create_access_token(
        user_id=user_uuid,
        username=user.username,
        role=user.role,
    )
    csrf_token = secrets.token_hex(32)
    settings = get_settings()
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
    _clear_auth_cookies(response, secure=request.url.scheme == "https")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=AuthUser)
async def me(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    return user


register(router)


__all__ = ["router"]
