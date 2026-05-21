import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";

/**
 * `GET /api/health` — overall container health plus an optional `ollama`
 * subblock when Ollama is the active LLM provider.
 *
 * Used by `NewRun.tsx` to surface an inline warning when the user is
 * about to submit a run against an upstream Ollama that the backend
 * health probe says is down. Without this, the only signal the user
 * gets is the engine 404'ing ~10 seconds after submit — the same
 * failure mode that motivated this whole change.
 *
 * Polled at 30s intervals while the form is open. The endpoint is
 * cheap (it reuses the catalog's TTL-cached Ollama probe), so this
 * doesn't add upstream load. The endpoint is also unauthenticated, so
 * we don't need any of the auth-fetch ceremony.
 */
export function useHealth(): UseQueryResult<HealthResponse> {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => api.get<HealthResponse>("/api/health"),
    staleTime: 15_000,
    gcTime: 60_000,
    refetchInterval: 30_000,
    refetchOnWindowFocus: false,
    // A failed health fetch must not throw an error boundary — the
    // form should still be usable even if /api/health blips.
    retry: 1,
  });
}
