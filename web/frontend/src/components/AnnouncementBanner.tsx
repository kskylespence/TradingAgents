import { useQuery } from "@tanstack/react-query";

import { api, ApiError } from "@/lib/api";
import type { Announcement } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Fetches /api/announcements (server-side proxy of api.tauric.ai) on a
 * 60s cadence. Renders nothing when the list is empty or the request
 * fails; we never want to block the rest of the UI on this.
 */
export function AnnouncementBanner() {
  const query = useQuery({
    queryKey: ["announcements"],
    queryFn: async (): Promise<Announcement[]> => {
      try {
        return await api.get<Announcement[]>("/api/announcements");
      } catch (err) {
        // 401 means not logged in yet — silently skip. Other errors also
        // resolve to empty so we never disrupt the page.
        if (err instanceof ApiError) return [];
        throw err;
      }
    },
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const items = query.data ?? [];
  if (!items.length) return null;

  return (
    <div className="space-y-1 border-b bg-muted/40 px-4 py-2 text-sm">
      {items.map((a) => (
        <div
          key={a.id}
          className={cn(
            "flex items-start gap-2",
            a.severity === "critical" && "text-destructive",
            a.severity === "warning" && "text-amber-600 dark:text-amber-400",
          )}
        >
          <span className="font-medium">{a.title}</span>
          <span className="text-muted-foreground">{a.body}</span>
          {a.url && (
            <a
              href={a.url}
              target="_blank"
              rel="noreferrer noopener"
              className="ml-auto underline"
            >
              Details
            </a>
          )}
        </div>
      ))}
    </div>
  );
}
