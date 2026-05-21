/**
 * Integration test for the Resume button flow on RunView.
 *
 * What this verifies (the gap the code review caught):
 *   - The backend's resume endpoint returns ``{run_id, parent_run_id}``.
 *   - RunView's ``resumeMutation`` reads ``data.run_id`` from the response.
 *   - On success it calls ``navigate(`/runs/${data.run_id}`)`` so the user
 *     follows the resumed run instead of staying on the parent's dead stream.
 *
 * We mock at three boundaries:
 *   1. ``@/hooks/useRun`` — return an interrupted+resumable run snapshot
 *      so the Resume button is rendered.
 *   2. ``@/lib/api`` — stub ``api.post`` to resolve the resume response.
 *   3. ``react-router-dom`` — replace ``useNavigate`` with a vi.fn spy.
 *
 * The other RunView mounts (charts, message log, report panel) work in
 * the mocked state because the mocked useRun returns realistic defaults.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

// ---- Mocks (declare BEFORE importing RunView) ---------------------------- //

const navigateSpy = vi.fn();

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    ...actual,
    useNavigate: () => navigateSpy,
  };
});

const postSpy = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return {
    ...actual,
    api: {
      ...actual.api,
      post: (...args: unknown[]) => postSpy(...args),
      get: vi.fn().mockResolvedValue({}),
      del: vi.fn(),
      put: vi.fn(),
    },
  };
});

// Replace useRun with a deterministic snapshot so the Resume button is
// rendered (runStatus="interrupted" OR run.resumable=true).
vi.mock("@/hooks/useRun", () => ({
  useRun: () => ({
    run: {
      id: "parent-run-id",
      ticker: "SPY",
      asset_type: "stock",
      analysis_date: "2026-05-19",
      status: "interrupted",
      rating: null,
      llm_provider: "openai",
      research_depth: 1,
      started_at: "2026-05-19T12:00:00Z",
      finished_at: null,
      created_at: "2026-05-19T12:00:00Z",
      elapsed_seconds: 42,
      analysts: ["market"],
      quick_think_llm: "q",
      deep_think_llm: "d",
      thinking_config: null,
      output_language: "English",
      checkpoint_enabled: true,
      decision_full: null,
      report_dir: null,
      error_message: null,
      stats: null,
      resumable: true,
    },
    agents: [],
    messages: [],
    reportSections: {},
    toolCalls: [],
    investmentDebate: undefined,
    riskDebate: undefined,
    analystWallTimes: {},
    stats: undefined,
    progress: 0.5,
    finalRating: undefined,
    runStatus: "interrupted",
    errorMessage: undefined,
    sseState: "closed",
    reconnect: () => {},
  }),
}));

import RunView from "@/routes/RunView";

// ------------------------------------------------------------------------ //

function renderRunViewAt(path: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
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

describe("RunView resume button → navigate to new run", () => {
  beforeEach(() => {
    navigateSpy.mockClear();
    postSpy.mockReset();
  });

  it("calls /resume, reads the returned run_id, and navigates to /runs/<new-id>", async () => {
    postSpy.mockResolvedValueOnce({
      run_id: "new-resumed-run-id",
      parent_run_id: "parent-run-id",
    });

    renderRunViewAt("/runs/parent-run-id");

    const resumeBtn = await screen.findByRole("button", { name: /resume/i });
    await userEvent.click(resumeBtn);

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith("/api/runs/parent-run-id/resume");
    });
    await waitFor(() => {
      expect(navigateSpy).toHaveBeenCalledWith("/runs/new-resumed-run-id");
    });
  });

  it("does NOT navigate when /resume fails", async () => {
    postSpy.mockRejectedValueOnce(new Error("resume failed"));

    renderRunViewAt("/runs/parent-run-id");

    const resumeBtn = await screen.findByRole("button", { name: /resume/i });
    await userEvent.click(resumeBtn);

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalled();
    });
    // Give onError a tick.
    await new Promise((r) => setTimeout(r, 50));
    expect(navigateSpy).not.toHaveBeenCalled();
  });
});
