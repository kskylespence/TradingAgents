/**
 * Shared types for the TradingAgents web UI.
 * Mirrors the backend Pydantic schemas (see plan: "API surface", "RunRequest schema", "RunEvent stream").
 */

// ----- Enums -----

/** 5-tier Portfolio Manager rating. Source: tradingagents/agents/utils/rating.py:RATINGS_5_TIER. */
export type Rating = "Buy" | "Overweight" | "Hold" | "Underweight" | "Sell";

export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type AssetType = "stock" | "crypto";

export type AnalystKey = "market" | "social" | "news" | "fundamentals";

export type ResearchDepth = 1 | 3 | 5;

export type AgentStatus = "pending" | "in_progress" | "completed" | "error";

export type GoogleThinkingLevel = "high" | "minimal";
export type OpenAIReasoningEffort = "low" | "medium" | "high";
export type AnthropicEffort = "low" | "medium" | "high";

// ----- Catalog (GET /api/catalog/*) -----

export interface ProviderRegion {
  key: string;
  label: string;
  default_base_url?: string;
}

export interface CatalogProvider {
  key: string;
  label: string;
  regions?: ProviderRegion[];
  requires_api_key: boolean;
  api_key_env: string;
}

export interface CatalogModel {
  id: string;
  label: string;
  allows_custom: boolean;
  /**
   * Only set for provider=ollama. `true` -> in the active curated cloud
   * catalog (web/backend/app/services/ollama_curated.py snapshot).
   * `false` -> reachable via /v1/models but Ollama has de-emphasised it
   * (often due to tracked reliability issues — see
   * ollama/ollama#15453, #14542). Field is OMITTED for non-Ollama
   * providers, so an older backend that doesn't know about the field
   * naturally falls into the "no badge" branch. Frontend code that
   * checks this MUST treat `undefined` as curated (don't badge models
   * we have no signal for).
   */
  curated?: boolean;
}

export interface CatalogAnalyst {
  key: AnalystKey;
  label: string;
}

export interface CatalogLanguage {
  key: string;
  label: string;
}

// ----- Auth -----

export interface AuthUser {
  username: string;
}

export interface LoginRequest {
  username: string;
  password: string;
}

// ----- Settings -----

export interface ApiKeyStatus {
  provider_env: string;
  configured: boolean;
  last_updated: string | null;
}

export interface UserDefaults {
  llm_provider: string | null;
  quick_think_llm: string | null;
  deep_think_llm: string | null;
  research_depth: ResearchDepth | null;
  analysts: AnalystKey[] | null;
  output_language: string | null;
  thinking_config: ThinkingConfig | null;
  enable_checkpoint: boolean;
  updated_at: string | null;
}

export interface ThinkingConfig {
  google_thinking_level?: GoogleThinkingLevel;
  openai_reasoning_effort?: OpenAIReasoningEffort;
  anthropic_effort?: AnthropicEffort;
}

// ----- Run submission -----

export interface RunRequest {
  ticker: string;
  /** ISO-8601 date (YYYY-MM-DD); the server enforces "not in the future". */
  analysis_date: string;
  output_language: string;
  analysts: AnalystKey[];
  research_depth: ResearchDepth;
  llm_provider: string;
  quick_think_llm: string;
  deep_think_llm: string;
  google_thinking_level?: GoogleThinkingLevel;
  openai_reasoning_effort?: OpenAIReasoningEffort;
  anthropic_effort?: AnthropicEffort;
  enable_checkpoint?: boolean;
}

export interface RunStats {
  llm_calls: number;
  tool_calls: number;
  tokens_in: number;
  tokens_out: number;
  elapsed_seconds: number;
  /** Per-analyst wall-clock breakdown: { market: 12.3, news: 8.1, ... } */
  analyst_wall_times?: Record<string, number>;
}

