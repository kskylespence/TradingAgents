import type { StatsEvent } from "@/lib/types";
import { cn, formatElapsed, formatTokens } from "@/lib/utils";

export interface StatsBarProps {
  stats: StatsEvent | undefined;
  className?: string;
}

/**
 * Compact one-line stats footer:
 *   LLM calls: 12  |  Tools: 8  |  Tokens in/out: 1.2k/0.5k  |  Elapsed: 03:42
 *
 * Uses formatTokens (thousands sep) and formatElapsed (h/m/s) from utils so
 * the entire UI shares one formatter pair.
 */
export function StatsBar({ stats, className }: StatsBarProps) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-muted-foreground",
        className,
      )}
      aria-label="Run stats"
    >
      <Stat label="LLM calls" value={stats?.llm_calls ?? 0} />
      <Stat label="Tools" value={stats?.tool_calls ?? 0} />
      <Stat
        label="Tokens in/out"
        value={`${formatTokens(stats?.tokens_in ?? 0)} / ${formatTokens(
          stats?.tokens_out ?? 0,
        )}`}
      />
      <Stat label="Elapsed" value={formatElapsed(stats?.elapsed_seconds ?? 0)} />
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="font-medium text-foreground">{label}:</span>
      <span className="tabular-nums">{value}</span>
    </span>
  );
}

export default StatsBar;
