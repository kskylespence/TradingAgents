"""Settings router tests.

Covers /api/settings/api-keys and /api/settings/defaults endpoints.

Strategy:
- Spin up the production app via `app.main:app` (so the router-registry
  auto-discovery wires our router in), then:
  * Override `get_session` to yield a per-test in-memory SQLite session
    so we don't touch the real DB.
  * Override `get_current_user` (the one resolved by our router at
    import time, whether it came from `app.auth` or the soft stub) to
    inject a synthetic user — keeps the test agnostic of whether the
    AUTH team has landed yet.
- The CSRFMiddleware is active on the production app and challenges
  PUT/DELETE. Tests issue PUT/DELETE with matching cookie + header.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import crypto
from app.middleware.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.models import ApiKey
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV


# A static CSRF token for state-changing requests; cookie and header must match.
CSRF_TOKEN = "test-csrf-token-1234567890"

# Known provider envs (non-None values from the canonical mapping).
KNOWN_ENVS: set[str] = {env for env in PROVIDER_API_KEY_ENV.values() if env}


def _run(coro):
    """Run an async coroutine to completion from sync code.

    Uses ``asyncio.new_event_loop()`` defensively: a prior test in the
    session may have closed the default loop (pytest-asyncio's per-test
    loop is torn down on test exit), so ``asyncio.get_event_loop()`` is
    unreliable in sync fixture code. The TestClient runs the FastAPI
    app inside its own threaded loop, so spinning a fresh loop here
    purely for schema-create + dispose is safe and isolated.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def settings_client(monkeypatch) -> AsyncIterator[TestClient]:
    """A TestClient configured for the settings router.

    Provides:
    - Fresh in-memory SQLite engine + override of `get_session`.
    - Override of `get_current_user` to inject a synthetic user.
    - Pre-set CSRF cookie so PUT/DELETE pass the CSRFMiddleware check.
    """
    # Make sure the crypto cache is fresh (conftest sets a Fernet key in env
    # but other tests may have rotated it).
    crypto.reset_cache()

    from app.db import get_session
    from app.main import app
    from app.routers.settings import get_current_user
    from app.schemas import AuthUser

    # In-memory SQLite engine, dedicated to this test. We use
    # ``StaticPool`` so every connection through this engine hits the
    # SAME underlying SQLite memory database — TestClient runs the
    # FastAPI route in a worker thread, and aiosqlite would otherwise
    # hand it a brand-new (empty) :memory: DB.
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    from app.db import Base
    from app import models  # noqa: F401 — register tables on Base.metadata

    async def _create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run(_create_schema())

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: AuthUser(username="tester")

    client = TestClient(app)
    # Pre-set the CSRF cookie so PUT/DELETE pass the CSRFMiddleware.
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_TOKEN)

    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_user, None)
        client.cookies.clear()

        async def _dispose() -> None:
            await engine.dispose()

        _run(_dispose())


def _state_headers() -> dict[str, str]:
    """Headers required for state-changing requests (CSRF double-submit)."""
    return {CSRF_HEADER_NAME: CSRF_TOKEN}


# --------------------------------------------------------------------------- #
# GET /api-keys                                                               #
# --------------------------------------------------------------------------- #


def test_list_api_keys_initially_all_unconfigured(settings_client: TestClient) -> None:
    """One entry per known provider env, all `configured=false` initially."""
    resp = settings_client.get("/api/settings/api-keys")
    assert resp.status_code == 200
    body = resp.json()

    assert isinstance(body, list) and len(body) > 0
    by_env = {entry["provider_env"]: entry for entry in body}

    # Every known provider env appears exactly once.
    assert set(by_env.keys()) == KNOWN_ENVS

    for entry in body:
        assert entry["configured"] is False
        assert entry["last_updated"] is None
        # Shape sanity.
        assert set(entry.keys()) >= {"provider_env", "configured", "last_updated"}


# --------------------------------------------------------------------------- #
# PUT /api-keys/{env}                                                         #
# --------------------------------------------------------------------------- #


def test_put_api_key_stores_encrypted_and_never_returns_plaintext(
    settings_client: TestClient,
) -> None:
    """PUT writes an encrypted row; response body NEVER echoes plaintext."""
    secret = "sk-test"
    resp = settings_client.put(
        "/api/settings/api-keys/OPENAI_API_KEY",
        json={"value": secret},
        headers=_state_headers(),
    )
    assert resp.status_code == 204
    # 204 has no body; if it did, ensure plaintext is not in it.
    assert secret not in resp.text


def test_get_api_keys_after_put_shows_only_target_configured(
    settings_client: TestClient,
) -> None:
    """After PUT OPENAI_API_KEY, only that env reads `configured=true`."""
    secret = "sk-test"
    put_resp = settings_client.put(
        "/api/settings/api-keys/OPENAI_API_KEY",
        json={"value": secret},
        headers=_state_headers(),
    )
    assert put_resp.status_code == 204

    list_resp = settings_client.get("/api/settings/api-keys")
    assert list_resp.status_code == 200
    by_env = {entry["provider_env"]: entry for entry in list_resp.json()}

    assert by_env["OPENAI_API_KEY"]["configured"] is True
    assert by_env["OPENAI_API_KEY"]["last_updated"] is not None

    # All others remain unconfigured.
    for env, entry in by_env.items():
        if env == "OPENAI_API_KEY":
            continue
        assert entry["configured"] is False, f"{env} unexpectedly configured"
        assert entry["last_updated"] is None


