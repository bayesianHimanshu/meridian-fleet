import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderApp } from "./render";
import { seen } from "./server";

describe("review queue", () => {
  it("lists open and escalated findings newest first, with the narrative headline", async () => {
    renderApp("/review");
    const list = await screen.findByRole("list", { name: "Findings to review" });
    const items = within(list).getAllByRole("listitem");
    expect(items.map((li) => within(li).getByRole("heading").textContent)).toEqual([
      "Site 1016: finding RBQM-0003",
      "Site 1001: vital signs vary unusually little",
      "Premature discontinuation breached its QTL",
    ]);
    const [first, , third] = items;
    if (!first || !third) throw new Error("expected three findings");
    expect(within(first).getByText("Site 1016: 18 protocol deviations against 5.1 expected")).toBeTruthy();
    expect(within(third).getByText("Study-wide")).toBeTruthy();
    expect(within(third).getByText("escalated")).toBeTruthy();
    expect(seen.some((s) => s.path === "/api/v1/review-queue?scope=operational")).toBe(true);
  });

  it("filters by severity, agent and site", async () => {
    const user = userEvent.setup();
    renderApp("/review");
    await screen.findByRole("list", { name: "Findings to review" });
    await user.selectOptions(screen.getByLabelText("Severity"), "medium");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    await user.selectOptions(screen.getByLabelText("Agent"), "rbqm.csm");
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    await user.selectOptions(screen.getByLabelText("Site"), "1016");
    expect(screen.getByText("Nothing waits for review with these filters.")).toBeTruthy();
    expect(screen.getByText("Showing 0 of 3 findings.")).toBeTruthy();
  });

  it("asks for evaluation cuts only when the reader opts in", async () => {
    const user = userEvent.setup();
    renderApp("/review");
    await screen.findByRole("list", { name: "Findings to review" });
    await user.click(screen.getByLabelText("Include evaluation cuts"));
    await screen.findByText("Showing 3 of 3 findings.");
    expect(seen.some((s) => s.path === "/api/v1/review-queue?scope=all")).toBe(true);
  });
});
