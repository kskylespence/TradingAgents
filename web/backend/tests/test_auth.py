"""Tests for app.auth, app.routers.auth and the cookie/JWT flow.

Strategy:
- A real bcrypt hash for the password "password" is monkeypatched onto
  settings; `get_settings.cache_clear()` is used so the change is visible
  to the running app.
- The router's DB dependency (`get_session`) is overridden to point at a
  per-test SQLite engine so the `login_attempts` writes don't bleed
  between tests.
- The in-memory rate limiter singleton is reset between tests so a
  previous test's failures don't tip the bucket over.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from passlib.hash import bcrypt
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)


PASSWORD = "password"
# Bcrypt is deliberately slow; precompute once per process.
PASSWORD_HASH = bcrypt.hash(PASSWORD)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def shared_engine(tmp_path) -> AsyncEngine:
    """A per-test file-based SQLite engine shared across the request and
    the test's own seeders.

    File-based (not ``:memory:``) so the engine the TestClient route uses
    can see rows seeded from a sibling session. Uses the same
    ``asyncio.get_event_loop().run_until_complete`` pattern as
    ``test_history.py`` to avoid stepping on the pytest-asyncio loop
    that other test files reuse later in the session.
    """
    from app.db import Base
    from app import models  # noqa: F401 - registers tables on Base.metadata

    db_path = tmp_path / "auth.sqlite"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True
    )

    async def _create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_create_schema())
    try:
        yield engine
    finally:
        asyncio.get_event_loop().run_until_complete(engine.dispose())


@pytest.fixture
def test_app(monkeypatch, shared_engine):
    """Build a fresh app, override DB + rate limiter, return (app, client)."""
    # Ensure settings reflect our test bcrypt hash + small JWT TTL.
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-not-for-production")
    monkeypatch.setenv("JWT_TTL_SECONDS", "3600")

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    from app.db import get_session
    from app.services.rate_limit import login_rate_limiter

    app = create_app()

    # Override the DB dep so requests use our per-test engine.
    factory = async_sessionmaker(bind=shared_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncIterator:
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = _override_get_session

    # Reset the global rate-limiter so previous tests' state is gone.
    login_rate_limiter.reset()

    yield app

    # Cleanup
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client(test_app) -> TestClient:
    with TestClient(test_app) as c:
        yield c


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_login_happy_path_sets_cookies_and_returns_204(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "test-admin", "password": PASSWORD},
    )
    assert resp.status_code == 204, resp.text
    # No body
    assert resp.content == b""
    # Both cookies set
    assert "access_token" in resp.cookies
    assert "csrf_token" in resp.cookies
    # CSRF cookie is 64 hex chars (32 bytes -> hex)
    assert len(resp.cookies["csrf_token"]) == 64
    # access_token is a JWT (three dot-separated b64 segments)
    assert resp.cookies["access_token"].count(".") == 2


def test_login_wrong_password_returns_401_and_records_attempt(
    client: TestClient, shared_engine
) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "test-admin", "password": "WRONG"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid credentials"}
    # No cookies set on failure
    assert "access_token" not in resp.cookies
    assert "csrf_token" not in resp.cookies
    # The attempt must have been persisted as a failure.
    from sqlalchemy import select
    from app.models import LoginAttempt

    factory = async_sessionmaker(bind=shared_engine, expire_on_commit=False)

    async def _count() -> tuple[int, int]:
        async with factory() as session:
            rows = (await session.execute(select(LoginAttempt))).scalars().all()
            return len(rows), sum(1 for r in rows if not r.succeeded)

    total, failed = asyncio.get_event_loop().run_until_complete(_count())
    assert total == 1
    assert failed == 1


def test_login_wrong_username_returns_401(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "wrong-user", "password": PASSWORD},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid credentials"}


def test_me_without_cookie_returns_401(client: TestClient) -> None:
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


def test_me_with_tampered_jwt_returns_401(client: TestClient) -> None:
    # Login to get a real JWT, then mangle it.
    resp = client.post(
        "/api/auth/login",
        json={"username": "test-admin", "password": PASSWORD},
    )
    assert resp.status_code == 204
    real = client.cookies.get("access_token")
    assert real is not None

    # Flip a character in the signature segment.
    head, payload, sig = real.split(".")
    bad_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    tampered = ".".join([head, payload, bad_sig])

    # Send ONLY the tampered cookie. httpx.Cookies.set() may add a
    # duplicate cookie rather than replacing when a domain mismatch is
    # involved, so clear the jar and set the tampered cookie explicitly
    # with the testserver domain so we don't accidentally ship the valid
    # one alongside it.
    client.cookies.clear()
    resp = client.get(
        "/api/auth/me",
        cookies={"access_token": tampered},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid token"


def test_me_with_expired_jwt_returns_401(
    monkeypatch, shared_engine
) -> None:
    """An already-expired JWT must be rejected with "Token expired".

    The plan calls for ``jwt_ttl_seconds=-1`` via monkeypatch + clearing
    the settings cache. We then mint a token (which immediately has
    ``exp`` in the past), inject it as the cookie directly (bypassing
    the login endpoint's cookie-clamping that would otherwise drop a
    ``max_age=0`` cookie in the test client), and hit ``/me``.
    """
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-not-for-production")
    monkeypatch.setenv("JWT_TTL_SECONDS", "-1")

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    from app.auth import create_access_token, COOKIE_ACCESS_TOKEN
    from app.db import get_session
    from app.services.rate_limit import login_rate_limiter

    app = create_app()
    factory = async_sessionmaker(bind=shared_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncIterator:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    login_rate_limiter.reset()

    try:
        # Mint an already-expired token (TTL is -1s so exp < iat).
        expired_token = create_access_token("test-admin")
        with TestClient(app) as c:
            c.cookies.set(COOKIE_ACCESS_TOKEN, expired_token)
            resp = c.get("/api/auth/me")
            assert resp.status_code == 401
            assert resp.json()["detail"] == "Token expired"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_login_then_me_round_trip(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "test-admin", "password": PASSWORD},
    )
    assert resp.status_code == 204
    # TestClient automatically persists cookies across calls.
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {"username": "test-admin"}


def test_logout_clears_cookies(client: TestClient) -> None:
    # Login first
    resp = client.post(
        "/api/auth/login",
        json={"username": "test-admin", "password": PASSWORD},
    )
    assert resp.status_code == 204
    # Logout is a state-changing POST → CSRF middleware requires the
    # double-submit header. Echo the cookie value as required.
    csrf = client.cookies.get("csrf_token")
    assert csrf is not None
    resp = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 204
    # Cookies should be cleared in the response (Set-Cookie with empty
    # value + past expiry); TestClient's jar reflects that as deletion.
    # Either the cookie is absent or its value is empty.
    assert client.cookies.get("access_token") in (None, "")
    # And /me should now reject.
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_logout_without_jwt_returns_401(client: TestClient) -> None:
    """Logout must require auth — proves the plan's `JWT` annotation.

    Bypass CSRF (would 403 first) by supplying a matching csrf_token
    cookie + X-CSRF-Token header; with no access_token cookie the JWT
    dependency must return 401.
    """
    client.cookies.set("csrf_token", "dummy-csrf-value")
    resp = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": "dummy-csrf-value"},
    )
    assert resp.status_code == 401, resp.text


def test_decode_access_token_rejects_garbage() -> None:
    """Unit test on the helper itself — not a TestClient round-trip."""
    import os
    os.environ["JWT_SECRET"] = "test-jwt-secret-not-for-production"
    from app.config import get_settings
    get_settings.cache_clear()
    from app.auth import decode_access_token, create_access_token
    from fastapi import HTTPException

    # Garbage string
    with pytest.raises(HTTPException) as ei:
        decode_access_token("not.a.jwt")
    assert ei.value.status_code == 401

    # Correct round-trip
    token = create_access_token("alice")
    payload = decode_access_token(token)
    assert payload["sub"] == "alice"
    assert "exp" in payload and "iat" in payload
