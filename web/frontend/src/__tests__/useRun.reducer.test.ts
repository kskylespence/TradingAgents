/**
 * Unit tests for the useRun reducer pipeline.
 *
 * The reducer is exported as `_reducer_for_tests` (and the per-event
 * applier as `_applyEvent_for_tests`) precisely so we can exercise it
 * without dragging in the full SSE + React Query lifecycle.
 */
import { describe, it, expect } from "vitest";

import {
  _applyEvent_for_tests as applyEvent,
  _reducer_for_tests as reducer,
} from "@/hooks/useRun";
import type {
  AgentStatusEvent,
  AnalystWallTimeEvent,
  InvestmentDebateEvent,
  MessageEvent as RunMessageEvent,
  ProgressUpdateEvent,
  ReportSectionEvent,
  RiskDebateEvent,
  RunCancelledEvent,
  RunCompletedEvent,
  RunFailedEvent,
  RunStartedEvent,
  StatsEvent,
  ToolCallEvent,
} from "@/lib/types";

const initial = () =>
  reducer(undefined as never, { type: "reset" });

describe("useRun reducer — reset", () => {
  it("returns a fresh ReducerState", () => {
    const s = initial();
    expect(s.agentsByName.size).toBe(0);
    expect(s.messages).toEqual([]);
    expect(s.reportSections).toEqual({});
    expect(s.toolCalls).toEqual([]);
    expect(s.investmentDebate).toBeUndefined();
    expect(s.riskDebate).toBeUndefined();
    expect(s.analystWallTimes).toEqual({});
    expect(s.stats).toBeUndefined();
    expect(s.progress).toBe(0);
    expect(s.finalRating).toBeUndefined();
    expect(s.terminalStatus).toBeUndefined();
    expect(s.errorMessage).toBeUndefined();
  });

  it("reset after apply discards state", () => {
    const after = reducer(initial(), {
      type: "apply",
      events: [
        {
          seq: 1,
          type: "agent_status",
          agent: "Market Analyst",
          status: "completed",
        } as AgentStatusEvent,
      ],
    });
    expect(after.agentsByName.size).toBe(1);
    const cleared = reducer(after, { type: "reset" });
    expect(cleared.agentsByName.size).toBe(0);
  });
});

