/**
 * Tests the structured-validation rendering branch of OllamaUpstreamAlert.
 *
 * Layer 1 of the resilience hardening pass adds a pre-flight liveness
 * probe to ``POST /api/runs`` and ``POST /api/runs/{id}/retry``. When a
 * selected model is unresponsive on Ollama Cloud, the backend returns
 * HTTP 400 with a ``RunValidationError`` body:
 *
 *     {
 *       code: "upstream_model_unhealthy",
 *       message: "...",
 *       unhealthy_models: [{ model, status, upstream_ref }],
 *       suggested_alternatives: ["glm-5", "kimi-k2.6"]
 *     }
 *
 * Before this change, the alert only knew how to render a steady-state
 * "the /api/health probe says upstream is down" notice. Now it also
 * needs to render the per-model probe failure detail that came back
 * from the submit attempt — same component, broader contract.
 *
 * The new prop is ``validation``: a ``RunValidationError`` (or null/undefined
 * when no submit has been attempted). When ``validation`` is present, the
 * alert renders the structured detail; the steady-state health probe
 * fallback still wins when ``validation`` is null.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { OllamaUpstreamAlert } from "@/components/OllamaUpstreamAlert";
import type { RunValidationError } from "@/lib/types";

const _validation = (
  overrides: Partial<RunValidationError> = {},
): RunValidationError => ({
  code: "upstream_model_unhealthy",
  message:
    "Selected model(s) kimi-k2-thinking are not responding on Ollama Cloud.",
  unhealthy_models: [
    {
      model: "kimi-k2-thinking",
      status: "timeout",
      upstream_ref: null,
    },
  ],
  suggested_alternatives: ["glm-5", "kimi-k2.6"],
  ...overrides,
});

afterEach(() => {
  cleanup();
});

describe("OllamaUpstreamAlert (validation prop)", () => {
  it("renders the unhealthy model and its status when validation is present", () => {
    render(
      <OllamaUpstreamAlert
        provider="ollama"
        health={null}
        validation={_validation()}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/kimi-k2-thinking/);
    // The status label is humanised — "timeout" renders as "timed out".
    expect(alert.textContent).toMatch(/timed out|timeout/i);
  });

  it("renders the upstream_ref when the backend extracted one", () => {
    render(
      <OllamaUpstreamAlert
        provider="ollama"
        health={null}
        validation={_validation({
          unhealthy_models: [
            {
              model: "glm-5",
              status: "http_5xx",
              upstream_ref: "fd44ca4b-1234-5678-abcd-deadbeef",
            },
          ],
        })}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/fd44ca4b-1234-5678-abcd-deadbeef/);
  });

  it("lists the suggested alternatives when present", () => {
    render(
      <OllamaUpstreamAlert
        provider="ollama"
        health={null}
        validation={_validation()}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/glm-5/);
    expect(alert.textContent).toMatch(/kimi-k2\.6/);
  });

  it("renders even when provider is ollama and health is ok — the user just hit a probe failure", () => {
    render(
      <OllamaUpstreamAlert
        provider="ollama"
        health={{
          status: "ok",
          url: "https://ollama.com/v1",
          model_count: 5,
          error: null,
        }}
        validation={_validation()}
      />,
    );

    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("does NOT render the validation banner when provider is not ollama", () => {
    // The probe is Ollama-only — a non-Ollama provider should never
    // be carrying RunValidationError, but if it somehow is, suppress.
    render(
      <OllamaUpstreamAlert
        provider="openai"
        health={null}
        validation={_validation()}
      />,
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("falls back to health-down behaviour when validation is null", () => {
    render(
      <OllamaUpstreamAlert
        provider="ollama"
        health={{
          status: "down",
          url: "https://ollama.com/v1",
          model_count: null,
          error: "ConnectError('boom')",
        }}
        validation={null}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/Ollama upstream is unreachable/i);
  });
});
