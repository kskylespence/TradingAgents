/**
 * Tests the curated-first sort + non-curated warning badge that NewRun's
 * model pickers use for Ollama models.
 *
 * Why this test exists
 * --------------------
 * Ollama Cloud's `/v1/models` advertises models that aren't in Ollama's
 * actively-curated cloud catalog. Two of those (`kimi-k2-thinking`,
 * `qwen3-coder:480b`) have publicly tracked reliability issues
 * (ollama/ollama#15453, #14542). The backend now flags every Ollama
 * model with `curated: boolean`; the frontend uses that to (1) sort
 * curated options first in the dropdown and (2) prefix the option
 * label with a `WARN` badge so users can make an informed choice
 * before submitting. The catalog endpoint omits the field for non-
 * Ollama providers — we treat missing as "curated" (no badge) so the
 * old static-catalog providers don't suddenly get warning badges.
 *
 * Why we don't mount NewRun directly
 * ----------------------------------
 * NewRun has ~10 useEffects, three Radix Select trees, and React
 * Query mutation wiring that combine to make jsdom hang in this
 * project's vitest config (the 300ms ticker debounce + React Query's
 * internal scheduler leave the event loop pinned). The same reason
 * `NewRun.ollamaAlert.test.tsx` tests `OllamaUpstreamAlert` directly:
 * we test the pure sort + badge functions and the small label
 * component, which give us the same coverage of the logic without
 * the test-host complexity.
 */
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  ModelOptionLabel,
  sortCuratedFirst,
} from "@/components/ModelOptionLabel";
import type { CatalogModel } from "@/lib/types";

function _m(id: string, curated?: boolean): CatalogModel {
  return {
    id,
    label: id,
    allows_custom: false,
    ...(curated === undefined ? {} : { curated }),
  };
}

afterEach(() => {
  cleanup();
});

describe("sortCuratedFirst", () => {
  it("places curated models before non-curated models", () => {
    const models = [_m("kimi-k2-thinking", false), _m("glm-5", true)];
    const sorted = sortCuratedFirst(models);
    expect(sorted.map((m) => m.id)).toEqual(["glm-5", "kimi-k2-thinking"]);
  });

  it("preserves relative order within each curated bucket", () => {
    const models = [
      _m("glm-5", true),
      _m("kimi-k2-thinking", false),
      _m("glm-5.1", true),
      _m("qwen3-coder:480b", false),
    ];
    const sorted = sortCuratedFirst(models);
    expect(sorted.map((m) => m.id)).toEqual([
      "glm-5",
      "glm-5.1",
      "kimi-k2-thinking",
      "qwen3-coder:480b",
    ]);
  });

  it("treats undefined (field omitted, e.g. non-Ollama backend) as curated", () => {
    // Back-compat: old API responses and non-Ollama providers omit the
    // field entirely. We must not retroactively badge models from
    // providers that have no curated/non-curated distinction.
    const models = [_m("gpt-4o-mini", undefined), _m("o1-preview", undefined)];
    const sorted = sortCuratedFirst(models);
    expect(sorted.map((m) => m.id)).toEqual(["gpt-4o-mini", "o1-preview"]);
  });

  it("does not mutate the input array", () => {
    const models = [_m("kimi-k2-thinking", false), _m("glm-5", true)];
    const ids_before = models.map((m) => m.id);
    sortCuratedFirst(models);
    expect(models.map((m) => m.id)).toEqual(ids_before);
  });
});

describe("ModelOptionLabel", () => {
  it("prefixes the label with the WARN badge when curated=false", () => {
    render(<ModelOptionLabel model={_m("kimi-k2-thinking", false)} />);
    const badge = screen.getByTestId("deprioritized-badge");
    expect(badge).toBeDefined();
    // The unicode warning sign is the visible affordance.
    expect(badge.textContent).toMatch(/⚠/);
    // The tooltip text explains the risk so the user can hover to learn why.
    expect(badge.getAttribute("title")).toMatch(/curated/i);
  });

  it("does NOT render the badge when curated=true", () => {
    render(<ModelOptionLabel model={_m("glm-5", true)} />);
    expect(screen.queryByTestId("deprioritized-badge")).toBeNull();
  });

  it("does NOT render the badge when curated is undefined (non-Ollama provider)", () => {
    render(<ModelOptionLabel model={_m("gpt-4o-mini", undefined)} />);
    expect(screen.queryByTestId("deprioritized-badge")).toBeNull();
  });

  it("renders the model label text in all three cases", () => {
    const { rerender } = render(
      <ModelOptionLabel model={_m("kimi-k2-thinking", false)} />,
    );
    expect(screen.getByText("kimi-k2-thinking")).toBeDefined();

    rerender(<ModelOptionLabel model={_m("glm-5", true)} />);
    expect(screen.getByText("glm-5")).toBeDefined();

    rerender(<ModelOptionLabel model={_m("gpt-4o-mini", undefined)} />);
    expect(screen.getByText("gpt-4o-mini")).toBeDefined();
  });
});
