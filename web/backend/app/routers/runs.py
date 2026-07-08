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
from .. import catalog as catalog_svc
from ..auth import get_current_user
from ..db import get_session
from ..models import Run
from ..schemas import (
    AuthUser,
    RunDetail,
    RunRequest,
    RunStats,
    RunValidationError,
    UnhealthyModel,
)
from ..services import event_bus, run_service

log = logging.getLogger(__name__)


router = APIRouter(prefix="/runs", tags=["runs"])


# --------------------------------------------------------------------------- #
# Submit                                                                      #
# --------------------------------------------------------------------------- #


async def _suggested_alternatives() -> list[str]:
    """Build the ``suggested_alternatives`` list for ``RunValidationError``.

    Intersect of:

    * The curated cloud catalog snapshot (``CURATED_2026_05``).
    * The currently-cached ``/v1/models`` listing — what the upstream
      account actually has access to.
    * Models NOT cached as unhealthy.

    Sorted alphabetically with the newest GLM headline model pinned first
    when present (``glm-5.2`` → ``glm-5.1`` → ``glm-5``), capped at 3
    entries. Returning fewer than 3 is fine — the UI handles the empty case.
    """
    from ..services.ollama_curated import CURATED_2026_05
    from ..services.ollama_models import (
        cached_probe_unhealthy_models,
        list_ollama_models,
    )

    available = set(await list_ollama_models())
    unhealthy = set(cached_probe_unhealthy_models())
    candidates = sorted(
        (mid for mid in CURATED_2026_05 if mid in available and mid not in unhealthy)
    )

    # Pin the GLM headline model to the front when it survived the filter;
    # prefer the newest release the snapshot knows about.
    _HEADLINE_PIN_ORDER = ("glm-5.2", "glm-5.1", "glm-5")
    head: list[str] = []
    for mid in _HEADLINE_PIN_ORDER:
        if mid in candidates:
            head.append(mid)
            candidates.remove(mid)
    return (head + candidates)[:3]


