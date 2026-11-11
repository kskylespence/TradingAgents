"""Temporary passwords: an admin reset forces a password change at next login.

Flow under test:

1. ``PATCH /api/users/{id}/password`` sets ``users.must_change_password``.
2. Login copies that flag into the JWT as the ``pwc`` claim.
3. ``get_current_user`` refuses a ``pwc`` session with 403
   ``password_change_required`` on every route, except ``/auth/me``,
   ``/auth/logout`` and ``/auth/change-password``, which use
   ``get_current_user_allow_password_change``.
4. ``POST /api/auth/change-password`` sets the new hash, clears the flag
   and reissues both cookies.

Uses the real cookie flow (login → cookies on the TestClient), not a
``dependency_overrides`` stand-in, so the claim and the guard are both
exercised for real.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from app.auth import (
    COOKIE_ACCESS_TOKEN,
    COOKIE_CSRF_TOKEN,
    JWT_ALGORITHM,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.models import User
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers import TEST_ADMIN_ID, TEST_USER_ID, seed_admin_user, seed_regular_user

ADMIN_PASSWORD = "admin-password"
ADMIN_HASH = hash_password(ADMIN_PASSWORD)
USER_PASSWORD = "user-password"
TEMP_PASSWORD = "temp-pass-123"
NEW_PASSWORD = "chosen-by-rob-456"
JWT_SECRET = "test-jwt-secret-not-for-production"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Yield ``(client, factory)`` on a fresh app and a file-backed SQLite DB."""
    from app import models  # noqa: F401
    from app.db import Base

    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", ADMIN_HASH)
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)

    from app.config import get_settings

    get_settings.cache_clear()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'pwc.sqlite').as_posix()}", future=True
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            await seed_admin_user(session, password_hash=ADMIN_HASH)
            await seed_regular_user(session, password=USER_PASSWORD)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_setup())

    from app.db import get_session
    from app.main import create_app
    from app.services.rate_limit import login_rate_limiter

    app = create_app()

    async def _override_get_session() -> AsyncIterator:
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = _override_get_session
    login_rate_limiter.reset()

    try:
        with TestClient(app) as client:
            yield client, factory
    finally:
        app.dependency_overrides.clear()
        login_rate_limiter.reset()
        get_settings.cache_clear()
        loop.run_until_complete(engine.dispose())
        loop.close()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _row(factory, user_id: str) -> User:
    async def _get() -> User:
        async with factory() as session:
            row = await session.get(User, user_id)
            assert row is not None
            return row

    return _run(_get())


def _login(client: TestClient, username: str, password: str):
    client.cookies.clear()
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get(COOKIE_CSRF_TOKEN) or ""}


def _admin_resets_rob(client: TestClient, password: str = TEMP_PASSWORD) -> None:
    assert _login(client, "test-admin", ADMIN_PASSWORD).status_code == 204
    resp = client.patch(
        f"/api/users/{TEST_USER_ID}/password",
        json={"password": password},
        headers=_csrf(client),
    )
    assert resp.status_code == 204, resp.text


def _flagged_rob(client: TestClient) -> None:
    """Admin resets rob, then rob signs in with the temporary password."""
    _admin_resets_rob(client)
    assert _login(client, "rob@rob", TEMP_PASSWORD).status_code == 204


def _change(client: TestClient, current: str, new: str, *, csrf: bool = True):
    return client.post(
        "/api/auth/change-password",
        json={"current_password": current, "new_password": new},
        headers=_csrf(client) if csrf else {},
    )


def _token_claims(client: TestClient) -> dict:
    token = client.cookies.get(COOKIE_ACCESS_TOKEN)
    assert token
    return decode_access_token(token)


# --------------------------------------------------------------------------- #
# Model / creation defaults                                                   #
# --------------------------------------------------------------------------- #


def test_user_model_has_must_change_password_not_null_default_false() -> None:
    col = User.__table__.c.must_change_password
    assert col.nullable is False
    assert col.server_default is not None
    assert col.default is not None and col.default.arg is False


def test_seeded_and_created_accounts_are_not_flagged(env) -> None:
    client, factory = env
    assert _row(factory, TEST_USER_ID).must_change_password is False
    assert _row(factory, TEST_ADMIN_ID).must_change_password is False

    assert _login(client, "test-admin", ADMIN_PASSWORD).status_code == 204
    created = client.post(
        "/api/users",
        json={"username": "new-person", "password": "password123"},
        headers=_csrf(client),
    )
    assert created.status_code == 201, created.text
    assert _row(factory, created.json()["id"]).must_change_password is False


