import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { AUTH_QUERY_KEY, useAuth } from "@/hooks/useAuth";
import { api, ApiError } from "@/lib/api";
import type { ChangePasswordRequest } from "@/lib/types";

/** Mirrors the backend's `ChangePasswordRequest.new_password` rules. */
const MIN_PASSWORD_LENGTH = 8;
const MAX_PASSWORD_BYTES = 72;

/** Code points, matching Python's len() — not UTF-16 units. */
const codePoints = (s: string) => [...s].length;
const utf8Bytes = (s: string) => new TextEncoder().encode(s).length;

export default function ChangePassword() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pending, setPending] = useState(false);

  let problem: string | null = null;
  if (next.length > 0) {
    if (codePoints(next) < MIN_PASSWORD_LENGTH) {
      problem = `New password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
    } else if (utf8Bytes(next) > MAX_PASSWORD_BYTES) {
      problem = `Too long — passwords are limited to ${MAX_PASSWORD_BYTES} bytes.`;
    } else if (current.length > 0 && next === current) {
      problem = "New password must be different from the temporary one.";
    } else if (confirm.length > 0 && confirm !== next) {
      problem = "The new passwords do not match.";
    }
  }
  const canSubmit =
    !pending &&
    current.length > 0 &&
    next.length > 0 &&
    confirm.length > 0 &&
    problem === null;

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!canSubmit) return;
    const body: ChangePasswordRequest = {
      current_password: current,
      new_password: next,
    };
    setPending(true);
    try {
      await api.post<void>("/api/auth/change-password", body);
      await queryClient.invalidateQueries({ queryKey: AUTH_QUERY_KEY });
      toast({ title: "Password changed" });
      navigate("/new", { replace: true });
    } catch (err) {
      toast({
        title: "Could not change password",
        description:
          err instanceof ApiError
            ? err.message
            : "Could not change password. Try again.",
        variant: "destructive",
      });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex min-h-svh items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="space-y-1">
          <CardTitle className="text-xl">Choose a new password</CardTitle>
          <CardDescription>
            An admin set a temporary password for your account; choose your own
            before you continue.
          </CardDescription>
        </CardHeader>
        <form onSubmit={onSubmit}>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="current-password">Temporary password</Label>
              <Input
                id="current-password"
                type="password"
                autoComplete="current-password"
                autoFocus
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="new-password">New password</Label>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm-password">Confirm new password</Label>
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
            {problem ? (
              <p role="alert" className="text-sm text-destructive">
                {problem}
              </p>
            ) : null}
          </CardContent>
          <CardFooter className="flex gap-2">
            <Button type="submit" className="flex-1" disabled={!canSubmit}>
              {pending ? "Saving..." : "Change password"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                void logout();
              }}
            >
              Sign out
            </Button>
          </CardFooter>
        </form>
      </Card>
    </div>
  );
}
