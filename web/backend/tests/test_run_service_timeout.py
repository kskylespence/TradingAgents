"""Tests for the per-run wall-clock timeout in ``run_service._run_async``.

Wave 5 of the resilience hardening pass: a hung LLM call must not be able
to hold ``GLOBAL_RUN_LOCK`` indefinitely. The lifecycle wraps its engine
invocation in ``asyncio.wait_for(timeout=...)`` so the lock always gets
released, even if the engine's worker thread is stuck on an upstream HTTP
read that never completes.

These tests stub ``run_service._run_engine`` directly (rather than going
through FAKE_LLM) because the goal is to simulate a runaway engine —
something FAKE_LLM cannot do by design (it's a fast, scripted finisher).

aiosqlite gotcha: same as the rest of the suite — use ``str(uuid)`` when
binding into the ``String(36).with_variant(UUID, "postgresql")`` column.
"""

from __future__ import annotations

from tests.helpers import make_auth_user

import asyncio
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# --------------------------------------------------------------------------- #
# Fixtures (mirrors test_runs_smoke.py)                                       #
# --------------------------------------------------------------------------- #


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
        return make_auth_user(username="test")

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


def _sample_request() -> dict:
    return {
        "ticker": "SPY",
        "analysis_date": "2026-05-19",
        "output_language": "English",
        "analysts": ["market", "news"],
        "research_depth": 1,
        "llm_provider": "openai",
        "quick_think_llm": "gpt-5.4-mini",
        "deep_think_llm": "gpt-5.4",
        "enable_checkpoint": False,
    }


def _wait_for_status(
    client: TestClient, run_id: str, statuses: set[str], timeout: float = 10.0
) -> dict:
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
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_run_async_aborts_on_timeout(
    client: TestClient, monkeypatch
) -> None:
    """A runaway ``_run_engine`` is aborted by the wall-clock timeout.

    Patch ``_run_engine`` to a 60-second sleep and set the timeout env
    var to 2s. The lifecycle must:

    * Mark the run ``failed`` with an error message that names the
      ``TRADINGAGENTS_RUN_MAX_SECONDS`` knob (so an operator can find
      and tune the right env var).
    * Release ``GLOBAL_RUN_LOCK`` so a follow-up ``POST /api/runs``
      succeeds without 409.
    """
    from app.services import run_service

    monkeypatch.setenv("TRADINGAGENTS_RUN_MAX_SECONDS", "2")
    # FAKE_LLM is irrelevant here — we patch _run_engine outright.
    monkeypatch.delenv("FAKE_LLM", raising=False)

    async def _runaway_engine(req, asset_type, observer, cancel_event):
        # Sleeps far longer than the configured timeout.
        await asyncio.sleep(60)
        return {"final_trade_decision": "Rating: Hold"}

    monkeypatch.setattr(run_service, "_run_engine", _runaway_engine)

    submit = client.post("/api/runs", json=_sample_request())
    assert submit.status_code == 200, submit.text
    run_id = submit.json()["run_id"]

    # The 2s timeout + finally bookkeeping should land "failed" within ~5s.
    detail = _wait_for_status(client, run_id, {"failed"}, timeout=8.0)
    assert detail["status"] == "failed"
    err = (detail.get("error_message") or "")
    assert "TRADINGAGENTS_RUN_MAX_SECONDS" in err, (
        f"error_message should name the env knob, got: {err!r}"
    )
    assert "2" in err  # the configured limit in seconds

    # Verify the lock was released. The status row flips to ``failed``
    # inside the lifecycle's ``finally:`` block, but the
    # ``async with _get_lock():`` only releases the lock once that
    # block also drains the observer + closes the event bus. Allow a
    # generous-but-bounded window to clear the trailing cleanup work;
    # the invariant is "lock released within ~1s", not "instantly".
    t0 = time.monotonic()
    lock_released = False
    while time.monotonic() - t0 < 1.5:
        if not run_service._get_lock().locked():
            lock_released = True
            break
        time.sleep(0.02)
    assert lock_released, (
        "GLOBAL_RUN_LOCK still held >1.5s after timeout; the lifecycle "
        "did not release it"
    )
    # And the public ``start_run`` gate must agree — no 409 even though
    # the prior run held the lock for the full 2s wall-clock window.
    second = client.post("/api/runs", json=_sample_request())
    assert second.status_code != 409, (
        f"second submit got 409 (lock-held); response was: {second.text}"
    )


async def test_run_async_timeout_sets_cancel_event(monkeypatch) -> None:
    """On timeout, the lifecycle MUST set ``cancel_event``.

    The engine runs synchronous code in a worker thread, so
    ``asyncio.wait_for``'s task-cancellation alone does NOT stop it.
    The cooperative-stop signal is ``cancel_event``; the timeout
    handler must set it so the next chunk boundary returns cleanly.

    This test reaches under the HTTP layer and drives ``_run_async``
    directly so it can capture the ``cancel_event`` instance.
    """
    from datetime import date

    from app import schemas as S
    from app.services import run_service

    monkeypatch.setenv("TRADINGAGENTS_RUN_MAX_SECONDS", "1")
    monkeypatch.delenv("FAKE_LLM", raising=False)

    captured: dict = {}

    async def _runaway_engine(req, asset_type, observer, cancel_event):
        captured["cancel_event"] = cancel_event
        # Hang forever; the timeout should yank us out.
        await asyncio.sleep(60)
        return {"final_trade_decision": "Rating: Hold"}

    monkeypatch.setattr(run_service, "_run_engine", _runaway_engine)

    # Stub the DB helpers — we don't want to require a session factory.
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(run_service, "_mark_running", _noop)
    monkeypatch.setattr(run_service, "_mark_completed", _noop)
    monkeypatch.setattr(run_service, "_mark_terminal", _noop)

    async def _no_keys(provider):
        return {}

    monkeypatch.setattr(run_service, "_collect_api_keys", _no_keys)

    # Stub event_bus to avoid the DB-backed event recorder.
    from app.services import event_bus as eb_mod

    monkeypatch.setattr(eb_mod, "publish", lambda *a, **kw: None)
    monkeypatch.setattr(eb_mod, "close", lambda *a, **kw: None)

    req = S.RunRequest(
        ticker="SPY",
        analysis_date=date(2026, 5, 19),
        output_language="English",
        analysts=["market"],
        research_depth=1,
        llm_provider="openai",
        quick_think_llm="gpt-5.4-mini",
        deep_think_llm="gpt-5.4",
        enable_checkpoint=False,
    )

    # Async so we run on pytest-asyncio's loop — earlier versions used
    # ``asyncio.run(...)`` which created a throwaway loop that left
    # ``run_service`` / ``event_bus`` / ``upstream_http`` module-level
    # state bound to a dead loop, polluting downstream tests in the
    # same file via cross-test event-loop pollution.
    run_service.reset_for_tests()
    run_id = uuid.uuid4()
    await run_service._run_async(run_id, req, "stock")

    assert "cancel_event" in captured, "engine was never invoked"
    assert captured["cancel_event"].is_set(), (
        "cancel_event must be set on timeout so the engine can stop "
        "cleanly at the next chunk boundary"
    )
