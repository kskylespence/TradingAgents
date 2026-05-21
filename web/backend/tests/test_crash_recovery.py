"""Crash-recovery tests.

Covers the contract spelled out in
``app/services/crash_recovery.py``:

1. Seeded ``status='running'`` row → ``run_startup_recovery`` flips it
   to ``'interrupted'``.
2. A terminal ``run_failed`` event is appended for the recovered run.
   Why ``run_failed`` (not a new ``run_interrupted`` type)?
   - ``event_bus.TERMINAL_EVENT_TYPES`` already includes
     ``run_failed``, so SSE subscribers exit cleanly without any
     other change.
   - The event carries ``interrupted: True`` + ``resumable: bool`` in
     its payload so callers that DO care can distinguish a crash
     recovery from a graceful failure. The ``runs.status`` column
     is the authoritative source for the distinction.
   - Adding a new event type would require touching
     ``app/schemas.py`` and ``event_bus.TERMINAL_EVENT_TYPES``,
     both off-limits in this wave.
3. Idempotency: second call finds nothing, makes no changes.
4. ``has_checkpoint`` returns False when the file is absent, True when
   it exists at the expected path.
5. Lifespan hook ``startup_recover`` runs end-to-end against a real
   session factory and performs the same recovery.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def crash_recovery_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    """File-based aiosqlite engine with the full schema created.

    File-based (not ``:memory:``) because the recovery service opens
    its own session against ``get_session_factory()`` (via the lifespan
    hook integration test) — a per-connection ``:memory:`` handle
    would look empty when re-opened.

    Also rebinds ``app.db.get_session_factory`` to point at our
    per-test factory so the lifespan-hook integration test exercises
    the same DB that the seed fixture wrote to.
    """
    from app import models  # noqa: F401 — register tables on Base.metadata
    from app.db import Base
    from app import db as db_module

    db_path = tmp_path / "crash_recovery.sqlite"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original = db_module.get_session_factory
    db_module.get_session_factory = lambda: factory  # type: ignore[assignment]
    try:
        yield engine
    finally:
        db_module.get_session_factory = original  # type: ignore[assignment]
        await engine.dispose()


@pytest.fixture
async def session_factory(crash_recovery_engine):
    """Convenience: yield the factory the recovery service will use."""
    from app import db as db_module

    return db_module.get_session_factory()


async def _seed_run(
    factory,
    *,
    status: str = "running",
    ticker: str = "SPY",
    analysis_date: date = date(2026, 5, 19),
    checkpoint_enabled: bool = False,
) -> uuid.UUID:
    """Insert a ``Run`` row and return its id."""
    from app.models import Run

    run_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            Run(
                id=str(run_id),
                ticker=ticker,
                asset_type="stock",
                analysis_date=analysis_date,
                analysts=["market"],
                research_depth=1,
                llm_provider="openai",
                quick_think_llm="gpt-4o-mini",
                deep_think_llm="gpt-4o",
                output_language="English",
                checkpoint_enabled=checkpoint_enabled,
                status=status,
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
    return run_id


async def _count_events(factory, run_id: uuid.UUID) -> int:
    from app.models import RunEvent as RunEventModel
    from sqlalchemy import func

    async with factory() as session:
        result = await session.execute(
            select(func.count())
            .select_from(RunEventModel)
            .where(RunEventModel.run_id == str(run_id))
        )
        return int(result.scalar_one())


async def _get_run_status(factory, run_id: uuid.UUID) -> str:
    from app.models import Run

    async with factory() as session:
        result = await session.execute(select(Run).where(Run.id == str(run_id)))
        return result.scalar_one().status


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


async def test_running_row_transitions_to_interrupted(session_factory) -> None:
    """Seed a 'running' row, recover, verify status='interrupted'."""
    from app.services.crash_recovery import run_startup_recovery

    run_id = await _seed_run(session_factory, status="running")

    async with session_factory() as session:
        recovered = await run_startup_recovery(session)

    assert recovered == [run_id], f"expected [{run_id}], got {recovered}"
    assert await _get_run_status(session_factory, run_id) == "interrupted"


async def test_terminal_event_appended_for_recovered_run(session_factory) -> None:
    """Recovery emits exactly one terminal event of type 'run_failed'.

    Documents the design choice: we use 'run_failed' because it's
    already in event_bus.TERMINAL_EVENT_TYPES and the schema's
    RunFailedEvent has an error field we can populate. The
    'interrupted: True' flag in the payload disambiguates this from
    a graceful failure for callers that care; the canonical source of
    truth is the runs.status column, which IS 'interrupted'.
    """
    from app.models import RunEvent as RunEventModel
    from app.services.crash_recovery import run_startup_recovery

    run_id = await _seed_run(session_factory, status="running")

    async with session_factory() as session:
        await run_startup_recovery(session)

    async with session_factory() as session:
        result = await session.execute(
            select(RunEventModel).where(RunEventModel.run_id == str(run_id))
        )
        events = list(result.scalars().all())

    assert len(events) == 1, f"expected 1 terminal event, got {len(events)}"
    ev = events[0]
    assert ev.type == "run_failed", (
        f"terminal event type must be 'run_failed' (a member of "
        f"event_bus.TERMINAL_EVENT_TYPES), got {ev.type!r}"
    )
    assert ev.seq == 1
    assert ev.payload["type"] == "run_failed"
    assert ev.payload["interrupted"] is True
    assert "resumable" in ev.payload
    assert "error" in ev.payload and ev.payload["error"]


async def test_terminal_event_starts_after_existing_events(session_factory) -> None:
    """If a run already has events, the terminator gets MAX(seq)+1."""
    from app.models import RunEvent as RunEventModel
    from app.services.crash_recovery import run_startup_recovery

    run_id = await _seed_run(session_factory, status="running")

    # Seed a couple of pre-existing events (as if the observer had
    # written them before the crash).
    async with session_factory() as session:
        for seq in (1, 2, 3):
            session.add(
                RunEventModel(
                    run_id=str(run_id),
                    seq=seq,
                    ts=datetime.now(timezone.utc),
                    type="message",
                    payload={"type": "message", "kind": "Agent", "content": "x"},
                )
            )
        await session.commit()

    async with session_factory() as session:
        await run_startup_recovery(session)

    async with session_factory() as session:
        result = await session.execute(
            select(RunEventModel)
            .where(RunEventModel.run_id == str(run_id))
            .order_by(RunEventModel.seq.asc())
        )
        events = list(result.scalars().all())

    assert [e.seq for e in events] == [1, 2, 3, 4]
    assert events[-1].type == "run_failed"


async def test_idempotent_second_call_no_changes(session_factory) -> None:
    """Re-running recovery is a no-op (the orphan is already 'interrupted')."""
    from app.services.crash_recovery import run_startup_recovery

    run_id = await _seed_run(session_factory, status="running")

    async with session_factory() as session:
        first = await run_startup_recovery(session)
    assert first == [run_id]

    events_after_first = await _count_events(session_factory, run_id)

    async with session_factory() as session:
        second = await run_startup_recovery(session)
    assert second == [], "second call must find no orphans"

    # No new events were added.
    assert await _count_events(session_factory, run_id) == events_after_first


async def test_non_running_runs_are_ignored(session_factory) -> None:
    """Completed/failed/cancelled rows must NOT be touched by recovery."""
    from app.services.crash_recovery import run_startup_recovery

    completed = await _seed_run(session_factory, status="completed")
    failed = await _seed_run(session_factory, status="failed")
    cancelled = await _seed_run(session_factory, status="cancelled")

    async with session_factory() as session:
        recovered = await run_startup_recovery(session)

    assert recovered == []
    for rid in (completed, failed, cancelled):
        assert await _count_events(session_factory, rid) == 0
    # Statuses untouched.
    assert await _get_run_status(session_factory, completed) == "completed"
    assert await _get_run_status(session_factory, failed) == "failed"
    assert await _get_run_status(session_factory, cancelled) == "cancelled"


async def test_multiple_orphans_all_recovered(session_factory) -> None:
    """Several orphans in one scan all transition + each gets a terminator."""
    from app.services.crash_recovery import run_startup_recovery

    ids = []
    for ticker in ("AAA", "BBB", "CCC"):
        ids.append(await _seed_run(session_factory, status="running", ticker=ticker))

    async with session_factory() as session:
        recovered = await run_startup_recovery(session)

    assert set(recovered) == set(ids)
    for rid in ids:
        assert await _get_run_status(session_factory, rid) == "interrupted"
        assert await _count_events(session_factory, rid) == 1


async def test_has_checkpoint_false_when_file_missing(
    session_factory, tmp_path, monkeypatch
) -> None:
    """No checkpoint file → has_checkpoint returns False."""
    from app.config import get_settings
    from app.services import crash_recovery

    # Point settings.data_dir at a fresh empty tmp dir.
    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # pydantic-settings reads env vars in case-insensitive mode; reset
    # the cache so the next call re-reads.
    get_settings.cache_clear()

    assert crash_recovery.has_checkpoint("SPY", date(2026, 5, 19)) is False


async def test_has_checkpoint_true_when_file_exists(
    session_factory, tmp_path, monkeypatch
) -> None:
    """Touching the expected path → has_checkpoint returns True.

    The canonical path is:
      <settings.data_dir>/cache/checkpoints/<TICKER_UPPER>.db

    matching tradingagents/graph/checkpointer.py:_db_path which writes
    <data_cache_dir>/checkpoints/<TICKER_UPPER>.db.
    """
    from app.config import get_settings
    from app.services import crash_recovery

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    cp_dir = tmp_path / "cache" / "checkpoints"
    cp_dir.mkdir(parents=True)
    (cp_dir / "SPY.db").write_bytes(b"")

    assert crash_recovery.has_checkpoint("SPY", date(2026, 5, 19)) is True
    # Different ticker, no file: still False.
    assert crash_recovery.has_checkpoint("AAPL", date(2026, 5, 19)) is False


async def test_resumable_true_when_checkpoint_enabled_and_file_exists(
    session_factory, tmp_path, monkeypatch
) -> None:
    """The event payload records resumable=True iff both conditions hold."""
    from app.config import get_settings
    from app.models import RunEvent as RunEventModel
    from app.services.crash_recovery import run_startup_recovery

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    cp_dir = tmp_path / "cache" / "checkpoints"
    cp_dir.mkdir(parents=True)
    (cp_dir / "SPY.db").write_bytes(b"")

    run_id = await _seed_run(
        session_factory, status="running", ticker="SPY", checkpoint_enabled=True
    )

    async with session_factory() as session:
        await run_startup_recovery(session)

    async with session_factory() as session:
        result = await session.execute(
            select(RunEventModel).where(RunEventModel.run_id == str(run_id))
        )
        ev = result.scalar_one()

    assert ev.payload["resumable"] is True


async def test_resumable_false_when_checkpoint_disabled(
    session_factory, tmp_path, monkeypatch
) -> None:
    """Even if the file exists, checkpoint_enabled=False ⇒ resumable=False."""
    from app.config import get_settings
    from app.models import RunEvent as RunEventModel
    from app.services.crash_recovery import run_startup_recovery

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    cp_dir = tmp_path / "cache" / "checkpoints"
    cp_dir.mkdir(parents=True)
    (cp_dir / "SPY.db").write_bytes(b"")

    run_id = await _seed_run(
        session_factory, status="running", ticker="SPY", checkpoint_enabled=False
    )

    async with session_factory() as session:
        await run_startup_recovery(session)

    async with session_factory() as session:
        result = await session.execute(
            select(RunEventModel).where(RunEventModel.run_id == str(run_id))
        )
        ev = result.scalar_one()

    assert ev.payload["resumable"] is False


async def test_lifespan_hook_runs_recovery(session_factory) -> None:
    """Calling the lifespan-hook function directly performs the recovery.

    The hook is a regular async function with the signature
    `(app: FastAPI) -> None` — we don't need to spin up uvicorn to
    exercise it, just invoke it with a placeholder app.
    """
    from fastapi import FastAPI

    from app.lifespan_hooks.crash_recovery import startup_recover

    run_id = await _seed_run(session_factory, status="running")

    app = FastAPI()
    await startup_recover(app)

    assert await _get_run_status(session_factory, run_id) == "interrupted"
    assert await _count_events(session_factory, run_id) == 1


def test_lifespan_hook_registered_at_startup() -> None:
    """Auto-discovery puts the hook in STARTUP_HOOKS.

    The lifespan-hook registry's ``_autoload`` walks every submodule of
    ``app/lifespan_hooks/`` on package import. Our module's ``@on_startup``
    decorator must have run as a side-effect, leaving ``startup_recover``
    in ``STARTUP_HOOKS``.
    """
    from app import lifespan_hooks
    from app.lifespan_hooks.crash_recovery import startup_recover

    assert startup_recover in lifespan_hooks.STARTUP_HOOKS, (
        "crash_recovery.startup_recover must be auto-registered on import"
    )
