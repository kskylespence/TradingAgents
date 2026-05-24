import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useReducer, useRef, useState } from "react";

import { api } from "@/lib/api";
import { useEventSource, type SSEState } from "@/lib/sse";
import type {
  AgentStatusEvent,
  InvestmentDebateEvent,
  LlmCallPendingEvent,
  MessageEvent as RunMessageEvent,
  Rating,
  RiskDebateEvent,
  RunDetail,
  RunEvent,
  RunStatus,
  StatsEvent,
  ToolCallEvent,
} from "@/lib/types";

/**
 * Cap on the number of tool-call events retained in component state. Tool
 * calls can be very chatty (per-vendor data fetches), so we bound memory by
 * keeping only the most recent N. The full transcript still lives on the
 * server side / decision log.
 */
const TOOL_CALL_CAP = 50;

/**
 * Result shape for {@link useRun}. See the run-view plan for the contract
 * each field fulfils.
 */
export interface UseRunResult {
  /** Baseline detail from GET /api/runs/:id (React Query). */
  run: RunDetail | undefined;
  /** Latest status per agent name (collapsed). */
  agents: AgentStatusEvent[];
  /** Append-only message log. */
  messages: RunMessageEvent[];
  /** section -> latest content from report_section events. */
  reportSections: Record<string, string>;
  /** Recent tool-call events (capped at {@link TOOL_CALL_CAP}). */
  toolCalls: ToolCallEvent[];
  /** Latest combined investment debate snapshot. */
  investmentDebate: InvestmentDebateEvent | undefined;
  /** Latest combined risk debate snapshot. */
  riskDebate: RiskDebateEvent | undefined;
  /** Wire-key -> wall-clock seconds (e.g. {market: 12.3}). */
  analystWallTimes: Record<string, number>;
  /** Latest stats snapshot. */
  stats: StatsEvent | undefined;
  /** 0..1, from latest progress_update or derived from agent statuses. */
  progress: number;
  /** Final rating once the run completes. */
  finalRating: Rating | undefined;
  /** Live run status, derived from terminal events and falling back to RunDetail. */
  runStatus: RunStatus;
  /** Error message populated by run_failed event. */
  errorMessage: string | undefined;
  /**
   * Latest in-flight heartbeat. Cleared as soon as any non-heartbeat event
   * arrives (the contract: heartbeats are implicitly stale once the call
   * resolves and the engine emits anything else). Undefined when no LLM
   * call has crossed the 30s threshold recently.
   */
  llmCallPending: LlmCallPendingEvent | undefined;
  /** SSE connection state passthrough. */
  sseState: SSEState;
  /** Force-close + reopen the EventSource. */
  reconnect: () => void;
}

interface ReducerState {
  agentsByName: Map<string, AgentStatusEvent>;
  messages: RunMessageEvent[];
  reportSections: Record<string, string>;
  toolCalls: ToolCallEvent[];
  investmentDebate: InvestmentDebateEvent | undefined;
  riskDebate: RiskDebateEvent | undefined;
  analystWallTimes: Record<string, number>;
  stats: StatsEvent | undefined;
  progress: number;
  finalRating: Rating | undefined;
  terminalStatus: RunStatus | undefined;
  errorMessage: string | undefined;
  /**
   * Latest in-flight heartbeat. The reducer sets it when an
   * `llm_call_pending` event arrives and clears it on any other event
   * — that's the implicit "call resolved" signal since the engine
   * emits something the moment the LLM call returns (e.g. a message,
   * tool_call, or agent_status flip).
   */
  llmCallPending: LlmCallPendingEvent | undefined;
}

const initialReducerState = (): ReducerState => ({
  agentsByName: new Map(),
  messages: [],
  reportSections: {},
  toolCalls: [],
  investmentDebate: undefined,
  riskDebate: undefined,
  analystWallTimes: {},
  stats: undefined,
  progress: 0,
  finalRating: undefined,
  terminalStatus: undefined,
  errorMessage: undefined,
  llmCallPending: undefined,
});

