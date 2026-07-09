"""Tests for POST /api/runs/{run_id}/retry — Fix E.

When a run fails (or is cancelled) the user shouldn't have to re-fill the
``NewRun`` form just to re-submit. The retry endpoint reconstructs the
``RunRequest`` from the persisted columns and queues a sibling run via
the same ``run_service.start_run`` path used by ``POST /api/runs`` —
giving us identical catalog validation + env-credential checks + global
lock semantics for free.

Auth + CSRF are bypassed the same way the rest of this test suite does
(``app.dependency_overrides[get_current_user]`` + monkeypatched
``app.middleware.csrf._csrf_required``). The endpoint inherits both
guards from the router-level dependency.

aiosqlite gotcha: ``uuid.UUID`` cannot bind to the
``String(36).with_variant(UUID, "postgresql")`` column on SQLite —
always coerce to ``str(uuid_instance)`` when inserting through the test
factory. See ``test_runs_smoke.py::test_resume_happy_path_returns_new_run_id``
for the same pattern.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# --------------------------------------------------------------------------- #
# Fixtures (mirrors test_runs_smoke.py)                                       #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _enable_fake_llm(monkeypatch):
    monkeypatch.setenv("FAKE_LLM", "1")


@pytest.fixture
async def runs_engine(tmp_path):
    from app import models  # noqa: F401 — register tables on Base.metadata
    from app.db import Base

    db_path = tmp_path / "runs.sqlite"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest.fixture
def patched_session_factory(runs_engine, monkeypatch):
    from app import db as db_mod
    from app.services import event_bus as eb_mod

    _engine, factory = runs_engine

    monkeypatch.setattr(db_mod, "get_session_factory", lambda: factory)
    monkeypatch.setattr(eb_mod, "get_session_factory", lambda: factory)
    eb_mod.reset_for_tests()
    eb_mod._lock = asyncio.Lock()
    yield factory
    eb_mod.reset_for_tests()


@pytest.fixture
def client(runs_engine, patched_session_factory) -> Iterator[TestClient]:
    from app.auth import get_current_user
    from app.db import get_session
    from app.main import app
    from app.schemas import AuthUser
    from app.services import run_service

    _engine, factory = runs_engine

    async def _override_session():
        async with factory() as session:
            yield session

    def _override_user() -> AuthUser:
        return AuthUser(username="test")

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = _override_user

    run_service.reset_for_tests()

    try:
        from sse_starlette.sse import AppStatus  # type: ignore

        AppStatus.should_exit = False
        AppStatus.should_exit_event = None
    except ImportError:
        pass

    import app.middleware.csrf as csrf_mod

    orig = csrf_mod._csrf_required
    csrf_mod._csrf_required = lambda method, path: False  # type: ignore[assignment]

    try:
        with TestClient(app) as c:
            yield c
    finally:
        csrf_mod._csrf_required = orig
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_user, None)
        run_service.reset_for_tests()


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _seed_run(factory, *, status: str, run_id: str | None = None, **overrides) -> str:
    """Seed a runs row directly through the test engine. Returns the str id."""
    from app.models import Run

    run_id = run_id or str(uuid.uuid4())
    payload = {
        "id": run_id,
        "ticker": "RETRYME",
        "asset_type": "stock",
        "analysis_date": date(2026, 5, 19),
        "analysts": ["market", "news"],
        "research_depth": 1,
        "llm_provider": "openai",
        "quick_think_llm": "gpt-5.4-mini",
        "deep_think_llm": "gpt-5.4",
        "thinking_config": None,
        "output_language": "English",
        "checkpoint_enabled": False,
        "status": status,
        "started_at": datetime(2026, 5, 19, 12, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 5, 19, 12, 5, tzinfo=timezone.utc),
        "error_message": "upstream transient blip" if status == "failed" else None,
    }
    payload.update(overrides)

    async def _do() -> None:
        async with factory() as session:
            session.add(Run(**payload))
            await session.commit()

    asyncio.get_event_loop().run_until_complete(_do())
    return run_id


def _wait_for_status(
    client: TestClient, run_id: str, statuses: set[str], timeout: float = 10.0
) -> dict:
    import time

    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}")
        if resp.status_code == 200:
            last = resp.json()
            if last.get("status") in statuses:
                return last
        time.sleep(0.05)
    raise AssertionError(
        f"Run {run_id} never reached {statuses}; last status={last.get('status')}"
    )


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #


def test_retry_failed_run_returns_new_run_id(
    client: TestClient, runs_engine
) -> None:
    """Seed a failed run, POST /retry, assert 200 + {run_id, parent_run_id}."""
    _engine, factory = runs_engine
    parent_id = _seed_run(factory, status="failed")

    try:
        resp = client.post(f"/api/runs/{parent_id}/retry")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) >= {"run_id", "parent_run_id"}
        assert body["parent_run_id"] == parent_id
        new_id = uuid.UUID(body["run_id"])
        assert str(new_id) != parent_id, "retry must mint a NEW run id"

        # Best-effort cleanup so the global lock doesn't block teardown.
        client.post(f"/api/runs/{new_id}/cancel")
        _wait_for_status(
            client, str(new_id), {"cancelled", "completed", "failed"}, timeout=10.0
        )
    finally:
        pass


def test_retry_cancelled_run_returns_new_run_id(
    client: TestClient, runs_engine
) -> None:
    """Cancelled runs are also retryable (same code path as failed)."""
    _engine, factory = runs_engine
    parent_id = _seed_run(factory, status="cancelled")

    resp = client.post(f"/api/runs/{parent_id}/retry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parent_run_id"] == parent_id
    new_id = uuid.UUID(body["run_id"])
    assert str(new_id) != parent_id

    client.post(f"/api/runs/{new_id}/cancel")
    _wait_for_status(
        client, str(new_id), {"cancelled", "completed", "failed"}, timeout=10.0
    )


# --------------------------------------------------------------------------- #
# Rejection paths                                                             #
# --------------------------------------------------------------------------- #


def test_retry_rejects_completed_run(client: TestClient, runs_engine) -> None:
    """A completed run is not retryable — 400."""
    _engine, factory = runs_engine
    parent_id = _seed_run(factory, status="completed")

    resp = client.post(f"/api/runs/{parent_id}/retry")
    assert resp.status_code == 400, resp.text


def test_retry_rejects_running_run(client: TestClient, runs_engine) -> None:
    """A running run cannot be retried (must finish/fail first) — 400."""
    _engine, factory = runs_engine
    parent_id = _seed_run(factory, status="running")

    resp = client.post(f"/api/runs/{parent_id}/retry")
    assert resp.status_code == 400, resp.text


def test_retry_rejects_queued_run(client: TestClient, runs_engine) -> None:
    """A queued (not yet started) run is not retryable — 400."""
    _engine, factory = runs_engine
    parent_id = _seed_run(factory, status="queued")

    resp = client.post(f"/api/runs/{parent_id}/retry")
    assert resp.status_code == 400, resp.text


def test_retry_rejects_interrupted_run(client: TestClient, runs_engine) -> None:
    """Interrupted runs already have /resume — /retry refuses them — 400."""
    _engine, factory = runs_engine
    parent_id = _seed_run(factory, status="interrupted")

    resp = client.post(f"/api/runs/{parent_id}/retry")
    assert resp.status_code == 400, resp.text


def test_retry_unknown_run_returns_404(client: TestClient) -> None:
    """A random UUID returns 404."""
    fake_id = uuid.uuid4()
    resp = client.post(f"/api/runs/{fake_id}/retry")
    assert resp.status_code == 404
