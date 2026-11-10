import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { cellText, humanDate } from "../src/views/Tmf";
import { renderApp } from "./render";
import { BASE, server } from "./server";

const SITE_KEYS = [
  "irb_iec_decision",
  "form_fda_1572",
  "pi_cv",
  "site_signature_sheet",
  "protocol_signature_page",
  "financial_disclosure_form",
  "initiation_report",
  "monitoring_visit_report",
  "close_out_report",
];
const NAMES: Record<string, string> = {
  irb_iec_decision: "IRB or IEC Decision",
  form_fda_1572: "Form FDA 1572",
  pi_cv: "Principal Investigator Curriculum Vitae",
  site_signature_sheet: "Site Signature Sheet",
  protocol_signature_page: "Protocol Signature Page",
  financial_disclosure_form: "Financial Disclosure Form",
  initiation_report: "Trial Initiation Monitoring Report",
  monitoring_visit_report: "Monitoring Visit Report",
  close_out_report: "Final Trial Close Out Monitoring Report",
};
const MILESTONE: Record<string, string> = {
  initiation_report: "initiation_visit",
  monitoring_visit_report: "routine_visit",
  close_out_report: "close_out_visit",
};

function artifacts(): Schemas["TmfArtifact"][] {
  const trial: Schemas["TmfArtifact"][] = [
    {
      key: "protocol",
      name: "Protocol",
      zone: "Central Trial Documents",
      level: "trial",
      milestone: "trial_first_subject",
      file_within_days: 30,
      essential: true,
    },
    {
      key: "protocol_amendment",
      name: "Protocol Amendment",
      zone: "Central Trial Documents",
      level: "trial",
      milestone: "on_issue",
      file_within_days: 30,
      essential: true,
    },
  ];
  return [
    ...trial,
    ...SITE_KEYS.map((key) => ({
      key,
      name: NAMES[key] ?? key,
      zone: "Site Management",
      level: "site" as const,
      milestone: MILESTONE[key] ?? "site_activation",
      file_within_days: 30,
      essential: true,
    })),
  ];
}

function cell(
  artifact: string,
  over: Partial<Schemas["TmfCell"]> = {},
): Schemas["TmfCell"] {
  return {
    artifact,
    status: "filed",
    expected: 1,
    filed: 1,
    late: 0,
    finding_ids: [],
    ...over,
  };
}

function view(
  over: Partial<Schemas["TmfCompletenessView"]> = {},
): Schemas["TmfCompletenessView"] {
  return {
    study_id: "MRD-201",
    cut_id: "DC-BASE",
    scope: "operational",
    export_date: "2015-05-28",
    agent_id: "quality.tmf_completeness",
    agent_version: "1.0.0",
    run_completed: true,
    completeness_percent: 97.5,
    timely_percent: 98.1,
    trial_completeness_percent: 100,
    trial_timely_percent: 100,
    artifacts: artifacts(),
    trial: [
      cell("protocol"),
      cell("protocol_amendment", { expected: 0, filed: 0 }),
    ],
    sites: [
      {
        site_id: "1001",
        completeness_percent: 100,
        timely_percent: 100,
        cells: SITE_KEYS.map((k) =>
          cell(
            k,
            k === "monitoring_visit_report" ? { expected: 6, filed: 6 } : {},
          ),
        ),
      },
      {
        site_id: "1002",
        completeness_percent: 88.9,
        timely_percent: 87.5,
        cells: SITE_KEYS.map((k) =>
          k === "form_fda_1572"
            ? cell(k, { status: "missing", filed: 0, finding_ids: ["QA-0002"] })
            : k === "monitoring_visit_report"
              ? cell(k, {
                  status: "filed_late",
                  expected: 4,
                  filed: 4,
                  late: 1,
                  finding_ids: ["QA-0003"],
                })
              : k === "close_out_report"
                ? cell(k, { status: "not_expected", expected: 0, filed: 0 })
                : cell(k),
        ),
      },
    ],
    findings: [
      {
        finding_id: "QA-0002",
        check: "tmf_missing_artifact",
        site_id: "1002",
        severity: "high",
        status: "open",
        title: "Form FDA 1572 is missing from the TMF of site 1002",
      },
      {
        finding_id: "QA-0003",
        check: "tmf_late_filing",
        site_id: "1002",
        severity: "medium",
        status: "open",
        title:
          "Monitoring Visit Report of site 1002 filed 45 days after its date",
      },
    ],
    ...over,
  };
}

