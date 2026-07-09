"""Tests for the per-run event bus + Postgres-backed SSE replay layer.

Covers:
1. Basic DB-backed replay yields events in publish order.
2. ``last_event_id`` resume skips already-delivered seqs.
3. Gap-free recovery across a disconnect (the core SSE contract).
4. Terminal events (``run_completed`` etc.) end the subscriber generator.
5. Backpressure: queue overflow drops live frames but keeps DB rows so
   a fresh subscriber rebuilds full state via replay.
6. 100 concurrent publishers produce a strictly monotonic 1..N seq
   sequence with no duplicates or gaps.

``event_bus.publish`` calls ``get_session_factory()`` directly (it isn't
a FastAPI dependency — runners invoke it from worker threads), so we
patch the symbol in the event_bus module to point at a per-test SQLite
factory. That keeps each test isolated AND lets us reuse the in-memory
DB across publish/subscribe calls within a single test.
"""

from __future__ import annotations

from tests.helpers import TEST_ADMIN_ID, seed_admin_user

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def event_bus_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    """File-based SQLite engine + session factory wired into event_bus.

    File-based (not :memory:) because aiosqlite's :memory: handle is
    bound to a single connection — once our session closes, the next
    one sees an empty DB. A tmp file shared by the factory's pool
    behaves like a real Postgres DB for our purposes.
    """
    from app import models  # noqa: F401 — register tables on Base.metadata
    from app.db import Base
    from app.services import event_bus

    db_path = tmp_path / "events.sqlite"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original = event_bus.get_session_factory
    event_bus.get_session_factory = lambda: factory  # type: ignore[assignment]

    event_bus.reset_for_tests()
    try:
        yield engine
    finally:
        event_bus.get_session_factory = original  # type: ignore[assignment]
        event_bus.reset_for_tests()
        await engine.dispose()