@pytest.mark.asyncio
async def test_upsert_admin_user_clears_flag(env) -> None:
    from app.services.users import upsert_admin_user

    _client, factory = env
    async with factory() as session:
        row = await session.get(User, TEST_ADMIN_ID)
        row.must_change_password = True
        await session.commit()
    async with factory() as session:
        row = await upsert_admin_user(session)
        assert row.must_change_password is False


# --------------------------------------------------------------------------- #
# Reset → login → token                                                       #
# --------------------------------------------------------------------------- #


def test_reset_sets_the_flag(env) -> None:
    client, factory = env
    _admin_resets_rob(client)
    row = _row(factory, TEST_USER_ID)
    assert row.must_change_password is True
    assert verify_password(TEMP_PASSWORD, row.password_hash)


def test_login_token_carries_pwc(env) -> None:
    client, _factory = env
    assert _login(client, "rob@rob", USER_PASSWORD).status_code == 204
    assert _token_claims(client)["pwc"] is False

    _flagged_rob(client)
    assert _token_claims(client)["pwc"] is True


def test_create_access_token_defaults_pwc_false() -> None:
    from app.config import get_settings

    get_settings.cache_clear()
    token = create_access_token(user_id=UUID(TEST_USER_ID), username="rob@rob", role="user")
    assert decode_access_token(token)["pwc"] is False
    token = create_access_token(
        user_id=UUID(TEST_USER_ID), username="rob@rob", role="user", must_change_password=True
    )
    assert decode_access_token(token)["pwc"] is True


