/**
 * Tests for the admin Users card (list / create / delete).
 *
 * What this verifies:
 *   - The list renders each account with its role and run count.
 *   - Delete is hidden for the signed-in user and for admin accounts, so
 *     the UI never offers an action the API refuses.
 *   - Submitting the form POSTs a trimmed username to /api/users.
 *   - The submit button stays disabled until both fields clear the
 *     backend's minimum lengths.
 *   - A 409 surfaces the backend's `detail` verbatim in a destructive
 *     toast (it already names the taken username / owned run count).
 *   - Delete calls DELETE /api/users/<id>.
 *
 * Mock style mirrors ``RunView.retry.test.tsx``: stub modules at the
 * boundary, keep the real exports via ``vi.importActual``.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { UserSummary } from "@/lib/types";

// ---- Mocks (declare BEFORE importing UsersCard) -------------------------- //

const getSpy = vi.fn();
const postSpy = vi.fn();
const deleteSpy = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      get: (...args: unknown[]) => getSpy(...args),
      post: (...args: unknown[]) => postSpy(...args),
      delete: (...args: unknown[]) => deleteSpy(...args),
    },
  };
});

const toastSpy = vi.fn();

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: toastSpy }),
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({
    user: { id: "admin-1", username: "acting-admin", role: "admin" },
  }),
}));

import { ApiError } from "@/lib/api";
import UsersCard from "@/components/UsersCard";

// ------------------------------------------------------------------------ //

const USERS: UserSummary[] = [
  {
    id: "admin-1",
    username: "acting-admin",
    role: "admin",
    created_at: "2026-01-01T00:00:00Z",
    run_count: 0,
  },
  {
    id: "user-2",
    username: "rob@rob",
    role: "user",
    created_at: "2026-02-01T00:00:00Z",
    run_count: 3,
  },
  {
    id: "user-3",
    username: "freshling",
    role: "user",
    created_at: "2026-03-01T00:00:00Z",
    run_count: 0,
  },
];

function renderCard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <UsersCard />
    </QueryClientProvider>,
  );
}

describe("UsersCard", () => {
  beforeEach(() => {
    getSpy.mockReset().mockResolvedValue(USERS);
    postSpy.mockReset();
    deleteSpy.mockReset();
    toastSpy.mockReset();
  });

  it("lists every account with its role and run count", async () => {
    renderCard();

    expect(await screen.findByText("rob@rob")).toBeTruthy();
    expect(screen.getByText("freshling")).toBeTruthy();
    expect(screen.getByText("acting-admin")).toBeTruthy();

    expect(screen.getByText(/3 runs/)).toBeTruthy();
    // Singular/plural is derived, so check the 0 case reads "0 runs".
    expect(screen.getAllByText(/0 runs/).length).toBeGreaterThan(0);
  });

  it("hides delete for the signed-in user and for admins", async () => {
    renderCard();
    await screen.findByText("rob@rob");

    // acting-admin is both the current user and an admin.
    expect(screen.queryByRole("button", { name: /delete acting-admin/i })).toBeNull();
    // Plain users are deletable.
    expect(screen.getByRole("button", { name: /delete rob@rob/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /delete freshling/i })).toBeTruthy();
  });

  it("keeps the submit button disabled until both fields are long enough", async () => {
    renderCard();
    await screen.findByText("rob@rob");

    const submit = screen.getByRole("button", { name: /add user/i });
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    await userEvent.type(screen.getByLabelText(/username/i), "ab");
    await userEvent.type(screen.getByLabelText(/password/i), "short");
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    await userEvent.type(screen.getByLabelText(/username/i), "cd");
    await userEvent.type(screen.getByLabelText(/password/i), "enough!!");
    await waitFor(() => {
      expect((submit as HTMLButtonElement).disabled).toBe(false);
    });
  });

  it("POSTs a trimmed username and password to /api/users", async () => {
    postSpy.mockResolvedValueOnce({
      id: "user-4",
      username: "analyst",
      role: "user",
      created_at: "2026-04-01T00:00:00Z",
      run_count: 0,
    });

    renderCard();
    await screen.findByText("rob@rob");

    await userEvent.type(screen.getByLabelText(/username/i), "  analyst  ");
    await userEvent.type(screen.getByLabelText(/password/i), "password123");
    await userEvent.click(screen.getByRole("button", { name: /add user/i }));

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith("/api/users", {
        username: "analyst",
        password: "password123",
      });
    });
  });

  it("surfaces the backend detail when creation conflicts", async () => {
    postSpy.mockRejectedValueOnce(
      new ApiError(409, "A user named 'rob@rob' already exists."),
    );

    renderCard();
    await screen.findByText("rob@rob");

    await userEvent.type(screen.getByLabelText(/username/i), "rob@rob");
    await userEvent.type(screen.getByLabelText(/password/i), "password123");
    await userEvent.click(screen.getByRole("button", { name: /add user/i }));

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "destructive",
          description: "A user named 'rob@rob' already exists.",
        }),
      );
    });
  });

  it("does not delete on the first click — it only arms", async () => {
    renderCard();
    await screen.findByText("freshling");

    await userEvent.click(
      screen.getByRole("button", { name: /^delete freshling$/i }),
    );

    expect(deleteSpy).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: /confirm delete freshling/i }),
    ).toBeTruthy();
  });

  it("cancels an armed delete without firing it", async () => {
    renderCard();
    await screen.findByText("freshling");

    await userEvent.click(
      screen.getByRole("button", { name: /^delete freshling$/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /cancel deleting freshling/i }),
    );

    expect(deleteSpy).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /^delete freshling$/i })).toBeTruthy();
  });

  it("DELETEs the selected user on the confirming click", async () => {
    deleteSpy.mockResolvedValueOnce(undefined);

    renderCard();
    await screen.findByText("freshling");

    await userEvent.click(
      screen.getByRole("button", { name: /^delete freshling$/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /confirm delete freshling/i }),
    );

    await waitFor(() => {
      expect(deleteSpy).toHaveBeenCalledWith("/api/users/user-3");
    });
  });

  it("blocks submit when the password exceeds bcrypt's 72-byte ceiling", async () => {
    renderCard();
    await screen.findByText("rob@rob");

    await userEvent.type(screen.getByLabelText(/username/i), "analyst");
    // 73 single-byte characters — one past the limit.
    await userEvent.type(screen.getByLabelText(/password/i), "x".repeat(73));

    const submit = screen.getByRole("button", { name: /add user/i });
    await waitFor(() => {
      expect((submit as HTMLButtonElement).disabled).toBe(true);
    });
    expect(screen.getByText(/limited to 72 bytes/i)).toBeTruthy();
    expect(postSpy).not.toHaveBeenCalled();
  });

  it("blocks submit when the username exceeds the backend's 128-char cap", async () => {
    renderCard();
    await screen.findByText("rob@rob");

    await userEvent.type(screen.getByLabelText(/password/i), "password123");
    // Paste rather than type — 129 keystrokes is needlessly slow.
    await userEvent.click(screen.getByLabelText(/username/i));
    await userEvent.paste("a".repeat(129));

    await waitFor(() => {
      expect(screen.getByText(/limited to 128 characters/i)).toBeTruthy();
    });
    expect(
      (screen.getByRole("button", { name: /add user/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(postSpy).not.toHaveBeenCalled();
  });

  it("counts minimum lengths in code points, matching Python's len()", async () => {
    renderCard();
    await screen.findByText("rob@rob");

    await userEvent.type(screen.getByLabelText(/username/i), "analyst");
    // 4 emoji: JS `.length` is 8 (UTF-16 code units) and would pass a naive
    // `>= 8` check, but Pydantic's min_length counts 4 code points and 422s.
    await userEvent.paste(""); // focus guard for the paste below
    await userEvent.click(screen.getByLabelText(/password/i));
    await userEvent.paste("😀".repeat(4));

    await waitFor(() => {
      expect(
        (screen.getByRole("button", { name: /add user/i }) as HTMLButtonElement)
          .disabled,
      ).toBe(true);
    });
    expect(postSpy).not.toHaveBeenCalled();
  });

  it("counts multi-byte characters as bytes, not characters", async () => {
    renderCard();
    await screen.findByText("rob@rob");

    await userEvent.type(screen.getByLabelText(/username/i), "analyst");
    // 25 four-byte emoji = 100 bytes, but only 25 JS characters. A naive
    // `password.length <= 72` check would wrongly allow this.
    await userEvent.type(screen.getByLabelText(/password/i), "😀".repeat(25));

    await waitFor(() => {
      expect(screen.getByText(/limited to 72 bytes/i)).toBeTruthy();
    });
    expect(
      (screen.getByRole("button", { name: /add user/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("explains why a user with runs cannot be deleted", async () => {
    deleteSpy.mockRejectedValueOnce(
      new ApiError(
        409,
        "'rob@rob' owns 3 run(s). Delete or reassign them before removing the account.",
      ),
    );

    renderCard();
    await screen.findByText("rob@rob");

    await userEvent.click(
      screen.getByRole("button", { name: /^delete rob@rob$/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /confirm delete rob@rob/i }),
    );

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "destructive",
          description: expect.stringContaining("owns 3 run(s)"),
        }),
      );
    });

    // A failed delete disarms, so a second stray click can't re-fire it.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^delete rob@rob$/i })).toBeTruthy();
    });
  });
});
