"""Per-run lifecycle orchestration.

This module owns the **end-to-end flow** for a single analysis run:

1. Accept a ``RunRequest`` from the HTTP layer.
2. Persist a ``runs`` row in status ``queued``.
3. Spawn an asyncio task that:
    a. Acquires the v1 ``GLOBAL_RUN_LOCK`` (one concurrent run, per plan).
    b. Decrypts the provider API key(s) from ``api_keys`` and injects them
       into ``os.environ`` for the duration of the run via
       :func:`app.services.env_inject.scope`.
    c. Builds a ``TradingAgentsGraph`` mirroring ``cli/main.py`` config
       assembly.
    d. Runs the shared synchronous engine loop
       (:func:`tradingagents.run_observer.stream_run`) on a worker thread
       via ``asyncio.to_thread`` so the asyncio loop is never blocked.
    e. Publishes every observed callback as an SSE event via the
       :mod:`app.services.event_bus`.
    f. Marks the row ``completed`` / ``failed`` / ``cancelled`` and
       finalises ``rating`` + ``report_dir`` + ``stats``.

Cancellation
------------
A per-run ``asyncio.Event`` is exposed via ``cancel_run(run_id)``. The
engine loop polls it between LangGraph chunks; when the event fires the
loop returns early, the observer's ``on_cancelled`` event is emitted, and
the row is marked ``cancelled``. There is **no** preemption mid-chunk —
cancellation is honored at chunk boundaries to keep the engine's state
machine consistent. ``CancelledError`` raised on the task itself is also
caught and translated into the same cancelled-row state.

FAKE_LLM hook
-------------
When ``FAKE_LLM=1`` is set in the environment, the lifecycle replaces the
``stream_run`` invocation with a scripted in-process simulator that emits
a deterministic sequence of events (run_started → analyst status flips →
messages → report sections) ending in ``run_completed("Buy", ...)``. The
hook exists so the end-to-end smoke test can drive the full lifecycle
without configuring real LLM credentials. See
:func:`_fake_stream_run` for the canned script and
:mod:`tests.test_runs_smoke` for the consumer.

Security
--------
The user-supplied ``ticker`` is preserved verbatim in the DB column (we
store user input) but is filtered through
:func:`tradingagents.dataflows.utils.safe_ticker_component` whenever it
reaches a filesystem path (e.g. ``report_dir`` under
``settings.data_dir / "logs"``). The CLAUDE.md security note is the
canonical reference; do not bypass.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas as S
from app.config import get_settings
from app.crypto import FernetNotConfiguredError, InvalidToken, decrypt
from app import db as _db  # access via _db.get_session_factory() so tests can monkeypatch
from app.models import ApiKey, Run
from app.observers.web_run_observer import WebRunObserver
from app.services import env_inject, event_bus

# Imports from the engine — kept module-scope so tests can monkeypatch
# them (e.g. ``monkeypatch.setattr(run_service, "stream_run", ...)``)
# without touching the engine package itself.
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.graph.analyst_execution import (
    AnalystWallTimeTracker,
    build_analyst_execution_plan,
)
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.run_observer import ANALYST_AGENT_NAMES, stream_run
from tradingagents.stats_handler import StatsCallbackHandler

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Wall-clock safety net                                                       #
# --------------------------------------------------------------------------- #

#: Hard ceiling on how long a single run is allowed to hold
#: ``GLOBAL_RUN_LOCK``. If the engine hangs beyond this — usually because
#: an upstream LLM provider stopped responding mid-stream — the lifecycle
#: aborts with a clear error so the lock is released and the operator can
#: queue another run. Overridable via the ``TRADINGAGENTS_RUN_MAX_SECONDS``
#: env var. Default 30 minutes: long enough for a deep ``research_depth=5``
#: run on a slow local model, short enough that a stuck cloud call won't
#: brick the UI for an hour. The 56-minute hang on 2026-05-22 is the
#: motivating incident (see commit 2ccfeda).
RUN_MAX_SECONDS_DEFAULT = 1800.0


# --------------------------------------------------------------------------- #
# Friendly error formatting                                                   #
# --------------------------------------------------------------------------- #


#: Permissive matcher for upstream "ref:" correlation IDs surfaced inside
#: provider error bodies. Ollama Cloud emits standard UUIDs; other
#: providers use hex tokens of varying length. The lower bound (8 chars
#: after the first nibble) avoids matching the literal word "ref:" with
#: nothing useful after it, while still catching the shortest sensible
#: correlation IDs.
_REF_PATTERN = re.compile(r"ref:\s*([0-9a-f][0-9a-f-]{7,})", re.I)


def _format_engine_error(exc: BaseException, provider: str) -> str:
    """Render an operator-actionable message for an engine-loop failure.

    The string is persisted into ``runs.error_message`` and rendered
    verbatim by the frontend, so it must read as a complete sentence to
    a non-technical operator. Where the underlying SDK error carries a
    correlation ID (``ref:<uuid>``) we surface it prominently so it can
    be quoted back to the upstream provider during incident triage.

    The classification ordering matters: ``InternalServerError``,
    ``AuthenticationError``, ``RateLimitError``, and ``BadRequestError``
    are all subclasses of ``openai.APIStatusError``. We match the more
    specific classes first so a 401 isn't mis-classified as a 5xx, and
    we match ``APITimeoutError`` before ``APIConnectionError`` because
    the timeout type inherits from the connection type. Any exception
    we don't recognise falls back to the legacy
    ``"{ClassName}: {message}"`` shape so unknown failure modes still
    leave a usable trace.
    """
    # Lazy imports keep the module importable in environments where the
    # OpenAI / httpx SDKs aren't installed (e.g. minimal test harnesses).
    try:
        import openai  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover — defensive
        openai = None  # type: ignore[assignment]
    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover — defensive
        httpx = None  # type: ignore[assignment]

    exc_str = str(exc)
    ref_match = _REF_PATTERN.search(exc_str)
    ref = ref_match.group(1) if ref_match else None

    if openai is not None:
        if isinstance(exc, openai.InternalServerError) or (
            isinstance(exc, openai.APIStatusError)
            and not isinstance(
                exc,
                (
                    openai.AuthenticationError,
                    openai.RateLimitError,
                    openai.BadRequestError,
                ),
            )
            and getattr(exc, "status_code", 0) >= 500
        ):
            status_code = getattr(exc, "status_code", 500) or 500
            return (
                f"Upstream provider error ({provider}, HTTP {status_code}). "
                f"This is usually transient. Reference: {ref or 'n/a'}. "
                "Click Retry below, or pick a different model if it persists."
            )
        if isinstance(exc, openai.APITimeoutError):
            return (
                f"Request to {provider} timed out. Retry shortly; if it keeps "
                "timing out, try a smaller/faster model."
            )
        if isinstance(exc, openai.APIConnectionError) or (
            httpx is not None and isinstance(exc, httpx.ConnectError)
        ):
            return (
                f"Could not reach {provider}. Verify network connectivity "
                "and provider base URL (e.g. OLLAMA_BASE_URL)."
            )
        if isinstance(exc, openai.AuthenticationError):
            return (
                f"Authentication failed for {provider}. Verify the API key "
                "environment variable is set correctly."
            )
        if isinstance(exc, openai.RateLimitError):
            return f"Rate limited by {provider}. Wait a moment and Retry."
        if isinstance(exc, openai.BadRequestError):
            return f"Bad request to {provider}: {exc_str}"
    elif httpx is not None and isinstance(exc, httpx.ConnectError):
        return (
            f"Could not reach {provider}. Verify network connectivity "
            "and provider base URL (e.g. OLLAMA_BASE_URL)."
        )

    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Module-level concurrency state (v1: one run at a time)                      #
# --------------------------------------------------------------------------- #

#: v1 invariant — only one analysis run executes at a time. ``start_run``
#: refuses (409) when this lock is held; the runtime asyncio task itself
#: acquires it for the duration of the engine loop. v2 will replace this
#: with per-user / per-tenant scheduling.
#:
#: Lazy-instantiated by :func:`_get_lock` so the lock binds to whichever
#: event loop is current at first use. A module-import-time
#: ``asyncio.Lock()`` binds to whatever loop happens to be running at
#: import — fine in prod (one long-lived loop), but pytest creates a
#: fresh loop per test, which would leave the lock stuck across loops.
GLOBAL_RUN_LOCK: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    """Return the per-process global run lock, lazily constructed."""
    global GLOBAL_RUN_LOCK
    if GLOBAL_RUN_LOCK is None:
        GLOBAL_RUN_LOCK = asyncio.Lock()
    return GLOBAL_RUN_LOCK

#: Currently-executing run id (only valid while the lock is held).
#: Reset to ``None`` in the task's ``finally`` block.
_active_run_id: Optional[UUID] = None

#: Per-run asyncio.Event used to signal cancellation. The engine loop
#: polls ``cancel_event.is_set()`` at chunk boundaries.
_cancel_events: Dict[UUID, asyncio.Event] = {}

#: Per-run asyncio.Task handles for the lifecycle coroutine. Tests rely
#: on these for deterministic awaits.
_run_tasks: Dict[UUID, asyncio.Task[Any]] = {}


# --------------------------------------------------------------------------- #
# Helpers exposed for other modules                                           #
# --------------------------------------------------------------------------- #


def get_active_run_id() -> Optional[UUID]:
    """Return the currently-executing run id, or ``None``.

    Used by the health router (``/api/health``) so dashboards can see if
    an analysis is in flight without polling the DB.
    """
    return _active_run_id


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


async def start_run(req: S.RunRequest, db: AsyncSession) -> UUID:
    """Persist a queued run row and spawn its lifecycle task.

    Returns the new ``run_id``. Raises HTTP 409 if another run is already
    in progress (v1 single-run constraint).
    """
    # The lock is held *inside* the lifecycle task — but we also refuse
    # at submit time so the client gets a clean 409 rather than waiting
    # in a queue for an unbounded amount of time.
    if _get_lock().locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another run is in progress",
        )

    run_id = uuid4()
    asset_type = _detect_asset_type(req.ticker)
    thinking_cfg = _build_thinking_config(req)

    row = Run(
        id=str(run_id),
        ticker=req.ticker,
        asset_type=asset_type,
        analysis_date=req.analysis_date,
        analysts=list(req.analysts),
        research_depth=int(req.research_depth),
        llm_provider=req.llm_provider,
        quick_think_llm=req.quick_think_llm,
        deep_think_llm=req.deep_think_llm,
        thinking_config=thinking_cfg,
        output_language=req.output_language,
        checkpoint_enabled=bool(req.enable_checkpoint),
        status="queued",
    )
    db.add(row)
    await db.commit()

    # Spawn the task on the currently-running loop. We don't await it —
    # the HTTP response returns immediately with the run_id.
    task = asyncio.create_task(_run_async(run_id, req, asset_type))
    _run_tasks[run_id] = task
    # Avoid an "unawaited task" warning if the task errors before anyone
    # awaits it (e.g. in tests that never poll the run).
    task.add_done_callback(lambda t: _run_tasks.pop(run_id, None))
    return run_id


async def cancel_run(run_id: UUID) -> None:
    """Signal the in-flight run to stop at the next chunk boundary.

    No-op if the run is not active (e.g. already completed); the HTTP
    layer turns this into a 204. If you need to cancel a run that has
    not yet acquired the global lock, the task will see the event when
    it does acquire it and exit before ``stream_run`` even starts.
    """
    event = _cancel_events.get(run_id)
    if event is not None:
        event.set()


async def resume_run(parent_id: UUID, db: AsyncSession) -> UUID:
    """Create a NEW run that resumes from ``parent_id``'s checkpoint.

    Per the plan: only allowed when the parent's status is
    ``interrupted`` AND ``checkpoint_enabled=True``. Returns the new
    run_id. The new row mirrors the parent's request fields so the
    same ``thread_id`` (ticker + date hash) is reused by the engine's
    SqliteSaver, which is what makes resume actually work.
    """
    parent = await db.get(Run, str(parent_id))
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
        )
    if parent.status != "interrupted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot resume run in status {parent.status!r}",
        )
    if not parent.checkpoint_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run was not checkpointed; cannot resume",
        )

    # Re-derive a RunRequest from the persisted row. We trust the row
    # because it was validated on the original POST.
    thinking_cfg = parent.thinking_config or {}
    req = S.RunRequest(
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
        enable_checkpoint=True,
    )
    return await start_run(req, db)


# --------------------------------------------------------------------------- #
# Lifecycle implementation                                                    #
# --------------------------------------------------------------------------- #


async def _run_async(run_id: UUID, req: S.RunRequest, asset_type: str) -> None:
    """The lifecycle coroutine.

    Runs under ``GLOBAL_RUN_LOCK`` so the env-var injection in
    :mod:`env_inject` is safe (see its module docstring).
    """
    global _active_run_id

    cancel_event = asyncio.Event()
    _cancel_events[run_id] = cancel_event

    observer: Optional[WebRunObserver] = None
    final_state: Dict[str, Any] = {}
    failed_error: Optional[str] = None
    was_cancelled = False
    completed_ok = False

    async with _get_lock():
        _active_run_id = run_id
        try:
            # ---- decrypt API keys ------------------------------------ #
            api_keys = await _collect_api_keys(req.llm_provider)

            with env_inject.scope(api_keys):
                # ---- mark running + publish run_started -------------- #
                await _mark_running(run_id)

                observer = WebRunObserver(
                    run_id=run_id,
                    publish=partial(event_bus.publish, run_id),
                )
                observer.on_started(
                    ticker=req.ticker,
                    asset_type=asset_type,
                    analysis_date=req.analysis_date.isoformat(),
                    analysts=list(req.analysts),
                    research_depth=int(req.research_depth),
                    llm_provider=req.llm_provider,
                    quick_think_llm=req.quick_think_llm,
                    deep_think_llm=req.deep_think_llm,
                    output_language=req.output_language,
                    checkpoint_enabled=bool(req.enable_checkpoint),
                    thinking_config=_build_thinking_config(req),
                )

                # ---- engine loop ------------------------------------- #
                # Wall-clock safety net: a hung LLM call must not hold
                # GLOBAL_RUN_LOCK indefinitely. ``asyncio.wait_for``
                # raises ``TimeoutError`` when the deadline lapses. We
                # also set ``cancel_event`` so the engine's worker-thread
                # ``stream_run`` loop can stop cooperatively at the next
                # chunk boundary (the asyncio cancellation alone does
                # NOT interrupt the synchronous worker thread).
                try:
                    run_max = float(
                        os.environ.get(
                            "TRADINGAGENTS_RUN_MAX_SECONDS",
                            str(RUN_MAX_SECONDS_DEFAULT),
                        )
                    )
                except ValueError:
                    run_max = RUN_MAX_SECONDS_DEFAULT
                try:
                    final_state = await asyncio.wait_for(
                        _run_engine(req, asset_type, observer, cancel_event),
                        timeout=run_max,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "run_service.timeout",
                        extra={"run_id": str(run_id), "limit_seconds": run_max},
                    )
                    # Signal the engine to stop cleanly at the next chunk
                    # boundary even though wait_for has given up on it.
                    cancel_event.set()
                    failed_error = (
                        f"Run exceeded TRADINGAGENTS_RUN_MAX_SECONDS "
                        f"({int(run_max)}s) — upstream likely stuck. "
                        "See logs and consider switching models."
                    )
                except asyncio.CancelledError:
                    # External task.cancel() — translate to "user cancelled".
                    was_cancelled = True
                    raise
                except _CancelledByEvent:
                    was_cancelled = True
                except Exception as exc:  # noqa: BLE001 — surface any engine error
                    log.exception("run_service.engine_failed", extra={"run_id": str(run_id)})
                    failed_error = _format_engine_error(exc, req.llm_provider)
                else:
                    completed_ok = True

        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except Exception as exc:  # noqa: BLE001 — pre-engine setup error
            log.exception("run_service.setup_failed", extra={"run_id": str(run_id)})
            failed_error = _format_engine_error(exc, req.llm_provider)
        finally:
            # ---- terminal event + DB update + cleanup ---------------- #
            try:
                if completed_ok and observer is not None:
                    rating, report_dir = _finalize_completion(
                        req, asset_type, final_state
                    )
                    observer.set_completion_info(
                        rating=rating, report_dir=report_dir
                    )
                    observer.on_completed()
                    await _mark_completed(
                        run_id,
                        rating=rating,
                        report_dir=report_dir,
                        decision_full=final_state.get("final_trade_decision"),
                        stats=observer.stats() if observer else None,
                    )
                elif was_cancelled:
                    if observer is not None:
                        observer.on_cancelled()
                    await _mark_terminal(
                        run_id,
                        new_status="cancelled",
                        stats=observer.stats() if observer else None,
                    )
                else:
                    err = failed_error or "Unknown error"
                    if observer is not None:
                        observer.on_failed(err)
                    await _mark_terminal(
                        run_id,
                        new_status="failed",
                        error_message=err,
                        stats=observer.stats() if observer else None,
                    )

                # Drain any in-flight publishes before closing the bus.
                if observer is not None:
                    await observer.aclose()
            finally:
                event_bus.close(run_id)
                _cancel_events.pop(run_id, None)
                _active_run_id = None


class _CancelledByEvent(Exception):
    """Raised when ``stream_run`` observed the cancel_event and exited."""


async def _run_engine(
    req: S.RunRequest,
    asset_type: str,
    observer: WebRunObserver,
    cancel_event: asyncio.Event,
) -> Dict[str, Any]:
    """Build the graph + state, then drive ``stream_run`` on a worker thread.

    Returns the merged ``final_state`` dict so the caller can extract
    the rating and report_dir for the ``run_completed`` event.

    The FAKE_LLM env-var short-circuits to :func:`_fake_stream_run`,
    which emits a scripted event sequence without touching any LLM.
    """
    if os.environ.get("FAKE_LLM") == "1":
        # The fake stream runs on the event loop directly (it's cheap).
        return await _fake_stream_run(req, asset_type, observer, cancel_event)

    # Build the config dict — mirrors cli/main.py:864-876.
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    settings = get_settings()
    config: Dict[str, Any] = DEFAULT_CONFIG.copy()
    config["max_debate_rounds"] = int(req.research_depth)
    config["max_risk_discuss_rounds"] = int(req.research_depth)
    config["quick_think_llm"] = req.quick_think_llm
    config["deep_think_llm"] = req.deep_think_llm
    config["llm_provider"] = req.llm_provider.lower()
    config["backend_url"] = _provider_backend_url(req.llm_provider)
    config["google_thinking_level"] = req.google_thinking_level
    config["openai_reasoning_effort"] = req.openai_reasoning_effort
    config["anthropic_effort"] = req.anthropic_effort
    config["output_language"] = req.output_language
    config["checkpoint_enabled"] = bool(req.enable_checkpoint)
    # Reports land under <data_dir>/logs/<safe_ticker>/<date>/reports —
    # matches the CLI layout and respects the ticker-sanitisation rule.
    config["results_dir"] = str(settings.data_dir / "logs")

    analyst_keys = list(req.analysts)
    analyst_plan = build_analyst_execution_plan(analyst_keys)
    wall_time_tracker = AnalystWallTimeTracker(analyst_plan)
    if analyst_keys:
        wall_time_tracker.mark_started(analyst_keys[0])

    stats_handler = StatsCallbackHandler()

    graph = TradingAgentsGraph(
        analyst_keys,
        config=config,
        debug=False,
        callbacks=[stats_handler],
        observer=observer,
    )

    # Canonical init_state — see cli/main.py:984-988.
    init_state = graph.propagator.create_initial_state(
        req.ticker,
        req.analysis_date.isoformat(),
        asset_type=asset_type,
    )
    args = graph.propagator.get_graph_args(callbacks=[stats_handler])

    def _sync_loop() -> Dict[str, Any]:
        return stream_run(
            graph,
            init_state,
            args,
            observer,
            selected_analysts=analyst_keys,
            cancel_event=cancel_event,
            wall_time_tracker=wall_time_tracker,
        )

    final_state = await asyncio.to_thread(_sync_loop)

    for key, seconds in wall_time_tracker.get_wall_times().items():
        agent_name = ANALYST_AGENT_NAMES.get(key, key.title())
        observer.on_analyst_wall_time(key, agent_name, seconds)
    observer.ingest_callback_stats(stats_handler.get_stats())
    if cancel_event.is_set():
        raise _CancelledByEvent()
    return final_state


# --------------------------------------------------------------------------- #
# FAKE_LLM end-to-end smoke hook                                              #
# --------------------------------------------------------------------------- #


async def _fake_stream_run(
    req: S.RunRequest,
    asset_type: str,
    observer: WebRunObserver,
    cancel_event: asyncio.Event,
) -> Dict[str, Any]:
    """Scripted observer-driven sequence used by the smoke test.

    Emits roughly:
    - 2 agent_status flips (pending -> in_progress -> completed) for the
      first two analysts in ``req.analysts``.
    - 2 messages
    - 1 report_section per analyst
    - returns a stub final_state with a ``final_trade_decision`` carrying
      "Rating: Buy" so the rating extraction at the caller picks Buy.

    Total wall-time roughly 0.2s — long enough that a concurrent cancel
    has a real window to trigger, short enough for tests to be fast.
    """
    # Slow enough to make the cancel test reliable, fast enough to keep
    # the smoke test quick. Honour cancel between every step.
    async def _pause(seconds: float) -> bool:
        """Returns True if cancelled during the wait."""
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return cancel_event.is_set()

    analyst_keys = list(req.analysts) or ["market"]
    from tradingagents.run_observer import ANALYST_AGENT_NAMES, ANALYST_REPORT_MAP

    total_steps = len(analyst_keys[:2]) + 1  # analysts + PM
    observer.emit_progress(0.0, "Starting")

    for i, key in enumerate(analyst_keys[:2]):
        if cancel_event.is_set():
            raise _CancelledByEvent()
        agent_name = ANALYST_AGENT_NAMES.get(key, key.title())
        observer.on_agent_status(agent_name, "in_progress")
        observer.emit_progress((i + 0.5) / total_steps, agent_name)
        if await _pause(0.05):
            raise _CancelledByEvent()
        observer.on_message(
            "Agent", f"[FAKE] {agent_name} draft for {req.ticker}",
            datetime.now(timezone.utc).strftime("%H:%M:%S"),
        )
        if await _pause(0.05):
            raise _CancelledByEvent()
        report_key = ANALYST_REPORT_MAP.get(key, f"{key}_report")
        observer.on_report_section(
            report_key, f"# {agent_name}\n\nFake analysis for {req.ticker}."
        )
        observer.on_agent_status(agent_name, "completed")
        observer.emit_progress((i + 1) / total_steps, agent_name)
        if await _pause(0.02):
            raise _CancelledByEvent()

    if cancel_event.is_set():
        raise _CancelledByEvent()

    observer.emit_progress(1.0, "Portfolio Manager")
    decision = (
        f"Rating: Buy\n\nFake portfolio manager decision for {req.ticker} "
        f"on {req.analysis_date.isoformat()}."
    )
    observer.on_report_section("final_trade_decision", decision)
    return {"final_trade_decision": decision}


# --------------------------------------------------------------------------- #
# DB helpers                                                                  #
# --------------------------------------------------------------------------- #


async def _collect_api_keys(provider: str) -> Dict[str, str]:
    """Decrypt the rows in ``api_keys`` whose env-var is needed for ``provider``.

    Ollama (and any other provider mapped to ``None`` in
    ``PROVIDER_API_KEY_ENV``) returns an empty dict — no key required.
    Missing rows are silently skipped; the engine itself will raise a
    clear error if the env var isn't set when an LLM call fires.
    """
    env_var = PROVIDER_API_KEY_ENV.get(provider.lower())
    if not env_var:
        return {}

    factory = _db.get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ApiKey).where(ApiKey.provider_env == env_var)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return {}
        try:
            plaintext = decrypt(row.encrypted_value)
        except (FernetNotConfiguredError, InvalidToken):
            log.warning(
                "run_service.api_key_decrypt_failed",
                extra={"provider_env": env_var},
            )
            return {}
        return {env_var: plaintext}


async def _mark_running(run_id: UUID) -> None:
    """Flip status to ``running`` and set ``started_at``."""
    factory = _db.get_session_factory()
    async with factory() as session:
        row = await session.get(Run, str(run_id))
        if row is None:
            return
        row.status = "running"
        row.started_at = datetime.now(timezone.utc)
        await session.commit()


async def _mark_completed(
    run_id: UUID,
    *,
    rating: S.Rating,
    report_dir: str,
    decision_full: Optional[str],
    stats: Optional[Dict[str, Any]],
) -> None:
    factory = _db.get_session_factory()
    async with factory() as session:
        row = await session.get(Run, str(run_id))
        if row is None:
            return
        row.status = "completed"
        row.rating = rating
        row.report_dir = report_dir
        row.decision_full = decision_full
        row.finished_at = datetime.now(timezone.utc)
        if stats is not None:
            row.stats = stats
        await session.commit()


async def _mark_terminal(
    run_id: UUID,
    *,
    new_status: str,
    error_message: Optional[str] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> None:
    factory = _db.get_session_factory()
    async with factory() as session:
        row = await session.get(Run, str(run_id))
        if row is None:
            return
        row.status = new_status
        row.finished_at = datetime.now(timezone.utc)
        if error_message is not None:
            row.error_message = error_message
        if stats is not None:
            row.stats = stats
        await session.commit()


# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


def _detect_asset_type(ticker: str) -> str:
    """Forward to the shared asset-type detector; returns the wire string.

    We use the wire string (``"stock"`` / ``"crypto"``) rather than the
    enum so the value drops straight into ``Run.asset_type`` and into
    the Pydantic ``AssetType`` literal without translation.
    """
    from tradingagents.asset_types import detect_asset_type

    return detect_asset_type(ticker).value


def _provider_backend_url(provider: str) -> Optional[str]:
    """Look up the canonical base URL for ``provider`` in ``PROVIDERS``.

    Returns ``None`` if the provider isn't in the catalog (the LLM
    client will fall back to its own default). For ``ollama`` we let
    the client resolve ``OLLAMA_BASE_URL`` at call time.
    """
    from tradingagents.providers import PROVIDERS

    p = provider.lower()
    for spec in PROVIDERS:
        if spec.key == p:
            return spec.default_base_url
    return None


def _build_thinking_config(req: S.RunRequest) -> Optional[Dict[str, Any]]:
    """Project the three provider-specific knobs into a JSON-safe dict.

    Returns ``None`` (so the DB column stays NULL) when none of the
    knobs are set, keeping the column tidy for non-reasoning providers.
    """
    cfg: Dict[str, Any] = {}
    if req.google_thinking_level is not None:
        cfg["google_thinking_level"] = req.google_thinking_level
    if req.openai_reasoning_effort is not None:
        cfg["openai_reasoning_effort"] = req.openai_reasoning_effort
    if req.anthropic_effort is not None:
        cfg["anthropic_effort"] = req.anthropic_effort
    return cfg or None


def _finalize_completion(
    req: S.RunRequest, asset_type: str, final_state: Dict[str, Any]
) -> tuple[S.Rating, str]:
    """Extract the rating + materialise a report_dir for the completed run.

    The rating comes from the Portfolio Manager's ``final_trade_decision``
    via :func:`parse_rating`; the report_dir is
    ``<data_dir>/logs/<safe_ticker>/<date>/reports`` — same shape as
    the CLI uses.
    """
    settings = get_settings()
    decision_text = str(final_state.get("final_trade_decision") or "")
    rating = parse_rating(decision_text, default="Hold")
    # parse_rating returns a Title-cased string; coerce to the Rating
    # literal (it'll be one of the five valid values).
    rating_typed: S.Rating = rating  # type: ignore[assignment]

    # CRITICAL security note (CLAUDE.md): tickers MUST be sanitised
    # before reaching a filesystem path. The DB column keeps the raw
    # user input; only path interpolation goes through this helper.
    safe_ticker = safe_ticker_component(req.ticker)
    report_dir = (
        settings.data_dir
        / "logs"
        / safe_ticker
        / req.analysis_date.isoformat()
        / "reports"
    )
    # Materialise the directory + write the final decision to disk so
    # the report-download endpoint can serve it. Best-effort — disk
    # errors here MUST NOT fail the run (it already succeeded).
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        if decision_text:
            (report_dir / "report.md").write_text(decision_text, encoding="utf-8")
    except OSError:
        log.exception(
            "run_service.report_dir_write_failed",
            extra={"report_dir": str(report_dir)},
        )

    return rating_typed, str(report_dir)


# --------------------------------------------------------------------------- #
# Test helpers                                                                #
# --------------------------------------------------------------------------- #


def reset_for_tests() -> None:
    """Wipe module-level state — for use by test fixtures only.

    Drops the cached ``GLOBAL_RUN_LOCK`` so the next call to ``_get_lock``
    binds a fresh asyncio.Lock to the current event loop.
    """
    global _active_run_id, GLOBAL_RUN_LOCK
    _active_run_id = None
    GLOBAL_RUN_LOCK = None
    _cancel_events.clear()
    _run_tasks.clear()


__all__ = [
    "GLOBAL_RUN_LOCK",
    "cancel_run",
    "get_active_run_id",
    "reset_for_tests",
    "resume_run",
    "start_run",
]
