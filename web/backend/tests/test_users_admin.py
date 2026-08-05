"""Admin user-management router tests.

Covers ``GET/POST/DELETE /api/users`` — the endpoints that let an admin
list, create, and remove application user accounts from the web UI
(replacing the hardcoded ``ensure_rob_user`` env-var bootstrap).

Strategy mirrors ``tests/test_settings.py``:
- Spin up the production app via ``app.main:app`` so the router-registry
  auto-discovery wires the new router in (no explicit import needed).
- Override ``get_session`` to yield a per-test in-memory SQLite session.
- Override ``get_current_user`` — NOT ``require_admin``. FastAPI's
  ``dependency_overrides`` substitutes a dependency anywhere it appears
  in the tree, including nested inside ``require_admin``, so the real
  admin guard still runs and the 403 path is genuinely exercised.
- CSRFMiddleware challenges POST/DELETE, so those carry a matching
  cookie/header pair.

Note on ids: ``BOOTSTRAP_ADMIN_ID`` is ``…0001``, the same value as
``tests.helpers.TEST_ADMIN_ID``. These tests therefore act as a
*different* admin (``ACTING_ADMIN_ID``) so that "cannot delete the
bootstrap admin" and "cannot delete yourself" stay distinguishable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import date

import pytest
from app.auth import verify_password
from app.middleware.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.models import BOOTSTRAP_ADMIN_ID, Run, User
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.helpers import make_auth_user

CSRF_TOKEN = "test-csrf-token-1234567890"

# The admin performing the requests. Deliberately NOT the bootstrap admin.
ACTING_ADMIN_ID = "00000000-0000-0000-0000-000000000009"
# A plain user that exists in most tests.
PLAIN_USER_ID = "00000000-0000-0000-0000-000000000002"


def _run(coro):
    """Run a coroutine from sync fixture code on a fresh loop.

    pytest-asyncio tears down its per-test loop on exit, so
    ``asyncio.get_event_loop()`` is unreliable here. See test_settings.py.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def users_env(monkeypatch):
    """Yield ``(client, act_as)`` for the users router.

    ``act_as(role=..., user_id=...)`` swaps the authenticated identity
    mid-test; calling it with ``None`` removes the override entirely so
    the real cookie-reading ``get_current_user`` runs and returns 401.
    """
    from app import models  # noqa: F401 — register tables on Base.metadata
    from app.auth import get_current_user
    from app.db import Base, get_session
    from app.main import app
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

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

    # Seed the bootstrap admin, the acting admin, and one plain user.
    async def _seed() -> None:
        async with factory() as session:
            session.add_all(
                [
                    User(
                        id=str(BOOTSTRAP_ADMIN_ID),
                        username="bootstrap-admin",
                        password_hash="x" * 60,
                        role="admin",
                    ),
                    User(
                        id=ACTING_ADMIN_ID,
                        username="acting-admin",
                        password_hash="x" * 60,
                        role="admin",
                    ),
                    User(
                        id=PLAIN_USER_ID,
                        username="rob@rob",
                        password_hash="x" * 60,
                        role="user",
                    ),
                ]
            )
            await session.commit()

    _run(_seed())

    app.dependency_overrides[get_session] = _override_session

    def act_as(role: str | None = "admin", user_id: str = ACTING_ADMIN_ID) -> None:
        if role is None:
            app.dependency_overrides.pop(get_current_user, None)
            return
        app.dependency_overrides[get_current_user] = lambda: make_auth_user(
            user_id=user_id, username=f"{role}-under-test", role=role
        )

    act_as("admin")

    client = TestClient(app)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_TOKEN)

    try:
        yield client, act_as, factory
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_user, None)
        client.cookies.clear()

        async def _dispose() -> None:
            await engine.dispose()

        _run(_dispose())


@pytest.fixture
def client(users_env) -> TestClient:
    return users_env[0]


@pytest.fixture
def act_as(users_env) -> Callable[..., None]:
    return users_env[1]


@pytest.fixture
def session_factory(users_env):
    return users_env[2]


def _hdrs() -> dict[str, str]:
    """CSRF double-submit header for state-changing requests."""
    return {CSRF_HEADER_NAME: CSRF_TOKEN}


# --------------------------------------------------------------------------- #
# Authorization gating                                                        #
# --------------------------------------------------------------------------- #


