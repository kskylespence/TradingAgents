"""WebRunObserver — bridge from the engine's ``RunObserver`` callbacks to
the event bus.

The engine (`tradingagents.run_observer.stream_run`) drives a
LangGraph stream synchronously and calls the observer callbacks
inline. In production we run that stream inside
``asyncio.to_thread(stream_run, ...)`` so the callbacks fire from a
worker thread. The observer must therefore translate every sync
callback into an async ``publish(payload)`` call that lands on the
main asyncio loop.

Sync-to-async bridge (chosen approach: ``run_coroutine_threadsafe``)
-------------------------------------------------------------------
At construction time we capture the running loop with
``asyncio.get_running_loop()``. Every callback builds the Pydantic
event, dumps it to a JSON-safe dict, and schedules the publish via
``loop.call_soon_threadsafe`` (when called from a worker thread) or
``loop.create_task`` (when called inline on the loop). Both paths
schedule the publish coroutine onto the same loop the bus is using,
so we don't need a dedicated worker, a queue, or a second loop.

We track the in-flight publish coroutines in ``self._pending`` so
``aclose()`` can await every published event before returning — this
gives the run-service team a clean point to drain on completion. We
do NOT call ``shield`` or ``wait_for`` here: the bus is responsible
for its own backpressure/timeout.

Stats
-----
``stats()`` returns a dict with the four counters + the per-analyst
wall-times dict that the runs service persists into the JSONB
``runs.stats`` column. The dict is shaped to feed directly into
``app.schemas.RunStats(**observer.stats())``.

Wire-key vs label gotcha (CLAUDE.md)
------------------------------------
For ``on_agent_status`` and ``on_analyst_wall_time``, the engine
passes whatever it knows: the wire key (``"social"``) for analyst
keys, and the display label (``"Sentiment Analyst"``) for agent
names. We forward both verbatim — no translation. The frontend
consumes ``AgentStatusEvent.agent`` as the display label and
``AnalystWallTimeEvent.key`` as the wire key.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

from pydantic import ValidationError

from app import schemas as S
from tradingagents.run_observer import RunObserver

logger = logging.getLogger(__name__)


_VALID_MESSAGE_KINDS: frozenset[str] = frozenset(
    ("User", "Agent", "Data", "Control", "System")
)

# When ``on_started`` is called without the extended kwargs (the base
# class only requires ticker/asset_type/analysis_date/analysts), we
# fill these defaults so the Pydantic event still validates. The
# run-service is expected to pass the full request kwargs in practice.
_RUN_STARTED_DEFAULTS: dict[str, Any] = {
    "research_depth": 1,
    "llm_provider": "openai",
    "quick_think_llm": "",
    "deep_think_llm": "",
    "output_language": "English",
    "checkpoint_enabled": True,
    "thinking_config": None,
}


class WebRunObserver(RunObserver):
    """RunObserver subclass that ships every callback to the event bus.

    Parameters
    ----------
    run_id:
        The UUID of the row in ``runs``. Carried for logging context;
        the ``publish`` callable already has the run_id baked in.
    publish:
        Async callable that accepts the JSON-safe event dict and
        returns the server-assigned ``seq``. In production this is
        ``functools.partial(event_bus.publish, run_id)`` — the
        runs-service wires it up. Tests pass a mock.
    """

    def __init__(
        self,
        run_id: UUID,
        publish: Callable[[dict[str, Any]], Awaitable[int]],
    ) -> None:
        self._run_id = run_id
        self._publish = publish

        # Capture the loop now so worker-thread callbacks can schedule
        # work back onto it. If we're constructed off-loop the caller
        # needs to pass the loop in explicitly — but in practice
        # WebRunObserver is built inside an async route handler.
        try:
            self._loop: Optional[asyncio.AbstractEventLoop] = (
                asyncio.get_running_loop()
            )
        except RuntimeError:  # pragma: no cover - constructed off-loop
            self._loop = None

        # Track every scheduled publish so ``aclose`` can drain them.
        self._pending: list[asyncio.Future[int]] = []
        self._pending_lock = threading.Lock()

        # Counters for ``stats()``. Lock for cross-thread mutation.
        self._stats_lock = threading.Lock()
        self._llm_calls = 0
        self._tool_calls = 0
        self._tokens_in = 0
        self._tokens_out = 0
        self._analyst_wall_times: dict[str, float] = {}
        self._started_at = time.monotonic()

        # Set via ``set_completion_info`` before ``on_completed``.
        self._completion_rating: Optional[S.Rating] = None
        self._completion_report_dir: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Public helpers                                                     #
    # ------------------------------------------------------------------ #

    def stats(self) -> dict[str, Any]:
        """Return the accumulated stats; shaped for ``RunStats(**stats)``."""
        with self._stats_lock:
            elapsed = max(0.0, time.monotonic() - self._started_at)
            return {
                "llm_calls": self._llm_calls,
                "tool_calls": self._tool_calls,
                "tokens_in": self._tokens_in,
                "tokens_out": self._tokens_out,
                "elapsed_seconds": elapsed,
                "analyst_wall_times": dict(self._analyst_wall_times),
            }

    def set_completion_info(
        self,
        *,
        rating: Optional[S.Rating] = None,
        report_dir: Optional[str] = None,
    ) -> None:
        """Tell the observer what to emit when ``on_completed`` fires.

        The base ``RunObserver.on_completed`` takes no arguments, but
        ``RunCompletedEvent`` needs ``rating`` and ``report_dir``. The
        run-service computes these from the merged ``final_state`` and
        calls this before the final callback.
        """
        if rating is not None:
            self._completion_rating = rating
        if report_dir is not None:
            self._completion_report_dir = report_dir

    def record_tokens(self, *, tokens_in: int = 0, tokens_out: int = 0) -> None:
        """Manual token accounting hook for the run-service to call when
        it tallies usage from the LLM provider response (the observer
        callbacks don't receive token counts).
        """
        with self._stats_lock:
            self._tokens_in += int(tokens_in)
            self._tokens_out += int(tokens_out)

    def emit_progress(self, progress: float, step: str) -> None:
        """Emit a ``ProgressUpdateEvent`` (0..1 with a step label).

        The engine's ``RunObserver`` base class doesn't include progress as a
        callback — the plan derives progress from agent_status. This method
        is a manual hook the run-service can call (or the FAKE_LLM stream)
        so the frontend's ``progress_update`` reducer branch is exercised
        without forcing the engine to compute a percentage.
        """
        payload = self._build(
            S.ProgressUpdateEvent,
            progress=max(0.0, min(1.0, float(progress))),
            step=step,
        )
        self._schedule(payload)

    async def aclose(self) -> None:
        """Wait for every scheduled publish to finish.

        Call this from the run-service after the engine stream returns
        so SSE clients see every event before the connection closes.
        Safe to call multiple times.
        """
        with self._pending_lock:
            pending = list(self._pending)
            self._pending.clear()
        if not pending:
            return
        # gather() raises if any future raised; we want to surface
        # publish failures (the bus is supposed to be best-effort).
        await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # RunObserver overrides                                              #
    # ------------------------------------------------------------------ #

    def on_started(
        self,
        ticker: str,
        asset_type: str,
        analysis_date: str,
        analysts: list[str],
        **kwargs: Any,
    ) -> None:
        payload = self._build(
            S.RunStartedEvent,
            ticker=ticker,
            asset_type=asset_type,
            analysis_date=analysis_date,
            analysts=analysts,
            research_depth=kwargs.get(
                "research_depth", _RUN_STARTED_DEFAULTS["research_depth"]
            ),
            llm_provider=kwargs.get(
                "llm_provider", _RUN_STARTED_DEFAULTS["llm_provider"]
            ),
            quick_think_llm=kwargs.get(
                "quick_think_llm", _RUN_STARTED_DEFAULTS["quick_think_llm"]
            ),
            deep_think_llm=kwargs.get(
                "deep_think_llm", _RUN_STARTED_DEFAULTS["deep_think_llm"]
            ),
            output_language=kwargs.get(
                "output_language", _RUN_STARTED_DEFAULTS["output_language"]
            ),
            checkpoint_enabled=kwargs.get(
                "checkpoint_enabled", _RUN_STARTED_DEFAULTS["checkpoint_enabled"]
            ),
            thinking_config=kwargs.get("thinking_config"),
        )
        self._schedule(payload)

    def on_agent_status(self, agent: str, status: str) -> None:
        payload = self._build(S.AgentStatusEvent, agent=agent, status=status)
        self._schedule(payload)

    def on_message(self, msg_type: str, content: str, timestamp: str) -> None:
        kind = msg_type if msg_type in _VALID_MESSAGE_KINDS else "System"
        with self._stats_lock:
            if kind == "Agent":
                # Each AI-produced message is a successful LLM call.
                self._llm_calls += 1
        payload = self._build(
            S.MessageEvent, kind=kind, content=content, timestamp=timestamp
        )
        self._schedule(payload)

    def on_tool_call(self, tool_name: str, args: Any, timestamp: str) -> None:
        with self._stats_lock:
            self._tool_calls += 1
        # ``ToolCallEvent.args`` is typed ``dict[str, Any]``. Coerce
        # non-dict args so we don't blow up validation when the engine
        # passes a string/list (old tool-call shapes).
        if isinstance(args, dict):
            coerced: dict[str, Any] = args
        else:
            coerced = {"value": args}
        payload = self._build(
            S.ToolCallEvent, name=tool_name, args=coerced, timestamp=timestamp
        )
        self._schedule(payload)

    def on_report_section(self, section: str, content: str) -> None:
        payload = self._build(
            S.ReportSectionEvent, section=section, content=content
        )
        self._schedule(payload)

    def on_investment_debate(
        self,
        bull: Optional[str],
        bear: Optional[str],
        judge: Optional[str],
    ) -> None:
        payload = self._build(
            S.InvestmentDebateEvent, bull=bull, bear=bear, judge=judge
        )
        self._schedule(payload)

    def on_risk_debate(
        self,
        aggressive: Optional[str],
        conservative: Optional[str],
        neutral: Optional[str],
        judge: Optional[str],
    ) -> None:
        payload = self._build(
            S.RiskDebateEvent,
            aggressive=aggressive,
            conservative=conservative,
            neutral=neutral,
            judge=judge,
        )
        self._schedule(payload)

    def on_analyst_wall_time(
        self, analyst_key: str, agent_name: str, seconds: float
    ) -> None:
        # Wire key — DO NOT translate. The frontend reads `key` as the
        # ``AnalystKey`` literal (market/social/news/fundamentals).
        with self._stats_lock:
            self._analyst_wall_times[analyst_key] = float(seconds)
        payload = self._build(
            S.AnalystWallTimeEvent,
            key=analyst_key,
            label=agent_name,
            seconds=float(seconds),
        )
        self._schedule(payload)

    def on_completed(self) -> None:
        # ``RunCompletedEvent`` requires rating + report_dir + finished_at.
        # If the run-service didn't pre-fill them, fall back to safe
        # defaults rather than crash — the runs-service should always
        # call ``set_completion_info`` first.
        rating: S.Rating = self._completion_rating or "Hold"
        report_dir = self._completion_report_dir or ""
        payload = self._build(
            S.RunCompletedEvent,
            rating=rating,
            report_dir=report_dir,
            finished_at=datetime.now(timezone.utc),
        )
        self._schedule(payload)

    def on_failed(self, error: str) -> None:
        payload = self._build(S.RunFailedEvent, error=error)
        self._schedule(payload)

    def on_cancelled(self) -> None:
        payload = self._build(S.RunCancelledEvent)
        self._schedule(payload)

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _build(self, event_cls: type[S._RunEventBase], **fields: Any) -> dict[str, Any]:
        """Construct a Pydantic event and dump to a JSON-safe dict.

        ``seq=0`` is a placeholder — the real bus assigns the seq from
        the BIGINT identity column. We keep the field so the Pydantic
        model validates locally without an Optional override.
        """
        try:
            event = event_cls(seq=0, **fields)
        except ValidationError:
            logger.exception(
                "WebRunObserver failed to build %s for run %s",
                event_cls.__name__,
                self._run_id,
            )
            raise
        return event.model_dump(mode="json")

    def _schedule(self, payload: dict[str, Any]) -> None:
        """Schedule the async publish onto the captured loop.

        - If we're already on the loop's thread, ``create_task`` is
          fine and runs eagerly on the next iteration.
        - If we're on a worker thread (the common case during a real
          run), ``run_coroutine_threadsafe`` is the safe way to hand
          the coroutine off without touching loop internals.
        """
        loop = self._loop
        if loop is None:
            # No loop captured — best-effort: try to grab the running
            # one now. If we're sync and off-loop, drop the event with
            # a warning rather than crashing the engine.
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                logger.warning(
                    "WebRunObserver._schedule: no event loop; dropping event %s",
                    payload.get("type"),
                )
                return

        coro = self._publish_safely(payload)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            # Same-thread, on-loop: create a task directly so the
            # publish runs at the next iteration without crossing
            # threads.
            future: asyncio.Future[int] = loop.create_task(coro)
        else:
            # Cross-thread (the engine's worker thread): hand off via
            # threadsafe — returns a concurrent.futures.Future, which
            # we wrap so ``aclose`` can await it.
            cf = asyncio.run_coroutine_threadsafe(coro, loop)
            future = asyncio.wrap_future(cf, loop=loop)

        with self._pending_lock:
            self._pending.append(future)

    async def _publish_safely(self, payload: dict[str, Any]) -> int:
        """Wrap the publish so a failure logs instead of leaking up."""
        try:
            return await self._publish(payload)
        except Exception:  # noqa: BLE001 — best-effort fan-out
            logger.exception(
                "WebRunObserver: publish failed for event %s run %s",
                payload.get("type"),
                self._run_id,
            )
            return 0


__all__ = ["WebRunObserver"]
