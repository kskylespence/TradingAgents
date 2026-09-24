"""The real engine path, offline: ``_run_engine`` driving a real TradingAgentsGraph.

Every other run test sets ``FAKE_LLM=1``, which short-circuits
``_run_engine`` before the graph is built. That left the web's actual
engine path — graph construction, ``stream_run``, the analyst wall-time
tracker, the decision log and checkpoint resume — covered by nothing, so an
upstream merge that moved or deleted anything on it would only fail in
production. Here the models are scripted and never call tools, and every
socket is refused, so a run that reaches out to the network fails loudly
instead of hanging.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
from types import SimpleNamespace
from typing import Any

import curl_cffi.requests as curl_requests
import pytest
from app import schemas as S
from app.services import run_service
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tradingagents.decision_log import TradingMemoryLog
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.run_observer import RunObserver

DECISION = "Report.\n\n**Rating**: Overweight\n\nFINAL TRANSACTION PROPOSAL: **BUY**"


class _Script:
    """Call counter shared by every model a graph builds; can fail once."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls = 0
        self.fail_at = fail_at

    def next_call(self) -> None:
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("provider unavailable")


class _AnswerOnlyModel(BaseChatModel):
    """Answers every prompt with DECISION and never calls a tool."""

    script: Any

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
        self.script.next_call()
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=DECISION))])


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
    # yfinance fetches through curl_cffi, which connects from C and never
    # touches Python's socket module; block it at its own request entry.
    monkeypatch.setattr(curl_requests.Session, "request", refuse)
    monkeypatch.setattr(curl_requests.AsyncSession, "request", refuse)


@pytest.fixture
def engine(monkeypatch, tmp_path, no_network):
    """Point every engine path at tmp_path; return a setter for the model script."""
    monkeypatch.delenv("FAKE_LLM", raising=False)
    monkeypatch.setitem(DEFAULT_CONFIG, "data_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    monkeypatch.setitem(DEFAULT_CONFIG, "memory_log_path", str(tmp_path / "memory.md"))
    monkeypatch.setattr(
        run_service, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path / "data")
    )
    state = {"script": _Script()}

    class _Client:
        def get_llm(self):
            return _AnswerOnlyModel(script=state["script"])

    monkeypatch.setattr(trading_graph, "create_llm_client", lambda **kwargs: _Client())

    def use(script: _Script) -> _Script:
        state["script"] = script
        return script

    return use


def _request(**overrides) -> S.RunRequest:
    fields = {
        "ticker": "NVDA",
        "analysis_date": dt.date(2026, 1, 9),
        "output_language": "English",
        "analysts": ["news"],
        "research_depth": 1,
        "llm_provider": "openai",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "enable_checkpoint": False,
    }
    fields.update(overrides)
    return S.RunRequest(**fields)


def _run(
    req: S.RunRequest, observer: RunObserver | None = None, *, resume: bool = False
) -> dict[str, Any]:
    return asyncio.run(
        run_service._run_engine(
            req, "stock", observer or _RecordingObserver(), asyncio.Event(), resume=resume
        )
    )


def _logged_ratings() -> list[str]:
    return [e["rating"] for e in TradingMemoryLog(DEFAULT_CONFIG).load_entries()]


@pytest.mark.unit
def test_no_network_fixture_blocks_price_fetches(no_network):
    """The tests below are only offline if this holds: settlement's price
    fetch (yfinance over curl_cffi) must come back empty, not with real bars."""
    from tradingagents.graph.settlement import get_closes

    try:
        bars = get_closes("NVDA", "2026-01-08", "2026-01-22")
    except OSError:
        return
    assert len(bars) == 0


@pytest.mark.unit
def test_real_engine_runs_a_graph_to_a_decision(engine):
    req = _request()
    observer = _RecordingObserver()

    final_state = _run(req, observer)

    assert "Overweight" in final_state["final_trade_decision"]
    assert "news_report" in observer.sections
    assert set(observer.wall_times) == {"news"}
    assert observer.stats is not None
    rating, _report_dir = run_service._finalize_completion(req, "stock", final_state)
    assert rating == "Overweight"


@pytest.mark.unit
def test_web_run_reads_and_writes_the_decision_log(engine, monkeypatch):
    """A web run starts from the same state propagate() and the CLI build —
    settled decision log and past context — and logs its own decision so the
    next same-ticker run can reflect on it."""
    built = []
    real_create_run_state = trading_graph.TradingAgentsGraph.create_run_state

    def spy(self, *args, **kwargs):
        built.append(args[0] if args else kwargs.get("company_name"))
        return real_create_run_state(self, *args, **kwargs)

    monkeypatch.setattr(trading_graph.TradingAgentsGraph, "create_run_state", spy)

    _run(_request())

    assert built == ["NVDA"]
    assert _logged_ratings() == ["Overweight"]


def _interrupt(engine, fail_at: int) -> None:
    """Leave an interrupted checkpointed run for the default request."""
    interrupted = engine(_Script(fail_at=fail_at))
    with pytest.raises(RuntimeError, match="provider unavailable"):
        _run(_request(enable_checkpoint=True))
    assert interrupted.calls == fail_at, "the interruption must land on the scripted call"


def _full_run_calls(engine) -> int:
    """Model calls a complete run makes, measured on a different date."""
    full = engine(_Script())
    _run(_request(analysis_date=dt.date(2026, 1, 8)))
    assert full.calls >= 6, "fixture must make enough calls to interrupt mid-run"
    return full.calls


@pytest.mark.unit
def test_web_resume_continues_an_interrupted_run(engine):
    """Resuming an interrupted checkpointed run (what resume_run asks for)
    continues from its last completed node instead of starting over."""
    full_run_calls = _full_run_calls(engine)
    fail_at = 4                                   # past the analyst, before the decision
    _interrupt(engine, fail_at)
    assert _logged_ratings() == ["Overweight"], "an interrupted run must not log a decision"

    resumed = engine(_Script())
    final_state = _run(_request(enable_checkpoint=True), resume=True)

    assert resumed.calls == full_run_calls - (fail_at - 1)
    assert "Overweight" in final_state["final_trade_decision"]
    # Reports from before the interruption come back from the checkpoint.
    assert final_state["news_report"].strip()
    assert _logged_ratings() == ["Overweight", "Overweight"]


@pytest.mark.unit
def test_a_run_that_is_not_a_resume_starts_fresh(engine):
    """A new run or a Retry with the same parameters as an interrupted run
    must not silently continue it: only the Resume action resumes."""
    full_run_calls = _full_run_calls(engine)
    _interrupt(engine, fail_at=4)

    fresh = engine(_Script())
    _run(_request(enable_checkpoint=True))

    assert fresh.calls == full_run_calls
