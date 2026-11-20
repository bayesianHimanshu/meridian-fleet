import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { renderApp } from "./render";
import { BASE, server } from "./server";

const OUTLOOK: Schemas["OutlookView"] = {
  status: "outlook", seq: 20200, recorded_at: "2026-11-20T10:00:00Z", pending_reviews: [], narrated: false,
  outlook: {
    type: "portfolio_outlook", agent_id: "portfolio.pipeline_intelligence", agent_version: "1.0.0", trigger_seq: 20190,
    review_id: "REVIEW-20261120T100000Z", dedup_key: "k", summary: "s", previous_outlook_seq: null,
    assets: [
      { asset_id: "MRD-101", status: "at_risk", static: false, status_text: "At risk",
        facts: [{ key: "noael:combined", group: "noael", label: "NOAEL (combined)", value: "2 mg/kg/day",
                  status: "established", seq: 19943, static: false }],
        risks: [{ text: "High-severity finding PRE-0155: Myocardial inflammation (open)", severity: "high", seqs: [155] }],
        more_risks: 2, open_findings: 93,
        changes: ["First outlook: nothing to compare with."] },
      { asset_id: "MRD-110", status: "static", static: true, status_text: "On track",
        facts: [{ key: "static:stage", group: "static", label: "Stage", value: "phase_1", status: "static", seq: null, static: true }],
        risks: [], more_risks: 0, open_findings: 0, changes: ["First outlook: nothing to compare with."] },
    ],
  },
};

const PACK: Schemas["GatePack"] = {
  type: "gate_pack", agent_id: "portfolio.stage_gate_pack", agent_version: "1.0.0", trigger_seq: 20201,
  gate_request_id: "GATE-20261120T100100Z", dedup_key: "k2", asset_id: "MRD-101", gate: "ind_submission",
  gate_title: "IND submission", summary: "Evidence pack for MRD-101, IND submission: 3 criteria met.",
  criteria: [
    { id: "noael_decided", title: "The NOAEL is decided", basis: "regulation", source: "21 CFR 312.23",
      section: "(a)(8)(ii)(a)", quote: "q", evidence: "e", status: "met", explanation: "Current NOAEL.", seqs: [19943] },
    { id: "no_open_high_preclinical", title: "No open high-severity preclinical finding", basis: "policy",
      source: null, section: null, quote: null, evidence: "e", status: "not_met", explanation: "2 open.", seqs: [155] },
  ],
  open_items: [{ criterion_id: "no_open_high_preclinical", text: "PRE-0155 is open", seq: 155, finding_id: "PRE-0155" }],
};

const GATES: Schemas["GatesView"] = {
  can_request: true,
  gates: [{ asset_id: "MRD-101", gate: "ind_submission", title: "IND submission",
            criteria: [{ id: "noael_decided", title: "t", basis: "regulation", source: "21 CFR 312.23", section: "x" }] }],
  requests: [{ seq: 20201, recorded_at: "2026-11-20T10:01:00Z", pack_seq: 20203, pack: PACK, decision: null,
               request: { type: "gate_request", gate_request_id: "GATE-20261120T100100Z", asset_id: "MRD-101",
                          gate: "ind_submission", requested_by: "rahul.iyer", note: null } }],
};

const posted: unknown[] = [];

function serve() {
  posted.length = 0;
  server.use(
    http.get(`${BASE}/portfolio/outlook`, () => HttpResponse.json(OUTLOOK)),
    http.get(`${BASE}/portfolio/gates`, () => HttpResponse.json(GATES)),
    http.get(`${BASE}/portfolio`, () => HttpResponse.json({ assets: [] })),
    http.post(`${BASE}/portfolio/gates/20201/decision`, async ({ request }) => {
      posted.push(await request.json());
      return HttpResponse.json({ seq: 20210, recorded_at: "2026-11-20T10:05:00Z", decision: {} }, { status: 201 });
    }),
  );
}

describe("Portfolio outlook and gates", () => {
  it("shows each asset's status, risks with seqs, static marking and the evidence pack", async () => {
    serve();
    renderApp("/portfolio", "rahul.iyer");
    const mrd101 = await screen.findByTestId("outlook-MRD-101");
    expect(mrd101.textContent).toContain("At risk");
    expect(mrd101.textContent).toContain("PRE-0155");
    expect(mrd101.textContent).toContain("seq 155");
    expect(screen.getByTestId("outlook-MRD-110").textContent).toContain("static: On track");
    // Wrap-up: the rest of the risks and the open findings are counted, with a link.
    expect(screen.getByTestId("more-risks-MRD-101").textContent).toContain("and 2 more risks");
    const open = screen.getByTestId("open-findings-MRD-101");
    expect(open.textContent).toContain("93 open findings");
    expect(within(open).getByRole("link").getAttribute("href")).toBe("/review?study=MRD-101");
    expect(screen.queryByTestId("more-risks-MRD-110")).toBeNull();
    const met = screen.getByTestId("criterion-noael_decided");
    expect(met.textContent).toContain("21 CFR 312.23");
    expect(met.textContent).toContain("met");
    expect(screen.getByTestId("criterion-no_open_high_preclinical").textContent).toContain("company policy");
  });

  it("asks for conditions on a conditional go and shows a summary before recording", async () => {
    serve();
    const user = userEvent.setup();
    renderApp("/portfolio", "rahul.iyer");
    const card = await screen.findByTestId("gate-20201");
    await user.click(within(card).getByLabelText("Conditional go"));
    await user.type(within(card).getByLabelText(/Rationale/), "Proceed once the two high findings are closed.");
    await user.click(within(card).getByRole("button", { name: "Review the decision" }));
    expect(within(card).getByRole("alert").textContent).toContain("needs at least one condition");
    await user.type(within(card).getByLabelText(/Conditions/), "Close PRE-0155");
    await user.click(within(card).getByRole("button", { name: "Review the decision" }));
    const summary = within(card).getByTestId("gate-decision-summary");
    expect(summary.textContent).toContain("Conditional go");
    expect(summary.textContent).toContain("Close PRE-0155");
    expect(posted).toEqual([]);
    await user.click(within(card).getByRole("button", { name: "Confirm and record" }));
    await waitFor(() => {
      expect(posted.length).toBe(1);
    });
    expect(posted[0]).toMatchObject({ decision: "conditional_go", conditions: ["Close PRE-0155"], pack_seq: 20203 });
  });

  it("tells other roles who sees it", async () => {
    server.use(http.get(`${BASE}/portfolio/outlook`, () => HttpResponse.json({ detail: "x" }, { status: 403 })));
    renderApp("/portfolio", "asha.rao");
    expect((await screen.findByTestId("portfolio-reviews-hidden")).textContent).toContain("portfolio lead and the QA auditor");
  });
});
