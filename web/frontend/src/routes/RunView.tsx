import { useMutation } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import { AgentStatusGrid } from "@/components/AgentStatusGrid";
import { DecisionBadge } from "@/components/DecisionBadge";
import { MessageLog } from "@/components/MessageLog";
import { ReportPanel } from "@/components/ReportPanel";
import { StatsBar } from "@/components/StatsBar";
import { WallTimePanel } from "@/components/WallTimePanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useToast } from "@/hooks/use-toast";
import { useRun } from "@/hooks/useRun";
import { ApiError, api } from "@/lib/api";
import type {
  InvestmentDebateEvent,
  RiskDebateEvent,
  RunStatus,
  ToolCallEvent,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Live-run dashboard. Layout matches the CLI's Rich panel:
 *
 *   ┌─ header (ticker • date • models • decision • cancel/resume) ─┐
 *   │ progress bar                                                  │
 *   ├──────────────┬───────────────────────────────────────────────┤
 *   │ agent grid   │ tabs: Messages / Report / Debate / Tools      │
 *   ├──────────────┴───────────────────────────────────────────────┤
 *   │ stats + per-analyst wall-time                                 │
 *   └───────────────────────────────────────────────────────────────┘
 */
export default function RunView() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const result = useRun(runId);
  const {
    run,
    agents,
    messages,
    reportSections,
    toolCalls,
    investmentDebate,
    riskDebate,
    analystWallTimes,
    stats,
    progress,
    finalRating,
    runStatus,
    errorMessage,
  } = result;

  // --- Cancel mutation ---
  const cancelMutation = useMutation({
    mutationFn: () => api.post<void>(`/api/runs/${runId}/cancel`),
    onSuccess: () => {
      toast({
        title: "Cancellation requested",
        description:
          "The run will stop at the next graph boundary (typically tens of seconds).",
      });
    },
    onError: (err) => {
      toast({
        title: "Cancel failed",
        description:
          err instanceof ApiError ? err.message : "Unknown error",
        variant: "destructive",
      });
    },
  });

  // --- Resume mutation ---
  // Backend creates a NEW run from the checkpoint and returns its id; we
  // navigate to it so the live SSE stream + UI follow the resumed run
  // instead of staying on the parent's dead stream.
  const resumeMutation = useMutation({
    mutationFn: () =>
      api.post<{ run_id: string; parent_run_id: string }>(
        `/api/runs/${runId}/resume`,
      ),
    onSuccess: (data) => {
      toast({
        title: "Resume requested",
        description: "Following the new run from the last checkpoint.",
      });
      navigate(`/runs/${data.run_id}`);
    },
    onError: (err) => {
      toast({
        title: "Resume failed",
        description:
          err instanceof ApiError ? err.message : "Unknown error",
        variant: "destructive",
      });
    },
  });

  // --- Retry mutation ---
  // Sibling-run from a failed/cancelled parent. Backend reconstructs the
  // RunRequest from the parent's persisted columns and queues a fresh
  // run — same shape as /resume, but no checkpoint dependency.
  const retryMutation = useMutation({
    mutationFn: () =>
      api.post<{ run_id: string; parent_run_id: string }>(
        `/api/runs/${runId}/retry`,
      ),
    onSuccess: (data) => {
      toast({
        title: "Retry queued",
        description: "Following the new run.",
      });
      navigate(`/runs/${data.run_id}`);
    },
    onError: (err) => {
      toast({
        title: "Retry failed",
        description:
          err instanceof ApiError ? err.message : "Unknown error",
        variant: "destructive",
      });
    },
  });
  const canRetry = runStatus === "failed" || runStatus === "cancelled";

  if (!runId) {
    return <NotFound />;
  }

  // --- Loading / error gates ---
  // We only block on the initial baseline fetch; the SSE stream may still be
  // connecting after the page renders.
  if (run === undefined && !errorMessage && runStatus === "queued") {
    return <LoadingState />;
  }

  return (
    <div className="container flex flex-col gap-4 py-6">
      {/* Header */}
      <Card>
        <CardHeader className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-3">
              <CardTitle className="text-2xl font-bold tracking-tight">
                {run?.ticker ?? "—"}
              </CardTitle>
              {run?.asset_type ? (
                <Badge variant="secondary" className="uppercase">
                  {run.asset_type}
                </Badge>
              ) : null}
              <StatusPill status={runStatus} />
              {finalRating ? <DecisionBadge rating={finalRating} /> : null}
            </div>
            <CardDescription>
              {run?.analysis_date ? `Analysis date: ${run.analysis_date}` : "—"}
              {run?.llm_provider ? ` • Provider: ${run.llm_provider}` : ""}
              {run?.quick_think_llm
                ? ` • Quick: ${run.quick_think_llm}`
                : ""}
              {run?.deep_think_llm
                ? ` • Deep: ${run.deep_think_llm}`
                : ""}
            </CardDescription>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {runStatus === "running" ? (
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={() => cancelMutation.mutate()}
                disabled={cancelMutation.isPending}
              >
                {cancelMutation.isPending ? "Cancelling…" : "Cancel"}
              </Button>
            ) : null}
            {runStatus === "interrupted" || run?.resumable ? (
              <Button
                type="button"
                variant="default"
                size="sm"
                onClick={() => resumeMutation.mutate()}
                disabled={resumeMutation.isPending}
              >
                {resumeMutation.isPending ? "Resuming…" : "Resume"}
              </Button>
            ) : null}
            {canRetry ? (
              <Button
                type="button"
                variant="default"
                size="sm"
                onClick={() => retryMutation.mutate()}
                disabled={retryMutation.isPending}
              >
                {retryMutation.isPending ? "Retrying…" : "Retry"}
              </Button>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <ProgressBar value={progress} status={runStatus} />
          {errorMessage ? (
            <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive sm:flex-row sm:items-start sm:justify-between">
              <span className="flex-1">{errorMessage}</span>
              {canRetry ? (
                <Button
                  type="button"
                  variant="default"
                  size="sm"
                  className="shrink-0"
                  onClick={() => retryMutation.mutate()}
                  disabled={retryMutation.isPending}
                >
                  {retryMutation.isPending ? "Retrying…" : "Retry"}
                </Button>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>

      {/* Two-column body */}
      <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
        {/* Left: agent grid */}
        <div className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Pipeline
          </h2>
          <AgentStatusGrid agents={agents} />
        </div>

        {/* Right: tabs */}
        <Tabs defaultValue="messages" className="flex flex-col">
          <TabsList className="self-start">
            <TabsTrigger value="messages">Messages</TabsTrigger>
            <TabsTrigger value="report">Report</TabsTrigger>
            <TabsTrigger value="debate">Debate</TabsTrigger>
            <TabsTrigger value="tools">Tools</TabsTrigger>
          </TabsList>

          <TabsContent value="messages">
            <MessageLog messages={messages} />
          </TabsContent>

          <TabsContent value="report">
            <ReportPanel sections={reportSections} />
          </TabsContent>

          <TabsContent value="debate">
            <DebatePanel
              investment={investmentDebate}
              risk={riskDebate}
            />
          </TabsContent>

          <TabsContent value="tools">
            <ToolCallsPanel toolCalls={toolCalls} />
          </TabsContent>
        </Tabs>
      </div>

      {/* Footer */}
      <Card>
        <CardContent className="flex flex-col gap-4 py-4">
          <StatsBar stats={stats} />
          <WallTimePanel times={analystWallTimes} />
        </CardContent>
      </Card>
    </div>
  );
}

// ---- helpers ----

function ProgressBar({ value, status }: { value: number; status: RunStatus }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const isError = status === "failed" || status === "cancelled";
  return (
    <div
      className="h-2 w-full overflow-hidden rounded-full bg-muted"
      role="progressbar"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className={cn(
          "h-full transition-[width] duration-300 ease-out",
          isError ? "bg-destructive" : "bg-primary",
        )}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

const STATUS_PILL: Record<RunStatus, string> = {
  queued: "bg-slate-200 text-slate-700",
  running: "bg-blue-100 text-blue-700",
  completed: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  cancelled: "bg-amber-100 text-amber-700",
  interrupted: "bg-orange-100 text-orange-700",
};

function StatusPill({ status }: { status: RunStatus }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide",
        STATUS_PILL[status],
      )}
    >
      {status}
    </span>
  );
}

function DebatePanel({
  investment,
  risk,
}: {
  investment: InvestmentDebateEvent | undefined;
  risk: RiskDebateEvent | undefined;
}) {
  if (!investment && !risk) {
    return (
      <div className="rounded-md border bg-card p-4 text-sm text-muted-foreground">
        Debate transcripts appear here once the researchers and risk team
        weigh in.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      {investment ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Investment Debate</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <DebateBlock title="Bull" body={investment.bull} />
            <DebateBlock title="Bear" body={investment.bear} />
            <DebateBlock title="Research Manager" body={investment.judge} />
          </CardContent>
        </Card>
      ) : null}
      {risk ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Risk Debate</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <DebateBlock title="Aggressive" body={risk.aggressive} />
            <DebateBlock title="Conservative" body={risk.conservative} />
            <DebateBlock title="Neutral" body={risk.neutral} />
            <DebateBlock title="Portfolio Manager" body={risk.judge} />
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

function DebateBlock({ title, body }: { title: string; body: string | undefined }) {
  if (!body) return null;
  return (
    <div className="flex flex-col gap-1">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <pre className="whitespace-pre-wrap break-words rounded-md border bg-muted/30 p-3 font-sans text-sm leading-relaxed">
        {body}
      </pre>
    </div>
  );
}

function ToolCallsPanel({ toolCalls }: { toolCalls: ToolCallEvent[] }) {
  if (toolCalls.length === 0) {
    return (
      <div className="rounded-md border bg-card p-4 text-sm text-muted-foreground">
        Tool invocations appear here in real time.
      </div>
    );
  }
  return (
    <div className="h-[28rem] overflow-y-auto rounded-md border bg-card">
      <ul className="divide-y">
        {toolCalls
          .slice()
          .reverse()
          .map((c, idx) => (
            <li
              key={`${c.seq}-${idx}`}
              className="flex flex-col gap-1 px-3 py-2 text-xs"
            >
              <div className="flex items-center gap-2">
                <span className="font-mono font-semibold">{c.name}</span>
                <span className="ml-auto text-muted-foreground">
                  {formatTime(c.timestamp)}
                </span>
              </div>
              <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded bg-muted/40 px-2 py-1 font-mono text-[11px] text-muted-foreground">
                {JSON.stringify(c.args, null, 2)}
              </pre>
            </li>
          ))}
      </ul>
    </div>
  );
}

function formatTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString(undefined, { hour12: false });
}

function LoadingState() {
  return (
    <div className="container flex flex-col gap-4 py-6">
      <Card>
        <CardHeader>
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-72" />
        </CardHeader>
        <CardContent>
          <Skeleton className="h-2 w-full" />
        </CardContent>
      </Card>
      <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
        <Skeleton className="h-96 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
    </div>
  );
}

function NotFound() {
  return (
    <div className="container py-16">
      <Card>
        <CardHeader>
          <CardTitle>Run not found</CardTitle>
          <CardDescription>
            We couldn't find this run. It may have been deleted or never
            existed.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Link
            to="/history"
            className="text-sm font-medium text-primary underline underline-offset-4"
          >
            Back to history →
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}
