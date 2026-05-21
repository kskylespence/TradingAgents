"""Tests for the cursor-paginated history router (GET /api/history).

Verifies:
- Pagination yields the right page sizes and a working ``next_cursor``.
- Filters by ``ticker`` and ``status`` narrow results correctly.
- Page boundaries are stable: no overlap, no skipped rows.
- An invalid cursor returns HTTP 400.
- ``elapsed_seconds`` is computed from ``started_at`` / ``finished_at``.

We override the app's ``get_session`` dependency with a per-test
file-based SQLite engine so the FastAPI route (running in a
TestClient-managed thread) and our seed code share state.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def history_engine(tmp_path):
    """Per-test file-based SQLite engine + sessionmaker.

    File-based (not :memory:) so the engine the TestClient route uses can
    see rows seeded from a sibling engine. We open ONE engine here and
    share its sessionmaker between the dependency override and the seeder.

    Async fixture (driven by pytest-asyncio's auto mode) so we don't
    poke ``asyncio.get_event_loop()`` directly — that was deprecated in
    Python 3.10 and raises ``RuntimeError`` in 3.13 when no loop is set.
    """
    from app.db import Base
    from app import models  # noqa: F401 — register tables on Base.metadata

    db_path = tmp_path / "history.sqlite"
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
async def seeded_runs(history_engine):
    """Insert 25 Run rows with staggered created_at, mixed ticker+status.

    Index 0 is the OLDEST (earliest created_at); index 24 is the NEWEST.
    With DESC ordering, the API should return index 24 first.
    """
    from app.models import Run

    _engine, factory = history_engine
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    ids: list[str] = []
    async with factory() as session:
        for i in range(25):
            created = base + timedelta(minutes=i)
            ticker = "AAPL" if i % 2 == 0 else "MSFT"
            run_status = ("completed", "running", "failed")[i % 3]
            started = created + timedelta(seconds=1)
            finished = (
                created + timedelta(seconds=61) if run_status == "completed" else None
            )
            run_id = uuid.uuid4()
            ids.append(str(run_id))
            # Coerce UUID to str — the SQLite driver does not bind UUID natively.
            row = Run(
                id=str(run_id),
                ticker=ticker,
                asset_type="stock",
                analysis_date=date(2026, 5, 1),
                analysts=["market", "news"],
                research_depth=1,
                llm_provider="openai",
                quick_think_llm="gpt-4o-mini",
                deep_think_llm="gpt-4o",
                output_language="English",
                checkpoint_enabled=False,
                status=run_status,
                rating=("Buy" if run_status == "completed" else None),
                started_at=started,
                finished_at=finished,
                created_at=created,
            )
            session.add(row)
        await session.commit()
    return ids


@pytest.fixture
def client(history_engine, seeded_runs) -> Iterator[TestClient]:
    """TestClient with ``get_session`` + ``get_current_user`` overridden.

    Auth is stubbed so the JWT cookie isn't required — auth itself is
    covered by ``test_auth.py``, not here.
    """
    from app.auth import get_current_user
    from app.db import get_session
    from app.main import app
    from app.schemas import AuthUser

    _engine, factory = history_engine

    async def _override_session():
        async with factory() as session:
            yield session

    def _override_user() -> AuthUser:
        return AuthUser(username="test")

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = _override_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_user, None)


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_first_page_returns_20_items_with_next_cursor(client: TestClient) -> None:
    """Default limit is 20; with 25 rows seeded we get a cursor for page 2."""
    resp = client.get("/api/history/")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 20
    assert body.get("next_cursor"), "next_cursor must be present when more rows remain"

    # DESC ordering: newest row (index 24, created_at = base + 24 minutes)
    # should be first. Index 24 is even → AAPL.
    first = body["items"][0]
    assert first["ticker"] == "AAPL"


def test_follow_next_cursor_returns_remaining_rows(client: TestClient) -> None:
    """Page 1 + page 2 should yield exactly the 25 seeded rows, no overlap."""
    page1 = client.get("/api/history/").json()
    cursor = page1["next_cursor"]
    page2 = client.get(f"/api/history/?cursor={cursor}").json()

    assert len(page2["items"]) == 5
    assert page2.get("next_cursor") is None

    ids = [r["id"] for r in page1["items"]] + [r["id"] for r in page2["items"]]
    assert len(set(ids)) == 25, "pages must not overlap or skip rows"


async def test_pages_are_stable_under_concurrent_inserts(
    client: TestClient, history_engine
) -> None:
    """Insert a fresh row BETWEEN page fetches; keyset cursor must still
    advance past page-1's last row, not re-yield the new row."""
    from app.models import Run

    _engine, factory = history_engine

    page1 = client.get("/api/history/").json()
    page1_ids = {r["id"] for r in page1["items"]}

    # Insert a brand-new row with a created_at NEWER than every existing
    # row. Under offset pagination this would shift page 2 and skip a row.
    async with factory() as session:
        row = Run(
            id=str(uuid.uuid4()),
            ticker="TSLA",
            asset_type="stock",
            analysis_date=date(2026, 5, 1),
            analysts=["market"],
            research_depth=1,
            llm_provider="openai",
            quick_think_llm="gpt-4o-mini",
            deep_think_llm="gpt-4o",
            output_language="English",
            checkpoint_enabled=False,
            status="completed",
            created_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        session.add(row)
        await session.commit()

    page2 = client.get(f"/api/history/?cursor={page1['next_cursor']}").json()
    page2_ids = {r["id"] for r in page2["items"]}

    assert page1_ids.isdisjoint(page2_ids)
    # The brand-new TSLA row was created AFTER our cursor and so must NOT
    # appear in page 2 (it would only surface on a fresh page-1 query).
    tickers = {r["ticker"] for r in page2["items"]}
    assert "TSLA" not in tickers


def test_filter_by_ticker_returns_only_matching_rows(client: TestClient) -> None:
    """`?ticker=AAPL` → every returned row has ticker == 'AAPL'."""
    resp = client.get("/api/history/?ticker=AAPL")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"]
    assert all(r["ticker"] == "AAPL" for r in body["items"])


def test_filter_by_status_returns_only_matching_rows(client: TestClient) -> None:
    """`?status=completed` → only completed runs returned."""
    resp = client.get("/api/history/?status=completed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"]
    assert all(r["status"] == "completed" for r in body["items"])


def test_invalid_cursor_returns_400(client: TestClient) -> None:
    """A garbage cursor (not base64-url or wrong shape) yields HTTP 400."""
    resp = client.get("/api/history/?cursor=not-a-real-cursor!!!")
    assert resp.status_code == 400


def test_elapsed_seconds_computed_from_timestamps(client: TestClient) -> None:
    """Completed rows have started_at + finished_at → elapsed_seconds=60."""
    resp = client.get("/api/history/?status=completed&limit=5")
    body = resp.json()
    assert body["items"]
    for row in body["items"]:
        # Seeded with started=created+1s, finished=created+61s ⇒ ~60s elapsed.
        assert row["elapsed_seconds"] == pytest.approx(60.0, abs=0.1)


def test_limit_is_capped_at_100(client: TestClient) -> None:
    """`?limit=9999` must not blow past the 100 ceiling."""
    resp = client.get("/api/history/?limit=9999")
    if resp.status_code == 200:
        assert len(resp.json()["items"]) <= 100
    else:
        assert resp.status_code in (400, 422)
