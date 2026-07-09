"""Per-run async event bus with Postgres-backed replay.

This module is the single source of truth for the SSE event stream the
runner produces. The contract has two halves:

1. ``publish(run_id, event)`` — called by the run observer for every
   event the LangGraph produces. The event is **persisted to Postgres
   first** (with a monotonic per-run ``seq``) and **then** fan-out to
   any live subscribers via in-memory queues. If a subscriber's queue
   is full we drop the live frame but keep the DB row — slow consumers
   can rebuild full state via ``subscribe(last_event_id=<their seq>)``.

2. ``subscribe(run_id, last_event_id=None)`` — async generator that:
     * Phase 1 (replay): yields every row in ``run_events`` with
       ``seq > last_event_id`` in seq order.
     * Phase 2 (live tail): registers a fresh per-subscriber queue and
       yields events as ``publish`` fan-outs them. Exits when a
       terminal event (``run_completed`` / ``run_failed`` /
       ``run_cancelled``) is yielded OR ``close(run_id)`` is called.

Design choice: **per-subscriber queues**, not a single shared queue.
``_queues[run_id]`` is a ``list[asyncio.Queue]``. Each ``subscribe``
call appends its own queue and removes it on exit; ``publish`` iterates
the list and ``put_nowait`` on each. This keeps multi-subscriber
fan-out trivial (no broker, no semaphores) and means a slow subscriber
can only drop events for itself — not for its peers.

Concurrency notes:
- A per-run ``asyncio.Lock`` serializes the seq read-modify-write so the
  ``MAX(seq)+1`` race window is eliminated. Runs are single-publisher
  (the observer) in practice anyway, so contention is essentially zero.
- A module-level ``_lock`` guards mutations of the ``_queues`` map and
  the per-run lock map.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_factory
from app.models import RunEvent as RunEventModel
from app.schemas import RunEvent as RunEventSchema

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Module-level state                                                          #
# --------------------------------------------------------------------------- #

# Per-run list of subscriber queues. ``publish`` fan-outs to each entry.
_queues: dict[UUID, list[asyncio.Queue[dict[str, Any]]]] = {}

# Runs that have been explicitly closed. Live ``subscribe`` generators
# exit on their next read when their run is in this set.
_closed: set[UUID] = set()

# Guards mutations of ``_queues``, ``_closed``, and ``_publish_locks``.
# Lazy-initialised so the lock binds to the running event loop the first
# time it's awaited, not whichever loop happens to be current at module
# import time. Without this, uvicorn ``--reload`` and pytest-asyncio's
# per-test loops both trip on ``RuntimeError: <Lock> is bound to a
# different event loop``. Mirrors the same pattern in ``run_service``.
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock

# Per-run publish lock — serializes the ``MAX(seq)+1`` read-modify-write
# so concurrent publishers can't assign duplicate or out-of-order seqs.
_publish_locks: dict[UUID, asyncio.Lock] = {}

# Queue depth before back-pressure kicks in. Per the plan, full queues
# DROP the live frame (the DB row is the source of truth — subscribers
# replay on reconnect).
QUEUE_MAXSIZE = 200

# Event types that signal end-of-stream to subscribers.
TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed", "run_cancelled"})


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _coerce_to_dict(event: dict[str, Any] | RunEventSchema) -> dict[str, Any]:
    """Accept either a raw dict or a Pydantic ``RunEvent`` instance.

    Returns a *mutable copy* — callers may add ``seq`` / ``ts`` to it.
    """
    if isinstance(event, dict):
        return dict(event)
    # The discriminated union wrapper is a TypeAdapter; concrete
    # ``RunEventSchema`` instances are Pydantic ``BaseModel`` subclasses.
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    raise TypeError(
        f"event must be dict or RunEvent Pydantic instance, got {type(event)!r}"
    )


async def _get_publish_lock(run_id: UUID) -> asyncio.Lock:
    """Return the per-run publish lock, creating it lazily."""
    async with _get_lock():
        lock = _publish_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            _publish_locks[run_id] = lock
        return lock


async def _persist_event(
    db: AsyncSession, run_id: UUID, payload: dict[str, Any]
) -> tuple[int, datetime]:
    """Assign the next ``seq`` and insert the event. Returns (seq, ts).

    Caller MUST hold ``_get_publish_lock(run_id)`` so the
    ``MAX(seq)+1`` read-modify-write is serialized.
    """
    from sqlalchemy import func

    result = await db.execute(
        select(func.coalesce(func.max(RunEventModel.seq), 0)).where(
            RunEventModel.run_id == _uuid_for_db(run_id)
        )
    )
    next_seq = int(result.scalar() or 0) + 1

    ts = datetime.now(timezone.utc)
    # ``type`` lives in the payload (discriminator key); fall back to a
    # sentinel so the NOT NULL constraint doesn't fire on malformed input.
    event_type = str(payload.get("type", "unknown"))

    db.add(
        RunEventModel(
            run_id=_uuid_for_db(run_id),
            seq=next_seq,
            ts=ts,
            type=event_type,
            payload=payload,
        )
    )
    await db.flush()
    return next_seq, ts


def _uuid_for_db(run_id: UUID) -> Any:
    """Coerce UUID for DB binding.

    The SQLite driver (aiosqlite) does not bind ``uuid.UUID`` natively,
    so we hand it a string. On Postgres the ``UuidType`` variant
    accepts both shapes.
    """
    return str(run_id)


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


async def publish(
    run_id: UUID,
    event: dict[str, Any] | RunEventSchema,
    db: AsyncSession | None = None,
) -> int:
    """Persist an event then fan-out to live subscribers.

    The DB write is the source of truth — if the in-memory fan-out
    drops the frame (queue full, no subscribers, etc.) the event is
    still recoverable via ``subscribe(last_event_id=...)``.

    Returns the assigned ``seq``.
    """
    payload = _coerce_to_dict(event)

    publish_lock = await _get_publish_lock(run_id)
    async with publish_lock:
        if db is None:
            factory = get_session_factory()
            async with factory() as session:
                seq, ts = await _persist_event(session, run_id, payload)
                await session.commit()
        else:
            seq, ts = await _persist_event(db, run_id, payload)
            await db.commit()

    # Decorate the payload with the assigned seq + iso timestamp before
    # fan-out. Subscribers expect the merged shape (matches what
    # the replay phase yields).
    delivered = {**payload, "seq": seq, "ts": ts.isoformat()}

    # Fan-out under the module lock so we snapshot the subscriber list
    # atomically. ``put_nowait`` is non-blocking so holding the lock is
    # cheap.
    async with _get_lock():
        subscribers = list(_queues.get(run_id, ()))

    for queue in subscribers:
        try:
            queue.put_nowait(delivered)
        except asyncio.QueueFull:
            # Slow subscriber — DROP the live frame. The DB row stays;
            # the subscriber rebuilds full state on reconnect via
            # ``subscribe(last_event_id=<their last seq>)``.
            log.warning(
                "event_bus.queue_full_dropped_frame",
                extra={"run_id": str(run_id), "seq": seq},
            )

    return seq


async def subscribe(
    run_id: UUID, last_event_id: int | None = None
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield every event for ``run_id`` after ``last_event_id``.

    Phase 1: SELECT replay from the DB (``seq > last_event_id``).
    Phase 2: live tail via a freshly-registered per-subscriber queue.

    The generator exits when:
      * a terminal event (``run_completed``/``run_failed``/
        ``run_cancelled``) is yielded, OR
      * ``close(run_id)`` is called.

    ``last_event_id=None`` is equivalent to ``last_event_id=0`` — yields
    the full stream.
    """
    cursor = 0 if last_event_id is None else int(last_event_id)

    # Register the per-subscriber queue BEFORE the replay phase so we
    # don't miss events that publish concurrently with our replay
    # SELECT. The queue collects them; after replay we drain any queued
    # events whose seq <= cursor (already-replayed) and yield the rest.
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    async with _get_lock():
        _queues.setdefault(run_id, []).append(queue)

    try:
        # --- Phase 1: DB replay ------------------------------------- #
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(RunEventModel)
                .where(
                    RunEventModel.run_id == _uuid_for_db(run_id),
                    RunEventModel.seq > cursor,
                )
                .order_by(RunEventModel.seq.asc())
            )
            for row in result.scalars().all():
                ts_str = (
                    row.ts.isoformat()
                    if isinstance(row.ts, datetime)
                    else str(row.ts)
                )
                yielded = {**(row.payload or {}), "seq": row.seq, "ts": ts_str}
                cursor = max(cursor, row.seq)
                yield yielded
                if str(yielded.get("type")) in TERMINAL_EVENT_TYPES:
                    return

        # --- Phase 2: live tail ------------------------------------- #
        while True:
            if run_id in _closed:
                return

            # Use a short timeout so ``close(run_id)`` is observed even
            # if no events are flowing.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            # De-dup against the replay phase: a publish racing the
            # SELECT may queue an event we already replayed.
            seq = int(event.get("seq", 0))
            if seq <= cursor:
                continue
            cursor = seq

            yield event
            if str(event.get("type")) in TERMINAL_EVENT_TYPES:
                return
    finally:
        async with _get_lock():
            queues = _queues.get(run_id)
            if queues is not None:
                try:
                    queues.remove(queue)
                except ValueError:
                    pass
                if not queues:
                    _queues.pop(run_id, None)


def close(run_id: UUID) -> None:
    """Mark a run as closed.

    Live ``subscribe`` generators exit on their next poll. This is the
    coarse-grained shutdown hook for tests + the run-service teardown
    path; it does NOT delete persisted events.
    """
    _closed.add(run_id)


def reset_for_tests() -> None:
    """Wipe all in-memory state. Test-only hook."""
    global _lock
    _queues.clear()
    _closed.clear()
    _publish_locks.clear()
    # Drop the cached top-level lock too — the next ``_get_lock()`` call
    # will rebind to whichever loop the next test owns.
    _lock = None


__all__ = [
    "QUEUE_MAXSIZE",
    "TERMINAL_EVENT_TYPES",
    "publish",
    "subscribe",
    "close",
    "reset_for_tests",
]
