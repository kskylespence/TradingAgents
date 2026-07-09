import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { UserDefaults } from "@/lib/types";

/**
 * React Query wrapper around `GET /api/settings/defaults`. The defaults
 * row is a singleton — fetch once per session and reuse.
 *
 * Used by the New Run form to pre-fill provider/model/depth/analysts/
 * language so the user doesn't have to re-enter their preferences on
 * every visit. All fields are nullable; the form falls back to catalog
 * defaults (first provider, etc.) for anything the user hasn't saved.
 */
export function useUserDefaults(enabled = true): UseQueryResult<UserDefaults> {
  return useQuery({
    queryKey: ["settings", "defaults"],
    queryFn: () => api.get<UserDefaults>("/api/settings/defaults"),
    enabled,
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
    refetchOnWindowFocus: false,
  });
}
