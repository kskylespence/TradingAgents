/**
 * RunView — in-run heartbeat rendering (Layer 4 resilience).
 *
 * The backend emits ``llm_call_pending`` events at ~30s intervals while a
 * single LLM call is outstanding. The frontend's job is to surface that
 * to the operator as an inline status row near the active agent so they
 * see "still waiting on this call (60s elapsed)" rather than going silent
 * for the full retry envelope.
 *
 * Contract pinned by these tests:
 *   - When ``useRun`` exposes ``llmCallPending``, RunView renders an inline
 *     row with the agent + model + elapsed seconds.
 *   - When the heartbeat is cleared (next non-heartbeat event arrived),
 *     the row disappears. The reducer test pins the clearing logic; this
 *     test confirms the UI reads ``undefined`` correctly.
 *   - When ``soft_warning`` is true, the row carries a distinguishing
 *     ``data-testid="heartbeat-warning"`` so styling tests (and visual
 *     inspection) can pick it out.
 *
 * We mock at three boundaries (matching RunView.resume.test.tsx):
 *   1. ``react-router-dom`` — replace ``useNavigate`` with a spy.
 *   2. ``@/lib/api`` — stub api.* to no-ops (we don't fire any requests).
 *   3. ``@/hooks/useRun`` — return a deterministic snapshot with a
 *      heartbeat (or not). Each test calls ``setUseRunReturn`` to flip
 *      the snapshot before mounting.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LlmCallPendingEvent, RunDetail } from "@/lib/types";

// ---- Mocks (declare BEFORE importing RunView) -------------------------- //

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return { ...actual, useNavigate: () => vi.fn() };
});

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return {
    ...actual,
    api: {
      ...actual.api,
      post: vi.fn(),
      get: vi.fn().mockResolvedValue({}),
      del: vi.fn(),
      put: vi.fn(),
    },
  };
});

// We control what useRun returns from each test so the heartbeat field can
// be a heartbeat event, undefined, or a soft-warning heartbeat.
let _useRunReturn: ReturnType<() => UseRunMock>;

interface UseRunMock {
  run: RunDetail | undefined;
  agents: never[];
  messages: never[];
  reportSections: Record<string, string>;
  toolCalls: never[];
  investmentDebate: undefined;
  riskDebate: undefined;
  analystWallTimes: Record<string, number>;
  stats: undefined;
  progress: number;
  finalRating: undefined;
  runStatus: "running" | "completed" | "queued";
  errorMessage: undefined;
  llmCallPending: LlmCallPendingEvent | undefined;
  sseState: "open" | "closed";
  reconnect: () => void;
}

function defaultUseRunReturn(
  override: Partial<UseRunMock> = {},
): UseRunMock {
  return {
    run: {
      id: "run-1",
      ticker: "SPY",
      asset_type: "stock",
      analysis_date: "2026-05-19",
      status: "running",
      rating: null,
      llm_provider: "ollama",
      research_depth: 1,
      started_at: "2026-05-19T12:00:00Z",
      finished_at: null,
      created_at: "2026-05-19T12:00:00Z",
      elapsed_seconds: 60,
      analysts: ["market"],
      quick_think_llm: "kimi-k2-thinking",
      deep_think_llm: "gpt-oss:120b",
      thinking_config: null,
      output_language: "English",
      checkpoint_enabled: true,
      decision_full: null,
      report_dir: null,
      error_message: null,
      stats: null,
      resumable: false,
    },
    agents: [],
    messages: [],
    reportSections: {},
    toolCalls: [],
    investmentDebate: undefined,
    riskDebate: undefined,
    analystWallTimes: {},
    stats: undefined,
    progress: 0.3,
    finalRating: undefined,
    runStatus: "running",
    errorMessage: undefined,
    llmCallPending: undefined,
    sseState: "open",
    reconnect: () => {},
    ...override,
  };
}

vi.mock("@/hooks/useRun", () => ({
  useRun: () => _useRunReturn,
}));

import RunView from "@/routes/RunView";

// ---- Helpers ----------------------------------------------------------- //

function renderRunViewAt(path: string) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/runs/:runId" element={<RunView />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  _useRunReturn = defaultUseRunReturn();
});

afterEach(() => {
  vi.clearAllMocks();
});

// ---- Tests ------------------------------------------------------------- //

describe("RunView — llm_call_pending heartbeat", () => {
  it("renders an inline row with agent, model, and elapsed seconds when llmCallPending is set", () => {
    const heartbeat: LlmCallPendingEvent = {
      seq: 42,
      type: "llm_call_pending",
      model: "kimi-k2-thinking",
      agent: "Fundamentals Analyst",
      elapsed_seconds: 60,
      soft_warning: false,
    };
    _useRunReturn = defaultUseRunReturn({ llmCallPending: heartbeat });

    renderRunViewAt("/runs/run-1");

    // The whole row is reachable by a stable test id so the underlying
    // markup can evolve without breaking the test.
    const row = screen.getByTestId("heartbeat-row");
    expect(row).not.toBeNull();
    expect(row.textContent ?? "").toMatch(/Fundamentals Analyst/i);
    expect(row.textContent ?? "").toMatch(/kimi-k2-thinking/);
    expect(row.textContent ?? "").toMatch(/60s/);
    // Not in warning state — the warning testid should NOT be present.
    expect(screen.queryByTestId("heartbeat-warning")).toBeNull();
  });

  it("hides the heartbeat row when the next event clears llmCallPending", () => {
    // First render with a heartbeat in flight…
    _useRunReturn = defaultUseRunReturn({
      llmCallPending: {
        seq: 5,
        type: "llm_call_pending",
        model: "gpt-oss:120b",
        agent: "Market Analyst",
        elapsed_seconds: 30,
        soft_warning: false,
      },
    });
    const { rerender } = renderRunViewAt("/runs/run-1");
    expect(screen.queryByTestId("heartbeat-row")).not.toBeNull();

    // …then clear it (the reducer does this when a non-heartbeat event
    // arrives). Re-render with the same wrapping but the new useRun
    // return value — we have to re-execute the mock by reassigning the
    // module-level snapshot before rerender triggers a hooks read.
    _useRunReturn = defaultUseRunReturn({ llmCallPending: undefined });
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          <Routes>
            <Route path="/runs/:runId" element={<RunView />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.queryByTestId("heartbeat-row")).toBeNull();
  });

  it("marks the row with the warning testid when soft_warning is true", () => {
    _useRunReturn = defaultUseRunReturn({
      llmCallPending: {
        seq: 7,
        type: "llm_call_pending",
        model: "kimi-k2-thinking",
        agent: "Bull Researcher",
        elapsed_seconds: 90,
        soft_warning: true,
      },
    });

    renderRunViewAt("/runs/run-1");

    const warningRow = screen.getByTestId("heartbeat-warning");
    expect(warningRow).not.toBeNull();
    expect(warningRow.textContent ?? "").toMatch(/Bull Researcher/i);
    expect(warningRow.textContent ?? "").toMatch(/90s/);
  });
});
