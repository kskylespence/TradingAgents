"""``/api/runs`` — submit, observe, cancel, resume and download runs.

This router is the HTTP-facing surface for the run lifecycle owned by
:mod:`app.services.run_service`. Endpoints:

============================  =====================================
``POST /api/runs``            Submit a new run (RunRequest -> run_id)
``GET  /api/runs/:id``        Run detail incl. computed elapsed/resumable
``GET  /api/runs/:id/events`` SSE stream of RunEvents (Last-Event-ID resume)
``POST /api/runs/:id/cancel`` Signal cancellation; 204
``POST /api/runs/:id/resume`` Spawn a sibling run from a checkpoint; 204
``GET  /api/runs/:id/report`` Download report.md / report.json / report.zip
============================  =====================================

Auth + CSRF
-----------
Every endpoint requires the JWT cookie (``Depends(get_current_user)``).
State-changing POSTs (submit, cancel, resume) are additionally guarded
by the global :class:`app.middleware.csrf.CSRFMiddleware`. ``GET
/events`` is reachable from ``EventSource``, which sends cookies
automatically — so JWT works fine and no header juggling is needed.

SSE replay contract
-------------------
The frontend reconnects with ``Last-Event-ID: <seq>``; we parse it (HTTP
header) and forward to :func:`event_bus.subscribe` which replays from
``seq > last_event_id`` then live-tails. The connection drops cleanly
when the generator yields a terminal event or the client disconnects
(``asyncio.CancelledError`` from inside the generator).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import AsyncIterator, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from . import register
from ..auth import get_current_user
from ..db import get_session
from ..models import Run
from ..schemas import AuthUser, RunDetail, RunRequest, RunStats
from ..services import event_bus, run_service

log = logging.getLogger(__name__)


router = APIRouter(prefix="/runs", tags=["runs"])


# --------------------------------------------------------------------------- #
# Submit                                                                      #
# --------------------------------------------------------------------------- #


@router.post("", status_code=status.HTTP_200_OK)
@router.post("/", status_code=status.HTTP_200_OK)
async def create_run(
    body: RunRequest,
    db: AsyncSession = Depends(get_session),
    _user: AuthUser = Depends(get_current_user),
) -> dict:
    """Queue a new run.

    Returns ``{run_id, status}`` immediately; the lifecycle runs in a
    background task and reports progress via SSE on ``/:id/events``.
    """
    run_id = await run_service.start_run(body, db)
    return {"run_id": str(run_id), "status": "queued"}


# --------------------------------------------------------------------------- #
# Detail                                                                      #
# --------------------------------------------------------------------------- #


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_session),
    _user: AuthUser = Depends(get_current_user),
) -> RunDetail:
    """Load a run row + compute derived fields (elapsed, resumable)."""
    row = await db.get(Run, str(run_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
        )
    return _to_detail(row)


# --------------------------------------------------------------------------- #
# SSE events                                                                  #
# --------------------------------------------------------------------------- #


@router.get("/{run_id}/events")
async def stream_events(
    run_id: UUID,
    request: Request,
    _user: AuthUser = Depends(get_current_user),
) -> EventSourceResponse:
    """Server-Sent Events stream of every event for ``run_id``.

    Honors the ``Last-Event-ID`` header for gap-free reconnection. A
    15-second keepalive ping is emitted by sse-starlette so intermediate
    proxies (Coolify / Traefik) don't idle-close the socket.

    The generator exits when either:
    - A terminal event (``run_completed``/``run_failed``/``run_cancelled``)
      is yielded by the bus, OR
    - The client disconnects (FastAPI cancels the request task and the
      generator raises ``CancelledError``, which we swallow).
    """
    last_event_id = _parse_last_event_id(request.headers.get("Last-Event-ID"))

    async def _gen() -> AsyncIterator[dict]:
        gen = event_bus.subscribe(run_id, last_event_id=last_event_id)
        try:
            async for event in gen:
                # sse-starlette accepts dicts with keys: id, event, data, retry.
                # We only set id (seq) and data (JSON payload). ``event``
                # is intentionally omitted so the default ``message`` event
                # name is used — the client discriminates on payload.type.
                yield {
                    "id": str(event.get("seq", 0)),
                    "data": json.dumps(event, default=str),
                }
        except asyncio.CancelledError:
            # Client disconnected; let the generator close cleanly so
            # the bus removes our per-subscriber queue.
            log.debug("runs.sse.client_disconnect", extra={"run_id": str(run_id)})
            raise
        finally:
            await gen.aclose()

    # ping=15 emits a `:keepalive\n\n` comment every 15 seconds.
    return EventSourceResponse(_gen(), ping=15)


def _parse_last_event_id(raw: Optional[str]) -> Optional[int]:
    """Parse the ``Last-Event-ID`` header into an int, or None on garbage.

    Per the SSE spec, the client echoes back whatever we sent as ``id``;
    we always send integer seqs, so any non-integer is treated as
    "start from the beginning".
    """
    if not raw:
        return None
    try:
        value = int(raw.strip())
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Cancel / resume                                                             #
# --------------------------------------------------------------------------- #


@router.post(
    "/{run_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def cancel(
    run_id: UUID,
    _user: AuthUser = Depends(get_current_user),
) -> Response:
    """Signal a graceful cancellation. 204 even if the run isn't active."""
    await run_service.cancel_run(run_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{run_id}/resume",
    status_code=status.HTTP_200_OK,
)
async def resume(
    run_id: UUID,
    db: AsyncSession = Depends(get_session),
    _user: AuthUser = Depends(get_current_user),
) -> dict:
    """Spawn a new run that resumes from ``run_id``'s checkpoint.

    Returns ``{run_id, parent_run_id}`` so the frontend can subscribe
    to the new SSE stream.
    """
    new_id = await run_service.resume_run(run_id, db)
    return {"run_id": str(new_id), "parent_run_id": str(run_id)}


