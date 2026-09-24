import { describe, expect, it } from "vitest";

import { cn, formatElapsed, formatTokens } from "@/lib/utils";

describe("utils", () => {
  it("merges class names with tailwind-merge conflict resolution", () => {
    expect(cn("p-2 p-4")).toBe("p-4");
    expect(cn("text-sm", undefined, false && "hidden", "font-bold")).toBe(
      "text-sm font-bold",
    );
  });

  it("resolves Tailwind v4 class syntax (tailwind-merge must match Tailwind)", () => {
    // The v4 codemod rewrote h-[var(--x)] as h-(--x) in components/ui/select
    // and toast, and outline-none as outline-hidden. tailwind-merge v2 does
    // not parse either and keeps both classes, so a className override on
    // those components would silently not apply.
    expect(cn("h-(--radix-select-trigger-height)", "h-8")).toBe("h-8");
    expect(cn("min-w-(--radix-select-trigger-width)", "min-w-32")).toBe("min-w-32");
    expect(cn("translate-x-(--radix-toast-swipe-end-x)", "translate-x-0")).toBe(
      "translate-x-0",
    );
    expect(cn("outline-hidden", "outline-dashed")).toBe("outline-dashed");
  });

  it("formats elapsed seconds", () => {
    expect(formatElapsed(0)).toBe("0s");
    expect(formatElapsed(45)).toBe("45s");
    expect(formatElapsed(65)).toBe("1m 5s");
    expect(formatElapsed(3725)).toBe("1h 2m 5s");
    expect(formatElapsed(null)).toBe("—");
    expect(formatElapsed(undefined)).toBe("—");
  });

  it("formats token counts", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(1234)).toBe("1,234");
    expect(formatTokens(null)).toBe("—");
  });
});
