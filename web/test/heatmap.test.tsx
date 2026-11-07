import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { bandsText } from "../src/views/Rbqm";
import { renderApp } from "./render";
import { BASE, server } from "./server";

type Rule = Schemas["ColumnRule"];

const rule = (over: Partial<Rule>): Rule => ({
  agent: "rbqm.kri_monitor",
  unit: "z",
  direction: "low",
  warning_from: 2,
  critical_beyond: -3.1683,
  rule: "x",
  ...over,
});

describe("legend text", () => {
  it("states each column's bands with the agent's numbers", () => {
    expect(bandsText(rule({}))).toBe(
      "warning from z -2 to -3.17, critical below -3.17; positive z: no fill (not tested)",
    );
    expect(bandsText(rule({ direction: "high", critical_beyond: 3.1683 }))).toBe(
      "warning from z +2 to +3.17, critical above +3.17; negative z: no fill (not tested)",
    );
    expect(bandsText(rule({ agent: "rbqm.csm", critical_beyond: -3.5 }))).toBe(
      "warning from z -2 to -3.5, critical below -3.5; positive z: no fill (not tested)",
    );
    expect(bandsText(rule({ unit: "days", direction: "high", warning_from: 5.25, critical_beyond: 7 }))).toBe(
      "warning above 5.25 days, critical above 7 days",
    );
  });
});

const columns: Record<string, Rule> = {
  ae_rate_z: rule({}),
  deviation_z: rule({ direction: "high", critical_beyond: 3.1683 }),
  median_entry_lag_days: rule({ unit: "days", direction: "high", warning_from: 5.25, critical_beyond: 7 }),
  median_open_query_age_days: rule({ unit: "days", direction: "high", warning_from: 15.75, critical_beyond: 21 }),
  "csm_log_sd_z:SYSBP": rule({ agent: "rbqm.csm", critical_beyond: -3.5 }),
};

const site: Schemas["SiteMetrics"] = {
  site_id: "1008",
  treated: 20,
  tested: true,
  ae_rate_z: 2.6,
  deviation_z: 3.6,
  median_entry_lag_days: 6,
  median_open_query_age_days: null,
  open_queries: null,
  csm_log_sd_z: { SYSBP: 4.1 },
  csm_sd_ratio: { SYSBP: 1.8 },
  bands: {
    ae_rate_z: "untested_direction",
    deviation_z: "critical",
    median_entry_lag_days: "warning",
    median_open_query_age_days: "na",
    "csm_log_sd_z:SYSBP": "untested_direction",
  },
  flagged_checks: ["deviations"],
  flagged_by: [
    { check: "deviations", finding_id: "RBQM-0134", agent_id: "rbqm.kri_monitor", agent_version: "2.2.0", status: "dismissed", parameters: [] },
  ],
};

describe("heatmap", () => {
  it("colours cells from the API's bands and outlines current findings", async () => {
    const sites: Schemas["RbqmSites"] = {
      study_id: "MRD-201",
      cut_id: "DC-BASE",
      parameters: ["SYSBP"],
      sites: [site],
      qtl: { name: "premature_discontinuation", rate: 0.5, limit: 0.617, approach_from: 0.597, status: "within", discontinued: 1, treated: 2 },
      columns,
    };
    const cuts: Schemas["CutSummary"][] = [
      { cut_id: "DC-BASE", trigger_seq: 1, kind: "base", label: null, opaque: false, scope: "operational", created: "2026-11-07T07:47:20Z", agent_runs_completed: 3 },
    ];
    server.use(
      http.get(`${BASE}/studies/:s/cuts`, () => HttpResponse.json(cuts)),
      http.get(`${BASE}/rbqm/:cut/sites`, () => HttpResponse.json(sites)),
    );
    renderApp("/rbqm");
    await screen.findByRole("heading", { name: "Site heatmap" });
    const cells = [...document.querySelectorAll("table.heat td.cell")];
    expect(cells.map((c) => c.className)).toEqual([
      "cell untested_direction",
      "cell critical flagged",
      "cell warning",
      "cell na",
      "cell untested_direction",
    ]);
    expect(document.querySelectorAll("td.critical:not(.flagged)")).toHaveLength(0);
    expect(cells[1]?.getAttribute("title")).toBe("Flagged by RBQM-0134, rbqm.kri_monitor 2.2.0 (dismissed)");
    expect(screen.getByText(/RBQM-0134 at site 1008 by rbqm.kri_monitor 2.2.0/)).toBeTruthy();
    expect(screen.getByText(/critical below -3.5/)).toBeTruthy();
    const row = screen.getByRole("rowheader");
    expect(row.querySelector(".mono")?.textContent).toBe("1008");
  });
});
