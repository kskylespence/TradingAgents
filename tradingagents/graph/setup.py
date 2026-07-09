# TradingAgents/graph/setup.py

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_msg_delete,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.utils.agent_states import AgentState

from .analyst_execution import build_analyst_execution_plan
from .conditional_logic import ConditionalLogic

# Every target a shared conditional router can return. Each edge driven by the
# router maps all of them, so a fall-through return (e.g. under prompt/i18n/
# refactor drift in the speaker labels) can never hit a missing path_map entry
# and crash LangGraph mid-run (#1088).
DEBATE_PATH_MAP = {
    "Bull Researcher": "Bull Researcher",
    "Bear Researcher": "Bear Researcher",
    "Research Manager": "Research Manager",
}
RISK_ANALYSIS_PATH_MAP = {
    "Aggressive Analyst": "Aggressive Analyst",
    "Conservative Analyst": "Conservative Analyst",
    "Neutral Analyst": "Neutral Analyst",
    "Portfolio Manager": "Portfolio Manager",
}


def _tag_agent(node_fn: Callable, *llms: Any, agent_label: str) -> Callable:
    """Wrap ``node_fn`` so heartbeat events emitted during its LLM calls
    carry ``agent=agent_label``.

    The LLM client's heartbeat wrapper reads ``_heartbeat_agent_hint`` off
    the client at emit time, so we set it on every LLM the node might
    touch (both quick and deep clients — analysts use quick, the manager
    nodes use deep, but stamping both is cheap and avoids guessing).
    The wrapper is intentionally tolerant of LLMs that don't expose
    ``set_agent_hint`` (Anthropic / Google clients) so it can be
    applied uniformly.
    """

    def wrapped(state):
        for llm in llms:
            if hasattr(llm, "set_agent_hint"):
                llm.set_agent_hint(agent_label)
        return node_fn(state)

    return wrapped


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        tool_nodes: dict[str, ToolNode],
        conditional_logic: ConditionalLogic,
    ):
        """Initialize with required components."""
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.tool_nodes = tool_nodes
        self.conditional_logic = conditional_logic

    def setup_graph(
        self, selected_analysts=("market", "social", "news", "fundamentals")
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst
                - "social": Social media analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
        """
        plan = build_analyst_execution_plan(selected_analysts)

        analyst_factories = {
            "market": lambda: create_market_analyst(self.quick_thinking_llm),
            "social": lambda: create_sentiment_analyst(self.quick_thinking_llm),
            "news": lambda: create_news_analyst(self.quick_thinking_llm),
            "fundamentals": lambda: create_fundamentals_analyst(self.quick_thinking_llm),
        }

        # Create researcher and manager nodes
        bull_researcher_node = create_bull_researcher(self.quick_thinking_llm)
        bear_researcher_node = create_bear_researcher(self.quick_thinking_llm)
        research_manager_node = create_research_manager(self.deep_thinking_llm)
        trader_node = create_trader(self.quick_thinking_llm)

        # Create risk analysis nodes
        aggressive_analyst = create_aggressive_debator(self.quick_thinking_llm)
        neutral_analyst = create_neutral_debator(self.quick_thinking_llm)
        conservative_analyst = create_conservative_debator(self.quick_thinking_llm)
        portfolio_manager_node = create_portfolio_manager(self.deep_thinking_llm)

        # Create workflow
        workflow = StateGraph(AgentState)

        # Tag every node with the agent label so heartbeat events emitted
        # during slow LLM calls carry an identifiable agent. The wrapper
        # is a no-op for non-OpenAI clients (Anthropic / Google) — see
        # _tag_agent. ``self.quick_thinking_llm`` and ``self.deep_thinking_llm``
        # are both stamped so we don't have to track which client each
        # node uses.
        llms = (self.quick_thinking_llm, self.deep_thinking_llm)

        # Add analyst nodes to the graph
        for spec in plan.specs:
            workflow.add_node(
                spec.agent_node,
                _tag_agent(
                    analyst_factories[spec.key](),
                    *llms,
                    agent_label=spec.agent_node,
                ),
            )
            workflow.add_node(spec.clear_node, create_msg_delete())
            workflow.add_node(spec.tool_node, self.tool_nodes[spec.key])

        # Add other nodes — each wrapped with its label so heartbeats name
        # the researcher/debater/PM instead of falling back to "Engine".
        workflow.add_node(
            "Bull Researcher",
            _tag_agent(bull_researcher_node, *llms, agent_label="Bull Researcher"),
        )
        workflow.add_node(
            "Bear Researcher",
            _tag_agent(bear_researcher_node, *llms, agent_label="Bear Researcher"),
        )
        workflow.add_node(
            "Research Manager",
            _tag_agent(research_manager_node, *llms, agent_label="Research Manager"),
        )
        workflow.add_node(
            "Trader",
            _tag_agent(trader_node, *llms, agent_label="Trader"),
        )
        workflow.add_node(
            "Aggressive Analyst",
            _tag_agent(aggressive_analyst, *llms, agent_label="Aggressive Analyst"),
        )
        workflow.add_node(
            "Neutral Analyst",
            _tag_agent(neutral_analyst, *llms, agent_label="Neutral Analyst"),
        )
        workflow.add_node(
            "Conservative Analyst",
            _tag_agent(conservative_analyst, *llms, agent_label="Conservative Analyst"),
        )
        workflow.add_node(
            "Portfolio Manager",
            _tag_agent(portfolio_manager_node, *llms, agent_label="Portfolio Manager"),
        )

        # Define edges
        # Start with the first analyst
        workflow.add_edge(START, plan.specs[0].agent_node)

        # Connect analysts in sequence
        for i, spec in enumerate(plan.specs):
            current_analyst = spec.agent_node
            current_tools = spec.tool_node
            current_clear = spec.clear_node

            # Add conditional edges for current analyst
            workflow.add_conditional_edges(
                current_analyst,
                getattr(self.conditional_logic, f"should_continue_{spec.key}"),
                [current_tools, current_clear],
            )
            workflow.add_edge(current_tools, current_analyst)

            # Connect to next analyst or to Bull Researcher if this is the last analyst
            if i < len(plan.specs) - 1:
                workflow.add_edge(current_clear, plan.specs[i + 1].agent_node)
            else:
                workflow.add_edge(current_clear, "Bull Researcher")

        # Both research-debate edges share the complete DEBATE_PATH_MAP (#1088).
        for debate_node in ("Bull Researcher", "Bear Researcher"):
            workflow.add_conditional_edges(
                debate_node,
                self.conditional_logic.should_continue_debate,
                DEBATE_PATH_MAP,
            )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        # All three risk edges share the complete RISK_ANALYSIS_PATH_MAP (#1088).
        for risk_node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
            workflow.add_conditional_edges(
                risk_node,
                self.conditional_logic.should_continue_risk_analysis,
                RISK_ANALYSIS_PATH_MAP,
            )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