@pytest.fixture
async def seeded_run(event_bus_engine) -> uuid.UUID:
    """Insert a parent Run row so the FK on run_events is satisfied."""
    from app.models import Run
    from app.services import event_bus

    run_id = uuid.uuid4()
    factory = event_bus.get_session_factory()
    async with factory() as session:
        session.add(
            Run(
                id=str(run_id),
                user_id=TEST_ADMIN_ID,
                ticker="TEST",
                asset_type="stock",
                analysis_date=date(2026, 5, 20),
                analysts=["market"],
                research_depth=1,
                llm_provider="openai",
                quick_think_llm="gpt-4o-mini",
                deep_think_llm="gpt-4o",
                output_language="English",
                checkpoint_enabled=False,
                status="running",
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
    return run_id


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _payload(type_: str, **extra) -> dict:
    """Build a minimal event payload — only ``type`` is required."""
    return {"type": type_, **extra}


async def _drain(gen, *, limit: int = 1000, timeout: float = 5.0) -> list[dict]:
    """Collect events from an async generator until it exits or limit hit."""
    out: list[dict] = []

    async def _pull():
        async for ev in gen:
            out.append(ev)
            if len(out) >= limit:
                break

    try:
        await asyncio.wait_for(_pull(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return out


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


async def test_basic_replay_returns_all_events_in_order(seeded_run) -> None:
    """Publish 3, subscribe from scratch, see all 3 with seq 1..3."""
    from app.services import event_bus

    run_id = seeded_run

    for i in range(3):
        await event_bus.publish(run_id, _payload("message", content=f"msg-{i}"))

    # Close so the live-tail phase exits after the replay.
    event_bus.close(run_id)

    received = await _drain(event_bus.subscribe(run_id, last_event_id=None))
    assert [e["seq"] for e in received] == [1, 2, 3]
    assert [e["content"] for e in received] == ["msg-0", "msg-1", "msg-2"]


async def test_last_event_id_resume_skips_already_delivered(seeded_run) -> None:
    """Subscribing with last_event_id=3 yields only seq 4..5."""
    from app.services import event_bus

    run_id = seeded_run

    for i in range(5):
        await event_bus.publish(run_id, _payload("message", content=f"m-{i}"))

    event_bus.close(run_id)

    received = await _drain(event_bus.subscribe(run_id, last_event_id=3))
    assert [e["seq"] for e in received] == [4, 5]


async def test_gap_free_across_disconnect(seeded_run) -> None:
    """A → publish 1-3 → A reads 1-3 → A disconnects → publish 4-6 →
    B reconnects with last_event_id=3 → B sees exactly 4-6, no
    duplicates, no gaps."""
    from app.services import event_bus

    run_id = seeded_run

    # Subscriber A: read first 3 then bail.
    for i in range(3):
        await event_bus.publish(run_id, _payload("message", content=f"early-{i}"))

    a_gen = event_bus.subscribe(run_id, last_event_id=None)
    a_received: list[dict] = []

    async def _read_a():
        async for ev in a_gen:
            a_received.append(ev)
            if len(a_received) >= 3:
                break

    await asyncio.wait_for(_read_a(), timeout=5.0)
    await a_gen.aclose()

    assert [e["seq"] for e in a_received] == [1, 2, 3]

    # Publish more while no live subscribers exist.
    for i in range(3, 6):
        await event_bus.publish(run_id, _payload("message", content=f"late-{i}"))

    event_bus.close(run_id)

    # Subscriber B reconnects with the last seq A saw.
    b_received = await _drain(event_bus.subscribe(run_id, last_event_id=3))
    assert [e["seq"] for e in b_received] == [4, 5, 6]
    assert [e["content"] for e in b_received] == ["late-3", "late-4", "late-5"]
    # No duplicate seqs anywhere.
    all_seqs = [e["seq"] for e in a_received] + [e["seq"] for e in b_received]
    assert len(all_seqs) == len(set(all_seqs))


async def test_terminal_event_ends_subscription(seeded_run) -> None:
    """run_completed should be yielded then the generator exits."""
    from app.services import event_bus

    run_id = seeded_run

    await event_bus.publish(run_id, _payload("message", content="hi"))
    await event_bus.publish(run_id, _payload("progress_update", progress=0.5, step="x"))
    await event_bus.publish(
        run_id,
        _payload(
            "run_completed",
            rating="Buy",
            report_dir="/tmp/r",
            finished_at=datetime.now(timezone.utc).isoformat(),
        ),
    )

    # No close() needed — terminal event ends the subscriber.
    received = await _drain(event_bus.subscribe(run_id, last_event_id=None))
    assert [e["type"] for e in received] == [
        "message",
        "progress_update",
        "run_completed",
    ]
    # The generator must have exited (drain returned cleanly, no timeout).
    assert received[-1]["type"] == "run_completed"


async def test_backpressure_drops_live_frames_but_db_persists(seeded_run) -> None:
    """Push 250 events with the queue cap at 200; verify all 250 land in DB
    so a fresh subscriber sees all 250 via replay."""
    from app.models import RunEvent as RunEventModel
    from app.services import event_bus
    from sqlalchemy import func, select

    run_id = seeded_run

    # Register a subscriber WITHOUT consuming, so its queue fills up
    # and forces ``publish`` into the drop-frame branch.
    slow_gen = event_bus.subscribe(run_id, last_event_id=None)
    # Prime the generator so the queue is registered before we publish.
    # Schedule a one-shot pull that lets the generator hit its live-tail
    # phase but then aborts immediately so the queue stops draining.
    prime_task = asyncio.create_task(slow_gen.__anext__())
    # Yield control so the generator runs replay (empty) and registers
    # its queue. Then cancel — the queue stays registered in _queues
    # because the finally block runs on aclose() not cancel here.
    await asyncio.sleep(0.05)
    prime_task.cancel()
    try:
        await prime_task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass

    # Publish far past the queue maxsize.
    total = 250
    for i in range(total):
        await event_bus.publish(run_id, _payload("message", content=f"x-{i}"))

    # Close the slow subscriber's queue cleanly.
    await slow_gen.aclose()

    # Confirm the DB has every event.
    factory = event_bus.get_session_factory()
    async with factory() as session:
        count = await session.execute(
            select(func.count())
            .select_from(RunEventModel)
            .where(RunEventModel.run_id == str(run_id))
        )
        assert count.scalar_one() == total

    # A fresh subscriber catches up via replay.
    event_bus.close(run_id)
    received = await _drain(
        event_bus.subscribe(run_id, last_event_id=None), limit=total + 5
    )
    assert len(received) == total
    assert [e["seq"] for e in received] == list(range(1, total + 1))


async def test_concurrent_publishers_serialize_seq(seeded_run) -> None:
    """100 concurrent publish() calls must yield seqs 1..100 with no gaps
    or duplicates."""
    from app.services import event_bus

    run_id = seeded_run

    n = 100
    seqs = await asyncio.gather(
        *(
            event_bus.publish(run_id, _payload("message", content=f"c-{i}"))
            for i in range(n)
        )
    )

    assert sorted(seqs) == list(range(1, n + 1)), (
        f"expected 1..{n}, got duplicates or gaps: "
        f"sorted={sorted(seqs)[:10]}... (len={len(seqs)}, unique={len(set(seqs))})"
    )

    event_bus.close(run_id)
    received = await _drain(
        event_bus.subscribe(run_id, last_event_id=None), limit=n + 5
    )
    assert [e["seq"] for e in received] == list(range(1, n + 1))