# --------------------------------------------------------------------------- #
# Report download                                                             #
# --------------------------------------------------------------------------- #


@router.get("/{run_id}/report")
async def get_report(
    run_id: UUID,
    format: str = Query("md", regex="^(md|json|zip)$"),
    db: AsyncSession = Depends(get_session),
    _user: AuthUser = Depends(get_current_user),
) -> Response:
    """Download the run's report in markdown, JSON, or zip-archive form.

    The report directory was materialised at completion by
    :func:`run_service._finalize_completion`. 404 if the run hasn't
    completed yet or the directory is missing.
    """
    row = await db.get(Run, str(run_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
        )
    if not row.report_dir:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run has not produced a report yet",
        )
    report_dir = Path(row.report_dir)
    if not report_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report directory missing on disk",
        )

    if format == "md":
        md_path = report_dir / "report.md"
        if not md_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="report.md missing",
            )
        return FileResponse(
            path=str(md_path),
            media_type="text/markdown; charset=utf-8",
            filename=f"{row.ticker}_{row.analysis_date}.md",
        )

    if format == "json":
        # Compose the JSON envelope from the row + the report text.
        md_text = ""
        md_path = report_dir / "report.md"
        if md_path.exists():
            md_text = md_path.read_text(encoding="utf-8")
        body = {
            "run_id": str(row.id),
            "ticker": row.ticker,
            "analysis_date": row.analysis_date.isoformat(),
            "rating": row.rating,
            "decision": row.decision_full,
            "report_markdown": md_text,
        }
        return Response(
            content=json.dumps(body, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{row.ticker}_{row.analysis_date}.json"'
                )
            },
        )

    # format == "zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in report_dir.rglob("*"):
            if path.is_file():
                arcname = path.relative_to(report_dir).as_posix()
                zf.write(path, arcname=arcname)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{row.ticker}_{row.analysis_date}.zip"'
            )
        },
    )


# --------------------------------------------------------------------------- #
# Row -> schema projection                                                    #
# --------------------------------------------------------------------------- #


def _to_detail(row: Run) -> RunDetail:
    """Project a ``Run`` ORM row into the ``RunDetail`` schema.

    Computes ``elapsed_seconds`` from ``started_at``/``finished_at`` (or
    just ``started_at`` if still running) and marks ``resumable`` when
    the run was interrupted with checkpointing enabled.
    """
    from datetime import datetime, timezone

    def _aware(dt: datetime) -> datetime:
        # SQLite drops tzinfo on the way out of the DB; normalise both
        # sides of the subtraction to UTC so the arithmetic works on
        # both SQLite (tests) and Postgres (prod, which preserves it).
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    elapsed: Optional[float] = None
    if row.started_at is not None:
        end = row.finished_at or datetime.now(timezone.utc)
        elapsed = max(0.0, (_aware(end) - _aware(row.started_at)).total_seconds())

    resumable = bool(row.status == "interrupted" and row.checkpoint_enabled)

    stats = None
    if row.stats:
        try:
            stats = RunStats.model_validate(row.stats)
        except Exception:  # noqa: BLE001 — bad stats should not break the row
            log.warning(
                "runs.stats_invalid",
                extra={"run_id": str(row.id)},
            )

    thinking_cfg = None
    if row.thinking_config:
        from ..schemas import ThinkingConfig

        try:
            thinking_cfg = ThinkingConfig.model_validate(row.thinking_config)
        except Exception:  # noqa: BLE001
            thinking_cfg = None

    return RunDetail(
        id=row.id,
        ticker=row.ticker,
        asset_type=row.asset_type,  # type: ignore[arg-type]
        analysis_date=row.analysis_date,
        status=row.status,  # type: ignore[arg-type]
        rating=row.rating,  # type: ignore[arg-type]
        llm_provider=row.llm_provider,
        research_depth=row.research_depth,  # type: ignore[arg-type]
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        elapsed_seconds=elapsed,
        analysts=list(row.analysts),  # type: ignore[arg-type]
        quick_think_llm=row.quick_think_llm,
        deep_think_llm=row.deep_think_llm,
        thinking_config=thinking_cfg,
        output_language=row.output_language,
        checkpoint_enabled=row.checkpoint_enabled,
        decision_full=row.decision_full,
        report_dir=row.report_dir,
        error_message=row.error_message,
        stats=stats,
        resumable=resumable,
    )


register(router)


__all__ = ["router"]
