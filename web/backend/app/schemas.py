"""Pydantic v2 schemas mirroring `web/frontend/src/lib/types.ts`.

The frontend types are the contract source of truth (they landed first).
These models MUST round-trip cleanly with the TS interfaces — same field
names (snake_case), same enum values, same discriminator key (`type` for
RunEvent, `kind` for the message sub-classification).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

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
    default_base_url: str | None = None


class CatalogProvider(_FrontendModel):
    key: str
    label: str
    regions: list[ProviderRegion] | None = None
    requires_api_key: bool
    api_key_env: str


class CatalogModel(_FrontendModel):
    id: str
    label: str
    allows_custom: bool
    # Only set for provider=ollama. ``True`` -> model is in the active
    # curated cloud catalog snapshot (see ``app.services.ollama_curated``).
    # ``False`` -> model is reachable via /v1/models but Ollama has
    # de-emphasised it, often because of tracked reliability issues. The
    # field is omitted entirely for non-Ollama providers because we have
    # no equivalent quality signal there.
    curated: bool | None = None


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
    id: UUID
    username: str
    role: Literal["admin", "user"]


class LoginRequest(_FrontendModel):
    username: str
    password: str


# --------------------------------------------------------------------------- #
# User administration                                                         #
# --------------------------------------------------------------------------- #

# bcrypt hashes only the first 72 BYTES of a password and passlib raises on
# anything longer. Note bytes, not characters: a 40-character password of
# 4-byte emoji is 160 bytes. Validating the encoded length (below) is what
# makes the limit honest — silently truncating would mean two different
# passwords authenticate the same account.
_BCRYPT_MAX_PASSWORD_BYTES = 72


class UserSummary(_FrontendModel):
    """A user account as exposed to admins.

    Deliberately has no ``password_hash`` field. Same discipline as
    ``ApiKeyStatus``: the credential never leaves the database, so it
    cannot leak through a response body, an error payload, or a log line.
    """

    id: UUID
    username: str
    role: Literal["admin", "user"]
    created_at: datetime
    # Number of runs owned by this user. Surfaced so the admin UI can
    # explain why a delete is blocked instead of showing a bare 409.
    run_count: int


class CreateUserRequest(_FrontendModel):
    """Body for ``POST /api/users``.

    Note there is no ``role`` field: new accounts are always ``"user"``,
    hardcoded server-side. Pydantic ignores unknown keys, so a client
    sending ``role: "admin"`` is silently dropped rather than honoured.
    """

    username: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=3, max_length=128),
    ]
    password: str = Field(min_length=8)

    @field_validator("password")
    @classmethod
    def _password_fits_bcrypt(cls, v: str) -> str:
        encoded = len(v.encode("utf-8"))
        if encoded > _BCRYPT_MAX_PASSWORD_BYTES:
            raise ValueError(
                f"password must be at most {_BCRYPT_MAX_PASSWORD_BYTES} bytes "
                f"when UTF-8 encoded (got {encoded})"
            )
        return v


# --------------------------------------------------------------------------- #
# Settings                                                                    #
# --------------------------------------------------------------------------- #


class ApiKeyStatus(_FrontendModel):
    provider_env: str
    configured: bool
    last_updated: datetime | None = None


class ThinkingConfig(_FrontendModel):
    google_thinking_level: GoogleThinkingLevel | None = None
    openai_reasoning_effort: OpenAIReasoningEffort | None = None
    anthropic_effort: AnthropicEffort | None = None


class UserDefaults(_FrontendModel):
    llm_provider: str | None = "ollama"
    quick_think_llm: str | None = "glm-5.2"
    deep_think_llm: str | None = "glm-5.2"
    research_depth: ResearchDepth | None = 1
    analysts: list[AnalystKey] | None = ["market", "social"]
    output_language: str | None = None
    thinking_config: ThinkingConfig | None = None
    enable_checkpoint: bool = True
    updated_at: datetime | None = None


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
    google_thinking_level: GoogleThinkingLevel | None = None
    openai_reasoning_effort: OpenAIReasoningEffort | None = None
    anthropic_effort: AnthropicEffort | None = None
    enable_checkpoint: bool = True


class RunStats(_FrontendModel):
    llm_calls: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_seconds: float = 0.0
    analyst_wall_times: dict[str, float] | None = None


class RunSummary(_FrontendModel):
    id: UUID
    ticker: str
    asset_type: AssetType
    analysis_date: date
    status: RunStatus
    rating: Rating | None = None
    llm_provider: str
    research_depth: ResearchDepth
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    elapsed_seconds: float | None = None


class RunDetail(RunSummary):
    analysts: list[AnalystKey]
    quick_think_llm: str
    deep_think_llm: str
    thinking_config: ThinkingConfig | None = None
    output_language: str
    checkpoint_enabled: bool
    decision_full: str | None = None
    report_dir: str | None = None
    error_message: str | None = None
    stats: RunStats | None = None
    resumable: bool | None = None


class HistoryPage(_FrontendModel):
    items: list[RunSummary]
    next_cursor: str | None = None


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
    ts: datetime | None = None


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
    thinking_config: ThinkingConfig | None = None


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
    bull: str | None = None
    bear: str | None = None
    judge: str | None = None


class RiskDebateEvent(_RunEventBase):
    type: Literal["risk_debate"] = "risk_debate"
    aggressive: str | None = None
    conservative: str | None = None
    neutral: str | None = None
    judge: str | None = None


class StatsEvent(_RunEventBase):
    type: Literal["stats"] = "stats"
    llm_calls: int
    tool_calls: int
    tokens_in: int
    tokens_out: int
    elapsed_seconds: float


class LlmCallPendingEvent(_RunEventBase):
    """Layer 4 in-run heartbeat — an LLM call has been pending ≥30s.

    The LLM client's heartbeat wrapper emits this at ``HEARTBEAT_INTERVAL_SECONDS``
    (30s) intervals while a single call is outstanding so the SSE client can
    show "still waiting on this call (60s elapsed)" rather than going silent
    for the full retry envelope (which is 37+ min for slow reasoning models
    after the Layer 2 timeout bump). ``soft_warning`` flips once
    ``elapsed_seconds`` crosses ~90s so the frontend can style the row
    distinctly (e.g. amber) to indicate the call is suspiciously slow.
    """

    type: Literal["llm_call_pending"] = "llm_call_pending"
    model: str
    agent: str
    elapsed_seconds: int
    soft_warning: bool = False


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
    at_node: str | None = None


RunEvent = Annotated[
    RunStartedEvent | AgentStatusEvent | ProgressUpdateEvent | AnalystWallTimeEvent | ToolCallEvent | MessageEvent | ReportSectionEvent | InvestmentDebateEvent | RiskDebateEvent | StatsEvent | LlmCallPendingEvent | RunCompletedEvent | RunFailedEvent | RunCancelledEvent,
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
    url: str | None = None
    severity: Literal["info", "warning", "critical"] | None = None
    published_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Health                                                                      #
# --------------------------------------------------------------------------- #


class OllamaAttempt(_FrontendModel):
    """One entry in ``OllamaHealth.recent_attempts``.

    The health endpoint exposes the last-3 probe outcomes so the
    frontend can render a small "last 3 polls" indicator next to the
    upstream-alert badge. ``at`` is an ISO8601 wallclock approximation
    (derived from the monotonic timestamp + current wallclock); ``error``
    is the ``repr(exc)`` of the failure (None on success).
    """

    at: str
    ok: bool
    error: str | None = None


class OllamaHealth(_FrontendModel):
    """Per-provider health subblock returned by `/api/health` when ollama is active.

    ``status`` distinguishes three cases:

    * ``"ok"``      — last upstream probe succeeded OR a single recent
                      failure with two prior successes (hysteresis;
                      ``model_count`` is the real count, which can
                      legitimately be ``0`` for an account with no
                      models provisioned — that's still "ok").
    * ``"down"``    — 2-of-3 recent probes failed (sustained outage).
                      ``error`` carries the underlying exception repr
                      for ops triage. ``model_count`` is ``None``.
    * ``"unknown"`` — no probe has been attempted yet in this process
                      (cold start before the catalog endpoint has been
                      hit). Both ``model_count`` and ``error`` are ``None``.

    v0.2.5+hf.4 added:
    * ``recent_attempts`` — the rolling-3 attempt log driving the
      hysteresis. Used by the UI to render a "last 3 polls" indicator.
    * ``circuit_state`` — the breaker's state (closed / open /
      half_open) so the frontend can render a yellow "recovering" pill
      during half-open instead of the red alert.
    """

    status: Literal["ok", "down", "unknown"]
    url: str
    model_count: int | None = None
    error: str | None = None
    recent_attempts: list[OllamaAttempt] = Field(default_factory=list)
    circuit_state: Literal["closed", "open", "half_open"] = "closed"


class UnhealthyModel(_FrontendModel):
    """Per-model probe failure detail for ``RunValidationError``.

    ``status`` mirrors the ``ProbeOutcome`` literal in
    ``app.services.ollama_models`` minus ``ok`` — only failure cases
    show up here. ``upstream_ref`` carries Ollama's ``(ref: ...)``
    identifier when the upstream wrapped one in its 5xx response;
    omitted otherwise.
    """

    model: str
    status: Literal[
        "timeout", "http_5xx", "http_4xx", "degraded_empty_response"
    ]
    upstream_ref: str | None = None


class RunValidationError(_FrontendModel):
    """Structured 400 body returned when a pre-flight model probe fails.

    The flat string the previous validator returned wasn't actionable
    enough — the frontend has no way to render "kimi-k2-thinking is
    timing out, here are 3 known-good models you could switch to"
    from a plain ``detail: str``. This shape gives the UI everything
    it needs to surface a recoverable error.

    Wire into FastAPI as::

        raise HTTPException(
            status_code=400,
            detail=RunValidationError(...).model_dump(),
        )
    """

    code: Literal["upstream_model_unhealthy"]
    message: str
    unhealthy_models: list[UnhealthyModel]
    suggested_alternatives: list[str]


class HealthResponse(_FrontendModel):
    """Shape of `GET /api/health`.

    The outer ``status`` reports OVERALL deployment health — it only flips
    to ``"degraded"`` for in-container failures (DB unreachable). Upstream
    LLM outages do NOT flip outer status (Coolify uses outer status to
    decide container restarts; restarting won't fix an upstream blip).
    Per-subsystem detail lives in dedicated fields (``db``, ``ollama``).
    """

    status: Literal["ok", "degraded"]
    version: str
    db: Literal["ok", "down"]
    disk_free_mb: int | None = None
    active_run_id: str | None = None
    ollama: OllamaHealth | None = None


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
    "LlmCallPendingEvent",
    "RunCompletedEvent",
    "RunFailedEvent",
    "RunCancelledEvent",
    "RunEvent",
    # Announcements
    "Announcement",
    # Run validation
    "UnhealthyModel",
    "RunValidationError",
    # Health
    "OllamaAttempt",
    "OllamaHealth",
    "HealthResponse",
]
