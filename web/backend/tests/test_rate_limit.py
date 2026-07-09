"""Tests for app.services.rate_limit.LoginRateLimiter.

Covers the three behaviors the plan calls out explicitly:
- 5 failed attempts allowed, 6th returns 401 with ``Retry-After``.
- Successful login resets the bucket for that IP.
- Restart simulation: clear the in-memory state, ensure recent DB rows
  still block.

Tests use the auth router via TestClient (end-to-end) plus a few unit
tests directly on the limiter for the restart case (which would
otherwise need a heavyweight setup).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from passlib.hash import bcrypt
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

PASSWORD = "password"
PASSWORD_HASH = bcrypt.hash(PASSWORD)


# --------------------------------------------------------------------------- #
# Fixtures (mirror test_auth.py — kept local so tests can be reordered freely)#
# --------------------------------------------------------------------------- #


@pytest.fixture
def shared_engine(tmp_path) -> AsyncEngine:
    from app import models  # noqa: F401
    from app.db import Base

    db_path = tmp_path / "ratelimit.sqlite"
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
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-not-for-production")
    monkeypatch.setenv("JWT_TTL_SECONDS", "3600")

    from app.config import get_settings
    get_settings.cache_clear()

    from app.db import get_session
    from app.main import create_app
    from app.services.rate_limit import login_rate_limiter

    app = create_app()
    factory = async_sessionmaker(bind=shared_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncIterator:
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = _override_get_session
    login_rate_limiter.reset()

    yield app

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client(test_app) -> TestClient:
    with TestClient(test_app) as c:
        yield c


# --------------------------------------------------------------------------- #
# End-to-end tests via the auth router                                        #
# --------------------------------------------------------------------------- #


def test_sixth_failed_attempt_returns_401_with_retry_after(client: TestClient) -> None:
    """5 failures are allowed; the 6th gets the lockout response."""
    for i in range(5):
        resp = client.post(
            "/api/auth/login",
            json={"username": "test-admin", "password": "wrong"},
        )
        assert resp.status_code == 401, f"attempt #{i+1}: {resp.text}"
        assert resp.json() == {"detail": "Invalid credentials"}

    # 6th attempt: still 401, but with the lockout message + Retry-After
    resp = client.post(
        "/api/auth/login",
        json={"username": "test-admin", "password": "wrong"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Too many login attempts"}
    assert "retry-after" in {k.lower() for k in resp.headers.keys()}
    retry_after = resp.headers["retry-after"]
    assert retry_after.isdigit()
    assert int(retry_after) > 0


def test_successful_login_resets_bucket(client: TestClient) -> None:
    """A correct password clears the IP's failure tally."""
    # 4 failures (one below the cap)
    for _ in range(4):
        resp = client.post(
            "/api/auth/login",
            json={"username": "test-admin", "password": "wrong"},
        )
        assert resp.status_code == 401

    # Now log in successfully
    resp = client.post(
        "/api/auth/login",
        json={"username": "test-admin", "password": PASSWORD},
    )
    assert resp.status_code == 204

    # After success, 5 more failures should still be allowed (bucket reset)
    for i in range(5):
        resp = client.post(
            "/api/auth/login",
            json={"username": "test-admin", "password": "wrong"},
        )
        assert resp.status_code == 401, f"post-reset attempt #{i+1}"
        assert resp.json() == {"detail": "Invalid credentials"}

    # 6th post-reset attempt is now the lockout.
    resp = client.post(
        "/api/auth/login",
        json={"username": "test-admin", "password": "wrong"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Too many login attempts"}


def test_restart_simulation_db_rows_still_block(
    monkeypatch, shared_engine
) -> None:
    """Reset the in-memory limiter (== process restart) and confirm the
    DB-backed failure history still gates the next attempt.

    Strategy:
    - Use the live app to drive 5 failed attempts.
    - Clear the in-memory bucket (sim a restart).
    - The next request should re-hydrate from `login_attempts` and 401.
    """
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-not-for-production")
    monkeypatch.setenv("JWT_TTL_SECONDS", "3600")

    from app.config import get_settings
    get_settings.cache_clear()

    from app.db import get_session
    from app.main import create_app
    from app.services.rate_limit import login_rate_limiter

    app = create_app()
    factory = async_sessionmaker(bind=shared_engine, expire_on_commit=False)

    async def _override_get_session() -> AsyncIterator:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    login_rate_limiter.reset()

    try:
        with TestClient(app) as c:
            # 5 failures fill the bucket
            for _ in range(5):
                resp = c.post(
                    "/api/auth/login",
                    json={"username": "test-admin", "password": "wrong"},
                )
                assert resp.status_code == 401

            # Simulate a process restart
            login_rate_limiter.reset()

            # Next attempt: should re-hydrate from DB rows and reject.
            resp = c.post(
                "/api/auth/login",
                json={"username": "test-admin", "password": "wrong"},
            )
            assert resp.status_code == 401
            assert resp.json() == {"detail": "Too many login attempts"}
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Direct unit tests on the limiter                                            #
# --------------------------------------------------------------------------- #


def test_client_ip_returns_request_client_host(monkeypatch) -> None:
    """``client_ip`` returns ``request.client.host`` and IGNORES XFF.

    Uvicorn's ``--proxy-headers`` (configured in ``entrypoint.sh``)
    resolves the real client IP from ``X-Forwarded-For`` upstream of the
    app, populating ``request.client.host`` with the trusted value. The
    app must NOT re-parse XFF itself, because the leftmost value of XFF
    is attacker-controlled (see ``test_rate_limit_resists_xff_spoof``).
    """
    from app.services.rate_limit import client_ip
    from starlette.requests import Request

    # XFF present but must be ignored — uvicorn already gave us the real
    # client in ``scope["client"]``.
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")],
        "client": ("10.0.0.99", 12345),
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
    }
    req = Request(scope)
    assert client_ip(req) == "10.0.0.99"

    # No XFF: still returns the trusted client host.
    scope_no_xff = {**scope, "headers": []}
    req2 = Request(scope_no_xff)
    assert client_ip(req2) == "10.0.0.99"

    # No client (e.g. raw ASGI test): falls back to "unknown".
    scope_no_client = {**scope, "client": None, "headers": []}
    req3 = Request(scope_no_client)
    assert client_ip(req3) == "unknown"


def test_rate_limit_resists_xff_spoof() -> None:
    """Rotating ``X-Forwarded-For`` does NOT create a fresh per-IP bucket.

    Pre-fix, ``client_ip`` parsed the leftmost XFF value, which a remote
    attacker can fully control. Rotating XFF per request would defeat
    the limiter entirely. Post-fix we rely on uvicorn's proxy-headers
    middleware (enabled in ``entrypoint.sh`` via ``--proxy-headers
    --forwarded-allow-ips='*'``) to set ``request.client.host`` to the
    real client IP. The app no longer touches XFF.
    """
    from app.services.rate_limit import client_ip, login_rate_limiter
    from starlette.requests import Request

    login_rate_limiter.reset()

    spoofs = [
        b"1.2.3.4",
        b"5.6.7.8",
        b"9.10.11.12",
    ]
    for spoof in spoofs:
        for _ in range(10):
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/login",
                "headers": [(b"x-forwarded-for", spoof)],
                "client": ("10.0.0.1", 12345),
                "query_string": b"",
                "scheme": "http",
                "server": ("testserver", 80),
                "root_path": "",
            }
            req = Request(scope)
            assert client_ip(req) == "10.0.0.1", (
                f"spoofed XFF {spoof!r} leaked into client_ip — limiter is "
                f"bypassable"
            )


def test_seconds_until_free_is_positive() -> None:
    """When the bucket is full, Retry-After must be > 0."""
    from app.services.rate_limit import LoginRateLimiter, _Bucket

    limiter = LoginRateLimiter(max_attempts=2, window_seconds=300)
    bucket = _Bucket()
    bucket.timestamps.append(datetime.now(timezone.utc) - timedelta(seconds=10))
    bucket.timestamps.append(datetime.now(timezone.utc))
    delay = limiter._seconds_until_free(bucket)
    assert delay > 0
    # Should be close to (window - 10s)
    assert 280 <= delay <= 300