@pytest.mark.asyncio
async def test_put_api_key_round_trips_through_decrypt(
    settings_client: TestClient,
) -> None:
    """Stored ciphertext decrypts back to the original plaintext."""
    secret = "sk-test"
    resp = settings_client.put(
        "/api/settings/api-keys/OPENAI_API_KEY",
        json={"value": secret},
        headers=_state_headers(),
    )
    assert resp.status_code == 204

    # Reach into the override'd session to fetch the stored row directly.
    from app.db import get_session
    from app.main import app

    override = app.dependency_overrides[get_session]
    agen = override()
    session: AsyncSession = await agen.__anext__()
    try:
        result = await session.execute(
            select(ApiKey).where(ApiKey.provider_env == "OPENAI_API_KEY")
        )
        row = result.scalar_one()
        assert row.encrypted_value, "encrypted_value should not be empty"
        assert row.encrypted_value != secret.encode(), (
            "stored value must be ciphertext, never plaintext bytes"
        )
        assert crypto.decrypt(row.encrypted_value) == secret
    finally:
        try:
            await agen.__anext__()
        except StopAsyncIteration:
            pass


def test_put_api_key_upserts_overwrites_previous_value(
    settings_client: TestClient,
) -> None:
    """A second PUT to the same env replaces the value (UPSERT, not duplicate)."""
    first = settings_client.put(
        "/api/settings/api-keys/OPENAI_API_KEY",
        json={"value": "sk-one"},
        headers=_state_headers(),
    )
    assert first.status_code == 204

    second = settings_client.put(
        "/api/settings/api-keys/OPENAI_API_KEY",
        json={"value": "sk-two"},
        headers=_state_headers(),
    )
    assert second.status_code == 204

    # Listing still shows exactly one entry per known env.
    resp = settings_client.get("/api/settings/api-keys")
    by_env = {entry["provider_env"]: entry for entry in resp.json()}
    assert by_env["OPENAI_API_KEY"]["configured"] is True


def test_put_api_key_unknown_env_is_rejected(settings_client: TestClient) -> None:
    """An env var not in PROVIDER_API_KEY_ENV → 400."""
    resp = settings_client.put(
        "/api/settings/api-keys/UNKNOWN_ENV",
        json={"value": "whatever"},
        headers=_state_headers(),
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# DELETE /api-keys/{env}                                                      #
# --------------------------------------------------------------------------- #


def test_delete_api_key_removes_row_and_returns_204(
    settings_client: TestClient,
) -> None:
    """After PUT then DELETE, the env is `configured=false` again."""
    put_resp = settings_client.put(
        "/api/settings/api-keys/OPENAI_API_KEY",
        json={"value": "sk-test"},
        headers=_state_headers(),
    )
    assert put_resp.status_code == 204

    del_resp = settings_client.delete(
        "/api/settings/api-keys/OPENAI_API_KEY",
        headers=_state_headers(),
    )
    assert del_resp.status_code == 204

    list_resp = settings_client.get("/api/settings/api-keys")
    by_env = {entry["provider_env"]: entry for entry in list_resp.json()}
    assert by_env["OPENAI_API_KEY"]["configured"] is False
    assert by_env["OPENAI_API_KEY"]["last_updated"] is None


def test_delete_unknown_env_is_rejected(settings_client: TestClient) -> None:
    """DELETE on a non-known env validates too, returning 400."""
    resp = settings_client.delete(
        "/api/settings/api-keys/UNKNOWN_ENV",
        headers=_state_headers(),
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# GET /defaults                                                               #
# --------------------------------------------------------------------------- #


def test_get_defaults_returns_default_shape(settings_client: TestClient) -> None:
    """With no row present, returns lite VPS-friendly schema defaults."""
    resp = settings_client.get("/api/settings/defaults")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enable_checkpoint"] is True
    assert body["research_depth"] == 1
    assert body["analysts"] == ["market", "social"]
    for optional in (
        "llm_provider",
        "quick_think_llm",
        "deep_think_llm",
        "output_language",
        "thinking_config",
    ):
        assert body.get(optional) is None


# --------------------------------------------------------------------------- #
# PUT /defaults                                                               #
# --------------------------------------------------------------------------- #


def test_put_defaults_merges_partial_update_and_preserves_other_fields(
    settings_client: TestClient,
) -> None:
    """PUT {llm_provider: 'openai'} then GET returns it with others preserved."""
    put_resp = settings_client.put(
        "/api/settings/defaults",
        json={"llm_provider": "openai"},
        headers=_state_headers(),
    )
    assert put_resp.status_code == 200
    body = put_resp.json()
    assert body["llm_provider"] == "openai"
    # Other fields keep their defaults.
    assert body["enable_checkpoint"] is True

    # GET round-trip confirms persistence.
    get_resp = settings_client.get("/api/settings/defaults")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["llm_provider"] == "openai"
    assert got["enable_checkpoint"] is True

    # A second PUT setting a different field doesn't clobber llm_provider.
    second_put = settings_client.put(
        "/api/settings/defaults",
        json={"quick_think_llm": "gpt-5.4-mini"},
        headers=_state_headers(),
    )
    assert second_put.status_code == 200
    merged = second_put.json()
    assert merged["llm_provider"] == "openai"
    assert merged["quick_think_llm"] == "gpt-5.4-mini"
