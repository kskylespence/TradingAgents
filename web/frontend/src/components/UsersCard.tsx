import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/hooks/useAuth";
import { useToast } from "@/hooks/use-toast";
import { api, ApiError } from "@/lib/api";
import type { CreateUserRequest, UserSummary } from "@/lib/types";

export const USERS_QUERY_KEY = ["users"] as const;

/** Mirrors the backend's `CreateUserRequest` constraints so the form can
 *  reject obvious mistakes before a round-trip. The server re-validates. */
const MIN_USERNAME_LENGTH = 3;
const MAX_USERNAME_LENGTH = 128;
const MIN_PASSWORD_LENGTH = 8;
/**
 * bcrypt's ceiling, in BYTES not characters — 20 emoji is 80 bytes.
 *
 * Enforcing it client-side isn't cosmetic: exceeding it makes the backend
 * return FastAPI's default 422, whose `detail` is an ARRAY of error objects.
 * `lib/api.ts` stringifies `detail` for the error message, and
 * `String([{...}])` renders as "[object Object]" — so without this guard a
 * long password produces a toast that tells the user nothing.
 */
const MAX_PASSWORD_BYTES = 72;

function utf8Bytes(s: string): number {
  return new TextEncoder().encode(s).length;
}

/**
 * Length in code points, matching Python's `len()`.
 *
 * NOT `s.length` — that counts UTF-16 code units, so "😀😀😀😀" is 8 in JS
 * but 4 to Pydantic's `min_length`. Using `.length` here would let the form
 * submit a password the backend then rejects with a 422, which renders as
 * "[object Object]" (see MAX_PASSWORD_BYTES above for why).
 */
function codePoints(s: string): number {
  return [...s].length;
}

function formatCreated(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString();
}

function errorMessage(err: unknown, fallback: string): string {
  // `ApiError.message` is FastAPI's `detail` string, which already spells
  // out the specific reason (taken username, owned run count, …). Prefer it
  // over generic frontend copy so the two never drift.
  return err instanceof ApiError ? err.message : fallback;
}

function CreateUserForm() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const createMutation = useMutation({
    mutationFn: (body: CreateUserRequest) =>
      api.post<UserSummary>("/api/users", body),
    onSuccess: async (created) => {
      toast({ title: `Created ${created.username}` });
      setUsername("");
      setPassword("");
      await queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    },
    onError: (err) => {
      toast({
        title: "Could not create user",
        description: errorMessage(err, "Please try again."),
        variant: "destructive",
      });
    },
  });

  const trimmed = username.trim();
  const passwordTooLong = utf8Bytes(password) > MAX_PASSWORD_BYTES;
  const usernameTooLong = codePoints(trimmed) > MAX_USERNAME_LENGTH;
  const canSubmit =
    codePoints(trimmed) >= MIN_USERNAME_LENGTH &&
    !usernameTooLong &&
    codePoints(password) >= MIN_PASSWORD_LENGTH &&
    !passwordTooLong &&
    !createMutation.isPending;

  return (
    <form
      className="space-y-4 border-t pt-5"
      onSubmit={(e) => {
        e.preventDefault();
        if (!canSubmit) return;
        createMutation.mutate({ username: trimmed, password });
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="new-username">Username</Label>
          <Input
            id="new-username"
            value={username}
            autoComplete="off"
            placeholder="e.g. analyst"
            onChange={(e) => setUsername(e.target.value)}
          />
          {usernameTooLong ? (
            <p className="text-sm text-destructive">
              Too long — usernames are limited to {MAX_USERNAME_LENGTH}{" "}
              characters.
            </p>
          ) : null}
        </div>
        <div className="space-y-2">
          <Label htmlFor="new-password">Password</Label>
          <Input
            id="new-password"
            type="password"
            value={password}
            autoComplete="new-password"
            placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
            onChange={(e) => setPassword(e.target.value)}
          />
          {passwordTooLong ? (
            <p className="text-sm text-destructive">
              Too long — passwords are limited to {MAX_PASSWORD_BYTES} bytes.
            </p>
          ) : null}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <Button type="submit" disabled={!canSubmit}>
          {createMutation.isPending ? "Adding…" : "Add user"}
        </Button>
        <p className="text-sm text-muted-foreground">
          New accounts are always created as standard users.
        </p>
      </div>
    </form>
  );
}

function UserRow({ user, canDelete }: { user: UserSummary; canDelete: boolean }) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  // Deleting an account is irreversible and the rows sit close together, so
  // the first click arms rather than fires. Cheaper than a modal and it keeps
  // the destructive action two deliberate clicks away from a misaimed one.
  const [confirming, setConfirming] = useState(false);

  const deleteMutation = useMutation({
    mutationFn: () => api.delete<void>(`/api/users/${user.id}`),
    onSuccess: async () => {
      toast({ title: `Deleted ${user.username}` });
      await queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    },
    onError: (err) => {
      setConfirming(false);
      toast({
        title: "Could not delete user",
        description: errorMessage(err, "Please try again."),
        variant: "destructive",
      });
    },
  });

  return (
    <div className="flex items-center justify-between gap-4 py-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate font-medium">{user.username}</span>
          {user.role === "admin" ? (
            <Badge variant="secondary">admin</Badge>
          ) : null}
        </div>
        <p className="text-sm text-muted-foreground">
          Added {formatCreated(user.created_at)} · {user.run_count}{" "}
          {user.run_count === 1 ? "run" : "runs"}
        </p>
      </div>

      {canDelete ? (
        <div className="flex shrink-0 items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive"
            disabled={deleteMutation.isPending}
            aria-label={
              confirming
                ? `Confirm delete ${user.username}`
                : `Delete ${user.username}`
            }
            onClick={() => {
              if (confirming) deleteMutation.mutate();
              else setConfirming(true);
            }}
          >
            {deleteMutation.isPending
              ? "Deleting…"
              : confirming
                ? "Confirm?"
                : "Delete"}
          </Button>
          {confirming && !deleteMutation.isPending ? (
            <Button
              variant="ghost"
              size="sm"
              aria-label={`Cancel deleting ${user.username}`}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Admin-only account management: list, create, delete.
 *
 * The security boundary is the backend's `require_admin` dependency, not
 * this component — hiding the card is presentation, not protection.
 */
export default function UsersCard() {
  const { user: currentUser } = useAuth();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: USERS_QUERY_KEY,
    queryFn: () => api.get<UserSummary[]>("/api/users"),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Users</CardTitle>
        <CardDescription>
          Add or remove accounts that can sign in. Users who already own runs
          can&apos;t be deleted — their analysis history is kept. Deleting
          someone doesn&apos;t end a session they&apos;re already signed into;
          that can take up to a week to expire.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-5">
        {isLoading ? (
          <div className="space-y-3" aria-label="Loading users">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : isError ? (
          <p className="text-sm text-destructive">
            Failed to load users
            {error instanceof ApiError ? `: ${error.message}` : ""}.
          </p>
        ) : !data || data.length === 0 ? (
          <p className="text-sm text-muted-foreground">No accounts yet.</p>
        ) : (
          <div className="divide-y">
            {data.map((u) => (
              <UserRow
                key={u.id}
                user={u}
                // Hide delete rather than disable it, so the UI never offers
                // an action the API will refuse. Admins are excluded because
                // this screen only ever creates standard users — admin
                // accounts are managed through environment configuration.
                canDelete={u.id !== currentUser?.id && u.role !== "admin"}
              />
            ))}
          </div>
        )}

        <CreateUserForm />
      </CardContent>
    </Card>
  );
}
