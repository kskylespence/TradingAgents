import {
  useInfiniteQuery,
  type UseInfiniteQueryResult,
} from "@tanstack/react-query";

import { apiFetch } from "@/lib/api";
import type { HistoryPage, RunStatus } from "@/lib/types";

export interface UseHistoryOpts {
  ticker?: string;
  status?: RunStatus;
}

const PAGE_SIZE = 20;

/**
 * Cursor-paginated history fetch backed by GET /api/history.
 *
 * The backend returns a `next_cursor` (string) or `null` when exhausted;
 * we feed that straight back as `pageParam`. The query key includes the
 * active filters so changing ticker/status starts a fresh paginated set.
 */
export function useHistory(
  opts: UseHistoryOpts,
): UseInfiniteQueryResult<{ pages: HistoryPage[]; pageParams: unknown[] }, Error> {
  const { ticker, status } = opts;
  return useInfiniteQuery({
    queryKey: ["history", ticker ?? "", status ?? ""],
    queryFn: ({ pageParam }) =>
      apiFetch<HistoryPage>("/api/history", {
        params: {
          cursor: pageParam ?? undefined,
          ticker: ticker || undefined,
          status: status || undefined,
          limit: PAGE_SIZE,
        },
      }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    initialPageParam: undefined as string | undefined,
  });
}
