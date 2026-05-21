"""Tests for ``app.observers.web_run_observer.WebRunObserver``.

The observer is the bridge from ``tradingagents.run_observer.RunObserver``
callbacks (which are synchronous and may be called from worker threads)
to the async event bus. Tests use a ``MockBus`` so we don't depend on
the EVENT_BUS team's exact ``publish`` signature.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from app import schemas as S
from app.observers.web_run_observer import WebRunObserver


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


class MockBus:
    """Records every payload published. Async by design — matches the real
    event_bus.publish signature: ``async publish(payload) -> int``.
    """

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self._next_seq = 1

    async def publish(self, payload: dict[str, Any]) -> int:
        # The real event_bus assigns seq from the DB; we mimic that here.
        seq = self._next_seq
        self._next_seq += 1
        # Stamp the payload with a server-assigned seq exactly like the bus
        # would (the observer may pass a placeholder seq=0).
        stamped = {**payload, "seq": seq}
        self.payloads.append(stamped)
        return seq


def _validate(payload: dict[str, Any]) -> Any:
    """Round-trip ``payload`` through the discriminated union; return the
    parsed concrete event so callers can assert ``isinstance``.
    """
    adapter: TypeAdapter = TypeAdapter(S.RunEvent)
    return adapter.validate_python(payload)


# --------------------------------------------------------------------------- #
# on_started                                                                  #
# --------------------------------------------------------------------------- #


async def test_on_started_emits_run_started_event() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_started(
        ticker="SPY",
        asset_type="stock",
        analysis_date="2026-05-19",
        analysts=["market", "news"],
        research_depth=1,
        llm_provider="openai",
        quick_think_llm="gpt-4o-mini",
        deep_think_llm="gpt-4o",
        output_language="English",
        checkpoint_enabled=True,
    )
    await obs.aclose()

    assert len(bus.payloads) == 1
    payload = bus.payloads[0]
    event = _validate(payload)
    assert isinstance(event, S.RunStartedEvent)
    assert event.ticker == "SPY"
    assert event.asset_type == "stock"
    assert event.analysts == ["market", "news"]


async def test_on_started_defaults_missing_kwargs() -> None:
    """``on_started`` may be called with only the four positional args (as
    the base class defines). The observer fills sensible defaults so the
    Pydantic event still validates.
    """
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_started(
        ticker="BTC-USD",
        asset_type="crypto",
        analysis_date="2026-05-19",
        analysts=["market", "social", "news"],
    )
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.RunStartedEvent)
    assert event.asset_type == "crypto"
    assert event.research_depth in (1, 3, 5)


# --------------------------------------------------------------------------- #
# on_agent_status                                                             #
# --------------------------------------------------------------------------- #


async def test_on_agent_status_emits_agent_status_event() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_agent_status("Market Analyst", "in_progress")
    obs.on_agent_status("Market Analyst", "completed")
    await obs.aclose()

    assert len(bus.payloads) == 2
    e1 = _validate(bus.payloads[0])
    e2 = _validate(bus.payloads[1])
    assert isinstance(e1, S.AgentStatusEvent)
    assert e1.agent == "Market Analyst"
    assert e1.status == "in_progress"
    assert isinstance(e2, S.AgentStatusEvent)
    assert e2.status == "completed"


# --------------------------------------------------------------------------- #
# on_message — uses `kind` not `type`                                         #
# --------------------------------------------------------------------------- #


async def test_on_message_uses_kind_field() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_message("Agent", "hello world", "12:00:01")
    await obs.aclose()

    payload = bus.payloads[0]
    assert payload["type"] == "message"  # union discriminator
    assert payload["kind"] == "Agent"    # sub-classification
    assert "type" != "kind"
    event = _validate(payload)
    assert isinstance(event, S.MessageEvent)
    assert event.kind == "Agent"
    assert event.content == "hello world"


async def test_on_message_unknown_kind_falls_back_to_system() -> None:
    """The engine may pass odd msg_type values; we don't want to crash."""
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_message("ToolReplyXYZ", "weird", "12:00:00")
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.MessageEvent)
    assert event.kind == "System"


# --------------------------------------------------------------------------- #
# on_tool_call                                                                #
# --------------------------------------------------------------------------- #


async def test_on_tool_call_emits_tool_call_event() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_tool_call("yfinance", {"ticker": "SPY"}, "12:00:01")
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.ToolCallEvent)
    assert event.name == "yfinance"
    assert event.args == {"ticker": "SPY"}


async def test_on_tool_call_coerces_non_dict_args() -> None:
    """``args`` may be a non-dict scalar from older tool-call shapes."""
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_tool_call("get_news", "raw-string-arg", "12:00:02")
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.ToolCallEvent)
    assert isinstance(event.args, dict)


