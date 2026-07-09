"""Tests for the shared run_observer chunk-routing module."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

import pytest

from tradingagents.run_observer import (
    RunObserver,
    classify_message_type,
    extract_content_string,
    format_tool_args,
    stream_run,
)


class _RecordingObserver(RunObserver):
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []
        self.messages: list[tuple[str, str, str]] = []
        self.tool_calls: list[tuple[str, Any, str]] = []
        self.report_sections: list[tuple[str, str]] = []
        self.investment_debates: list[tuple] = []
        self.risk_debates: list[tuple] = []
        self.completed = False
        self.cancelled = False

    def on_agent_status(self, agent, status):
        self.statuses.append((agent, status))

    def on_message(self, msg_type, content, timestamp):
        self.messages.append((msg_type, content, timestamp))

    def on_tool_call(self, tool_name, args, timestamp):
        self.tool_calls.append((tool_name, args, timestamp))

    def on_report_section(self, section, content):
        self.report_sections.append((section, content))

    def on_investment_debate(self, bull, bear, judge):
        self.investment_debates.append((bull, bear, judge))

    def on_risk_debate(self, aggressive, conservative, neutral, judge):
        self.risk_debates.append((aggressive, conservative, neutral, judge))

    def on_completed(self):
        self.completed = True

    def on_cancelled(self):
        self.cancelled = True


def _fake_graph(chunks: list[dict[str, Any]]) -> Any:
    """Build a minimal stand-in for TradingAgentsGraph that yields chunks."""

    inner = SimpleNamespace(
        stream=lambda init_state, **kwargs: iter(chunks),
    )
    return SimpleNamespace(graph=inner)


@pytest.mark.unit
class StreamRunChunkRoutingTests(unittest.TestCase):
    def test_market_report_chunk_marks_market_completed_and_advances(self):
        observer = _RecordingObserver()
        chunks = [{"market_report": "AAPL is up"}]
        final = stream_run(
            _fake_graph(chunks),
            init_state={},
            args={},
            observer=observer,
            selected_analysts=["market", "news"],
        )

        # Final state merges chunks.
        self.assertEqual(final["market_report"], "AAPL is up")

        # Report section was forwarded.
        self.assertIn(("market_report", "AAPL is up"), observer.report_sections)

        # Market Analyst marked completed; News Analyst becomes the next active.
        self.assertIn(("Market Analyst", "completed"), observer.statuses)
        self.assertIn(("News Analyst", "in_progress"), observer.statuses)

    def test_signal_processor_called_with_final_decision(self):
        observer = _RecordingObserver()
        chunks = [{"final_trade_decision": "BUY"}]
        called_with: list[str] = []
        stream_run(
            _fake_graph(chunks),
            init_state={},
            args={},
            observer=observer,
            selected_analysts=["market"],
            signal_processor=lambda decision: called_with.append(decision),
        )
        self.assertEqual(called_with, ["BUY"])

    def test_cancel_event_short_circuits_loop(self):
        observer = _RecordingObserver()

        class _Cancel:
            def __init__(self) -> None:
                self.count = 0

            def is_set(self) -> bool:
                self.count += 1
                return self.count > 1  # cancel after the first chunk

        chunks = [
            {"market_report": "first"},
            {"news_report": "second"},
        ]
        stream_run(
            _fake_graph(chunks),
            init_state={},
            args={},
            observer=observer,
            selected_analysts=["market", "news"],
            cancel_event=_Cancel(),
        )

        self.assertTrue(observer.cancelled)
        # First chunk processed, second was short-circuited by cancel.
        self.assertIn(("market_report", "first"), observer.report_sections)
        self.assertNotIn(("news_report", "second"), observer.report_sections)

    def test_risk_debate_judge_only_runs_once(self):
        observer = _RecordingObserver()
        chunks = [
            {"risk_debate_state": {"judge_decision": "HOLD"}},
            {"risk_debate_state": {"judge_decision": "HOLD"}},
        ]
        stream_run(
            _fake_graph(chunks),
            init_state={},
            args={},
            observer=observer,
            selected_analysts=["market"],
        )

        # Portfolio Manager Decision section must be appended exactly once,
        # even though two chunks carried a judge_decision (the original
        # cli/main.py guards this with `!= "completed"`).
        pm_sections = [
            r for r in observer.report_sections
            if r[0] == "final_trade_decision" and r[1].startswith("### Portfolio Manager Decision")
        ]
        self.assertEqual(len(pm_sections), 1)

    def test_investment_debate_judge_transitions_to_trader(self):
        observer = _RecordingObserver()
        chunks = [
            {"investment_debate_state": {"judge_decision": "Buy thesis wins"}},
        ]
        stream_run(
            _fake_graph(chunks),
            init_state={},
            args={},
            observer=observer,
            selected_analysts=["market"],
        )

        self.assertIn(("Bull Researcher", "completed"), observer.statuses)
        self.assertIn(("Bear Researcher", "completed"), observer.statuses)
        self.assertIn(("Research Manager", "completed"), observer.statuses)
        self.assertIn(("Trader", "in_progress"), observer.statuses)


@pytest.mark.unit
class HelperTests(unittest.TestCase):
    def test_format_tool_args_truncates_long_strings(self):
        long = "a" * 200
        self.assertEqual(len(format_tool_args(long)), 80)
        self.assertTrue(format_tool_args(long).endswith("..."))

    def test_extract_content_string_handles_text_part_lists(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]
        self.assertEqual(extract_content_string(content), "hello world")

    def test_extract_content_string_returns_none_for_empty(self):
        self.assertIsNone(extract_content_string(""))
        self.assertIsNone(extract_content_string(None))
        self.assertIsNone(extract_content_string("{}"))

    def test_classify_message_type_recognises_continue_as_control(self):
        from langchain_core.messages import HumanMessage

        msg = HumanMessage(content="Continue")
        self.assertEqual(classify_message_type(msg), ("Control", "Continue"))


if __name__ == "__main__":
    unittest.main()
