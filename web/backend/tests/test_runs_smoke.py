"""End-to-end smoke for the /api/runs router + run_service lifecycle.

We use the FAKE_LLM=1 hook in :mod:`app.services.run_service` to drive
the full lifecycle without configuring real LLM credentials. Each test
pins its own SQLite file-engine into ``get_session_factory`` so the
HTTP routes (running in TestClient's worker thread), the run_service
background task (running on the loop), the event_bus persistence layer
(another worker), and our test-side seed/fetch code all share state.

Auth + CSRF are bypassed via dependency / middleware overrides — they
are covered by ``test_auth.py`` and ``test_csrf.py`` and are not what
this suite is exercising.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import date
from typing import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _enable_fake_llm(monkeypatch):
    """Force the FAKE_LLM hook for every test in this file."""
    monkeypatch.setenv("FAKE_LLM", "1")


@pytest.fixture
async def runs_engine(tmp_path):
    """A per-test SQLite file engine + sessionmaker, with all tables created.

    File-based (not :memory:) so multiple sessions opened from different
    code paths (HTTP route, run_service background task, event_bus
    publish) all observe the same data.
    """
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
    """Wire the test engine into every code path that asks for a session.

    - ``app.db.get_session_factory`` is used by event_bus, run_service
      DB helpers (``_mark_running`` etc.), and the FastAPI ``get_session``
      dependency we're about to override.
    """
    from app import db as db_mod
    from app.services import event_bus as eb_mod

    _engine, factory = runs_engine

    monkeypatch.setattr(db_mod, "get_session_factory", lambda: factory)
    monkeypatch.setattr(eb_mod, "get_session_factory", lambda: factory)
    eb_mod.reset_for_tests()
    # The event_bus module-level ``_lock`` is an asyncio.Lock that
    # binds to the first loop it touches; pytest-asyncio gives each
    # test its own loop, so we must re-create the lock per test to
    # avoid "bound to a different event loop" RuntimeErrors. Public
    # ``reset_for_tests`` doesn't currently cover this; do it inline.
    eb_mod._lock = asyncio.Lock()
    yield factory
    eb_mod.reset_for_tests()


@pytest.fixture
def client(runs_engine, patched_session_factory) -> Iterator[TestClient]:
    """TestClient with auth stubbed and CSRF disabled.

    Also resets the run_service module state between tests so the global
    lock is fresh.
    """
    from app.auth import get_current_user
    from app.db import get_session
    from app.main import app
    from app.middleware.csrf import CSRFMiddleware
    from app.schemas import AuthUser
    from app.services import run_service

    _engine, factory = runs_engine

    async def _override_session():
        async with factory() as session:
            yield session

    def _override_user() -> AuthUser:
        return AuthUser(username="test")

    # Best-effort CSRF disable: monkey-patch the predicate to never
    # require the token. TestClient does not include the cookie/header
    # pair by default. (We do NOT touch the auth router or login flow
    # in these tests; auth is a noop via dependency_overrides.)
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = _override_user

    run_service.reset_for_tests()

    # sse-starlette caches an ``anyio.Event`` on ``AppStatus.should_exit_event``
    # at first use; pytest-asyncio creates a fresh loop per test, so the
    # cached Event is bound to a dead loop. Reset before every test.
    try:
        from sse_starlette.sse import AppStatus  # type: ignore

        AppStatus.should_exit = False
        AppStatus.should_exit_event = None
    except ImportError:
        pass

    # Disable CSRF for these tests by no-op'ing the predicate.
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
        "quick_think_llm": "gpt-4o-mini",
        "deep_think_llm": "gpt-4o",
        "enable_checkpoint": False,
    }


def _wait_for_status(
    client: TestClient, run_id: str, statuses: set[str], timeout: float = 10.0
) -> dict:
    """Poll ``GET /:id`` until ``status`` matches or the timeout fires."""
    deadline = time.monotonic() + timeout
    last_body: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}")
        assert resp.status_code == 200, resp.text
        last_body = resp.json()
        if last_body.get("status") in statuses:
            return last_body
        time.sleep(0.05)
    raise AssertionError(
        f"Run {run_id} never reached {statuses}; last status={last_body.get('status')}"
    )


# --------------------------------------------------------------------------- #
# Submit + lifecycle                                                          #
# --------------------------------------------------------------------------- #


def test_post_run_returns_id_and_completes(client: TestClient) -> None:
    """POST a run → 200 + run_id; within ~3s the row is completed."""
    resp = client.post("/api/runs", json=_sample_request())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    run_id = body["run_id"]
    uuid.UUID(run_id)  # well-formed UUID

    detail = _wait_for_status(client, run_id, {"completed"}, timeout=10.0)
    assert detail["status"] == "completed"
    assert detail["rating"] == "Buy"  # FAKE_LLM script always emits Buy
    assert detail["elapsed_seconds"] is not None
    assert detail["elapsed_seconds"] > 0
    assert detail["report_dir"]


def test_get_unknown_run_returns_404(client: TestClient) -> None:
    """Asking for a run id that doesn't exist returns 404."""
    fake_id = uuid.uuid4()
    resp = client.get(f"/api/runs/{fake_id}")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# SSE stream                                                                  #
