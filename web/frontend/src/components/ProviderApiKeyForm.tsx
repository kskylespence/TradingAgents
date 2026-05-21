import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { api, ApiError } from "@/lib/api";
import type { ApiKeyStatus } from "@/lib/types";

export interface ProviderApiKeyFormProps {
  entry: ApiKeyStatus;
}

const API_KEYS_QUERY_KEY = ["api-keys"] as const;

/**
 * Render one provider env var row: masked input + Save / Clear actions.
 *
 * PUT `/api/settings/api-keys/{env}` to persist a new value, DELETE to
 * clear. Both mutations invalidate the `['api-keys']` list query so the
 * configured/last-updated subtitle refreshes.
 *
 * "Clear" requires an extra confirmation step because losing a key
 * silently mid-run causes opaque downstream failures.
 */
export function ProviderApiKeyForm({ entry }: ProviderApiKeyFormProps) {
  const inputId = `api-key-${entry.provider_env}`;
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [value, setValue] = useState("");
  const [confirmingClear, setConfirmingClear] = useState(false);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY });

  const saveMutation = useMutation({
    mutationFn: (next: string) =>
      api.put<void>(`/api/settings/api-keys/${entry.provider_env}`, {
        value: next,
      }),
    onSuccess: async () => {
      setValue("");
      toast({ title: "Saved", description: entry.provider_env });
      await invalidate();
    },
    onError: (err) => {
      const message =
        err instanceof ApiError ? err.message : "Could not save the API key.";
      toast({
        title: "Save failed",
        description: message,
        variant: "destructive",
      });
    },
  });

  const clearMutation = useMutation({
    mutationFn: () =>
      api.delete<void>(`/api/settings/api-keys/${entry.provider_env}`),
    onSuccess: async () => {
      setConfirmingClear(false);
      setValue("");
      toast({ title: "Cleared", description: entry.provider_env });
      await invalidate();
    },
    onError: (err) => {
      const message =
        err instanceof ApiError ? err.message : "Could not clear the API key.";
      toast({
        title: "Clear failed",
        description: message,
        variant: "destructive",
      });
    },
  });

  const handleSave = () => {
    const trimmed = value.trim();
    if (!trimmed) {
      toast({
        title: "Enter a value first",
        description: `Provide a value for ${entry.provider_env} before saving.`,
        variant: "destructive",
      });
      return;
    }
    saveMutation.mutate(trimmed);
  };

  const placeholder = entry.configured ? "•••• configured" : "Not set";
  const busy = saveMutation.isPending || clearMutation.isPending;

  return (
    <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end sm:gap-3">
      <div className="space-y-1.5">
        <Label htmlFor={inputId} className="font-mono">
          {entry.provider_env}
        </Label>
        <Input
          id={inputId}
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder={placeholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={busy}
          aria-label={entry.provider_env}
        />
      </div>
      <div className="flex items-center gap-2 sm:pb-px">
        <Button
          type="button"
          size="sm"
          onClick={handleSave}
          disabled={busy}
        >
          {saveMutation.isPending ? "Saving..." : "Save"}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy || !entry.configured}
          onClick={() => setConfirmingClear(true)}
        >
          Clear
        </Button>
      </div>

      <Dialog
        open={confirmingClear}
        onOpenChange={(open) => {
          if (!clearMutation.isPending) setConfirmingClear(open);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Clear {entry.provider_env}?</DialogTitle>
            <DialogDescription>
              Runs that require this provider will fail until the key is set
              again. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setConfirmingClear(false)}
              disabled={clearMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => clearMutation.mutate()}
              disabled={clearMutation.isPending}
            >
              {clearMutation.isPending ? "Clearing..." : "Clear key"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default ProviderApiKeyForm;