/**
 * Apply a single event to a draft state, mutating it in place. The caller is
 * responsible for handing in a fresh draft (so the returned reference is
 * new — required by ``useReducer``).
 */
function applyEvent(state: ReducerState, ev: RunEvent): ReducerState {
  // Clear any active heartbeat row whenever a non-heartbeat event arrives.
  // The contract (see LlmCallPendingEvent docstring): the engine emits SOME
  // event the moment a slow LLM call resolves (a message, tool_call,
  // agent_status flip, …). We don't need an explicit "call done" event —
  // the next event implicitly retires the heartbeat. Without this, a slow
  // call's heartbeat would linger on screen indefinitely after the call
  // returned.
  if (ev.type !== "llm_call_pending") {
    state.llmCallPending = undefined;
  }

  switch (ev.type) {
    case "run_started":
      // Nothing to mutate — RunDetail is the source of truth for the baseline.
      break;
    case "agent_status":
      state.agentsByName.set(ev.agent, ev);
      break;
    case "progress_update":
      state.progress = Math.max(0, Math.min(1, ev.progress));
      break;
    case "analyst_wall_time":
      state.analystWallTimes[ev.key] = ev.seconds;
      break;
    case "tool_call":
      state.toolCalls.push(ev);
      if (state.toolCalls.length > TOOL_CALL_CAP) {
        state.toolCalls.splice(0, state.toolCalls.length - TOOL_CALL_CAP);
      }
      break;
    case "message":
      state.messages.push(ev);
      break;
    case "report_section":
      state.reportSections[ev.section] = ev.content;
      break;
    case "investment_debate":
      // Latest snapshot wins; bull/bear/judge fields are cumulative on the
      // server side, so we don't merge per-field here.
      state.investmentDebate = ev;
      break;
    case "risk_debate":
      state.riskDebate = ev;
      break;
    case "stats":
      state.stats = ev;
      break;
    case "llm_call_pending":
      // Latest heartbeat replaces the previous one — same call, just with
      // a higher elapsed_seconds value (or a soft_warning that wasn't
      // there yet). The cleared-by-other-events branch above handles the
      // "call resolved" case.
      state.llmCallPending = ev;
      break;
    case "run_completed":
      state.finalRating = ev.rating;
      state.terminalStatus = "completed";
      state.progress = 1;
      break;
    case "run_failed":
      state.errorMessage = ev.error;
      state.terminalStatus = "failed";
      break;
    case "run_cancelled":
      state.terminalStatus = "cancelled";
      break;
    default: {
      // Exhaustiveness check — if a new RunEvent variant is added the
      // compiler will flag this assignment.
      const _exhaustive: never = ev;
      void _exhaustive;
      break;
    }
  }
  return state;
}

type ReducerAction =
  | { type: "reset" }
  | { type: "apply"; events: RunEvent[] };

/**
 * Incremental reducer driven by ``useReducer``. Applies only NEW events on
 * each dispatch, so cost is O(delta) per render instead of O(N) over the
 * whole event history. ``reset`` is used on URL change (when ``useEventSource``
 * clears its events array).
 */
function reducer(state: ReducerState, action: ReducerAction): ReducerState {
  if (action.type === "reset") {
    return initialReducerState();
  }
  // ``action.type === "apply"``
  // Shallow-clone to get a new reference (required for React re-render),
  // then mutate in place — the original draft is discarded after this call.
  const draft: ReducerState = {
    agentsByName: new Map(state.agentsByName),
    messages: state.messages.slice(),
    reportSections: { ...state.reportSections },
    toolCalls: state.toolCalls.slice(),
    investmentDebate: state.investmentDebate,
    riskDebate: state.riskDebate,
    analystWallTimes: { ...state.analystWallTimes },
    stats: state.stats,
    progress: state.progress,
    finalRating: state.finalRating,
    terminalStatus: state.terminalStatus,
    errorMessage: state.errorMessage,
    llmCallPending: state.llmCallPending,
  };
  for (const ev of action.events) {
    applyEvent(draft, ev);
  }
  return draft;
}

