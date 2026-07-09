"""Run observer abstraction + shared chunk-streaming loop.

Defines `RunObserver`, an abstract base that both the CLI's MessageBuffer
and the web backend's run-event sink implement. `stream_run` drives a
`TradingAgentsGraph.stream` and routes every chunk into observer
callbacks, so the same routing logic is shared by CLI and web.

The CLI's panel-render state and file-logging decorators live in
`cli/main.py` — this module is observer-agnostic.
"""

from __future__ import annotations

import ast
import datetime
from abc import ABC
from collections.abc import Callable
from typing import Any, Literal

ANALYST_ORDER: list[str] = ["market", "social", "news", "fundamentals"]

ANALYST_AGENT_NAMES: dict[str, str] = {
    "market": "Market Analyst",
    "social": "Sentiment Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}

ANALYST_REPORT_MAP: dict[str, str] = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}

_RESEARCH_TEAM_AGENTS = ("Bull Researcher", "Bear Researcher", "Research Manager")

AgentStatus = Literal["pending", "in_progress", "completed", "error"]


class RunObserver(ABC):
    """Callback sink for a streamed TradingAgents run.

    Concrete subclasses can ignore any callback by leaving the default
    no-op pass; the chunk-stream loop calls everything unconditionally.
    """

    def on_started(
        self,
        ticker: str,
        asset_type: str,
        analysis_date: str,
        analysts: list[str],
        **kwargs: Any,
    ) -> None: pass

    def on_agent_status(self, agent: str, status: AgentStatus) -> None: pass

    def on_message(self, msg_type: str, content: str, timestamp: str) -> None: pass

    def on_tool_call(self, tool_name: str, args: Any, timestamp: str) -> None: pass

    def on_report_section(self, section: str, content: str) -> None: pass

    def on_investment_debate(
        self,
        bull: str | None,
        bear: str | None,
        judge: str | None,
    ) -> None: pass

    def on_risk_debate(
        self,
        aggressive: str | None,
        conservative: str | None,
        neutral: str | None,
        judge: str | None,
    ) -> None: pass

    def on_analyst_wall_time(
        self, analyst_key: str, agent_name: str, seconds: float
    ) -> None: pass

    def on_completed(self) -> None: pass

    def on_failed(self, error: str) -> None: pass

    def on_cancelled(self) -> None: pass

    def emit_llm_call_pending(self, payload: dict[str, Any]) -> None:
        """In-run heartbeat: a single LLM call has been outstanding past 30s.

        Layer 4 of the resilience pass. The LLM client's ``invoke`` /
        ``ainvoke`` wrapper calls this at 30s intervals so the UI can
        render "still waiting on this call (60s elapsed)" instead of
        going silent for the full retry envelope. ``payload`` is
        ``{model, agent, elapsed_seconds, soft_warning}``. The CLI
        observer's default is to ignore this entirely; the web
        observer publishes it as an SSE event.
        """
        pass


# --------------------------------------------------------------------------- #
# Chunk-routing helpers (formerly in cli/main.py)                             #
# --------------------------------------------------------------------------- #


def _is_empty(val: Any) -> bool:
    """Check if value is empty using Python's truthiness."""
    if val is None or val == '':
        return True
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return True
        try:
            # ast.literal_eval is the safe AST-only literal parser (not eval);
            # used here to detect stringified empty containers like "{}" or "[]".
            parsed = ast.literal_eval(s)
            return not bool(parsed)
        except (ValueError, SyntaxError):
            return False
    return not bool(val)


def extract_content_string(content: Any) -> str | None:
    """Extract string content from various message formats.

    Returns None if no meaningful text content is found.
    """
    if _is_empty(content):
        return None

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, dict):
        text = content.get('text', '')
        return text.strip() if not _is_empty(text) else None

    if isinstance(content, list):
        text_parts = [
            item.get('text', '').strip() if isinstance(item, dict) and item.get('type') == 'text'
            else (item.strip() if isinstance(item, str) else '')
            for item in content
        ]
        result = ' '.join(t for t in text_parts if t and not _is_empty(t))
        return result if result else None

    return str(content).strip() if not _is_empty(content) else None


