/**
 * The Defaults card shows the saved defaults the server returns.
 *
 * Regression pinned here: after the React 19 / Radix Select upgrade the
 * Research depth select showed "No default" although the server returned
 * research_depth: 1. Found by comparing before/after screenshots of
 * /settings against the same database.
 *
 * Mock style mirrors ``UsersCard.test.tsx``: stub the API boundary.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { UserDefaults } from "@/lib/types";

const SAVED_DEFAULTS: UserDefaults = {
  llm_provider: "openai",
  quick_think_llm: null,
  deep_think_llm: null,
  research_depth: 1,
  analysts: ["market", "social"],
  output_language: null,
  thinking_config: null,
  enable_checkpoint: true,
  updated_at: null,
} as UserDefaults;

const RESPONSES: Record<string, unknown> = {
  "/api/settings/defaults": SAVED_DEFAULTS,
  "/api/settings/api-keys": [],
  "/api/catalog/providers": [
    { key: "openai", label: "OpenAI", requires_api_key: true, api_key_env: "OPENAI_API_KEY" },
  ],
  "/api/catalog/languages": [{ code: "English", label: "English" }],
  "/api/catalog/analysts": [
    { key: "market", label: "Market Analyst" },
    { key: "social", label: "Sentiment Analyst" },
  ],
  "/api/catalog/models": [],
};

const getSpy = vi.fn(async (path: unknown) => {
  if (typeof path !== "string") throw new Error(`GET called without a path: ${String(path)}`);
  const key = Object.keys(RESPONSES).find((p) => path.startsWith(p));
  if (!key) throw new Error(`unexpected GET ${path}`);
  return RESPONSES[key];
});

// jsdom lacks the layout APIs Radix Select calls.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.releasePointerCapture ??= () => {};
Element.prototype.scrollIntoView ??= () => {};

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: { ...actual.api, get: (path: string) => getSpy(path) },
  };
});

vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u-1", username: "someone", role: "user" } }),
}));

import Settings from "@/routes/Settings";

function renderSettings() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <Settings />
    </QueryClientProvider>,
  );
}

describe("Settings defaults card", () => {
  beforeEach(() => {
    getSpy.mockClear();
    RESPONSES["/api/settings/defaults"] = SAVED_DEFAULTS;
  });

  it("shows the saved research depth and provider", async () => {
    renderSettings();

    const depth = await screen.findByRole("combobox", { name: /research depth/i });
    await waitFor(() => expect(depth.textContent).toContain("Shallow (1)"));
    expect(screen.getByRole("combobox", { name: /llm provider/i }).textContent).toContain("OpenAI");
  });

  it("names a saved provider that is not configured instead of hiding it", async () => {
    // The saved default can outlive the provider's configuration (e.g. the
    // Ollama URL was removed). The select must still show what is saved,
    // not a blank trigger, and not a misleading "No default".
    RESPONSES["/api/settings/defaults"] = { ...SAVED_DEFAULTS, llm_provider: "ollama" };
    renderSettings();

    const provider = await screen.findByRole("combobox", { name: /llm provider/i });
    await waitFor(() => expect(provider.textContent).toContain("ollama (not configured)"));
  });
});
