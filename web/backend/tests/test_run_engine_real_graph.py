"""The real engine path, offline: ``_run_engine`` driving a real TradingAgentsGraph.

Every other run test sets ``FAKE_LLM=1``, which short-circuits
``_run_engine`` before the graph is built. That left the web's actual
engine path — graph construction, ``stream_run``, the analyst wall-time
tracker — covered by nothing, so an upstream merge that moved or deleted
anything on it would only fail in production. Here the models are
scripted and never call tools, and every socket is refused, so a run that
reaches out to the network fails loudly instead of hanging.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
from types import SimpleNamespace
from typing import Any

import pytest
from app import schemas as S
from app.services import run_service
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.run_observer import RunObserver

DECISION = "Report.\n\n**Rating**: Overweight\n\nFINAL TRANSACTION PROPOSAL: **BUY**"


class _AnswerOnlyModel(BaseChatModel):
    """Answers every prompt with DECISION and never calls a tool."""

    @property
    def _llm_type(self) -> str:
        return "answer-only"

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        # Decision agents fall back to free text when structured output is
        # unavailable, which is what DECISION is shaped for.
        raise NotImplementedError

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=DECISION))])


class _Client:
    def get_llm(self):
        return _AnswerOnlyModel()


class _RecordingObserver(RunObserver):
    def __init__(self) -> None:
        self.wall_times: dict[str, float] = {}
        self.stats: dict[str, Any] | None = None
        self.sections: set[str] = set()

    def on_report_section(self, section: str, content: str) -> None:
        self.sections.add(section)

    def on_analyst_wall_time(self, key: str, agent_name: str, seconds: float) -> None:
        self.wall_times[key] = seconds

    def ingest_callback_stats(self, stats: dict[str, Any]) -> None:
        self.stats = stats


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("network access is blocked in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def offline_engine(monkeypatch, tmp_path, no_network):
    monkeypatch.delenv("FAKE_LLM", raising=False)
    monkeypatch.setitem(DEFAULT_CONFIG, "data_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(DEFAULT_CONFIG, "memory_log_path", str(tmp_path / "memory.md"))
    monkeypatch.setattr(
        run_service, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path / "data")
    )
    monkeypatch.setattr(trading_graph, "create_llm_client", lambda **kwargs: _Client())


@pytest.mark.unit
def test_real_engine_runs_a_graph_to_a_decision(offline_engine):
    req = S.RunRequest(
        ticker="NVDA",
        analysis_date=dt.date(2026, 1, 9),
        output_language="English",
        analysts=["news"],
        research_depth=1,
        llm_provider="openai",
        quick_think_llm="gpt-6-luna",
        deep_think_llm="gpt-6-sol",
        enable_checkpoint=False,
    )
    observer = _RecordingObserver()

    final_state = asyncio.run(
        run_service._run_engine(req, "stock", observer, asyncio.Event())
    )

    assert "Overweight" in final_state["final_trade_decision"]
    assert "news_report" in observer.sections
    assert set(observer.wall_times) == {"news"}
    assert observer.stats is not None
    rating, _report_dir = run_service._finalize_completion(req, "stock", final_state)
    assert rating == "Overweight"