# --------------------------------------------------------------------------- #


def _parse_sse_events(text: str) -> list[dict]:
    """Parse an SSE blob into event payload dicts.

    sse-starlette uses CRLF separators (``\\r\\n\\r\\n`` between frames);
    we normalise CRLF -> LF then split on blank lines.
    """
    out: list[dict] = []
    normalised = text.replace("\r\n", "\n")
    for frame in normalised.split("\n\n"):
        data_line = next(
            (l for l in frame.splitlines() if l.startswith("data:")), None
        )
        if not data_line:
            continue
        payload = data_line[len("data:"):].strip()
        if not payload:
            continue
        try:
            out.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return out


def _drain_sse(
    client: TestClient,
    run_id: str,
    max_events: int = 200,
    *,
    last_event_id: int | None = None,
) -> list[dict]:
    """Open the SSE stream and yield parsed event payload dicts.

    Reads from the stream and stops when a terminal event is parsed or
    the stream closes (the generator in the router exits when the bus
    publishes ``run_completed``/etc.).
    """
    headers = {"Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)

    buf = ""
    events: list[dict] = []
    with client.stream("GET", f"/api/runs/{run_id}/events", headers=headers) as resp:
        assert resp.status_code == 200
        for chunk in resp.iter_text():
            buf += chunk
            parsed = _parse_sse_events(buf)
            # Re-keep the trailing partial frame (no double blank line yet)
            # by tracking how many complete frames we've fully consumed.
            if len(parsed) > len(events):
                events = parsed
                if events and events[-1].get("type") in {
                    "run_completed", "run_failed", "run_cancelled",
                }:
                    return events
                if len(events) >= max_events:
                    return events
    return events


def test_sse_stream_yields_start_and_complete(client: TestClient) -> None:
    """SSE replay (after completion) yields at least run_started and run_completed."""
    submit = client.post("/api/runs", json=_sample_request())
    run_id = submit.json()["run_id"]
    _wait_for_status(client, run_id, {"completed"}, timeout=10.0)

    events = _drain_sse(client, run_id)
    types = [e.get("type") for e in events]
    assert "run_started" in types, f"events={types}"
    assert "run_completed" in types, f"events={types}"
    # run_started must precede run_completed.
    assert types.index("run_started") < types.index("run_completed")


def test_sse_last_event_id_resume(client: TestClient) -> None:
    """Re-subscribing with ``Last-Event-ID: N`` only yields seq > N."""
    submit = client.post("/api/runs", json=_sample_request())
    run_id = submit.json()["run_id"]
    _wait_for_status(client, run_id, {"completed"}, timeout=10.0)

    all_events = _drain_sse(client, run_id)
    assert len(all_events) >= 3
    cut_seq = all_events[1]["seq"]  # resume past the second event

    # Reconnect with Last-Event-ID set to cut_seq.
    resumed = _drain_sse(client, run_id, last_event_id=cut_seq)

    assert resumed, "expected at least one event on resume"
    assert all(e["seq"] > cut_seq for e in resumed), (
        f"events with seq <= {cut_seq}: {[e['seq'] for e in resumed]}"
    )


# --------------------------------------------------------------------------- #
# Cancel                                                                      #
# --------------------------------------------------------------------------- #


def test_post_cancel_terminates_run(client: TestClient, monkeypatch) -> None:
    """Cancelling mid-run flips status to cancelled.

    We patch the fake stream into an idle loop so the cancel has a real
    window to fire.
    """
    from app.services import run_service

    async def _slow_fake(req, asset_type, observer, cancel_event):
        # Spin until cancelled or until a generous timeout — we want
        # this to outlast the cancel POST.
        observer.on_agent_status("Market Analyst", "in_progress")
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            if cancel_event.is_set():
                raise run_service._CancelledByEvent()
            await asyncio.sleep(0.02)
        raise AssertionError("cancel never fired")

    monkeypatch.setattr(run_service, "_fake_stream_run", _slow_fake)

    submit = client.post("/api/runs", json=_sample_request())
    run_id = submit.json()["run_id"]
    _wait_for_status(client, run_id, {"running"}, timeout=5.0)

    cancel = client.post(f"/api/runs/{run_id}/cancel")
    assert cancel.status_code == 204

    detail = _wait_for_status(client, run_id, {"cancelled"}, timeout=5.0)
    assert detail["status"] == "cancelled"


# --------------------------------------------------------------------------- #
# Concurrency guard                                                           #
# --------------------------------------------------------------------------- #


def test_second_concurrent_run_is_rejected_with_409(
    client: TestClient, monkeypatch
) -> None:
    """Submitting a second run while the first is in flight returns 409."""
    from app.services import run_service

    # Make the first run slow so we have time to POST the second.
    async def _slow_fake(req, asset_type, observer, cancel_event):
        observer.on_agent_status("Market Analyst", "in_progress")
        for _ in range(50):
            if cancel_event.is_set():
                raise run_service._CancelledByEvent()
            await asyncio.sleep(0.05)
        return {"final_trade_decision": "Rating: Hold"}

    monkeypatch.setattr(run_service, "_fake_stream_run", _slow_fake)

    first = client.post("/api/runs", json=_sample_request())
    assert first.status_code == 200, first.text
    first_id = first.json()["run_id"]
    _wait_for_status(client, first_id, {"running"}, timeout=5.0)

    second = client.post("/api/runs", json=_sample_request())
    assert second.status_code == 409
    assert "in progress" in second.json()["detail"].lower()

    # Clean up — cancel the first run so the lock is released and the
    # test fixture teardown doesn't hang.
    client.post(f"/api/runs/{first_id}/cancel")
    _wait_for_status(client, first_id, {"cancelled", "completed"}, timeout=5.0)


# --------------------------------------------------------------------------- #
# Resume                                                                      #
# --------------------------------------------------------------------------- #


def test_resume_rejects_when_not_interrupted(client: TestClient) -> None:
    """POST /:id/resume on a completed run returns 409."""
    submit = client.post("/api/runs", json=_sample_request())
    run_id = submit.json()["run_id"]
    _wait_for_status(client, run_id, {"completed"}, timeout=10.0)

    resume = client.post(f"/api/runs/{run_id}/resume")
    # Plan allowed either 409 or 400; we standardized on 409 with a
    # meaningful detail.
    assert resume.status_code in (400, 409)


def test_resume_happy_path_returns_new_run_id(
    client: TestClient, runs_engine, tmp_path, monkeypatch
) -> None:
    """Seed an interrupted+resumable run with a real checkpoint file, POST
    /resume, assert 200 + {run_id, parent_run_id} with a fresh new run_id.

    This is the behavioral test that proves the frontend Resume button's
    backend contract — backend returns the new run id, which the frontend
    uses to navigate to /runs/:new_id (see
    ``web/frontend/src/routes/RunView.tsx::resumeMutation``).
    """
    import uuid as _uuid
    from datetime import date, datetime, timezone

    from app.config import get_settings
    from app.models import Run

    # Repoint settings.data_dir at tmp_path so the checkpoint exists where
    # ``crash_recovery.has_checkpoint`` looks.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    # Touch the checkpoint file at the canonical layout:
    #   <data_dir>/cache/checkpoints/<TICKER_UPPER>.db
    ticker = "RESUMEABLE"
    analysis_date = date(2026, 5, 19)
    ckpt_dir = tmp_path / "cache" / "checkpoints"
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    (ckpt_dir / f"{ticker}.db").write_bytes(b"")

    # Seed an interrupted run row directly through the test engine.
    # aiosqlite won't bind raw UUID instances to the
    # String(36).with_variant(UUID, "postgresql") column — coerce to str
    # (same as ``test_history.py`` does for the same reason).
    _engine, factory = runs_engine
    parent_id_uuid = _uuid.uuid4()
    parent_id = str(parent_id_uuid)

    async def _seed() -> None:
        async with factory() as session:
            session.add(
                Run(
                    id=parent_id,
                    ticker=ticker,
                    asset_type="stock",
                    analysis_date=analysis_date,
                    analysts=["market"],
                    research_depth=1,
                    llm_provider="openai",
                    quick_think_llm="gpt-4o-mini",
                    deep_think_llm="gpt-4o",
                    output_language="English",
                    checkpoint_enabled=True,
                    status="interrupted",
                    started_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc),
                    finished_at=None,
                )
            )
            await session.commit()

    asyncio.get_event_loop().run_until_complete(_seed())

    try:
        resp = client.post(f"/api/runs/{parent_id}/resume")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) >= {"run_id", "parent_run_id"}
        assert body["parent_run_id"] == parent_id
        new_id = _uuid.UUID(body["run_id"])
        assert str(new_id) != parent_id, "resume must mint a NEW run id"

        # Best-effort cleanup so the global run lock doesn't block teardown.
        client.post(f"/api/runs/{new_id}/cancel")
        _wait_for_status(
            client, str(new_id), {"cancelled", "completed", "failed"}, timeout=10.0
        )
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Report download                                                             #
# --------------------------------------------------------------------------- #


def test_report_md_download_returns_markdown(client: TestClient) -> None:
    """After completion, GET /:id/report?format=md returns the report text."""
    submit = client.post("/api/runs", json=_sample_request())
    run_id = submit.json()["run_id"]
    detail = _wait_for_status(client, run_id, {"completed"}, timeout=10.0)
    assert detail["report_dir"]

    resp = client.get(f"/api/runs/{run_id}/report", params={"format": "md"})
    assert resp.status_code == 200, resp.text
    assert "text/markdown" in resp.headers["content-type"]
    body = resp.text
    assert "Rating: Buy" in body


def test_report_404_when_run_unknown(client: TestClient) -> None:
    """GET /:id/report on an unknown run returns 404."""
    fake_id = uuid.uuid4()
    resp = client.get(f"/api/runs/{fake_id}/report")
    assert resp.status_code == 404
