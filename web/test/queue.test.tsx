import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { secondLine } from "../src/views/ReviewQueue";
import { renderApp } from "./render";
import { BASE, finding, seen, server } from "./server";

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
    expect(within(third).getByText("Escalated")).toBeTruthy();
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


describe("the line under the title", () => {
  it("is the accepted narrative headline when there is one", () => {
    const f = finding("RBQM-0009", {
      latest_narrative: {
        narrator_version: "1.1.0",
        headline: "A headline",
        explanation: "x",
        seq: 1,
        narrator_id: "rbqm.narrator",
        model: "m",
      },
    });
    expect(secondLine(f)).toBe("A headline");
  });

  it("falls back to the primary evidence displays, never the template summary", async () => {
    const f = finding("RBQM-0009");
    expect(secondLine(f)).toBe("18 observed (5.1 expected); Bonferroni-adjusted p = 1.6e-06 (threshold 0.01)");
    expect(secondLine(finding("RBQM-0010", { primary_evidence: [] }))).toBe("");
    renderApp("/review");
    await screen.findByRole("list", { name: "Findings to review" });
    expect(screen.queryByText(/the rest of the study implies 5.1/)).toBeNull(); // the summary
    expect(screen.getAllByText("18 observed (5.1 expected); Bonferroni-adjusted p = 1.6e-06 (threshold 0.01)")).toHaveLength(2);
  });
});

describe("empty queue", () => {
  it("says so plainly and offers to include evaluation cuts", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`${BASE}/review-queue`, ({ request }) => {
        const url = new URL(request.url);
        seen.push({ method: "GET", path: url.pathname + url.search, actor: request.headers.get("X-Meridian-Actor"), role: request.headers.get("X-Meridian-Role"), body: null });
        return HttpResponse.json(url.searchParams.get("scope") === "all" ? { items: [finding("RBQM-0050", { scope: "evaluation", cut_id: "DC-RBQM-CORE" })] } : { items: [] });
      }),
    );
    renderApp("/review");
    expect(await screen.findByText("No open findings on the study's current data.")).toBeTruthy();
    const checkbox = screen.getByRole("checkbox", { name: "Include evaluation cuts" });
    expect((checkbox as HTMLInputElement).checked).toBe(false);
    await user.click(screen.getByRole("button", { name: "Include evaluation cuts" }));
    expect((checkbox as HTMLInputElement).checked).toBe(true);
    expect(await screen.findByText("Site 1016: finding RBQM-0050")).toBeTruthy();
    expect(seen.some((s) => s.path === "/api/v1/review-queue?scope=all")).toBe(true);
  });
});
