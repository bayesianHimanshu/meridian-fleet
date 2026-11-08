import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { CHECK_TEXT } from "../src/views/Safety";
import { renderApp } from "./render";
import { BASE, server } from "./server";

const CHECKS = Object.keys(CHECK_TEXT);

function rec(items: Schemas["ReconciliationItem"][], over: Partial<Schemas["Reconciliation"]> = {}) {
  const byCheck = CHECKS.map((check) => ({ check, count: items.filter((i) => i.check === check).length }));
  const sites = [...new Set(items.map((i) => i.site_id).filter((s): s is string => s !== null))].sort();
  const body: Schemas["Reconciliation"] = {
    study_id: "MRD-201",
    cut_id: "DC-BASE",
    scope: "operational",
    agent_id: "safety.sae_reconciler",
    agent_version: "1.0.0",
    run_completed: true,
    by_check: byCheck,
    by_site: sites.map((site_id) => ({ site_id, count: items.filter((i) => i.site_id === site_id).length })),
    items,
    ...over,
  };
  return body;
}

const outcome: Schemas["ReconciliationItem"] = {
  finding_id: "SAF-0004",
  check: "outcome_mismatch",
  site_id: "1018",
  subject_id: "201-1018-006",
  severity: "medium",
  status: "open",
  title: "Outcome of a serious AE of 201-1018-006 differs from its case",
  left_label: "Clinical outcome",
  left: "RECOVERED/RESOLVED",
  right_label: "Case outcome",
  right: "FATAL",
};
const site: Schemas["ReconciliationItem"] = {
  finding_id: "SAF-0005",
  check: "sae_late_reporting_site",
  site_id: "1018",
  subject_id: null,
  severity: "high",
  status: "open",
  title: "Site 1018 reports serious AEs late",
  left_label: "Late cases",
  left: "2 of 2 cases late",
  right_label: "Median delay",
  right: "median 60 hours after awareness",
};

function serve(body: Schemas["Reconciliation"]) {
  const asked: string[] = [];
  server.use(
    http.get(`${BASE}/studies/:s/cuts`, () =>
      HttpResponse.json([
        {
          cut_id: "DC-BASE",
          trigger_seq: 1,
          kind: "base",
          label: null,
          opaque: false,
          scope: "operational",
          created: "2026-11-07T07:47:20Z",
          agent_runs_completed: 4,
        },
      ]),
    ),
    http.get(`${BASE}/safety/:cut/reconciliation`, ({ params }) => {
      asked.push(String(params.cut));
      return HttpResponse.json(body);
    }),
  );
  return asked;
}

describe("SAE reconciliation page", () => {
  it("shows counts by check and site, and each discrepancy with its two values side by side", async () => {
    const asked = serve(rec([outcome, site]));
    renderApp("/safety");
    expect(await screen.findByRole("heading", { name: "SAE reconciliation" })).toBeTruthy();
    expect(asked).toEqual(["DC-BASE"]);
    const byCheck = (await screen.findByRole("heading", { name: "By check" })).closest("section");
    if (!byCheck) throw new Error("no by-check panel");
    const outcomeRow = within(byCheck).getByRole("row", { name: /Outcome differs/ });
    expect(within(outcomeRow).getByRole("cell").textContent).toBe("1");
    const zero = within(byCheck).getByRole("row", { name: /Onset dates differ/ });
    expect(within(zero).getByRole("cell").textContent).toBe("0");
    const link = screen.getByRole("link", { name: "SAF-0004" });
    expect(link.getAttribute("href")).toBe("/findings/SAF-0004");
    const card = link.closest("li");
    if (!card) throw new Error("no card");
    const compare = card.querySelector("dl.compare");
    expect(compare?.textContent).toBe("Clinical outcomeRECOVERED/RESOLVEDCase outcomeFATAL");
    expect(card.textContent).toContain("subject 201-1018-006");
    const siteCard = screen.getByRole("link", { name: "SAF-0005" }).closest("li");
    expect(siteCard?.textContent).not.toContain("subject");
    expect(screen.getByText("2 you may see")).toBeTruthy();
  });

  it("says plainly when there is nothing to see, and when the agent has not run", async () => {
    serve(rec([]));
    const view = renderApp("/safety");
    expect(await screen.findByText("No discrepancies you may see on this cut.")).toBeTruthy();
    expect(screen.getByText("No site has a discrepancy you may see.")).toBeTruthy();
    view.unmount();
    serve(rec([], { run_completed: false, agent_version: null }));
    renderApp("/safety");
    expect(await screen.findByText("The reconciliation agent has not run on this cut yet.")).toBeTruthy();
    expect(screen.queryByText("No discrepancies you may see on this cut.")).toBeNull();
  });

  it("is a live item in the navigation", async () => {
    serve(rec([]));
    renderApp("/review");
    const nav = await screen.findByRole("navigation", { name: "Views" });
    expect(within(nav).getByRole("link", { name: "Safety" }).getAttribute("href")).toBe("/safety");
  });
});

describe("unblinded zone", () => {
  it("tells the unblinded statistician how many cases carry treatment, never the treatment", async () => {
    server.use(
      http.get(`${BASE}/findings`, () => HttpResponse.json({ items: [], total: 0, limit: 500, offset: 0 })),
      http.get(`${BASE}/safety/unblinded-cases`, () =>
        HttpResponse.json({ cut_id: "DC-BASE", cases: 3, cases_with_treatment: 1 }),
      ),
    );
    renderApp("/unblinded", "omar.siddiqui");
    const line = await screen.findByTestId("unblinded-cases");
    expect(line.textContent).toBe("On cut DC-BASE, 1 of 3 safety cases carry an unblinded treatment field.");
  });
});
