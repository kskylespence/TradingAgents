/**
 * Tests the inline "Ollama upstream is unreachable" alert on NewRun.
 *
 * The alert is the user-visible safety net for the Ollama Cloud 404
 * fix — without it, the only signal a user gets that the backend
 * can't reach Ollama is an engine failure ~10s after submit. With it,
 * they see the upstream-down state BEFORE submit and can fix the env
 * var instead of waiting for a doomed run.
 *
 * We test the extracted `OllamaUpstreamAlert` component directly
 * rather than mounting `NewRun`. NewRun has ~10 useEffects, three
 * Radix Select trees, and React Query mutation wiring that combine
 * to make jsdom hang on render in this project's vitest config (the
 * 300ms debounce timer + React Query's internal scheduler leave the
 * event loop pinned). Testing the pure-presentational component
 * gives us the same coverage of the visibility logic without that
 * test-host complexity.
 *
 * Three branches in the visibility logic:
 *   1. provider="ollama" + ollama.status="down"    → alert visible
 *   2. provider="ollama" + ollama.status="unknown" → no alert
 *   3. provider="openai" + ollama.status="down"    → no alert
 * Plus the null-health / missing-error fallback paths.
 */
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { OllamaUpstreamAlert } from "@/components/OllamaUpstreamAlert";
import type { OllamaHealth } from "@/lib/types";

function _h(
  status: OllamaHealth["status"],
  error: string | null = null,
): OllamaHealth {
  return {
    status,
    url: "https://ollama.com/v1",
    model_count: status === "ok" ? 1 : null,
    error,
  };
}

afterEach(() => {
  cleanup();
});

describe("OllamaUpstreamAlert", () => {
  it("renders the destructive alert when provider=ollama and status=down", () => {
    render(
      <OllamaUpstreamAlert
        provider="ollama"
        health={_h("down", "ConnectError('upstream unreachable')")}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/Ollama upstream is unreachable/i);
    expect(alert.textContent).toMatch(/ConnectError\('upstream unreachable'\)/);
    expect(alert.textContent).toMatch(/OLLAMA_BASE_URL/);
  });

  it("falls back to a generic message when health.error is null", () => {
    render(
      <OllamaUpstreamAlert provider="ollama" health={_h("down", null)} />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/probe to OLLAMA_BASE_URL failed/i);
  });

  it("does NOT render when ollama.status=unknown (avoid alert fatigue on cold start)", () => {
    render(<OllamaUpstreamAlert provider="ollama" health={_h("unknown")} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does NOT render when ollama.status=ok (including ok with zero models)", () => {
    render(<OllamaUpstreamAlert provider="ollama" health={_h("ok")} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does NOT render when provider is not ollama, even if status=down", () => {
    render(<OllamaUpstreamAlert provider="openai" health={_h("down")} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does NOT render when health data is null (initial loading state)", () => {
    render(<OllamaUpstreamAlert provider="ollama" health={null} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does NOT render when health data is undefined (no probe yet)", () => {
    render(<OllamaUpstreamAlert provider="ollama" health={undefined} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
