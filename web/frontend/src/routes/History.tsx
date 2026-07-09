import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useHistory } from "@/hooks/useHistory";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import type { Rating, RunStatus, RunSummary } from "@/lib/types";

const STATUS_OPTIONS: Array<{ value: "all" | RunStatus; label: string }> = [
  { value: "all", label: "All statuses" },
  { value: "queued", label: "Queued" },
  { value: "running", label: "Running" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
  { value: "interrupted", label: "Interrupted" },
];

const RATING_VARIANT: Record<Rating, BadgeProps["variant"]> = {
  Buy: "default",
  Overweight: "secondary",
  Hold: "outline",
  Underweight: "secondary",
  Sell: "destructive",
};

/** mm:ss formatter for elapsed seconds; null/undefined -> em-dash. */
function formatMMSS(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Generic debounce hook; updates returned value after `delay`ms of quiet. */
function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

export default function History() {
  const navigate = useNavigate();
  const isAdmin = useIsAdmin();
  const colCount = isAdmin ? 6 : 5;

  const [tickerInput, setTickerInput] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | RunStatus>("all");

  const debouncedTicker = useDebounced(tickerInput.trim().toUpperCase(), 300);

  const {
    data,
    isLoading,
    isError,
    error,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
    refetch,
  } = useHistory({
    ticker: debouncedTicker || undefined,
    status: statusFilter === "all" ? undefined : statusFilter,
  });

  const rows = useMemo<RunSummary[]>(
    () => (data ? data.pages.flatMap((p) => p.items) : []),
    [data],
  );

  return (
    <div className="container py-8">
      <Card className="border-emerald-900/40 bg-card/80">
        <CardHeader>
          <CardTitle className="font-mono tracking-tight">Run blotter</CardTitle>
          <CardDescription>
            Past analyses, newest first. Click a row to open the full report.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Filters */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <div className="sm:w-64">
              <Input
                aria-label="Filter by ticker"
                placeholder="Filter by ticker (e.g. NVDA)"
                value={tickerInput}
                onChange={(e) => setTickerInput(e.target.value)}
              />
            </div>
            <div className="sm:w-56">
              <Select
                value={statusFilter}
                onValueChange={(v) => setStatusFilter(v as "all" | RunStatus)}
              >
                <SelectTrigger aria-label="Filter by status">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {STATUS_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {/* Table */}
          <div className="overflow-x-auto rounded-md border border-emerald-900/30">
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-left text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 font-medium">Date</th>
                  <th className="px-4 py-2 font-medium">Ticker</th>
                  <th className="px-4 py-2 font-medium">Rating</th>
                  <th className="px-4 py-2 font-medium">Depth</th>
                  {isAdmin ? (
                    <th className="px-4 py-2 font-medium">Provider</th>
                  ) : null}
                  <th className="px-4 py-2 font-medium text-right">Elapsed</th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  Array.from({ length: 5 }).map((_, i) => (
                    <tr key={`sk-${i}`} className="border-t">
                      {Array.from({ length: colCount }).map((__, j) => (
                        <td key={j} className="px-4 py-3">
                          <Skeleton className="h-4 w-full max-w-[140px]" />
                        </td>
                      ))}
                    </tr>
                  ))
                ) : isError ? (
                  <tr>
                    <td colSpan={colCount} className="px-4 py-10 text-center">
                      <div className="space-y-3">
                        <p className="text-sm text-destructive">
                          Failed to load history
                          {error instanceof Error ? `: ${error.message}` : "."}
                        </p>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            void refetch();
                          }}
                        >
                          Retry
                        </Button>
                      </div>
                    </td>
                  </tr>
                ) : rows.length === 0 ? (
                  <tr>
                    <td colSpan={colCount} className="px-4 py-16">
                      <div className="flex flex-col items-center justify-center gap-3 text-center">
                        <p className="text-sm text-muted-foreground">
                          No runs yet.
                        </p>
                        <Button asChild size="sm">
                          <Link to="/new">Start your first run</Link>
                        </Button>
                      </div>
                    </td>
                  </tr>
                ) : (
                  rows.map((run) => (
                    <tr
                      key={run.id}
                      role="link"
                      tabIndex={0}
                      onClick={() => navigate(`/runs/${run.id}`)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          navigate(`/runs/${run.id}`);
                        }
                      }}
                      className="cursor-pointer border-t transition-colors hover:bg-muted/40 focus:bg-muted/40 focus:outline-none"
                    >
                      <td className="px-4 py-3 whitespace-nowrap">
                        {formatDate(run.created_at)}
                      </td>
                      <td className="px-4 py-3 font-mono font-medium">
                        {run.ticker}
                      </td>
                      <td className="px-4 py-3">
                        {run.rating ? (
                          <Badge variant={RATING_VARIANT[run.rating]}>
                            {run.rating}
                          </Badge>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3">{run.research_depth}</td>
                      {isAdmin ? (
                        <td className="px-4 py-3">{run.llm_provider}</td>
                      ) : null}
                      <td className="px-4 py-3 text-right font-mono tabular-nums">
                        {formatMMSS(run.elapsed_seconds)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {/* Load more */}
          {!isLoading && !isError && rows.length > 0 && (
            <div className="flex justify-center pt-2">
              {hasNextPage ? (
                <Button
                  type="button"
                  variant="outline"
                  disabled={isFetchingNextPage}
                  onClick={() => {
                    void fetchNextPage();
                  }}
                >
                  {isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              ) : (
                <p className="text-xs text-muted-foreground">
                  End of history.
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
