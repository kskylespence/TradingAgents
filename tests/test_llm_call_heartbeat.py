"""Tests for the in-run LLM-call heartbeat (Layer 4 resilience).

A real run on 2026-05-22 succeeded on the first 7 LLM calls then hung for
~37 minutes on the 8th. Layer 1's pre-flight probe can't see this — the
model was healthy when the run started, then went unhealthy mid-run. The
heartbeat wrapper emits ``llm_call_pending`` SSE events at 30/60/90s while
a single LLM call is outstanding so the operator sees "this call is taking
too long" early enough to hit Cancel rather than discovering it 30+ minutes
later when the timeout envelope finally expires.

Test surface (see plan: "Layer 4 — in-run heartbeat"):
- Calls under 30s emit nothing.
- Calls between 30s and 60s emit 1 heartbeat.
- Calls between 60s and 90s emit 2 heartbeats.
- Calls past 90s flip ``soft_warning`` true on subsequent heartbeats so the
  frontend can style the row differently.
- Cancellation propagates cleanly — no orphaned LLM task, no exceptions
  leak past the wrapper.
- When no observer is attached, behavior is identical to the bare
  ``ChatOpenAI.ainvoke`` — back-compat for non-web call sites (CLI).
- The event payload carries ``model`` and ``agent`` so the UI can render
  "Fundamentals Analyst – kimi-k2-thinking (60s elapsed)".

We mock ``ChatOpenAI.ainvoke`` rather than going over the wire — the
heartbeat wrapper is pure orchestration and the test is about timing,
not HTTP. ``asyncio.sleep`` is real because the wait_for/shield interplay
depends on real loop scheduling; tests use small intervals (1s) and a
small ``HEARTBEAT_INTERVAL`` monkeypatch where appropriate to keep the
suite fast.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any
from unittest.mock import patch

import pytest

import tradingagents.llm_clients.openai_client as openai_client_mod


def _reload_client():
    return importlib.reload(openai_client_mod)


class _FakeObserver:
    """Minimal stand-in for ``WebRunObserver`` for the heartbeat tests.

    Only ``emit_llm_call_pending`` is exercised. The wrapper must call
    this and pass a dict with ``model``, ``agent``, ``elapsed_seconds``,
    and ``soft_warning``. The bare-name attribute mirrors what the
    real observer exposes (see web_run_observer.py:emit_llm_call_pending).
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit_llm_call_pending(self, payload: dict[str, Any]) -> None:
        self.events.append(dict(payload))