describe("useRun reducer — apply (per-event semantics)", () => {
  it("run_started is a no-op for the reducer (RunDetail is the source)", () => {
    const ev: RunStartedEvent = {
      seq: 1,
      type: "run_started",
      ticker: "SPY",
      asset_type: "stock",
      analysis_date: "2026-05-19",
      analysts: ["market"],
      research_depth: 1,
      llm_provider: "openai",
      quick_think_llm: "q",
      deep_think_llm: "d",
      output_language: "English",
      checkpoint_enabled: false,
      thinking_config: null,
    };
    const s = reducer(initial(), { type: "apply", events: [ev] });
    // Should be observably unchanged from initial.
    expect(s).toMatchObject({
      agentsByName: new Map(),
      messages: [],
      reportSections: {},
      progress: 0,
    });
  });

  it("agent_status collapses by agent name (latest wins)", () => {
    const a1: AgentStatusEvent = {
      seq: 1,
      type: "agent_status",
      agent: "Market Analyst",
      status: "in_progress",
    };
    const a2: AgentStatusEvent = {
      seq: 2,
      type: "agent_status",
      agent: "Market Analyst",
      status: "completed",
    };
    const s = reducer(initial(), { type: "apply", events: [a1, a2] });
    expect(s.agentsByName.size).toBe(1);
    expect(s.agentsByName.get("Market Analyst")?.status).toBe("completed");
  });

  it("progress_update clamps to [0, 1]", () => {
    const tooLow: ProgressUpdateEvent = {
      seq: 1,
      type: "progress_update",
      progress: -0.5,
      step: "x",
    };
    const tooHigh: ProgressUpdateEvent = {
      seq: 2,
      type: "progress_update",
      progress: 99,
      step: "y",
    };
    const s1 = reducer(initial(), { type: "apply", events: [tooLow] });
    expect(s1.progress).toBe(0);
    const s2 = reducer(s1, { type: "apply", events: [tooHigh] });
    expect(s2.progress).toBe(1);
  });

  it("analyst_wall_time stores wire-key -> seconds", () => {
    const ev: AnalystWallTimeEvent = {
      seq: 1,
      type: "analyst_wall_time",
      key: "social",
      label: "Sentiment Analyst",
      seconds: 12.3,
    };
    const s = reducer(initial(), { type: "apply", events: [ev] });
    expect(s.analystWallTimes.social).toBe(12.3);
  });

  it("tool_call appends and caps at 50 (drops oldest)", () => {
    const evs: ToolCallEvent[] = Array.from({ length: 60 }, (_, i) => ({
      seq: i + 1,
      type: "tool_call",
      name: `tool-${i}`,
      args: {},
      timestamp: "00:00:00",
    }));
    const s = reducer(initial(), { type: "apply", events: evs });
    expect(s.toolCalls).toHaveLength(50);
    // The OLDEST 10 should have been dropped — keep most-recent 50.
    expect(s.toolCalls[0].name).toBe("tool-10");
    expect(s.toolCalls[49].name).toBe("tool-59");
  });

  it("message events append in order", () => {
    const m1: RunMessageEvent = {
      seq: 1,
      type: "message",
      kind: "Agent",
      content: "first",
      timestamp: "00:00:00",
    };
    const m2: RunMessageEvent = {
      seq: 2,
      type: "message",
      kind: "Data",
      content: "second",
      timestamp: "00:00:01",
    };
    const s = reducer(initial(), { type: "apply", events: [m1, m2] });
    expect(s.messages).toHaveLength(2);
    expect(s.messages[0].content).toBe("first");
    expect(s.messages[1].kind).toBe("Data");
  });

  it("report_section: latest content per section key wins", () => {
    const r1: ReportSectionEvent = {
      seq: 1,
      type: "report_section",
      section: "market_report",
      content: "v1",
    };
    const r2: ReportSectionEvent = {
      seq: 2,
      type: "report_section",
      section: "market_report",
      content: "v2",
    };
    const r3: ReportSectionEvent = {
      seq: 3,
      type: "report_section",
      section: "news_report",
      content: "n1",
    };
    const s = reducer(initial(), { type: "apply", events: [r1, r2, r3] });
    expect(s.reportSections.market_report).toBe("v2");
    expect(s.reportSections.news_report).toBe("n1");
  });

  it("investment_debate + risk_debate: latest snapshot wins", () => {
    const inv: InvestmentDebateEvent = {
      seq: 1,
      type: "investment_debate",
      bull: "buy",
      bear: "sell",
      judge: "buy",
    };
    const risk: RiskDebateEvent = {
      seq: 2,
      type: "risk_debate",
      aggressive: "max",
      judge: "moderate",
    };
    const s = reducer(initial(), { type: "apply", events: [inv, risk] });
    expect(s.investmentDebate).toEqual(inv);
    expect(s.riskDebate).toEqual(risk);
  });

  it("stats: latest snapshot wins", () => {
    const s1: StatsEvent = {
      seq: 1,
      type: "stats",
      llm_calls: 1,
      tool_calls: 0,
      tokens_in: 100,
      tokens_out: 50,
      elapsed_seconds: 1.0,
    };
    const s2: StatsEvent = {
      seq: 2,
      type: "stats",
      llm_calls: 3,
      tool_calls: 2,
      tokens_in: 500,
      tokens_out: 200,
      elapsed_seconds: 5.0,
    };
    const out = reducer(initial(), { type: "apply", events: [s1, s2] });
    expect(out.stats?.llm_calls).toBe(3);
    expect(out.stats?.elapsed_seconds).toBe(5.0);
  });

  it("run_completed: sets rating, terminal status, and forces progress=1", () => {
    const ev: RunCompletedEvent = {
      seq: 1,
      type: "run_completed",
      rating: "Buy",
      report_dir: "/r",
      finished_at: "2026-05-19T12:00:00Z",
    };
    const s = reducer(initial(), { type: "apply", events: [ev] });
    expect(s.finalRating).toBe("Buy");
    expect(s.terminalStatus).toBe("completed");
    expect(s.progress).toBe(1);
  });

  it("run_failed: stores error + terminal status", () => {
    const ev: RunFailedEvent = {
      seq: 1,
      type: "run_failed",
      error: "boom",
    };
    const s = reducer(initial(), { type: "apply", events: [ev] });
    expect(s.errorMessage).toBe("boom");
    expect(s.terminalStatus).toBe("failed");
  });

  it("run_cancelled: terminal status only", () => {
    const ev: RunCancelledEvent = {
      seq: 1,
      type: "run_cancelled",
      at_node: "Trader",
    };
    const s = reducer(initial(), { type: "apply", events: [ev] });
    expect(s.terminalStatus).toBe("cancelled");
    expect(s.errorMessage).toBeUndefined();
  });
});

describe("useRun reducer — incremental dispatch", () => {
  it("apply on existing state preserves prior fields and adds delta only", () => {
    const start: AgentStatusEvent = {
      seq: 1,
      type: "agent_status",
      agent: "Market Analyst",
      status: "in_progress",
    };
    const s1 = reducer(initial(), { type: "apply", events: [start] });
    expect(s1.agentsByName.size).toBe(1);

    const delta: RunMessageEvent = {
      seq: 2,
      type: "message",
      kind: "Agent",
      content: "hello",
      timestamp: "00:00:00",
    };
    const s2 = reducer(s1, { type: "apply", events: [delta] });

    // Prior agent_status still there:
    expect(s2.agentsByName.get("Market Analyst")?.status).toBe("in_progress");
    // New message added:
    expect(s2.messages).toHaveLength(1);
    expect(s2.messages[0].content).toBe("hello");
    // Reference must change (new state object for React to re-render).
    expect(s2).not.toBe(s1);
  });
});

describe("applyEvent (per-event helper) is mutation-style", () => {
  it("mutates the draft passed in and returns it", () => {
    const draft = initial();
    const ev: AgentStatusEvent = {
      seq: 1,
      type: "agent_status",
      agent: "Market Analyst",
      status: "completed",
    };
    const out = applyEvent(draft, ev);
    expect(out).toBe(draft);
    expect(draft.agentsByName.get("Market Analyst")?.status).toBe("completed");
  });
});
