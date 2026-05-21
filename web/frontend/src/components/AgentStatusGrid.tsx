import { Loader2 } from "lucide-react";

import type { AgentStatus, AgentStatusEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Canonical ordered roster used by the live dashboard. Order mirrors the
 * LangGraph pipeline (analysts → researchers → research manager → trader →
 * risk debaters → portfolio manager) so the grid reads top-to-bottom in
 * execution order.
 *
 * NOTE: the wire keys for analysts are `market / social / news /
 * fundamentals` (see CLAUDE.md "Wire keys vs user-facing labels") — but the
 * dashboard speaks user-facing labels because the backend emits
 * `agent_status` events keyed by label, not wire-key.
 */
export const DEFAULT_AGENT_ROSTER: readonly string[] = [
  "Market Analyst",
  "Sentiment Analyst",
  "News Analyst",
  "Fundamentals Analyst",
  "Bull Researcher",
  "Bear Researcher",
  "Research Manager",
  "Trader",
  "Aggressive Debater",
  "Conservative Debater",
  "Neutral Debater",
  "Portfolio Manager",
];

const STATUS_DOT_STYLES: Record<AgentStatus, string> = {
  pending: "bg-slate-300",
  in_progress: "bg-blue-500",
  completed: "bg-green-500",
  error: "bg-red-500",
};

const STATUS_LABEL: Record<AgentStatus, string> = {
  pending: "Pending",
  in_progress: "In progress",
  completed: "Completed",
  error: "Error",
};

export interface AgentStatusGridProps {
  /** Latest status event per agent name (already collapsed by useRun). */
  agents: AgentStatusEvent[];
  /** Roster to render. Defaults to {@link DEFAULT_AGENT_ROSTER}. */
  roster?: readonly string[];
  className?: string;
}

/**
 * Renders the full roster in execution order with a status dot per agent.
 * Agents missing from `agents` show as `pending`. Unknown agent names that
 * arrive from the wire are appended at the bottom (forward-compat).
 */
export function AgentStatusGrid({
  agents,
  roster = DEFAULT_AGENT_ROSTER,
  className,
}: AgentStatusGridProps) {
  const byName = new Map(agents.map((a) => [a.agent, a]));
  const known = new Set(roster);
  const unknown = agents.filter((a) => !known.has(a.agent));
  const all = [...roster, ...unknown.map((a) => a.agent)];

  return (
    <ul
      className={cn("flex flex-col divide-y rounded-md border bg-card", className)}
      aria-label="Agent status"
    >
      {all.map((name) => {
        const ev = byName.get(name);
        const status: AgentStatus = ev?.status ?? "pending";
        return (
          <li
            key={name}
            className="flex items-center gap-3 px-3 py-2 text-sm"
            data-agent={name}
            data-status={status}
          >
            <span className="relative flex h-2.5 w-2.5 items-center justify-center">
              <span
                className={cn(
                  "absolute inline-flex h-2.5 w-2.5 rounded-full",
                  STATUS_DOT_STYLES[status],
                )}
                aria-hidden
              />
            </span>
            <span className="flex-1 font-medium">{name}</span>
            {status === "in_progress" ? (
              <Loader2 className="h-4 w-4 animate-spin text-blue-500" aria-hidden />
            ) : null}
            <span className="text-xs text-muted-foreground">
              {STATUS_LABEL[status]}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export default AgentStatusGrid;
