import { cn, formatElapsed } from "@/lib/utils";

/**
 * Wire-key → user-facing label, per CLAUDE.md ("Wire keys vs user-facing
 * labels"). `social` is the wire key for the Sentiment Analyst.
 */
const ANALYST_LABELS: Record<string, string> = {
  market: "Market",
  social: "Sentiment",
  news: "News",
  fundamentals: "Fundamentals",
};

const ANALYST_ORDER: readonly string[] = ["market", "social", "news", "fundamentals"];

/**
 * Stable colour per analyst for the stacked bar. Picked to read clearly in
 * both light and dark themes.
 */
const ANALYST_COLOURS: Record<string, string> = {
  market: "bg-blue-500",
  social: "bg-purple-500",
  news: "bg-amber-500",
  fundamentals: "bg-emerald-500",
};

const FALLBACK_COLOUR = "bg-slate-400";

export interface WallTimePanelProps {
  /** Wire-key -> seconds; e.g. {market: 12.3, news: 8.1} */
  times: Record<string, number>;
  className?: string;
}

/**
 * Stacked horizontal bar of per-analyst wall-clock time, plus a sorted list
 * with absolute seconds. Renders nothing visually distracting if there's no
 * data yet.
 */
export function WallTimePanel({ times, className }: WallTimePanelProps) {
  const entries = Object.entries(times).filter(([, s]) => s > 0);
  if (entries.length === 0) {
    return (
      <div className={cn("text-xs text-muted-foreground", className)}>
        Per-analyst wall-times appear here as analysts complete.
      </div>
    );
  }

  // Order: canonical first, then any unknown wire-keys.
  const known = new Set(ANALYST_ORDER);
  const ordered = [
    ...ANALYST_ORDER.filter((k) => k in times),
    ...entries.map(([k]) => k).filter((k) => !known.has(k)),
  ];

  const total = entries.reduce((s, [, v]) => s + v, 0);

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div
        className="flex h-3 w-full overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label="Analyst wall-time breakdown"
      >
        {ordered.map((key) => {
          const seconds = times[key] ?? 0;
          if (seconds <= 0) return null;
          const pct = (seconds / total) * 100;
          return (
            <div
              key={key}
              className={cn("h-full", ANALYST_COLOURS[key] ?? FALLBACK_COLOUR)}
              style={{ width: `${pct}%` }}
              title={`${labelFor(key)}: ${formatElapsed(seconds)}`}
            />
          );
        })}
      </div>
      <ul className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground sm:grid-cols-4">
        {ordered.map((key) => (
          <li key={key} className="flex items-center gap-2">
            <span
              className={cn(
                "inline-block h-2 w-2 rounded-full",
                ANALYST_COLOURS[key] ?? FALLBACK_COLOUR,
              )}
              aria-hidden
            />
            <span className="font-medium text-foreground">{labelFor(key)}</span>
            <span className="ml-auto tabular-nums">
              {formatElapsed(times[key] ?? 0)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function labelFor(wireKey: string): string {
  return (
    ANALYST_LABELS[wireKey] ??
    wireKey.charAt(0).toUpperCase() + wireKey.slice(1)
  );
}

export default WallTimePanel;
