import { describe, expect, it } from "vitest";

import { cn, formatElapsed, formatTokens } from "@/lib/utils";

describe("utils", () => {
  it("merges class names with tailwind-merge conflict resolution", () => {
    expect(cn("p-2 p-4")).toBe("p-4");
    expect(cn("text-sm", undefined, false && "hidden", "font-bold")).toBe(
      "text-sm font-bold",
    );
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
