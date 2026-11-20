// The styles we control keep controls and tables inside the viewport. jsdom does no
// layout, so this checks the computed styles from the real stylesheets, not pixel widths.
import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { hasMoreRight, tooFewReason } from "../src/views/Rbqm";
import { renderApp } from "./render";
import { BASE, server } from "./server";

let style: HTMLStyleElement;
beforeEach(() => {
  style = document.createElement("style");
  style.textContent = ["tokens.css", "app.css"]
    .map((f) => readFileSync(`src/styles/${f}`, "utf-8")) // vitest runs from web/
    .join("\n");
  document.head.appendChild(style);
});
afterEach(() => {
  style.remove();
});

const rule = (over: Partial<Schemas["ColumnRule"]>): Schemas["ColumnRule"] => ({
  agent: "rbqm.kri_monitor",
  unit: "days",
  direction: "high",
  warning_from: 15.75,
  critical_beyond: 21,
  rule: "x",
  min_count: null,
  count_label: null,
  ...over,
});

const sites: Schemas["RbqmSites"] = {
  study_id: "MRD-201",
  cut_id: "DC-BASE",
  parameters: [],
  sites: [
    {
      site_id: "1002",
      treated: 3,
      tested: false,
      ae_rate_z: null,
      deviation_z: null,
      median_entry_lag_days: 2,
      median_open_query_age_days: 15.8,
      open_queries: 3,
      csm_log_sd_z: {},
      csm_sd_ratio: {},
      bands: {
        ae_rate_z: "na",
        deviation_z: "na",
        median_entry_lag_days: "normal",
        median_open_query_age_days: "too_few",
      },
      flagged_checks: [],
      flagged_by: [],
    },
  ],
  qtl: { name: "premature_discontinuation", rate: 0.567, limit: 0.617, approach_from: 0.597, status: "within", discontinued: 144, treated: 254 },
  columns: {
    ae_rate_z: rule({ unit: "z", direction: "low", warning_from: 2, critical_beyond: -3.17 }),
    deviation_z: rule({ unit: "z", warning_from: 2, critical_beyond: 3.17 }),
    median_entry_lag_days: rule({ warning_from: 5.25, critical_beyond: 7 }),
    median_open_query_age_days: rule({ min_count: 5, count_label: "open queries" }),
  },
};
const cuts: Schemas["CutSummary"][] = [
  {
    cut_id: "DC-SWEEP-DISCONTINUATION-DRIFT-PLUS0P1",
    trigger_seq: 1,
    kind: "sweep",
    label: null,
    opaque: false,
    scope: "evaluation",
    created: "2026-11-07T07:47:20Z",
    agent_runs_completed: 3,
  },
];

function showRbqm() {
  server.use(
    http.get(`${BASE}/studies/:s/cuts`, () => HttpResponse.json(cuts)),
    http.get(`${BASE}/rbqm/:cut/sites`, () => HttpResponse.json(sites)),
  );
  renderApp("/rbqm");
}

describe("nothing wider than the viewport", () => {
  it("the data cut select and the table container are capped at 100%", async () => {
    showRbqm();
    await screen.findByRole("heading", { name: "Site heatmap" });
    const select = screen.getByRole("combobox", { name: /Data cut/ });
    expect(getComputedStyle(select).maxWidth).toBe("100%");
    const wrap = document.querySelector(".heat-wrap");
    if (!wrap) throw new Error("no table container");
    expect(getComputedStyle(wrap).maxWidth).toBe("100%");
    expect(getComputedStyle(wrap).overflowX).toBe("auto");
    expect(getComputedStyle(screen.getByLabelText("Acting as")).maxWidth).toBe("100%");
  });

  it("keeps the site column sticky and its note on one line", async () => {
    showRbqm();
    const note = await screen.findByText("too small to test");
    expect(getComputedStyle(note).whiteSpace).toBe("nowrap");
    const rowHeader = note.closest("th");
    if (!rowHeader) throw new Error("no row header");
    expect(getComputedStyle(rowHeader).position).toBe("sticky");
    expect(getComputedStyle(rowHeader).textTransform).not.toBe("uppercase");
  });

  it("sets figures in running text in the UI face with tabular numbers", async () => {
    showRbqm();
    await screen.findByRole("heading", { name: /Quality tolerance limit/ });
    const figure = screen.getByText("56.7%");
    expect(figure.className).toBe("fig");
    expect(getComputedStyle(figure).fontVariantNumeric).toBe("tabular-nums");
    expect(getComputedStyle(figure).fontFamily).not.toContain("Mono");
  });
});

describe("open query age below the minimum count", () => {
  it("is muted, with the reason in the title and for screen readers", async () => {
    showRbqm();
    await screen.findByRole("heading", { name: "Site heatmap" });
    const cell = [...document.querySelectorAll("td.cell")].find((c) => c.textContent.startsWith("15.8"));
    if (!cell) throw new Error("no query age cell");
    expect(cell.className).toBe("cell too_few");
    const reason = "too few open queries (3; rbqm.kri_monitor needs at least 5)";
    expect(cell.getAttribute("title")).toBe(reason);
    expect(cell.querySelector(".visually-hidden")?.textContent).toBe(`, ${reason}`);
  });

  it("has no reason for a column without a minimum", () => {
    expect(tooFewReason(rule({}), 2)).toBeUndefined();
    expect(tooFewReason(undefined, 2)).toBeUndefined();
  });
});

describe("scroll hint", () => {
  it("is on while columns are hidden to the right", () => {
    expect(hasMoreRight({ scrollLeft: 0, clientWidth: 320, scrollWidth: 900 })).toBe(true);
    expect(hasMoreRight({ scrollLeft: 580, clientWidth: 320, scrollWidth: 900 })).toBe(false);
    expect(hasMoreRight({ scrollLeft: 0, clientWidth: 900, scrollWidth: 900 })).toBe(false);
  });
});
