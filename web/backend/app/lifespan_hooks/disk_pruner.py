"""Lifespan hook that runs the disk-pruner background task.

Spawns ``app.services.disk_pruner.prune_loop`` as an ``asyncio.Task`` on
startup and cancels it cleanly on shutdown. The loop itself handles its
own scheduling (default: every 6 hours) and per-tick DB-session lifecycle.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from . import on_shutdown, on_startup

log = logging.getLogger(__name__)

# Module-level handle on the running task. Held so ``stop`` can cancel it
# during FastAPI shutdown. ``None`` until ``start`` runs.
_task: asyncio.Task | None = None


@on_startup
async def start(app: FastAPI) -> None:
    """Kick off the disk-pruner background task on app startup."""
    global _task
    from app.config import get_settings
    from app.services.disk_pruner import prune_loop

    settings = get_settings()
    _task = asyncio.create_task(
        prune_loop(settings.data_dir, settings.retention_days),
        name="disk_pruner",
    )
    log.info(
        "disk_pruner.task_started",
        extra={
            "data_dir": str(settings.data_dir),
            "retention_days": settings.retention_days,
        },
    )


@on_shutdown
async def stop(app: FastAPI) -> None:
    """Cancel the background task and await its CancelledError cleanly."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("disk_pruner.task_shutdown_failed")
    finally:
        _task = None
        log.info("disk_pruner.task_stopped")
