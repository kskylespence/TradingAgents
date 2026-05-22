/**
 * Integration test for the Retry button flow on RunView.
 *
 * What this verifies:
 *   - The Retry button is rendered when ``runStatus === "failed"``
 *     (and stays hidden when the run is still ``running``).
 *   - Clicking Retry POSTs to ``/api/runs/<id>/retry``.
 *   - On success, RunView reads ``data.run_id`` and navigates to
 *     ``/runs/<new-id>`` so the user follows the sibling run.
 *   - On error, a destructive toast fires and no navigation happens.
 *
 * Mocks mirror ``RunView.resume.test.tsx`` (the resume button's sibling
 * test): react-router's ``useNavigate`` → spy, ``@/lib/api.post`` → spy,
 * and ``@/hooks/useRun`` → deterministic snapshot.
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

const toastSpy = vi.fn();

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: toastSpy }),
}));

// Make the mocked useRun return value pluggable per-test so we can
// flip status between "failed" and "running".
const useRunState = { runStatus: "failed" as string };

vi.mock("@/hooks/useRun", () => ({
  useRun: () => ({
    run: {
      id: "parent-run-id",
      ticker: "SPY",
      asset_type: "stock",
      analysis_date: "2026-05-19",
      status: useRunState.runStatus,
      rating: null,
      llm_provider: "openai",
      research_depth: 1,
      started_at: "2026-05-19T12:00:00Z",
      finished_at: "2026-05-19T12:05:00Z",
      created_at: "2026-05-19T12:00:00Z",
      elapsed_seconds: 42,
      analysts: ["market"],
      quick_think_llm: "q",
      deep_think_llm: "d",
      thinking_config: null,
      output_language: "English",
      checkpoint_enabled: false,
      decision_full: null,
      report_dir: null,
      error_message:
        useRunState.runStatus === "failed" ? "upstream blip" : null,
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
    progress: 1,
    finalRating: undefined,
    runStatus: useRunState.runStatus,
    errorMessage:
      useRunState.runStatus === "failed" ? "upstream blip" : undefined,
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

describe("RunView retry button → navigate to new run", () => {
  beforeEach(() => {
    navigateSpy.mockClear();
    postSpy.mockReset();
    toastSpy.mockReset();
    useRunState.runStatus = "failed";
  });

  it("renders the Retry button when the run failed", async () => {
    renderRunViewAt("/runs/parent-run-id");
    // There may be two copies (header + banner) — findAllByRole tolerates that.
    const buttons = await screen.findAllByRole("button", { name: /retry/i });
    expect(buttons.length).toBeGreaterThan(0);
  });

  it("does NOT render the Retry button when the run is still running", async () => {
    useRunState.runStatus = "running";
    renderRunViewAt("/runs/parent-run-id");
    // Give the render a tick to settle.
    await screen.findByRole("button", { name: /cancel/i });
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("calls /retry, reads the returned run_id, and navigates to /runs/<new-id>", async () => {
    postSpy.mockResolvedValueOnce({
      run_id: "new-retried-run-id",
      parent_run_id: "parent-run-id",
    });

    renderRunViewAt("/runs/parent-run-id");

    const retryBtns = await screen.findAllByRole("button", { name: /retry/i });
    await userEvent.click(retryBtns[0]);

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith("/api/runs/parent-run-id/retry");
    });
    await waitFor(() => {
      expect(navigateSpy).toHaveBeenCalledWith("/runs/new-retried-run-id");
    });
  });

  it("shows a destructive toast and does not navigate when /retry fails", async () => {
    postSpy.mockRejectedValueOnce(new Error("retry failed"));

    renderRunViewAt("/runs/parent-run-id");

    const retryBtns = await screen.findAllByRole("button", { name: /retry/i });
    await userEvent.click(retryBtns[0]);

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalled();
      const call = toastSpy.mock.calls[toastSpy.mock.calls.length - 1][0];
      expect(call.variant).toBe("destructive");
    });
    // Give onError a tick.
    await new Promise((r) => setTimeout(r, 50));
    expect(navigateSpy).not.toHaveBeenCalled();
  });
});
