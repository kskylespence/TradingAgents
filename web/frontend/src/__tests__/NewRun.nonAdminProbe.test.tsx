/**
 * A standard (non-admin) user must SEE why their run was refused.
 *
 * On 2026-11-14 a standard user pressed "Start analysis" twice; both
 * times `POST /api/runs` answered 400 `upstream_model_unhealthy`.
 * NewRun stored the error but rendered the alert only inside the
 * admin-only block, and the 400 branch skips the toast, so the page
 * showed nothing at all.
 *
 * Unlike the other NewRun tests this one mounts the real page: the bug
 * was WHERE NewRun places the alert, which a component-only test can't
 * see. Every data hook is mocked to static data, so no React Query
 * fetch or timer is left running.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunValidationError } from "@/lib/types";

const postMock = vi.fn();

// Each hook returns the SAME object on every call. NewRun's effects
// depend on `query.data`; a fresh array per render re-fires them and
// sets state forever, which is what hung earlier attempts to mount it.
const stable = vi.hoisted(() => ({
  empty: { data: [], isLoading: false },
  none: { data: undefined },
  analysts: {
    data: [{ key: "market", label: "Market Analyst" }],
    isLoading: false,
  },
}));

vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => false }));
vi.mock("@/hooks/useHealth", () => ({ useHealth: () => stable.none }));
vi.mock("@/hooks/useUserDefaults", () => ({
  useUserDefaults: () => stable.none,
}));
vi.mock("@/hooks/useCatalog", () => ({
  useProviders: () => stable.empty,
  useModels: () => stable.empty,
  useLanguages: () => stable.empty,
  useAnalysts: () => stable.analysts,
}));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, post: (...a: unknown[]) => postMock(...a) } };
});

import { ApiError } from "@/lib/api";
import NewRun from "@/routes/NewRun";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <NewRun />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function rejectWith(validation: RunValidationError) {
  postMock.mockRejectedValueOnce(
    new ApiError(400, "Bad Request", { detail: validation }),
  );
}

async function submit() {
  fireEvent.change(screen.getByLabelText("Ticker"), {
    target: { value: "NVDA" },
  });
  // Analysts are seeded by an effect once the (mocked) catalog is read.
  await screen.findByRole("checkbox", { checked: true });
  fireEvent.click(screen.getByRole("button", { name: /start analysis/i }));
  // Proves the form really submitted, so "no alert" can't mean "no submit".
  await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
}

beforeEach(() => {
  postMock.mockReset();
  // Radix Checkbox measures itself; jsdom has no ResizeObserver.
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

afterEach(() => {
  cleanup();
});

describe("NewRun as a standard user", () => {
  it("shows a plain alert when the models are not answering", async () => {
    rejectWith({
      code: "upstream_model_unhealthy",
      message: "Selected model(s) glm-5.3-flash are not responding.",
      unhealthy_models: [
        { model: "glm-5.3-flash", status: "timeout", upstream_ref: null },
      ],
      suggested_alternatives: ["glm-5.2"],
    });
    renderPage();

    await submit();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/not answering/i);
    expect(alert.textContent).toMatch(/try again/i);
    // A standard user cannot pick models, so don't offer alternatives.
    expect(alert.textContent).not.toMatch(/glm-5\.2/);
  });

  it("says the usage limit was reached and to tell an admin", async () => {
    rejectWith({
      code: "upstream_model_unhealthy",
      message: "The Ollama Cloud account has reached its usage limit.",
      unhealthy_models: [
        { model: "glm-5.3", status: "rate_limited", upstream_ref: null },
      ],
      suggested_alternatives: [],
    });
    renderPage();

    await submit();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/usage limit/i);
    expect(alert.textContent).toMatch(/admin/i);
  });

  it("tells the user the check can take a while while it runs", async () => {
    postMock.mockReturnValueOnce(new Promise(() => {}));
    renderPage();

    await submit();

    expect(
      await screen.findByText(/checking that the AI models are answering/i),
    ).toBeTruthy();
  });
});