function serve(body: Schemas["TmfCompletenessView"] | number) {
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
          agent_runs_completed: 5,
        },
      ]),
    ),
    http.get(`${BASE}/quality/:cut/tmf`, ({ params }) => {
      asked.push(String(params.cut));
      return typeof body === "number"
        ? HttpResponse.json({ detail: "no" }, { status: body })
        : HttpResponse.json(body);
    }),
  );
  return asked;
}

describe("TMF page", () => {
  it("shows sites as rows, site records as columns, and links cells to their findings", async () => {
    const asked = serve(view());
    renderApp("/quality", "nina.fernandes");
    expect(
      await screen.findByRole("heading", { name: "Trial Master File" }),
    ).toBeTruthy();
    expect(asked).toEqual(["DC-BASE"]);
    const table = await screen.findByRole("table");
    const heads = within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(heads.slice(0, 3)).toEqual(["Site", "Complete", "On time"]);
    expect(heads).toContain("Delegation log");
    expect(heads).toHaveLength(3 + SITE_KEYS.length);
    const row = within(table).getByRole("row", { name: /^1002/ });
    expect(within(row).getByText("88.9%")).toBeTruthy();
    expect(within(row).getByText("Missing")).toBeTruthy();
    expect(within(row).getByText("4 of 4, 1 late")).toBeTruthy();
    expect(within(row).getByText("Form FDA 1572: Missing")).toBeTruthy(); // for screen readers
    const link = within(row).getAllByRole("link")[0];
    expect(link?.getAttribute("href")).toBe("/findings/QA-0002");
    expect(
      screen.getByText("Index exported").nextElementSibling?.textContent,
    ).toBe("28 May 2015");
    expect(screen.getByTestId("tmf-complete").textContent).toBe("97.5%");
    // Trial records as a short list, the amendment marked as filed on issue.
    const trial = screen
      .getByRole("heading", { name: "Trial records" })
      .closest("section");
    if (!trial) throw new Error("no trial panel");
    expect(within(trial).getByText("(filed when issued)")).toBeTruthy();
    // Findings list, and the legend.
    expect(screen.getByText("Expected record missing")).toBeTruthy();
    expect(
      screen.getByLabelText("How to read the site records").textContent,
    ).toContain("not yet expected");
    // No decision controls: the page is read-only for everyone, the QA auditor included.
    expect(
      screen.queryByRole("button", { name: /Accept|Escalate|Dismiss/ }),
    ).toBeNull();
  });

  it("is a live navigation item and the table scrolls by keyboard", async () => {
    serve(view());
    const user = userEvent.setup();
    renderApp("/review");
    const nav = await screen.findByRole("navigation", { name: "Views" });
    const item = within(nav).getByRole("link", { name: "Quality and audit" });
    expect(item.getAttribute("href")).toBe("/quality");
    await user.click(item);
    const wrap = await screen.findByLabelText(
      "Site records table, scrolls sideways",
    );
    expect(wrap.getAttribute("tabindex")).toBe("0");
  });

  it("says plainly when the role may not see it, and when the agent has not run", async () => {
    serve(403);
    const first = renderApp("/quality", "leela.nair");
    expect(
      await screen.findByText(
        "Your role does not review the Trial Master File.",
      ),
    ).toBeTruthy();
    first.unmount();
    serve(view({ run_completed: false, agent_version: null, findings: [] }));
    renderApp("/quality");
    expect(
      await screen.findByText(
        "The TMF completeness agent has not run on this cut yet.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("No finding on this cut.")).toBeTruthy();
  });

  it("formats dates and visit counts in plain words", () => {
    expect(humanDate("2013-01-20")).toBe("20 Jan 2013");
    expect(humanDate("not a date")).toBe("not a date");
    expect(
      cellText(
        cell("monitoring_visit_report", {
          expected: 6,
          filed: 5,
          status: "missing",
        }),
        true,
      ),
    ).toBe("5 of 6");
    expect(cellText(cell("pi_cv", { status: "not_expected" }), false)).toBe(
      "-",
    );
  });

  it("uses only design tokens for its colours", () => {
    const css = readFileSync("src/styles/app.css", "utf-8");
    const tmf = css.slice(css.indexOf("/* TMF matrix"));
    expect(tmf).not.toMatch(/#[0-9a-fA-F]{3,6}\b/);
    expect(tmf).toContain("var(--critical-soft)");
  });
});