def classify_message_type(message: Any) -> tuple[str, str | None]:
    """Classify a LangChain message into display type and extract content.

    Returns:
        (type, content) - type is one of: User, Agent, Data, Control, System
                        - content is extracted string or None
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    content = extract_content_string(getattr(message, 'content', None))

    if isinstance(message, HumanMessage):
        if content and content.strip() == "Continue":
            return ("Control", content)
        return ("User", content)

    if isinstance(message, ToolMessage):
        return ("Data", content)

    if isinstance(message, AIMessage):
        return ("Agent", content)

    return ("System", content)


def format_tool_args(args: Any, max_length: int = 80) -> str:
    """Format tool arguments for terminal display."""
    result = str(args)
    if len(result) > max_length:
        return result[:max_length - 3] + "..."
    return result


# --------------------------------------------------------------------------- #
# stream_run                                                                  #
# --------------------------------------------------------------------------- #


def stream_run(
    graph: Any,
    init_state: dict[str, Any],
    args: dict[str, Any],
    observer: RunObserver,
    selected_analysts: list[str],
    cancel_event: Any | None = None,
    wall_time_tracker: Any | None = None,
    signal_processor: Callable[[str], Any] | None = None,
    on_chunk: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Stream a TradingAgentsGraph run and dispatch chunks to the observer.

    Returns the merged final_state. Chunks from `graph.graph.stream(...)`
    are per-node deltas; we merge them so every report field populated
    across the run is present in the returned dict.

    Args:
        graph: TradingAgentsGraph instance.
        init_state: Initial agent state from `propagator.create_initial_state`.
        args: Graph stream kwargs from `propagator.get_graph_args(...)`.
        observer: RunObserver implementation.
        selected_analysts: List of analyst keys (e.g. ["market", "news"]).
        cancel_event: Optional threading.Event-like; if set, abort the loop.
        wall_time_tracker: Optional AnalystWallTimeTracker passed through to
            analyst-status sync.
        signal_processor: Optional callable invoked at end with
            final_state["final_trade_decision"]; the CLI calls
            graph.process_signal here.
        on_chunk: Optional callback invoked once per chunk after observer
            dispatch (lets the CLI trigger a Rich Live re-render).
    """
    processed_message_ids: set = set()
    accumulated_reports: dict[str, Any] = {}
    # Pre-populate agent_status with the same initial "pending" set the
    # CLI's MessageBuffer.init_for_analysis used to seed. stream_run uses
    # this for the original "skip if already completed" gates without
    # depending on observer internals.
    agent_status: dict[str, str] = {}
    for analyst_key in selected_analysts:
        if analyst_key in ANALYST_AGENT_NAMES:
            agent_status[ANALYST_AGENT_NAMES[analyst_key]] = "pending"
    for agent in (
        "Bull Researcher", "Bear Researcher", "Research Manager",
        "Trader",
        "Aggressive Analyst", "Neutral Analyst", "Conservative Analyst",
        "Portfolio Manager",
    ):
        agent_status[agent] = "pending"
    trace: list[dict[str, Any]] = []

    def _now() -> str:
        return datetime.datetime.now().strftime("%H:%M:%S")

    def _set_status(agent: str, status: AgentStatus) -> None:
        agent_status[agent] = status
        observer.on_agent_status(agent, status)

    def _set_research_team(status: AgentStatus) -> None:
        for agent in _RESEARCH_TEAM_AGENTS:
            _set_status(agent, status)

    for chunk in graph.graph.stream(init_state, **args):
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            observer.on_cancelled()
            return _merge_chunks(trace)

        for message in chunk.get("messages", []):
            msg_id = getattr(message, "id", None)
            if msg_id is not None:
                if msg_id in processed_message_ids:
                    continue
                processed_message_ids.add(msg_id)

            msg_type, content = classify_message_type(message)
            if content and content.strip():
                observer.on_message(msg_type, content, _now())

            if hasattr(message, "tool_calls") and message.tool_calls:
                for tool_call in message.tool_calls:
                    if isinstance(tool_call, dict):
                        observer.on_tool_call(tool_call["name"], tool_call["args"], _now())
                    else:
                        observer.on_tool_call(tool_call.name, tool_call.args, _now())

        # Analyst statuses (mirrors original update_analyst_statuses)
        if wall_time_tracker is not None:
            from tradingagents.graph.analyst_execution import (
                sync_analyst_tracker_from_chunk,
            )

            sync_analyst_tracker_from_chunk(wall_time_tracker, chunk)

        found_active = False
        for analyst_key in ANALYST_ORDER:
            if analyst_key not in selected_analysts:
                continue
            agent_name = ANALYST_AGENT_NAMES[analyst_key]
            report_key = ANALYST_REPORT_MAP[analyst_key]
            if chunk.get(report_key):
                accumulated_reports[report_key] = chunk[report_key]
                observer.on_report_section(report_key, chunk[report_key])
            has_report = bool(accumulated_reports.get(report_key))
            if has_report:
                _set_status(agent_name, "completed")
            elif not found_active:
                _set_status(agent_name, "in_progress")
                found_active = True
            else:
                _set_status(agent_name, "pending")
        if not found_active and selected_analysts and agent_status.get("Bull Researcher") == "pending":
            _set_status("Bull Researcher", "in_progress")

        # Research Team — investment debate
        if chunk.get("investment_debate_state"):
            debate_state = chunk["investment_debate_state"]
            bull_hist = debate_state.get("bull_history", "").strip()
            bear_hist = debate_state.get("bear_history", "").strip()
            judge = debate_state.get("judge_decision", "").strip()

            if bull_hist or bear_hist:
                _set_research_team("in_progress")
            if bull_hist:
                observer.on_report_section(
                    "investment_plan", f"### Bull Researcher Analysis\n{bull_hist}"
                )
            if bear_hist:
                observer.on_report_section(
                    "investment_plan", f"### Bear Researcher Analysis\n{bear_hist}"
                )
            if judge:
                observer.on_report_section(
                    "investment_plan", f"### Research Manager Decision\n{judge}"
                )
                _set_research_team("completed")
                _set_status("Trader", "in_progress")

            observer.on_investment_debate(
                bull_hist or None, bear_hist or None, judge or None
            )

        # Trading Team
        if chunk.get("trader_investment_plan"):
            accumulated_reports["trader_investment_plan"] = chunk["trader_investment_plan"]
            observer.on_report_section(
                "trader_investment_plan", chunk["trader_investment_plan"]
            )
            if agent_status.get("Trader") != "completed":
                _set_status("Trader", "completed")
                _set_status("Aggressive Analyst", "in_progress")

        # Risk Management Team
        if chunk.get("risk_debate_state"):
            risk_state = chunk["risk_debate_state"]
            agg_hist = risk_state.get("aggressive_history", "").strip()
            con_hist = risk_state.get("conservative_history", "").strip()
            neu_hist = risk_state.get("neutral_history", "").strip()
            judge = risk_state.get("judge_decision", "").strip()

            if agg_hist:
                if agent_status.get("Aggressive Analyst") != "completed":
                    _set_status("Aggressive Analyst", "in_progress")
                observer.on_report_section(
                    "final_trade_decision", f"### Aggressive Analyst Analysis\n{agg_hist}"
                )
            if con_hist:
                if agent_status.get("Conservative Analyst") != "completed":
                    _set_status("Conservative Analyst", "in_progress")
                observer.on_report_section(
                    "final_trade_decision", f"### Conservative Analyst Analysis\n{con_hist}"
                )
            if neu_hist:
                if agent_status.get("Neutral Analyst") != "completed":
                    _set_status("Neutral Analyst", "in_progress")
                observer.on_report_section(
                    "final_trade_decision", f"### Neutral Analyst Analysis\n{neu_hist}"
                )
            if judge and agent_status.get("Portfolio Manager") != "completed":
                _set_status("Portfolio Manager", "in_progress")
                observer.on_report_section(
                    "final_trade_decision", f"### Portfolio Manager Decision\n{judge}"
                )
                _set_status("Aggressive Analyst", "completed")
                _set_status("Conservative Analyst", "completed")
                _set_status("Neutral Analyst", "completed")
                _set_status("Portfolio Manager", "completed")

            observer.on_risk_debate(
                agg_hist or None, con_hist or None, neu_hist or None, judge or None
            )

        if on_chunk is not None:
            on_chunk(chunk)

        trace.append(chunk)

    final_state = _merge_chunks(trace)

    if signal_processor is not None and final_state.get("final_trade_decision"):
        signal_processor(final_state["final_trade_decision"])

    return final_state


def _merge_chunks(trace: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-node delta chunks into a single dict."""
    final_state: dict[str, Any] = {}
    for chunk in trace:
        final_state.update(chunk)
    return final_state
