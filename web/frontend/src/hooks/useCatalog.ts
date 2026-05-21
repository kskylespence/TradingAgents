import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type {
  AssetType,
  CatalogAnalyst,
  CatalogLanguage,
  CatalogModel,
  CatalogProvider,
} from "@/lib/types";

/**
 * React Query wrappers around /api/catalog/*. All catalog responses are
 * effectively immutable for the lifetime of a session, so we set a long
 * stale time and disable refetch-on-focus.
 */

const SHARED_OPTS = {
  staleTime: 5 * 60_000,
  gcTime: 30 * 60_000,
  refetchOnWindowFocus: false,
} as const;

export function useProviders(): UseQueryResult<CatalogProvider[]> {
  return useQuery({
    queryKey: ["catalog", "providers"],
    queryFn: () => api.get<CatalogProvider[]>("/api/catalog/providers"),
    ...SHARED_OPTS,
  });
}

export type ModelMode = "quick" | "deep";

export function useModels(
  provider: string | null | undefined,
  mode: ModelMode,
): UseQueryResult<CatalogModel[]> {
  return useQuery({
    queryKey: ["catalog", "models", provider, mode],
    queryFn: () =>
      api.get<CatalogModel[]>("/api/catalog/models", { provider: provider ?? "", mode }),
    enabled: !!provider,
    ...SHARED_OPTS,
  });
}

export function useAnalysts(
  assetType: AssetType | null | undefined,
): UseQueryResult<CatalogAnalyst[]> {
  return useQuery({
    queryKey: ["catalog", "analysts", assetType ?? "stock"],
    queryFn: () =>
      api.get<CatalogAnalyst[]>("/api/catalog/analysts", {
        asset_type: assetType ?? "stock",
      }),
    ...SHARED_OPTS,
  });
}

export function useLanguages(): UseQueryResult<CatalogLanguage[]> {
  return useQuery({
    queryKey: ["catalog", "languages"],
    queryFn: () => api.get<CatalogLanguage[]>("/api/catalog/languages"),
    ...SHARED_OPTS,
  });
}
