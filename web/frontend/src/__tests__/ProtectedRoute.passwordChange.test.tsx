/**
 * ProtectedRoute sends a user with a temporary (admin-reset) password to
 * /change-password, and never loops once they are there.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

let mockUser: unknown = null;

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: mockUser, isLoading: false }),
}));

import { ProtectedRoute } from "@/components/ProtectedRoute";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<div>login page</div>} />
        <Route
          path="/change-password"
          element={
            <ProtectedRoute>
              <div>change password page</div>
            </ProtectedRoute>
          }
        />
        <Route
          path="/new"
          element={
            <ProtectedRoute>
              <div>new run page</div>
            </ProtectedRoute>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

const base = { id: "u-1", username: "rob", role: "user" as const };

describe("ProtectedRoute password-change redirect", () => {
  beforeEach(() => {
    mockUser = null;
  });

  it("redirects a flagged user to /change-password", () => {
    mockUser = { ...base, must_change_password: true };
    renderAt("/new");
    expect(screen.getByText("change password page")).toBeTruthy();
    expect(screen.queryByText("new run page")).toBeNull();
  });

  it("does not redirect an unflagged user", () => {
    mockUser = { ...base, must_change_password: false };
    renderAt("/new");
    expect(screen.getByText("new run page")).toBeTruthy();
  });

  it("does not loop when a flagged user is already on /change-password", () => {
    mockUser = { ...base, must_change_password: true };
    renderAt("/change-password");
    expect(screen.getByText("change password page")).toBeTruthy();
  });

  it("lets an unflagged user open /change-password", () => {
    mockUser = { ...base, must_change_password: false };
    renderAt("/change-password");
    expect(screen.getByText("change password page")).toBeTruthy();
  });
});
