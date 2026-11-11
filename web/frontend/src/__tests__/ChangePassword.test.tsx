/**
 * Forced password-change form (shown after an admin resets a password).
 *
 * Verifies: POST body/path, each client-side check with its own message,
 * success (invalidate auth + toast + navigate to /new), ApiError message in a
 * destructive toast, and Sign out.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const postSpy = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: { ...actual.api, post: (...args: unknown[]) => postSpy(...args) },
  };
});

const toastSpy = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: toastSpy }),
}));

const logoutSpy = vi.fn();
vi.mock("@/hooks/useAuth", async () => {
  const actual =
    await vi.importActual<typeof import("@/hooks/useAuth")>("@/hooks/useAuth");
  return {
    ...actual,
    useAuth: () => ({
      user: {
        id: "u-1",
        username: "rob",
        role: "user",
        must_change_password: true,
      },
      isLoading: false,
      logout: logoutSpy,
    }),
  };
});

import { ApiError } from "@/lib/api";
import { AUTH_QUERY_KEY } from "@/hooks/useAuth";
import ChangePassword from "@/routes/ChangePassword";

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/change-password"]}>
        <Routes>
          <Route path="/change-password" element={<ChangePassword />} />
          <Route path="/new" element={<div>new run page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { invalidate };
}

async function fill(current: string, next: string, confirm: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Temporary password"), current);
  await user.type(screen.getByLabelText("New password"), next);
  await user.type(screen.getByLabelText("Confirm new password"), confirm);
  return user;
}

const submit = () => screen.getByRole("button", { name: "Change password" });

describe("ChangePassword", () => {
  beforeEach(() => {
    postSpy.mockReset();
    toastSpy.mockReset();
    logoutSpy.mockReset();
  });

  it("explains the temporary password", () => {
    setup();
    expect(screen.getByText(/admin set a temporary password/i)).toBeTruthy();
  });

  it("posts current and new password, invalidates auth, toasts and goes to /new", async () => {
    postSpy.mockResolvedValue(undefined);
    const { invalidate } = setup();
    const user = await fill("temp-pass-1", "brand-new-pass", "brand-new-pass");
    await user.click(submit());
    await waitFor(() => expect(screen.getByText("new run page")).toBeTruthy());
    expect(postSpy).toHaveBeenCalledWith("/api/auth/change-password", {
      current_password: "temp-pass-1",
      new_password: "brand-new-pass",
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: AUTH_QUERY_KEY });
    expect(toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Password changed" }),
    );
  });

  it("blocks a new password under 8 characters", async () => {
    setup();
    await fill("temp-pass-1", "short", "short");
    expect(screen.getByText(/at least 8 characters/i)).toBeTruthy();
    expect((submit() as HTMLButtonElement).disabled).toBe(true);
  });

  it("counts code points, not UTF-16 units (4 emoji is too short)", async () => {
    setup();
    await fill("temp-pass-1", "😀😀😀😀", "😀😀😀😀");
    expect(screen.getByText(/at least 8 characters/i)).toBeTruthy();
    expect((submit() as HTMLButtonElement).disabled).toBe(true);
  });

  it("blocks a new password over 72 bytes", async () => {
    setup();
    const long = "😀".repeat(19); // 76 bytes, 19 code points
    await fill("temp-pass-1", long, long);
    expect(screen.getByText(/limited to 72 bytes/i)).toBeTruthy();
    expect((submit() as HTMLButtonElement).disabled).toBe(true);
  });

  it("blocks a confirmation that does not match", async () => {
    setup();
    await fill("temp-pass-1", "brand-new-pass", "brand-new-pasz");
    expect(screen.getByText(/do not match/i)).toBeTruthy();
    expect((submit() as HTMLButtonElement).disabled).toBe(true);
  });

  it("blocks a new password equal to the temporary one", async () => {
    setup();
    await fill("same-password", "same-password", "same-password");
    expect(screen.getByText(/must be different/i)).toBeTruthy();
    expect((submit() as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the ApiError message in a destructive toast and stays put", async () => {
    postSpy.mockRejectedValue(new ApiError(400, "Current password is incorrect."));
    setup();
    const user = await fill("wrong-temp", "brand-new-pass", "brand-new-pass");
    await user.click(submit());
    await waitFor(() =>
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          description: "Current password is incorrect.",
          variant: "destructive",
        }),
      ),
    );
    expect(screen.queryByText("new run page")).toBeNull();
  });

  it("Sign out calls logout", async () => {
    logoutSpy.mockResolvedValue(undefined);
    setup();
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
    expect(logoutSpy).toHaveBeenCalled();
  });
});