# --------------------------------------------------------------------------- #
# on_report_section                                                           #
# --------------------------------------------------------------------------- #


async def test_on_report_section_emits_report_section_event() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_report_section("market_report", "# Market\nLooks bullish.")
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.ReportSectionEvent)
    assert event.section == "market_report"


# --------------------------------------------------------------------------- #
# on_investment_debate / on_risk_debate                                       #
# --------------------------------------------------------------------------- #


async def test_on_investment_debate_emits_investment_debate_event() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_investment_debate("buy", "sell", "buy")
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.InvestmentDebateEvent)
    assert event.bull == "buy"
    assert event.bear == "sell"
    assert event.judge == "buy"


async def test_on_risk_debate_emits_risk_debate_event() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_risk_debate("agg", "cons", "neu", "judge-decision")
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.RiskDebateEvent)
    assert event.aggressive == "agg"
    assert event.conservative == "cons"
    assert event.neutral == "neu"
    assert event.judge == "judge-decision"


# --------------------------------------------------------------------------- #
# on_analyst_wall_time — uses wire key (e.g. "social", not "Sentiment ...")   #
# --------------------------------------------------------------------------- #


async def test_on_analyst_wall_time_uses_wire_key() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_analyst_wall_time("social", "Sentiment Analyst", 12.34)
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.AnalystWallTimeEvent)
    assert event.key == "social"  # wire key, NOT label
    assert event.label == "Sentiment Analyst"
    assert event.seconds == 12.34


# --------------------------------------------------------------------------- #
# on_completed / on_failed / on_cancelled                                     #
# --------------------------------------------------------------------------- #


async def test_on_completed_emits_run_completed_event() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    # The engine doesn't pass rating/report_dir to on_completed (the
    # observer doesn't see them); the observer stores whatever the
    # caller sets via the helper methods. We test that the observer at
    # minimum emits the event with required fields populated.
    obs.set_completion_info(rating="Buy", report_dir="/data/SPY_2026-05-19")
    obs.on_completed()
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.RunCompletedEvent)
    assert event.rating == "Buy"
    assert event.report_dir == "/data/SPY_2026-05-19"


async def test_on_failed_emits_run_failed_event() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_failed("LLM provider returned 500")
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.RunFailedEvent)
    assert event.error == "LLM provider returned 500"


async def test_on_cancelled_emits_run_cancelled_event() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    obs.on_cancelled()
    await obs.aclose()

    event = _validate(bus.payloads[0])
    assert isinstance(event, S.RunCancelledEvent)


# --------------------------------------------------------------------------- #
# stats()                                                                     #
# --------------------------------------------------------------------------- #


async def test_stats_tracks_tool_and_llm_call_counts() -> None:
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    # 2 tool_calls + 3 Agent messages (counted as LLM calls) + 1 Data msg
    obs.on_tool_call("yfinance", {"ticker": "SPY"}, "12:00:01")
    obs.on_tool_call("get_news", {"ticker": "SPY"}, "12:00:02")
    obs.on_message("Agent", "hello 1", "12:00:03")
    obs.on_message("Agent", "hello 2", "12:00:04")
    obs.on_message("Agent", "hello 3", "12:00:05")
    obs.on_message("Data", "tool output", "12:00:06")
    obs.on_analyst_wall_time("market", "Market Analyst", 5.5)
    obs.on_analyst_wall_time("news", "News Analyst", 3.1)
    await obs.aclose()

    stats = obs.stats()
    assert stats["tool_calls"] == 2
    assert stats["llm_calls"] == 3
    assert stats["analyst_wall_times"] == {"market": 5.5, "news": 3.1}
    # elapsed_seconds is best-effort but non-negative
    assert stats["elapsed_seconds"] >= 0.0


# --------------------------------------------------------------------------- #
# Sync-to-async bridge — callbacks fired from a worker thread                  #
# --------------------------------------------------------------------------- #


async def test_callbacks_from_worker_thread_arrive_on_main_loop() -> None:
    """The real engine streams via ``asyncio.to_thread(stream_run, ...)`` so
    the observer's sync callbacks are invoked from a worker thread.
    Events must still arrive on the bus (which runs on the main loop).
    """
    bus = MockBus()
    obs = WebRunObserver(run_id=uuid4(), publish=bus.publish)

    def _fire_from_worker() -> None:
        obs.on_agent_status("Market Analyst", "in_progress")
        obs.on_message("Agent", "from-worker", "12:00:00")
        obs.on_tool_call("yfinance", {"ticker": "SPY"}, "12:00:01")
        obs.on_agent_status("Market Analyst", "completed")

    await asyncio.to_thread(_fire_from_worker)
    await obs.aclose()

    assert len(bus.payloads) == 4
    types = [p["type"] for p in bus.payloads]
    assert types == ["agent_status", "message", "tool_call", "agent_status"]