def test_me_returns_the_flag(env) -> None:
    client, _factory = env
    assert _login(client, "rob@rob", USER_PASSWORD).status_code == 204
    assert client.get("/api/auth/me").json()["must_change_password"] is False

    _flagged_rob(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True
    assert me.json()["username"] == "rob@rob"


# --------------------------------------------------------------------------- #
# Enforcement                                                                 #
# --------------------------------------------------------------------------- #


def test_flagged_session_is_blocked_on_normal_routes(env) -> None:
    client, _factory = env
    _flagged_rob(client)
    for path in ("/api/history", "/api/settings/defaults", "/api/catalog/providers"):
        resp = client.get(path)
        assert resp.status_code == 403, (path, resp.text)
        assert resp.json()["detail"] == "password_change_required"


def _flagged_token(*, role: str, user_id: str = TEST_ADMIN_ID) -> str:
    return create_access_token(
        user_id=UUID(user_id), username="someone", role=role, must_change_password=True
    )


def test_flagged_session_is_blocked_on_admin_routes(env) -> None:
    client, _factory = env
    client.cookies.clear()
    client.cookies.set(COOKIE_ACCESS_TOKEN, _flagged_token(role="admin"))
    resp = client.get("/api/users")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "password_change_required"


def test_flagged_session_can_read_me_and_log_out(env) -> None:
    client, _factory = env
    _flagged_rob(client)
    assert client.get("/api/auth/me").status_code == 200
    resp = client.post("/api/auth/logout", headers=_csrf(client))
    assert resp.status_code == 204


def test_token_without_pwc_still_works(env) -> None:
    """Sessions issued before this change carry no ``pwc``; they read as False."""
    client, _factory = env
    now = datetime.now(UTC)
    legacy = jwt.encode(
        {
            "sub": TEST_USER_ID,
            "username": "rob@rob",
            "role": "user",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    client.cookies.clear()
    client.cookies.set(COOKIE_ACCESS_TOKEN, legacy)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["must_change_password"] is False
    assert client.get("/api/history").status_code == 200


def test_non_bool_pwc_claim_is_rejected(env) -> None:
    """Fail closed: a malformed claim is an invalid token, not 'no flag'."""
    client, _factory = env
    now = datetime.now(UTC)
    bad = jwt.encode(
        {
            "sub": TEST_USER_ID,
            "username": "rob@rob",
            "role": "user",
            "pwc": "no",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    client.cookies.clear()
    client.cookies.set(COOKIE_ACCESS_TOKEN, bad)
    assert client.get("/api/history").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


# --------------------------------------------------------------------------- #
# POST /api/auth/change-password                                              #
# --------------------------------------------------------------------------- #


def test_change_password_success(env) -> None:
    client, factory = env
    _flagged_rob(client)
    old_access = client.cookies.get(COOKIE_ACCESS_TOKEN)
    old_csrf = client.cookies.get(COOKIE_CSRF_TOKEN)

    resp = _change(client, TEMP_PASSWORD, NEW_PASSWORD)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    assert NEW_PASSWORD not in resp.text

    # Fresh cookies, both of them.
    assert COOKIE_ACCESS_TOKEN in resp.cookies
    assert COOKIE_CSRF_TOKEN in resp.cookies
    assert resp.cookies[COOKIE_ACCESS_TOKEN] != old_access
    assert resp.cookies[COOKIE_CSRF_TOKEN] != old_csrf
    assert len(resp.cookies[COOKIE_CSRF_TOKEN]) == 64
    claims = decode_access_token(resp.cookies[COOKIE_ACCESS_TOKEN])
    assert claims["pwc"] is False
    assert claims["sub"] == TEST_USER_ID

    row = _row(factory, TEST_USER_ID)
    assert row.must_change_password is False
    assert verify_password(NEW_PASSWORD, row.password_hash)
    assert not verify_password(TEMP_PASSWORD, row.password_hash)

    # The new session is unblocked.
    assert client.get("/api/history").status_code == 200
    assert client.get("/api/auth/me").json()["must_change_password"] is False

    # Old password no longer logs in; new one does.
    assert _login(client, "rob@rob", TEMP_PASSWORD).status_code == 401
    assert _login(client, "rob@rob", NEW_PASSWORD).status_code == 204
    assert _token_claims(client)["pwc"] is False


def test_change_password_wrong_current_is_400(env) -> None:
    client, factory = env
    _flagged_rob(client)
    before = _row(factory, TEST_USER_ID).password_hash
    resp = _change(client, "not-the-temp-pass", NEW_PASSWORD)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Current password is incorrect."
    row = _row(factory, TEST_USER_ID)
    assert row.password_hash == before
    assert row.must_change_password is True


def test_change_password_wrong_current_counts_toward_rate_limit(env) -> None:
    """Failures share the login limiter's per-IP bucket. A lockout answers
    429, not 401, so the SPA does not read it as signed out."""
    from app.services.rate_limit import DEFAULT_MAX_ATTEMPTS

    client, _factory = env
    _flagged_rob(client)
    for _ in range(DEFAULT_MAX_ATTEMPTS):
        assert _change(client, "wrong-guess-xx", NEW_PASSWORD).status_code == 400
    locked = _change(client, TEMP_PASSWORD, NEW_PASSWORD)
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers


def test_change_password_same_as_current_is_400(env) -> None:
    client, factory = env
    _flagged_rob(client)
    resp = _change(client, TEMP_PASSWORD, TEMP_PASSWORD)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "New password must be different from the current one."
    assert _row(factory, TEST_USER_ID).must_change_password is True


def test_change_password_bootstrap_admin_is_400(env) -> None:
    client, factory = env
    assert _login(client, "test-admin", ADMIN_PASSWORD).status_code == 204
    resp = _change(client, ADMIN_PASSWORD, NEW_PASSWORD)
    assert resp.status_code == 400
    assert verify_password(ADMIN_PASSWORD, _row(factory, TEST_ADMIN_ID).password_hash)


@pytest.mark.parametrize("password", ["short1", "y" * 73, "😀" * 19])
def test_change_password_rules_422_without_echo(env, password: str) -> None:
    client, factory = env
    _flagged_rob(client)
    resp = _change(client, TEMP_PASSWORD, password)
    assert resp.status_code == 422
    assert password not in resp.text
    assert TEMP_PASSWORD not in resp.text
    assert resp.json()["detail"][0]["loc"][-1] == "new_password"
    assert _row(factory, TEST_USER_ID).must_change_password is True


def test_change_password_requires_csrf(env) -> None:
    client, factory = env
    _flagged_rob(client)
    resp = _change(client, TEMP_PASSWORD, NEW_PASSWORD, csrf=False)
    assert resp.status_code == 403
    assert _row(factory, TEST_USER_ID).must_change_password is True


def test_change_password_unauthenticated_is_401(env) -> None:
    client, _factory = env
    client.cookies.clear()
    client.cookies.set(COOKIE_CSRF_TOKEN, "a" * 64)
    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": TEMP_PASSWORD, "new_password": NEW_PASSWORD},
        headers={"X-CSRF-Token": "a" * 64},
    )
    assert resp.status_code == 401


def test_change_password_deleted_user_is_401(env) -> None:
    """A valid token for a row that no longer exists is not authenticated."""
    client, _factory = env
    ghost = "00000000-0000-0000-0000-0000000000ee"
    client.cookies.clear()
    client.cookies.set(
        COOKIE_ACCESS_TOKEN,
        create_access_token(user_id=UUID(ghost), username="ghost", role="user"),
    )
    client.cookies.set(COOKIE_CSRF_TOKEN, "b" * 64)
    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": TEMP_PASSWORD, "new_password": NEW_PASSWORD},
        headers={"X-CSRF-Token": "b" * 64},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"