@pytest.fixture
def fast_heartbeat(monkeypatch):
    """Compress the heartbeat interval to 1s so tests finish in seconds.

    The production constant is 30s; using it directly would push test
    runtime into minutes. Patch it on the wrapper module so each test
    can scale ``call_duration`` proportionally.
    """
    mod = _reload_client()
    monkeypatch.setattr(mod, "HEARTBEAT_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(mod, "HEARTBEAT_SOFT_WARNING_AFTER", 3.0)
    return mod


def _build_chat(mod, **kwargs):
    """Construct a ``NormalizedChatOpenAI`` for testing without hitting the network.

    ``ChatOpenAI`` requires an api key + model name. We pass a placeholder
    so initialization succeeds and immediately mock ``ainvoke`` to control
    timing.
    """
    return mod.NormalizedChatOpenAI(
        model="kimi-k2-thinking",
        api_key="placeholder",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_call_completes_in_5s_emits_no_heartbeat(fast_heartbeat):
    """Fast calls — anything finishing under one heartbeat interval — emit no events.

    With ``HEARTBEAT_INTERVAL_SECONDS=1`` (fast_heartbeat fixture) and a
    0.3s mock call we're comfortably under the first heartbeat boundary.
    """
    mod = fast_heartbeat
    llm = _build_chat(mod)
    observer = _FakeObserver()
    llm.set_observer(observer)
    llm.set_agent_hint("Market Analyst")

    async def fast_call(*args, **kwargs):
        await asyncio.sleep(0.3)
        return "result"

    with patch.object(
        mod.NormalizedChatOpenAI.__mro__[1], "ainvoke", new=fast_call
    ):
        result = await llm.ainvoke([("user", "hi")])

    assert result == "result"
    assert observer.events == [], (
        f"Expected no heartbeats for a 0.3s call, got {observer.events}"
    )


@pytest.mark.asyncio
async def test_call_takes_65s_emits_2_heartbeats(fast_heartbeat):
    """A call spanning 2 heartbeat intervals (~2.2s with the fixture) emits 2 events.

    Each event carries the cumulative ``elapsed_seconds`` so the UI can
    render the running total. With HEARTBEAT_INTERVAL_SECONDS=1, the
    elapsed values should land at 1 and 2 seconds.
    """
    mod = fast_heartbeat
    llm = _build_chat(mod)
    observer = _FakeObserver()
    llm.set_observer(observer)
    llm.set_agent_hint("Fundamentals Analyst")

    async def slow_call(*args, **kwargs):
        await asyncio.sleep(2.2)
        return "slow"

    with patch.object(
        mod.NormalizedChatOpenAI.__mro__[1], "ainvoke", new=slow_call
    ):
        result = await llm.ainvoke([("user", "hi")])

    assert result == "slow"
    assert len(observer.events) == 2, (
        f"Expected 2 heartbeats for ~2.2s call at 1s interval, got "
        f"{len(observer.events)} events: {observer.events}"
    )
    assert observer.events[0]["elapsed_seconds"] == pytest.approx(1.0, abs=0.05)
    assert observer.events[1]["elapsed_seconds"] == pytest.approx(2.0, abs=0.05)
    # Neither event has crossed the soft-warning threshold (3s).
    assert observer.events[0]["soft_warning"] is False
    assert observer.events[1]["soft_warning"] is False


@pytest.mark.asyncio
async def test_call_takes_95s_emits_3_heartbeats_with_soft_warning(fast_heartbeat):
    """A call past the soft-warning threshold flips the flag on the 3rd event.

    With the fixture: HEARTBEAT_INTERVAL_SECONDS=1, soft warning at 3s.
    A 3.3s call should produce events at 1s, 2s, 3s — the 3rd should
    carry ``soft_warning=True`` so the frontend can render it amber.
    """
    mod = fast_heartbeat
    llm = _build_chat(mod)
    observer = _FakeObserver()
    llm.set_observer(observer)
    llm.set_agent_hint("Bull Researcher")

    async def very_slow_call(*args, **kwargs):
        await asyncio.sleep(3.3)
        return "very slow"

    with patch.object(
        mod.NormalizedChatOpenAI.__mro__[1], "ainvoke", new=very_slow_call
    ):
        result = await llm.ainvoke([("user", "hi")])

    assert result == "very slow"
    assert len(observer.events) == 3
    assert observer.events[0]["soft_warning"] is False
    assert observer.events[1]["soft_warning"] is False
    # 3rd event hits elapsed=3.0s which is >= 3.0s threshold.
    assert observer.events[2]["soft_warning"] is True
    assert observer.events[2]["elapsed_seconds"] == pytest.approx(3.0, abs=0.05)


@pytest.mark.asyncio
async def test_cancellation_during_heartbeat(fast_heartbeat):
    """asyncio.CancelledError on the wrapper must NOT orphan the underlying call.

    Real-world: user clicks Cancel while a heartbeat is in flight. We must
    cancel the inner ``ainvoke`` task too — otherwise the LLM call keeps
    burning provider quota in the background. We also must not swallow the
    CancelledError; the engine loop relies on it propagating to unwind.
    """
    mod = fast_heartbeat
    llm = _build_chat(mod)
    observer = _FakeObserver()
    llm.set_observer(observer)

    cancelled_inner = False
    inner_completed = False

    async def slow_call(*args, **kwargs):
        nonlocal cancelled_inner, inner_completed
        try:
            await asyncio.sleep(10)
            inner_completed = True
            return "should not happen"
        except asyncio.CancelledError:
            cancelled_inner = True
            raise

    with patch.object(
        mod.NormalizedChatOpenAI.__mro__[1], "ainvoke", new=slow_call
    ):
        task = asyncio.create_task(llm.ainvoke([("user", "hi")]))
        # Let one heartbeat fire so we know the wrapper is mid-loop.
        await asyncio.sleep(1.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert cancelled_inner, "wrapper must cancel the inner ainvoke task on cancellation"
    assert not inner_completed, "inner call should NOT have completed normally"


@pytest.mark.asyncio
async def test_no_observer_no_heartbeat(fast_heartbeat):
    """Back-compat: with no observer attached, ainvoke is a plain pass-through.

    Non-web call sites (CLI runs, scripts, tests) never construct a
    WebRunObserver. The wrapper must be invisible to them — no scheduled
    task, no overhead, no behavior change. We assert by patching ainvoke
    with a sentinel and watching it return verbatim.
    """
    mod = fast_heartbeat
    llm = _build_chat(mod)
    # No set_observer call — _observer stays None.

    async def fast(*args, **kwargs):
        await asyncio.sleep(2.5)
        return "raw"

    with patch.object(
        mod.NormalizedChatOpenAI.__mro__[1], "ainvoke", new=fast
    ):
        result = await llm.ainvoke([("user", "hi")])

    assert result == "raw"
    # The call took 2.5s, which would emit 2 heartbeats if the wrapper
    # were active. With no observer, no events should fire — but we
    # have no observer to check, so the assertion is that no exception
    # is raised and the return value is verbatim.


@pytest.mark.asyncio
async def test_heartbeat_includes_model_and_agent_hint(fast_heartbeat):
    """Each heartbeat payload carries enough context for the UI to render meaningfully.

    The frontend renders "Fundamentals Analyst – kimi-k2-thinking
    (60s elapsed)" — that needs ``model``, ``agent``, ``elapsed_seconds``.
    Heartbeats without agent context still emit (``agent`` defaults to
    a placeholder) so debaters / risk team calls don't break the stream.
    """
    mod = fast_heartbeat
    llm = _build_chat(mod)
    observer = _FakeObserver()
    llm.set_observer(observer)
    llm.set_agent_hint("Fundamentals Analyst")

    async def slow_call(*args, **kwargs):
        await asyncio.sleep(1.3)
        return "x"

    with patch.object(
        mod.NormalizedChatOpenAI.__mro__[1], "ainvoke", new=slow_call
    ):
        await llm.ainvoke([("user", "hi")])

    assert len(observer.events) == 1
    ev = observer.events[0]
    assert ev["model"] == "kimi-k2-thinking"
    assert ev["agent"] == "Fundamentals Analyst"
    assert ev["elapsed_seconds"] == pytest.approx(1.0, abs=0.05)


@pytest.mark.asyncio
async def test_heartbeat_default_agent_when_hint_missing(fast_heartbeat):
    """When agent_hint isn't set the wrapper still emits with a safe default.

    Calls outside the analyst pipeline (e.g. reflection, signal processing)
    don't set agent_hint. The heartbeat should still fire so the operator
    can see "something is taking too long" even when we can't name it.
    """
    mod = fast_heartbeat
    llm = _build_chat(mod)
    observer = _FakeObserver()
    llm.set_observer(observer)
    # No set_agent_hint call.

    async def slow_call(*args, **kwargs):
        await asyncio.sleep(1.3)
        return "x"

    with patch.object(
        mod.NormalizedChatOpenAI.__mro__[1], "ainvoke", new=slow_call
    ):
        await llm.ainvoke([("user", "hi")])

    assert len(observer.events) == 1
    # The placeholder string is what the frontend will render — must be
    # human-readable, not "None" or empty.
    assert observer.events[0]["agent"] not in (None, "", "None")


@pytest.mark.asyncio
async def test_underlying_exception_propagates(fast_heartbeat):
    """When ainvoke raises mid-call, the wrapper re-raises after cleanup.

    The heartbeat loop must not swallow real errors (auth failures, network
    drops, etc.). The engine-loop retry policy can only react if it sees
    the exception.
    """
    mod = fast_heartbeat
    llm = _build_chat(mod)
    observer = _FakeObserver()
    llm.set_observer(observer)

    class _ProviderError(Exception):
        pass

    async def raises(*args, **kwargs):
        await asyncio.sleep(0.3)
        raise _ProviderError("upstream 500")

    with patch.object(
        mod.NormalizedChatOpenAI.__mro__[1], "ainvoke", new=raises
    ), pytest.raises(_ProviderError, match="upstream 500"):
        await llm.ainvoke([("user", "hi")])
