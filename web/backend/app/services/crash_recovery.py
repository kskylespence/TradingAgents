"""Crash recovery: reconcile orphaned 'running' rows on backend startup.

The previous process may have been killed mid-stream (Coolify redeploy,
OOM, manual ``kill -9``). Rows in ``runs`` left with ``status='running'``
correspond to those orphans; reconnecting SSE clients would wait forever
for a terminator that will never arrive.

On startup we scan for ``status='running'`` rows and, for each one:

1. Transition the row to ``status='interrupted'``.
2. Append a terminal event to ``run_events`` so any (re-)connecting SSE
   subscriber sees a stream terminator and disconnects cleanly. We emit
   ``type='run_failed'`` (rather than a new ``run_interrupted`` event
   type) because:

   * ``event_bus.TERMINAL_EVENT_TYPES`` already contains ``run_failed``,
     so SSE generators exit on it without any other change.
   * The ``RunFailedEvent`` schema already has an ``error`` field that
     can carry the human-readable reason ("Server restarted while run
     was in progress").
   * Adding a new event type would require modifying ``app/schemas.py``
     and ``event_bus.TERMINAL_EVENT_TYPES`` — both off-limits for this
     wave. The DB column ``runs.status`` IS ``'interrupted'``, so the
     UI can still distinguish a graceful failure from a crash recovery
     by reading the status — the event is just the stream terminator.
3. Determine whether the run is ``resumable``: True iff
   ``checkpoint_enabled=True`` and the LangGraph SQLite checkpoint file
   for ``(ticker, analysis_date)`` exists on disk. The resumable flag
   is stashed in the event payload's ``error`` string (best-effort —
   the canonical place is ``RunDetail.resumable`` which the run-detail
   router computes on read).

The transition + event emission are committed atomically per run, so a
crash mid-recovery leaves a clean state: either both happened (status
flipped AND terminator emitted) or neither did (next startup re-runs
the recovery for that row).

This module is **idempotent**: running it twice in a row finds nothing
the second time because the first call has already flipped every
orphan out of ``'running'``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Run, RunEvent as RunEventModel

log = logging.getLogger(__name__)


_INTERRUPTED_ERROR_MESSAGE = (
    "Server restarted while run was in progress; transitioning to 'interrupted'."
)


def _uuid_for_db(run_id: UUID) -> Any:
    """Coerce UUID for DB binding (matches event_bus convention).

    aiosqlite does not bind ``uuid.UUID`` natively; Postgres' ``UuidType``
    accepts both shapes.
    """
    return str(run_id)


def _checkpoint_dir() -> Path:
    """Return the directory holding LangGraph checkpoint SQLite files.

    The canonical TradingAgents layout (see
    ``tradingagents/graph/checkpointer.py:_db_path`` and
    ``tradingagents/graph/trading_graph.py:311-316``) is::

        <data_cache_dir>/checkpoints/<TICKER_UPPER>.db

    where ``data_cache_dir`` defaults to ``~/.tradingagents/cache`` and
    is overridable via the ``TRADINGAGENTS_CACHE_DIR`` env var. The
    web backend wraps that as ``settings.data_dir / "cache"`` so the
    container's persistent volume holds both report output and the
    checkpoint store side-by-side.
    """
    settings = get_settings()
    return Path(settings.data_dir) / "cache" / "checkpoints"


def has_checkpoint(ticker: str, analysis_date: date) -> bool:
    """True iff a LangGraph checkpoint SQLite file exists for this ticker.

    The presence of the per-ticker DB file is a necessary precondition
    for resume; the actual *thread* (per-date) lookup happens inside
    LangGraph's ``SqliteSaver.get_tuple`` when the run is restarted.
    We deliberately do not open the SQLite file here — that would
    require a sync DB read in an async startup path and would be a much
    larger contract than "does the file exist?".

    ``analysis_date`` is part of the signature for symmetry with the
    eventual full resume API (which keys on both ticker AND date), but
    the file path is ticker-only because the DB is shared across dates
    for the same ticker (one row per thread, where ``thread_id`` =
    ``sha256(f"{ticker}:{date}")[:16]``).
    """
    # Lazy import so this module can be imported even if the
    # tradingagents package can't be installed in some test envs.
    from tradingagents.dataflows.utils import safe_ticker_component

    try:
        safe = safe_ticker_component(ticker).upper()
    except ValueError:
        # An unsafe ticker can never have a legitimate checkpoint file —
        # the writer goes through ``safe_ticker_component`` too. Treat
        # it as no-checkpoint rather than blowing up startup.
        return False

    return (_checkpoint_dir() / f"{safe}.db").exists()


async def _next_seq(db: AsyncSession, run_id: UUID) -> int:
    """Return ``MAX(seq) + 1`` for a run, or 1 if no events exist yet."""
    result = await db.execute(
        select(func.coalesce(func.max(RunEventModel.seq), 0)).where(
            RunEventModel.run_id == _uuid_for_db(run_id)
        )
    )
    return int(result.scalar() or 0) + 1


async def _emit_interrupted_terminal(
    db: AsyncSession, run_id: UUID, resumable: bool
) -> None:
    """Insert a terminal ``run_failed`` event for the recovered run.

    The same session is used as the row transition so commit is atomic.
    """
    payload: dict[str, Any] = {
        "type": "run_failed",
        "error": _INTERRUPTED_ERROR_MESSAGE,
        "interrupted": True,
        "resumable": resumable,
    }
    seq = await _next_seq(db, run_id)
    db.add(
        RunEventModel(
            run_id=_uuid_for_db(run_id),
            seq=seq,
            ts=datetime.now(timezone.utc),
            type="run_failed",
            payload=payload,
        )
    )


async def run_startup_recovery(db: AsyncSession) -> list[UUID]:
    """Find ``status='running'`` rows, transition to ``'interrupted'``.

    For each orphan we also emit a terminal event so any subscriber that
    reconnects after restart sees the stream end. The whole transition
    (status flip + event insert) is committed as a single transaction
    so we never leave a row stuck half-recovered.

    Returns the list of run IDs that were recovered (empty on a clean
    startup with no orphans, or on a re-run after recovery has already
    happened — this function is idempotent).
    """
    result = await db.execute(select(Run).where(Run.status == "running"))
    orphans = list(result.scalars().all())

    recovered: list[UUID] = []
    for run in orphans:
        # Coerce the row's id to UUID for the return value, regardless of
        # whether the DB driver gave us a str (aiosqlite) or UUID (asyncpg).
        run_uuid = run.id if isinstance(run.id, UUID) else UUID(str(run.id))

        try:
            resumable = bool(run.checkpoint_enabled) and has_checkpoint(
                run.ticker, run.analysis_date
            )
        except Exception:
            # Disk-IO failures on the checkpoint scan must not abort
            # recovery — the row still needs to leave 'running'.
            log.exception(
                "crash_recovery.checkpoint_scan_failed",
                extra={"run_id": str(run_uuid), "ticker": run.ticker},
            )
            resumable = False

        run.status = "interrupted"
        run.finished_at = datetime.now(timezone.utc)
        if not run.error_message:
            run.error_message = _INTERRUPTED_ERROR_MESSAGE

        await _emit_interrupted_terminal(db, run_uuid, resumable)
        # Commit per-orphan so a crash mid-loop leaves a clean partial
        # state: every run we've processed is fully transitioned, and
        # every one we haven't is still 'running' (and will be picked
        # up on the next startup).
        await db.commit()
        recovered.append(run_uuid)

    return recovered


__all__ = ["run_startup_recovery", "has_checkpoint"]