async def _validate_and_probe(body: RunRequest) -> None:
    """Reject invalid AND unresponsive model selections BEFORE engine launch.

    Two layers:

    1. **Catalog validation** — same defense-in-depth logic that landed
       in Phase 1. Rejects stale form submissions / replayed POSTs that
       reference a model the provider doesn't expose.

    2. **Liveness probe (Ollama only)** — ``POST /v1/chat/completions``
       with a tiny tool-bearing payload to every selected model. A run
       on 2026-05-22 hung for 56 minutes because the engine launched on
       an upstream-stuck ``kimi-k2-thinking``; the probe catches that
       failure mode in ~15s and returns a structured 400 with healthy
       alternatives. Other providers (openai, anthropic, ...) are out
       of scope here — they will get their own liveness checks in a
       follow-up pass.
    """
    from tradingagents.providers import available_providers

    provider = body.llm_provider
    avail_keys = {p.key for p in available_providers()}
    if provider not in avail_keys:
        available = ", ".join(sorted(avail_keys)) if avail_keys else "(none configured)"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Provider {provider!r} is not configured on this deployment. "
                f"Available providers: {available}"
            ),
        )

    for field_name, value, mode in (
        ("quick_think_llm", body.quick_think_llm, "quick"),
        ("deep_think_llm", body.deep_think_llm, "deep"),
    ):
        ids = {m.id for m in await catalog_svc.list_models(provider, mode)}  # type: ignore[arg-type]
        if "__custom__" in ids:
            continue
        if value not in ids:
            available = ", ".join(sorted(ids)) if ids else "(no models available)"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Model {value!r} is not available on provider "
                    f"{provider!r}. Available: {available}"
                ),
            )

    # Liveness probe — Ollama only. Dedup so quick == deep is one probe.
    if provider == "ollama":
        from ..services.ollama_models import probe_model_liveness

        to_probe = {body.quick_think_llm, body.deep_think_llm}
        # asyncio.gather preserves submission order, but we want a
        # deterministic listing in the response — sort the model ids so
        # the unhealthy_models block reads the same on every call.
        ordered = sorted(to_probe)
        results = await asyncio.gather(*(probe_model_liveness(m) for m in ordered))

        unhealthy: list[UnhealthyModel] = []
        for result in results:
            outcome = result.get("status")
            if outcome == "ok":
                continue
            unhealthy.append(
                UnhealthyModel(
                    model=result["model"],
                    status=outcome,  # type: ignore[arg-type]
                    upstream_ref=result.get("upstream_ref"),
                )
            )

        if unhealthy:
            # Preserve the dual-selection ordering in the response — the
            # user's quick_think model first, then deep_think — when
            # both are unhealthy. Single-model failures keep their natural
            # order.
            wanted = [body.quick_think_llm, body.deep_think_llm]
            unhealthy.sort(
                key=lambda u: (
                    wanted.index(u.model)
                    if u.model in wanted
                    else len(wanted)
                )
            )
            alternatives = await _suggested_alternatives()
            names = ", ".join(u.model for u in unhealthy)
            error = RunValidationError(
                code="upstream_model_unhealthy",
                message=(
                    f"Selected model(s) {names} are not responding on Ollama "
                    "Cloud. Pick a known-good alternative below or wait for "
                    "the upstream to recover."
                ),
                unhealthy_models=unhealthy,
                suggested_alternatives=alternatives,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error.model_dump(),
            )


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

    Validates provider/model against the live catalog AND runs a pre-flight
    liveness probe (Ollama only) before launching the engine — see
    ``_validate_and_probe``.
    """
    await _validate_and_probe(body)
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


@router.post(
    "/{run_id}/retry",
    status_code=status.HTTP_200_OK,
)
async def retry(
    run_id: UUID,
    db: AsyncSession = Depends(get_session),
    _user: AuthUser = Depends(get_current_user),
) -> dict:
    """Spawn a sibling run from a failed/cancelled run's persisted params.

    The user shouldn't have to re-fill the entire ``NewRun`` form after
    an upstream transient blip. We reconstruct the ``RunRequest`` from
    the parent row and delegate to the same submit path used by
    ``POST /api/runs`` — guaranteeing catalog validation, env-credential
    checks, and global-lock semantics all behave identically. Returns
    ``{run_id, parent_run_id}`` matching ``/resume``.

    Only ``failed`` and ``cancelled`` runs are retryable.
    ``interrupted`` already has ``/resume``; ``completed`` / ``running``
    / ``queued`` are not retry-shaped.
    """
    parent = await db.get(Run, str(run_id))
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
        )
    if parent.status not in {"failed", "cancelled"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot retry run in status {parent.status!r}",
        )

    thinking_cfg = parent.thinking_config or {}
    req = RunRequest(
        ticker=parent.ticker,
        analysis_date=parent.analysis_date,
        output_language=parent.output_language,
        analysts=list(parent.analysts),  # type: ignore[arg-type]
        research_depth=parent.research_depth,  # type: ignore[arg-type]
        llm_provider=parent.llm_provider,
        quick_think_llm=parent.quick_think_llm,
        deep_think_llm=parent.deep_think_llm,
        google_thinking_level=thinking_cfg.get("google_thinking_level"),
        openai_reasoning_effort=thinking_cfg.get("openai_reasoning_effort"),
        anthropic_effort=thinking_cfg.get("anthropic_effort"),
        enable_checkpoint=bool(parent.checkpoint_enabled),
    )
    await _validate_and_probe(req)
    new_id = await run_service.start_run(req, db)
    return {"run_id": str(new_id), "parent_run_id": str(run_id)}


# --------------------------------------------------------------------------- #
# Report download                                                             #
# --------------------------------------------------------------------------- #


@router.get("/{run_id}/report")
async def get_report(
    run_id: UUID,
    format: str = Query("md", pattern="^(md|json|zip)$"),
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