def test_non_admin_is_forbidden_on_every_endpoint(client, act_as) -> None:
    """A signed-in `role="user"` gets 403, not 200 and not 401."""
    act_as("user", user_id=PLAIN_USER_ID)

    assert client.get("/api/users").status_code == 403
    assert (
        client.post(
            "/api/users",
            json={"username": "sneaky", "password": "password123"},
            headers=_hdrs(),
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/users/{BOOTSTRAP_ADMIN_ID}", headers=_hdrs()).status_code == 403
    )


def test_unauthenticated_is_rejected(client, act_as) -> None:
    """No JWT cookie at all → 401 from the real get_current_user."""
    act_as(None)

    assert client.get("/api/users").status_code == 401
    assert (
        client.post(
            "/api/users",
            json={"username": "nobody", "password": "password123"},
            headers=_hdrs(),
        ).status_code
        == 401
    )
    assert (
        client.delete(f"/api/users/{PLAIN_USER_ID}", headers=_hdrs()).status_code == 401
    )


# --------------------------------------------------------------------------- #
# POST /api/users                                                             #
# --------------------------------------------------------------------------- #


def test_create_user_returns_201_and_never_leaks_the_hash(client) -> None:
    """Response carries the new user's identity but no credential material."""
    resp = client.post(
        "/api/users",
        json={"username": "newperson", "password": "correct-horse"},
        headers=_hdrs(),
    )
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["username"] == "newperson"
    assert body["role"] == "user"
    assert body["run_count"] == 0
    assert body["id"]
    assert body["created_at"]

    # No credential material anywhere in the payload.
    assert "password" not in body
    assert "password_hash" not in body
    assert "correct-horse" not in resp.text


@pytest.mark.asyncio
async def test_created_user_password_verifies_and_role_is_user(
    client, session_factory
) -> None:
    """The stored bcrypt hash round-trips, and the role is not admin."""
    resp = client.post(
        "/api/users",
        json={"username": "verifyme", "password": "correct-horse"},
        headers=_hdrs(),
    )
    assert resp.status_code == 201, resp.text

    async with session_factory() as session:
        row = (
            await session.execute(select(User).where(User.username == "verifyme"))
        ).scalar_one()
        assert row.role == "user"
        assert row.password_hash != "correct-horse", "must not store plaintext"
        assert verify_password("correct-horse", row.password_hash)


@pytest.mark.asyncio
async def test_role_in_request_body_cannot_escalate_to_admin(
    client, session_factory
) -> None:
    """A client sending role="admin" is ignored — privilege escalation guard.

    Asserting this explicitly rather than trusting that the field simply
    goes unread: an accidental ``**body.model_dump()`` in a later refactor
    would silently turn this into an escalation path.
    """
    resp = client.post(
        "/api/users",
        json={"username": "wannabe", "password": "password123", "role": "admin"},
        headers=_hdrs(),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "user"

    async with session_factory() as session:
        row = (
            await session.execute(select(User).where(User.username == "wannabe"))
        ).scalar_one()
        assert row.role == "user"


def test_duplicate_username_is_conflict(client) -> None:
    """Exact-match collision → 409."""
    resp = client.post(
        "/api/users",
        json={"username": "rob@rob", "password": "password123"},
        headers=_hdrs(),
    )
    assert resp.status_code == 409


def test_duplicate_username_differing_only_in_case_is_conflict(client) -> None:
    """`ROB@ROB` vs `rob@rob` → 409.

    The DB unique constraint is case-sensitive, so without an explicit
    check both rows can coexist and appear identical in the UI.
    """
    resp = client.post(
        "/api/users",
        json={"username": "ROB@ROB", "password": "password123"},
        headers=_hdrs(),
    )
    assert resp.status_code == 409


def test_username_is_stripped_of_surrounding_whitespace(client) -> None:
    """"  spaced  " is stored as "spaced"."""
    resp = client.post(
        "/api/users",
        json={"username": "  spaced  ", "password": "password123"},
        headers=_hdrs(),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["username"] == "spaced"


@pytest.mark.parametrize(
    "password",
    [
        "short",  # under the 8-char minimum
        "x" * 73,  # over bcrypt's 72-byte ceiling
    ],
)
def test_password_length_is_validated(client, password: str) -> None:
    """Too short → 422. Too long → 422 rather than silent bcrypt truncation."""
    resp = client.post(
        "/api/users",
        json={"username": f"len{len(password)}", "password": password},
        headers=_hdrs(),
    )
    assert resp.status_code == 422


def test_username_too_short_is_validated(client) -> None:
    """Under 3 characters → 422."""
    resp = client.post(
        "/api/users",
        json={"username": "ab", "password": "password123"},
        headers=_hdrs(),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# GET /api/users                                                              #
# --------------------------------------------------------------------------- #


def test_list_users_returns_seeded_accounts_without_hashes(client) -> None:
    """All three seeded users appear; no response contains a hash."""
    resp = client.get("/api/users")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    by_name = {u["username"]: u for u in body}
    assert {"bootstrap-admin", "acting-admin", "rob@rob"} <= set(by_name)

    assert by_name["rob@rob"]["role"] == "user"
    assert by_name["bootstrap-admin"]["role"] == "admin"

    for entry in body:
        assert "password_hash" not in entry
        assert "password" not in entry
        assert set(entry.keys()) == {
            "id",
            "username",
            "role",
            "created_at",
            "run_count",
        }


@pytest.mark.asyncio
async def test_list_users_reports_run_count(client, session_factory) -> None:
    """A user owning runs reports the count; others report 0."""
    async with session_factory() as session:
        session.add(
            Run(
                id="00000000-0000-0000-0000-0000000000f1",
                user_id=PLAIN_USER_ID,
                ticker="NVDA",
                asset_type="equity",
                analysis_date=date(2026, 1, 15),
                analysts=["market"],
                research_depth=1,
                llm_provider="ollama",
                quick_think_llm="glm-5.2",
                deep_think_llm="glm-5.2",
                status="completed",
            )
        )
        await session.commit()

    resp = client.get("/api/users")
    assert resp.status_code == 200, resp.text
    by_name = {u["username"]: u for u in resp.json()}

    assert by_name["rob@rob"]["run_count"] == 1
    assert by_name["acting-admin"]["run_count"] == 0


# --------------------------------------------------------------------------- #
# DELETE /api/users/{user_id}                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_removes_the_row(client, session_factory) -> None:
    """A user with no runs deletes cleanly and disappears from the list."""
    resp = client.delete(f"/api/users/{PLAIN_USER_ID}", headers=_hdrs())
    assert resp.status_code == 204, resp.text

    async with session_factory() as session:
        row = await session.get(User, PLAIN_USER_ID)
        assert row is None

    listed = {u["username"] for u in client.get("/api/users").json()}
    assert "rob@rob" not in listed


def test_cannot_delete_the_bootstrap_admin(client) -> None:
    """The env-seeded admin is re-upserted on every boot; deleting is a no-op."""
    resp = client.delete(f"/api/users/{BOOTSTRAP_ADMIN_ID}", headers=_hdrs())
    assert resp.status_code == 400


def test_cannot_delete_yourself(client) -> None:
    """Guards against an admin locking themselves out."""
    resp = client.delete(f"/api/users/{ACTING_ADMIN_ID}", headers=_hdrs())
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_user_with_runs_is_conflict(client, session_factory) -> None:
    """Runs are the app's output — removing a login must not destroy history.

    ``Run.user_id`` is ``ondelete="RESTRICT"``, but SQLite does not enforce
    foreign keys without a per-connection PRAGMA, so the guard must live in
    application code for the behaviour to match Postgres.
    """
    async with session_factory() as session:
        session.add(
            Run(
                id="00000000-0000-0000-0000-0000000000f2",
                user_id=PLAIN_USER_ID,
                ticker="AAPL",
                asset_type="equity",
                analysis_date=date(2026, 1, 16),
                analysts=["market"],
                research_depth=1,
                llm_provider="ollama",
                quick_think_llm="glm-5.2",
                deep_think_llm="glm-5.2",
                status="completed",
            )
        )
        await session.commit()

    resp = client.delete(f"/api/users/{PLAIN_USER_ID}", headers=_hdrs())
    assert resp.status_code == 409, resp.text

    # The row survives.
    async with session_factory() as session:
        assert await session.get(User, PLAIN_USER_ID) is not None


def test_delete_unknown_user_is_not_found(client) -> None:
    """An id that doesn't exist → 404."""
    resp = client.delete(
        "/api/users/00000000-0000-0000-0000-0000000000ff", headers=_hdrs()
    )
    assert resp.status_code == 404
