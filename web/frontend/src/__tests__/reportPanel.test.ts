/** @vitest-environment jsdom */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { REPORT_SECTION_ORDER, ReportPanel } from "@/components/ReportPanel";

vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));
vi.mock("remark-gfm", () => ({ default: () => {} }));

describe("ReportPanel", () => {
  it("renders final_trade_decision first", () => {
    const sections = {
      market_report: "market",
      final_trade_decision: "decision",
      news_report: "news",
    };
    render(<ReportPanel sections={sections} />);
    const titles = screen.getAllByRole("button").map((el) => el.textContent);
    expect(titles[0]).toContain("Final Trade Decision");
    expect(REPORT_SECTION_ORDER[0]).toBe("final_trade_decision");
  });
});
