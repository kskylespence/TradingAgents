"""Pydantic v2 schemas mirroring `web/frontend/src/lib/types.ts`.

The frontend types are the contract source of truth (they landed first).
These models MUST round-trip cleanly with the TS interfaces — same field
names (snake_case), same enum values, same discriminator key (`type` for
RunEvent, `kind` for the message sub-classification).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Enums (typed string literals to match TS exactly)                           #
# --------------------------------------------------------------------------- #

Rating = Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
"""5-tier Portfolio Manager rating. Mirrors tradingagents/agents/utils/rating.py:RATINGS_5_TIER."""

RunStatus = Literal[
    "queued", "running", "completed", "failed", "cancelled", "interrupted"
]
AssetType = Literal["stock", "crypto"]
AnalystKey = Literal["market", "social", "news", "fundamentals"]
ResearchDepth = Literal[1, 3, 5]
AgentStatus = Literal["pending", "in_progress", "completed", "error"]
GoogleThinkingLevel = Literal["high", "minimal"]
OpenAIReasoningEffort = Literal["low", "medium", "high"]
AnthropicEffort = Literal["low", "medium", "high"]
MessageKind = Literal["User", "Agent", "Data", "Control", "System"]
ReportSectionKey = Literal[
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_plan",
    "trader_investment_plan",
    "final_trade_decision",
]


class _FrontendModel(BaseModel):
    """Shared base — keeps JSON-serialization aligned with the TS types."""

    model_config = ConfigDict(
        # Allow population by field name AND alias; we don't use aliases here,
        # but this future-proofs the contract.
        populate_by_name=True,
        # Coerce json-ish primitives sensibly (e.g. ISO datetimes).
        str_strip_whitespace=False,
    )


# --------------------------------------------------------------------------- #
# Catalog                                                                     #
# --------------------------------------------------------------------------- #


class ProviderRegion(_FrontendModel):
    key: str
    label: str
    default_base_url: Optional[str] = None


class CatalogProvider(_FrontendModel):
    key: str
    label: str
    regions: Optional[list[ProviderRegion]] = None
    requires_api_key: bool
    api_key_env: str


class CatalogModel(_FrontendModel):
    id: str
    label: str
    allows_custom: bool


class CatalogAnalyst(_FrontendModel):
    key: AnalystKey
    label: str


class CatalogLanguage(_FrontendModel):
    key: str
    label: str


# --------------------------------------------------------------------------- #
# Auth                                                                        #
# --------------------------------------------------------------------------- #


class AuthUser(_FrontendModel):
    username: str


class LoginRequest(_FrontendModel):
    username: str
    password: str


# --------------------------------------------------------------------------- #
# Settings                                                                    #
# --------------------------------------------------------------------------- #


class ApiKeyStatus(_FrontendModel):
    provider_env: str
    configured: bool
    last_updated: Optional[datetime] = None


class ThinkingConfig(_FrontendModel):
    google_thinking_level: Optional[GoogleThinkingLevel] = None
    openai_reasoning_effort: Optional[OpenAIReasoningEffort] = None
    anthropic_effort: Optional[AnthropicEffort] = None


class UserDefaults(_FrontendModel):
    llm_provider: Optional[str] = None
    quick_think_llm: Optional[str] = None
    deep_think_llm: Optional[str] = None
    research_depth: Optional[ResearchDepth] = None
    analysts: Optional[list[AnalystKey]] = None
    output_language: Optional[str] = None
    thinking_config: Optional[ThinkingConfig] = None
    enable_checkpoint: bool = True
    updated_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Run submission                                                              #
# --------------------------------------------------------------------------- #


class RunRequest(_FrontendModel):
    """Mirrors the 8 CLI steps from `cli/main.py:625-639`."""

    ticker: str
    analysis_date: date
    output_language: str
    analysts: list[AnalystKey]
    research_depth: ResearchDepth
    llm_provider: str
    quick_think_llm: str
    deep_think_llm: str
    google_thinking_level: Optional[GoogleThinkingLevel] = None
    openai_reasoning_effort: Optional[OpenAIReasoningEffort] = None
    anthropic_effort: Optional[AnthropicEffort] = None
    enable_checkpoint: bool = True


class RunStats(_FrontendModel):
    llm_calls: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_seconds: float = 0.0
    analyst_wall_times: Optional[dict[str, float]] = None


class RunSummary(_FrontendModel):
    id: UUID
    ticker: str
    asset_type: AssetType
    analysis_date: date
    status: RunStatus
    rating: Optional[Rating] = None
    llm_provider: str
    research_depth: ResearchDepth
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    elapsed_seconds: Optional[float] = None


class RunDetail(RunSummary):
    analysts: list[AnalystKey]
    quick_think_llm: str
    deep_think_llm: str
    thinking_config: Optional[ThinkingConfig] = None
    output_language: str
    checkpoint_enabled: bool
    decision_full: Optional[str] = None
    report_dir: Optional[str] = None
    error_message: Optional[str] = None
    stats: Optional[RunStats] = None
    resumable: Optional[bool] = None


class HistoryPage(_FrontendModel):
    items: list[RunSummary]
    next_cursor: Optional[str] = None


# --------------------------------------------------------------------------- #
# RunEvent discriminated union (SSE payloads)                                  #
# --------------------------------------------------------------------------- #


class _RunEventBase(_FrontendModel):
    """Shared base for every event in the SSE stream.

    `seq` is the BIGINT sequence from `run_events`; clients reconnect with
    `Last-Event-ID: <seq>` and the server replays from `seq + 1`. `ts` is
    optional because the database default fills it server-side.
    """

    seq: int
    ts: Optional[datetime] = None


class RunStartedEvent(_RunEventBase):
    type: Literal["run_started"] = "run_started"
    ticker: str
    asset_type: AssetType
    analysis_date: date
    analysts: list[AnalystKey]
    research_depth: ResearchDepth
    llm_provider: str
    quick_think_llm: str
    deep_think_llm: str
    output_language: str
    checkpoint_enabled: bool
    thinking_config: Optional[ThinkingConfig] = None


class AgentStatusEvent(_RunEventBase):
    type: Literal["agent_status"] = "agent_status"
    agent: str
    status: AgentStatus


class ProgressUpdateEvent(_RunEventBase):
    type: Literal["progress_update"] = "progress_update"
    progress: float = Field(ge=0.0, le=1.0)
    step: str


class AnalystWallTimeEvent(_RunEventBase):
    type: Literal["analyst_wall_time"] = "analyst_wall_time"
    key: AnalystKey
    label: str
    seconds: float


class ToolCallEvent(_RunEventBase):
    type: Literal["tool_call"] = "tool_call"
    name: str
    args: dict[str, Any]
    timestamp: str


class MessageEvent(_RunEventBase):
    """Note: uses `kind` for the sub-classification, not `type`.

    `type` is already taken by the union discriminator. Matches frontend.
    """

    type: Literal["message"] = "message"
    kind: MessageKind
    content: str
    timestamp: str


class ReportSectionEvent(_RunEventBase):
    type: Literal["report_section"] = "report_section"
    # The frontend accepts either the known keys OR an arbitrary string for
    # forward-compat; we allow the same with `str` and trust callers.
    section: str
    content: str


class InvestmentDebateEvent(_RunEventBase):
    type: Literal["investment_debate"] = "investment_debate"
    bull: Optional[str] = None
    bear: Optional[str] = None
    judge: Optional[str] = None


class RiskDebateEvent(_RunEventBase):
    type: Literal["risk_debate"] = "risk_debate"
    aggressive: Optional[str] = None
    conservative: Optional[str] = None
    neutral: Optional[str] = None
    judge: Optional[str] = None


class StatsEvent(_RunEventBase):
    type: Literal["stats"] = "stats"
    llm_calls: int
    tool_calls: int
    tokens_in: int
    tokens_out: int
    elapsed_seconds: float


class RunCompletedEvent(_RunEventBase):
    type: Literal["run_completed"] = "run_completed"
    rating: Rating
    report_dir: str
    finished_at: datetime


class RunFailedEvent(_RunEventBase):
    type: Literal["run_failed"] = "run_failed"
    error: str


class RunCancelledEvent(_RunEventBase):
    type: Literal["run_cancelled"] = "run_cancelled"
    at_node: Optional[str] = None


RunEvent = Annotated[
    Union[
        RunStartedEvent,
        AgentStatusEvent,
        ProgressUpdateEvent,
        AnalystWallTimeEvent,
        ToolCallEvent,
        MessageEvent,
        ReportSectionEvent,
        InvestmentDebateEvent,
        RiskDebateEvent,
        StatsEvent,
        RunCompletedEvent,
        RunFailedEvent,
        RunCancelledEvent,
    ],
    Field(discriminator="type"),
]
"""Discriminated union of every SSE event the backend emits."""


# --------------------------------------------------------------------------- #
# Announcements (proxied from api.tauric.ai)                                  #
# --------------------------------------------------------------------------- #


class Announcement(_FrontendModel):
    id: str
    title: str
    body: str
    url: Optional[str] = None
    severity: Optional[Literal["info", "warning", "critical"]] = None
    published_at: Optional[datetime] = None


__all__ = [
    # Enums
    "Rating",
    "RunStatus",
    "AssetType",
    "AnalystKey",
    "ResearchDepth",
    "AgentStatus",
    "GoogleThinkingLevel",
    "OpenAIReasoningEffort",
    "AnthropicEffort",
    "MessageKind",
    "ReportSectionKey",
    # Catalog
    "ProviderRegion",
    "CatalogProvider",
    "CatalogModel",
    "CatalogAnalyst",
    "CatalogLanguage",
    # Auth
    "AuthUser",
    "LoginRequest",
    # Settings
    "ApiKeyStatus",
    "ThinkingConfig",
    "UserDefaults",
    # Runs
    "RunRequest",
    "RunStats",
    "RunSummary",
    "RunDetail",
    "HistoryPage",
    # Events
    "RunStartedEvent",
    "AgentStatusEvent",
    "ProgressUpdateEvent",
    "AnalystWallTimeEvent",
    "ToolCallEvent",
    "MessageEvent",
    "ReportSectionEvent",
    "InvestmentDebateEvent",
    "RiskDebateEvent",
    "StatsEvent",
    "RunCompletedEvent",
    "RunFailedEvent",
    "RunCancelledEvent",
    "RunEvent",
    # Announcements
    "Announcement",
]