export interface RunSummary {
  id: string;
  ticker: string;
  asset_type: AssetType;
  analysis_date: string;
  status: RunStatus;
  rating: Rating | null;
  llm_provider: string;
  research_depth: ResearchDepth;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  elapsed_seconds: number | null;
}

export interface RunDetail extends RunSummary {
  analysts: AnalystKey[];
  quick_think_llm: string;
  deep_think_llm: string;
  thinking_config: ThinkingConfig | null;
  output_language: string;
  checkpoint_enabled: boolean;
  decision_full: string | null;
  report_dir: string | null;
  error_message: string | null;
  stats: RunStats | null;
  resumable?: boolean;
}

export interface HistoryPage {
  items: RunSummary[];
  next_cursor: string | null;
}

// ----- RunEvent discriminated union (SSE payloads) -----

export interface RunEventBase {
  seq: number;
  type: string;
  ts?: string;
}

/** Emitted when the engine begins; mirrors RunRequest plus derived fields. */
export interface RunStartedEvent extends RunEventBase {
  type: "run_started";
  ticker: string;
  asset_type: AssetType;
  analysis_date: string;
  analysts: AnalystKey[];
  research_depth: ResearchDepth;
  llm_provider: string;
  quick_think_llm: string;
  deep_think_llm: string;
  output_language: string;
  checkpoint_enabled: boolean;
  thinking_config: ThinkingConfig | null;
}

export interface AgentStatusEvent extends RunEventBase {
  type: "agent_status";
  agent: string;
  status: AgentStatus;
}

export interface ProgressUpdateEvent extends RunEventBase {
  type: "progress_update";
  /** 0..1 */
  progress: number;
  step: string;
}

export interface AnalystWallTimeEvent extends RunEventBase {
  type: "analyst_wall_time";
  key: AnalystKey;
  label: string;
  seconds: number;
}

export interface ToolCallEvent extends RunEventBase {
  type: "tool_call";
  name: string;
  args: Record<string, unknown>;
  timestamp: string;
}

export type MessageKind = "User" | "Agent" | "Data" | "Control" | "System";

export interface MessageEvent extends RunEventBase {
  type: "message";
  kind: MessageKind;
  content: string;
  timestamp: string;
}

export type ReportSectionKey =
  | "market_report"
  | "sentiment_report"
  | "news_report"
  | "fundamentals_report"
  | "investment_plan"
  | "trader_investment_plan"
  | "final_trade_decision";

export interface ReportSectionEvent extends RunEventBase {
  type: "report_section";
  section: ReportSectionKey | string;
  content: string;
}

export interface InvestmentDebateEvent extends RunEventBase {
  type: "investment_debate";
  bull?: string;
  bear?: string;
  judge?: string;
}

export interface RiskDebateEvent extends RunEventBase {
  type: "risk_debate";
  aggressive?: string;
  conservative?: string;
  neutral?: string;
  judge?: string;
}

export interface StatsEvent extends RunEventBase {
  type: "stats";
  llm_calls: number;
  tool_calls: number;
  tokens_in: number;
  tokens_out: number;
  elapsed_seconds: number;
}

/**
 * Layer 4 in-run heartbeat. The backend emits these at ~30s intervals while
 * a single LLM call is outstanding so the live dashboard can show
 * "still waiting on Fundamentals Analyst – kimi-k2-thinking (60s elapsed)"
 * rather than going silent for the full retry envelope (which can be 30+
 * minutes for slow reasoning models). `soft_warning` flips once
 * `elapsed_seconds` crosses ~90s — the frontend uses it to style the row
 * distinctly (amber) so operators notice and can hit Cancel.
 *
 * Heartbeats are implicitly stale: as soon as the next non-heartbeat
 * event arrives for the run, the UI replaces the row. There is no
 * explicit "call completed" event — the absence of further heartbeats
 * combined with the next normal event signals the call is over.
 */
export interface LlmCallPendingEvent extends RunEventBase {
  type: "llm_call_pending";
  model: string;
  agent: string;
  elapsed_seconds: number;
  soft_warning: boolean;
}

