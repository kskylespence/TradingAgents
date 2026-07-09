import { cn } from "@/lib/utils";
import type { Rating } from "@/lib/types";

/**
 * 5-tier rating pill.
 *
 * Colour mapping is canonical (see plan: "DecisionBadge"); each rating maps
 * to a single Tailwind palette pair. We render as a plain pill rather than
 * extending the shadcn Badge variants so the strong colour stays consistent
 * across light / dark themes (the shadcn Badge variants tint via CSS vars).
 */

const RATING_STYLES: Record<Rating, string> = {
  Buy: "bg-market-up text-black shadow-[0_0_20px_hsl(var(--market-up)/0.35)]",
  Overweight: "bg-green-400 text-black",
  Hold: "bg-slate-500 text-white",
  Underweight: "bg-orange-500 text-white",
  Sell: "bg-market-down text-white shadow-[0_0_20px_hsl(var(--market-down)/0.35)]",
};

export interface DecisionBadgeProps {
  rating: Rating;
  className?: string;
}

export function DecisionBadge({ rating, className }: DecisionBadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-3 py-1 text-sm font-semibold tracking-wide",
        RATING_STYLES[rating],
        className,
      )}
      aria-label={`Recommendation: ${rating}`}
    >
      {rating}
    </span>
  );
}

export default DecisionBadge;
