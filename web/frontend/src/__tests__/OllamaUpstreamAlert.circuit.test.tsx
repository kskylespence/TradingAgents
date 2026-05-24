/**
 * Tests the circuit-breaker-state rendering branch of OllamaUpstreamAlert.
 *
 * v0.2.5+hf.4 added a shared resilient HTTP client (`upstream_http`)
 * that runs every Ollama call through a circuit breaker. The breaker
 * exposes its state on `/api/health` (`ollama.circuit_state`) so the
 * frontend can render three nuanced visual states instead of a binary
 * "down / not-down":
 *
 *   - `closed` + `status: "ok"`  → no alert
 *   - `closed` + `status: "down"` → red alert (sustained outage)
 *   - `open`                      → red "cooling down" with countdown
 *   - `half_open`                 → yellow "recovering" pill
 *
 * Hysteresis (also in hf.4) means the backend's `status` field never
 * flips to "down" on a single transient — so the legacy "1 failure → red"
 * branch is intentionally deleted from the alert.
 *
 * These tests pin the new rendering rules.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { OllamaUpstreamAlert } from "@/components/OllamaUpstreamAlert";
import type { OllamaHealth } from "@/lib/types";

const _health = (overrides: Partial<OllamaHealth> = {}): OllamaHealth => ({
  status: "ok",
  url: "https://ollama.com/v1",
  model_count: 39,
  error: null,
  recent_attempts: [],
  circuit_state: "closed",
  ...overrides,
});

afterEach(() => {
  cleanup();
});

describe("OllamaUpstreamAlert — circuit-state branches", () => {
  it("renders a yellow recovering pill when circuit_state === 'half_open'", () => {
    render(
      <OllamaUpstreamAlert
        provider="ollama"
        health={_health({ status: "ok", circuit_state: "half_open" })}
      />,
    );
    const alert = screen.getByRole("status");
    expect(alert.textContent?.toLowerCase()).toMatch(/recover/);
    // Yellow palette — Tailwind's amber/warning. The alert is NOT red
    // (red is the destructive class for sustained-down).
    expect(alert.className).toMatch(/amber|warning|yellow/);
    expect(alert.className).not.toMatch(/destructive/);
  });

  it("renders a cool-down notice when circuit_state === 'open'", () => {
    render(
      <OllamaUpstreamAlert
        provider="ollama"
        health={_health({
          status: "down",
          circuit_state: "open",
          error: "ConnectTimeout('')",
        })}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent?.toLowerCase()).toMatch(/cool|cooldown|cool-down/);
    expect(alert.className).toMatch(/destructive/);
  });

  it("renders nothing when status is 'ok' and circuit_state is 'closed'", () => {
    const { container } = render(
      <OllamaUpstreamAlert
        provider="ollama"
        health={_health({ status: "ok", circuit_state: "closed" })}
      />,
    );
    expect(container.textContent).toBe("");
  });
});