export interface RunCompletedEvent extends RunEventBase {
  type: "run_completed";
  rating: Rating;
  report_dir: string;
  finished_at: string;
}

export interface RunFailedEvent extends RunEventBase {
  type: "run_failed";
  error: string;
}

export interface RunCancelledEvent extends RunEventBase {
  type: "run_cancelled";
  at_node?: string;
}

export type RunEvent =
  | RunStartedEvent
  | AgentStatusEvent
  | ProgressUpdateEvent
  | AnalystWallTimeEvent
  | ToolCallEvent
  | MessageEvent
  | ReportSectionEvent
  | InvestmentDebateEvent
  | RiskDebateEvent
  | StatsEvent
  | LlmCallPendingEvent
  | RunCompletedEvent
  | RunFailedEvent
  | RunCancelledEvent;

// ----- Announcements -----

export interface Announcement {
  id: string;
  title: string;
  body: string;
  url?: string;
  severity?: "info" | "warning" | "critical";
  published_at?: string;
}

// ----- Run validation (HTTP 400 detail body from POST /api/runs) -----

/**
 * Per-model probe failure shape inside `RunValidationError`. Mirrors
 * `app.schemas.UnhealthyModel`.
 *
 * `status` is the failure mode; `upstream_ref` is the Ollama `(ref: ...)`
 * identifier when the upstream surfaced one in its 5xx error body.
 */
export interface UnhealthyModel {
  model: string;
  status:
    | "timeout"
    | "http_5xx"
    | "http_4xx"
    | "degraded_empty_response";
  upstream_ref: string | null;
}

/**
 * Structured 400 body returned by `/api/runs` and `/api/runs/{id}/retry`
 * when a pre-flight liveness probe fails for one or more selected
 * Ollama models. Mirrors `app.schemas.RunValidationError`.
 *
 * The body is delivered as the `detail` field of the FastAPI response
 * envelope, so callers extract it via:
 *
 *   (apiError.body as { detail?: RunValidationError })?.detail
 */
export interface RunValidationError {
  code: "upstream_model_unhealthy";
  message: string;
  unhealthy_models: UnhealthyModel[];
  suggested_alternatives: string[];
}

// ----- Health (GET /api/health) -----

/**
 * One entry in `OllamaHealth.recent_attempts` — a rolling 3-attempt log
 * exposed for the UI to render a "last 3 polls" indicator.
 */
export interface OllamaAttempt {
  /** ISO8601 wallclock approximation of when the attempt was recorded. */
  at: string;
  ok: boolean;
  error: string | null;
}

export interface OllamaHealth {
  /**
   * `"ok"` — last probe succeeded, OR a single recent failure with two
   *          prior successes (hysteresis added in v0.2.5+hf.4).
   * `"down"` — 2-of-3 recent probes failed; sustained outage.
   * `"unknown"` — no probe attempted yet in this process.
   */
  status: "ok" | "down" | "unknown";
  url: string;
  model_count: number | null;
  error: string | null;
  /**
   * v0.2.5+hf.4: rolling-3 attempt log driving the hysteresis. Each
   * entry shows when an attempt was made and whether it succeeded.
   * Default to an empty array if absent (older backends).
   */
  recent_attempts: OllamaAttempt[];
  /**
   * v0.2.5+hf.4: circuit-breaker state for the shared upstream client.
   * `"closed"` — normal; `"open"` — cooling down, requests
   * short-circuit; `"half_open"` — one trial probe in flight after
   * recovery_timeout elapsed.
   */
  circuit_state: "closed" | "open" | "half_open";
}

export interface HealthResponse {
  /** Overall container health. Only `"degraded"` for in-container failures (e.g. DB down). */
  status: "ok" | "degraded";
  version: string;
  db: "ok" | "down";
  disk_free_mb: number | null;
  active_run_id: string | null;
  /** Populated when the active provider is ollama; absent otherwise. */
  ollama: OllamaHealth | null;
}
