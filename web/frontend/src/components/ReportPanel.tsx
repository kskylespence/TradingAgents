import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * Canonical ordered section list. Mirrors the plan's ReportSectionKey union
 * and the order downstream report-rendering code expects.
 */
export const REPORT_SECTION_ORDER: readonly string[] = [
  "final_trade_decision",
  "market_report",
  "sentiment_report",
  "news_report",
  "fundamentals_report",
  "investment_plan",
  "trader_investment_plan",
];

const SECTION_LABELS: Record<string, string> = {
  market_report: "Market Report",
  sentiment_report: "Sentiment Report",
  news_report: "News Report",
  fundamentals_report: "Fundamentals Report",
  investment_plan: "Investment Plan",
  trader_investment_plan: "Trader Investment Plan",
  final_trade_decision: "Final Trade Decision",
};

function labelFor(section: string): string {
  if (SECTION_LABELS[section]) return SECTION_LABELS[section];
  // Forward-compat: prettify unknown section keys.
  return section
    .split("_")
    .map((w) => (w.length ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

export interface ReportPanelProps {
  sections: Record<string, string>;
  className?: string;
}

/**
 * Renders the report sections that are present, in canonical order, as
 * collapsible cards. Markdown is rendered via ``react-markdown`` with
 * ``remark-gfm`` for GitHub-flavored tables / lists / strikethrough.
 */
export function ReportPanel({ sections, className }: ReportPanelProps) {
  // Render in canonical order, then any unknown sections (forward-compat).
  const known = new Set(REPORT_SECTION_ORDER);
  const present = REPORT_SECTION_ORDER.filter((k) => sections[k]);
  const extras = Object.keys(sections).filter((k) => !known.has(k));
  const order = [...present, ...extras];

  if (order.length === 0) {
    return (
      <div
        className={cn(
          "rounded-md border bg-card p-4 text-sm text-muted-foreground",
          className,
        )}
      >
        No report sections yet. They appear here as agents complete.
      </div>
    );
  }

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {order.map((key) => (
        <ReportSection key={key} title={labelFor(key)} content={sections[key]} />
      ))}
    </div>
  );
}

interface ReportSectionProps {
  title: string;
  content: string;
  defaultOpen?: boolean;
}

function ReportSection({ title, content, defaultOpen = true }: ReportSectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  const isDecision = title === "Final Trade Decision";
  return (
    <Card className={isDecision ? "border-emerald-700/50 bg-emerald-950/20" : undefined}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
        aria-expanded={open}
      >
        <CardHeader className="flex-1 cursor-pointer flex-row items-center gap-2 py-3">
          {open ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" aria-hidden />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" aria-hidden />
          )}
          <CardTitle className="text-base">{title}</CardTitle>
        </CardHeader>
      </button>
      {open ? (
        <CardContent>
          <div className="prose prose-sm max-w-none text-foreground">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
          </div>
        </CardContent>
      ) : null}
    </Card>
  );
}

export default ReportPanel;
