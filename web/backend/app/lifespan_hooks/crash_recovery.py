"""Lifespan hook: scan for orphaned runs on startup.

Wires ``app.services.crash_recovery.run_startup_recovery`` into the
FastAPI lifespan. Any row in ``runs`` left at ``status='running'`` by
the previous (crashed) process is transitioned to ``'interrupted'`` and
gets a terminal event appended so reconnecting SSE clients see a clean
end-of-stream.

Idempotent: re-running finds nothing because the first call has already
flipped every orphan out of ``'running'``.

Lives in ``app/lifespan_hooks/`` so the auto-discovery loader picks it
up — no edits to ``main.py`` are needed (see
``app/lifespan_hooks/__init__.py:_autoload``).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from . import on_startup

log = logging.getLogger(__name__)


@on_startup
async def startup_recover(app: FastAPI) -> None:
    """Scan for orphaned 'running' rows and transition them to 'interrupted'.

    We swallow ``OperationalError`` (e.g. "no such table: runs") on
    purpose: it means the schema isn't materialized yet, which is the
    normal state for ``TestClient`` smoke tests against an empty
    in-memory DB and for first-boot before Alembic has run. The next
    real startup (or migration) will produce a populated schema, at
    which point recovery does its job.

    Any OTHER exception is logged but NOT re-raised — losing crash-
    recovery is a soft failure, not a reason to refuse to serve traffic.
    The next restart will retry.
    """
    # Imports kept local so this module loads even when the parent
    # ``app.services.crash_recovery`` has an import-time failure (e.g.
    # missing optional dep). The lifespan-registry autoloader walks
    # every submodule on package load — we don't want a downstream
    # import error to prevent OTHER hooks from registering.
    from sqlalchemy.exc import OperationalError

    from app.db import get_session_factory
    from app.services.crash_recovery import run_startup_recovery

    factory = get_session_factory()
    try:
        async with factory() as session:
            recovered = await run_startup_recovery(session)
    except OperationalError as exc:
        # "no such table" / "relation does not exist" — schema not
        # materialized. Quiet by design (tests + first-boot path).
        log.info(
            "crash_recovery.skipped_schema_not_ready",
            extra={"reason": str(exc.orig) if exc.orig else str(exc)},
        )
        return
    except Exception:
        # Don't take the app down for a recovery glitch.
        log.exception("crash_recovery.failed")
        return

    if recovered:
        log.warning(
            "crash_recovery.transitioned_orphaned_runs",
            extra={
                "count": len(recovered),
                "run_ids": [str(rid) for rid in recovered],
            },
        )
    else:
        log.info("crash_recovery.no_orphans_found")


__all__ = ["startup_recover"]