// Re-export for unit tests — the SSE test exercises the reducer pipeline by
// dispatching ``apply`` actions and asserting state transitions.
export const _reducer_for_tests = reducer;
export const _applyEvent_for_tests = applyEvent;

/**
 * Roster wire-key fallback for deriving progress when no progress_update has
 * arrived yet. Counts completed/in_progress agents over the full pipeline.
 */
const PROGRESS_AGENT_COUNT = 12;

/**
 * useRun — drives the live dashboard. Combines:
 *   1. React Query for the baseline RunDetail (status, rating, error).
 *   2. EventSource (via {@link useEventSource}) for the live SSE stream.
 *   3. A pure reducer over events for derived UI state.
 */
export function useRun(runId: string | undefined): UseRunResult {
  // SSE source. A bump-key forces re-init on reconnect().
  const [sseKey, setSseKey] = useState(0);
  const sseUrl = runId ? `/api/runs/${runId}/events?k=${sseKey}` : null;
  const sse = useEventSource(sseUrl);

  // Baseline fetch. Refetch when the SSE stream signals a terminal event so
  // RunDetail picks up persisted rating/report_dir.
  const runQuery = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.get<RunDetail>(`/api/runs/${runId}`),
    enabled: !!runId,
    staleTime: 5_000,
  });

  // Incremental reducer. ``useEventSource`` gives us a growing events array,
  // not a per-event callback, so we slice the unprocessed tail on each render
  // and dispatch only those. Cost per event is O(1) instead of O(N) — the
  // earlier ``useMemo(() => reduceEvents(sse.events))`` approach re-reduced
  // the entire history every time a new event arrived, which became visible
  // jank for long runs (200+ events).
  const [reduced, dispatch] = useReducer(reducer, undefined, initialReducerState);
  const processedRef = useRef(0);

  useEffect(() => {
    // Detect a reset: ``useEventSource`` clears its events array on URL
    // change. When that happens, drop our derived state too.
    if (sse.events.length < processedRef.current) {
      dispatch({ type: "reset" });
      processedRef.current = 0;
      return;
    }
    const newEvents = sse.events.slice(processedRef.current);
    if (newEvents.length > 0) {
      dispatch({ type: "apply", events: newEvents });
      processedRef.current = sse.events.length;
    }
  }, [sse.events]);

  // When a terminal event arrives, refetch the baseline so persisted fields
  // (rating, finished_at, stats, report_dir) line up.
  useEffect(() => {
    if (reduced.terminalStatus && runQuery.refetch) {
      void runQuery.refetch();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reduced.terminalStatus]);

  const agents = useMemo(
    () => Array.from(reduced.agentsByName.values()),
    [reduced.agentsByName],
  );

  // Derive progress when no progress_update event has been seen.
  const derivedProgress = useMemo(() => {
    if (reduced.progress > 0) return reduced.progress;
    const completed = agents.filter((a) => a.status === "completed").length;
    const inProgress = agents.filter((a) => a.status === "in_progress").length;
    const partial = completed + inProgress * 0.5;
    return Math.min(1, partial / PROGRESS_AGENT_COUNT);
  }, [reduced.progress, agents]);

  // Resolve the live run status: terminal SSE event > RunDetail.status >
  // 'queued' default.
  const runStatus: RunStatus =
    reduced.terminalStatus ?? runQuery.data?.status ?? "queued";

  const errorMessage =
    reduced.errorMessage ?? runQuery.data?.error_message ?? undefined;

  const finalRating: Rating | undefined =
    reduced.finalRating ?? runQuery.data?.rating ?? undefined;

  return {
    run: runQuery.data,
    agents,
    messages: reduced.messages,
    reportSections: reduced.reportSections,
    toolCalls: reduced.toolCalls,
    investmentDebate: reduced.investmentDebate,
    riskDebate: reduced.riskDebate,
    analystWallTimes: reduced.analystWallTimes,
    stats: reduced.stats,
    progress: derivedProgress,
    finalRating,
    runStatus,
    errorMessage,
    llmCallPending: reduced.llmCallPending,
    sseState: sse.state,
    reconnect: () => {
      sse.close();
      setSseKey((k) => k + 1);
    },
  };
}
