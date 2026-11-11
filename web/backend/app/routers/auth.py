"""Authentication endpoints: /auth/login, /auth/logout, /auth/me,
/auth/change-password."""

from __future__ import annotations

import logging
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    COOKIE_ACCESS_TOKEN,
    COOKIE_CSRF_TOKEN,
    create_access_token,
    get_current_user_allow_password_change,
    hash_password,
    verify_password,
)
from ..config import get_settings
from ..db import get_session
from ..models import BOOTSTRAP_ADMIN_ID
from ..schemas import AuthUser, ChangePasswordRequest, LoginRequest
from ..services.rate_limit import login_rate_limiter
from ..services.users import get_user_by_id, get_user_by_username
from . import register

log = logging.getLogger(__name__)

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


def _issue_session(request: Request, response: Response, user) -> None:
    """Mint a JWT (carrying the row's ``must_change_password`` as ``pwc``)
    and a fresh CSRF token, and set both cookies."""
    user_uuid = user.id if isinstance(user.id, UUID) else UUID(str(user.id))
    token = create_access_token(
        user_id=user_uuid,
        username=user.username,
        role=user.role,
        must_change_password=bool(user.must_change_password),
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
    _issue_session(request, response, user)
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
    # The allow variant: a user with a temporary password must still be
    # able to sign out.
    _user: AuthUser = Depends(get_current_user_allow_password_change),
) -> Response:
    _clear_auth_cookies(response, secure=request.url.scheme == "https")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=AuthUser)
async def me(
    user: AuthUser = Depends(get_current_user_allow_password_change),
) -> AuthUser:
    """Who is signed in, including ``must_change_password`` so the SPA can
    send a user with a temporary password to the change-password screen."""
    return user


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: AuthUser = Depends(get_current_user_allow_password_change),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Replace the signed-in user's password and clear ``must_change_password``.

    CSRF-protected like every POST except login. Errors are 400, not 401, so
    the SPA does not read a typo as being signed out. Failed attempts share
    the login limiter's per-IP bucket; a lockout answers 429 here (the
    limiter's own 401 would sign the SPA out).
    """
    row = await get_user_by_id(db, user.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    if user.id == BOOTSTRAP_ADMIN_ID:
        # `upsert_admin_user()` rewrites this hash from env on every boot.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "The bootstrap admin's password is set by environment "
                "variables and cannot be changed here."
            ),
        )

    try:
        await login_rate_limiter.check(request, db)
    except HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Try again later.",
            headers=exc.headers,
        ) from None

    current_ok = verify_password(body.current_password, row.password_hash)
    await login_rate_limiter.record(request, db, succeeded=current_ok)
    if not current_ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )

    if body.new_password == body.current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current one.",
        )

    row.password_hash = hash_password(body.new_password)
    row.must_change_password = False
    await db.commit()

    # New JWT (pwc false) and new CSRF token, exactly as login does.
    _issue_session(request, response, row)
    log.info("auth.password_changed", extra={"username": row.username})
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


register(router)


__all__ = ["router"]
