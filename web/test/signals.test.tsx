import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { renderApp } from "./render";
import { BASE, server } from "./server";

function row(term: string, over: Partial<Schemas["SignalRow"]> = {}): Schemas["SignalRow"] {
  return {
    term, reports: 50, drug_reports: 2767, term_reports: 646830, prr: 6.1, prr_low: 5.6, prr_high: 6.6,
    ror: 7.2, ror_low: 6.5, ror_high: 7.9, chi2: 2100.4, signal: true, label_listed: true,
    finding_id: "SIG-0001", status: "open", tier: "listed", ...over,
  };
}

const VIEW: Schemas["SafetySignalsView"] = {
  study_id: "MRD-201", drug: "xanomeline (with trospium chloride, COBENFY)", status: "scanned",
  scan_id: "SCAN-20261120T030000Z", scan_seq: 19990, data_date: "2026-10-28",
  product_query: 'patient.drug.openfda.generic_name.exact:"XANOMELINE AND TROSPIUM CHLORIDE"',
  drug_reports: 2767, all_reports: 21054158, terms_with_drug: 659, tested: 250,
  signals: [row("NAUSEA", { finding_id: null, status: null }), row("GASTROOESOPHAGEAL REFLUX DISEASE", { finding_id: "SIG-0002", label_listed: false, reports: 12, tier: "priority" })],
  not_signals: [row("HEADACHE", { signal: false, prr: 0.8, finding_id: null, status: null })],
  label_set_id: "8f0e73bf", label_version: "6", label_effective: "20260116", signals_listed: 1,
  label_most_common: [{ reaction: "nausea", signal_terms: ["NAUSEA"] }, { reaction: "dyspepsia", signal_terms: [] }],
  rule: "Evans et al. 2001: at least 3 reports, PRR at least 2, chi-squared (Yates) at least 4",
  caveats: ["There is no certainty that the reported event (adverse event or medication error) was actually due to the product."],
  caveats_source: "open.fda.gov/apis/drug/event (Disclaimer)", requests: 254,
  triage: "Only priority signals are findings.",
  tiers: { listed: 1, indication: 0, use: 0, weak: 0, priority: 1 },
  context: ["FAERS reports here concern COBENFY: oral xanomeline with trospium chloride."],
  scan: {
    scan_seq: 20100, scan_id: "SCAN-20261120T030000Z", agent_version: "1.0.1", requested_by: "operator:himanshu",
    reuses_responses_of: 19985, findings: 1, open: 1, corrections: [],
  },
  earlier_scans: [{
    scan_seq: 19985, scan_id: "SCAN-20261119T211657Z", agent_version: "1.0.0", requested_by: "karan.mehta",
    reuses_responses_of: null, findings: 76, open: 0,
    corrections: [{
      seq: 20070, corrects_seq: 19985, field: "requested_by", recorded_value: "karan.mehta",
      true_value: "operator:himanshu", explanation: "Started from the CLI by the coding agent.",
      corrected_by: "operator:himanshu", recorded_at: "2026-11-20T10:00:00Z",
    }],
  }],
};

describe("Safety signals", () => {
  it("shows the signals with PRR, ROR and counts, the label comparison, the data date and the caveats", async () => {
    server.use(http.get(`${BASE}/safety/signals`, () => HttpResponse.json(VIEW)));
    renderApp("/safety/signals", "karan.mehta");
    expect((await screen.findByTestId("scan-summary")).textContent).toContain("2767 reports of 21054158 in FAERS");
    expect(screen.getByText(/openFDA data date 2026-10-28/)).toBeTruthy();
    expect(screen.getByTestId("openfda-notice").textContent).toContain(
      "Adverse event reports and labeling are from openFDA (CC0). No endorsement by the FDA or the manufacturer is implied.",
    );
    const nausea = screen.getByTestId("signal-NAUSEA");
    expect(nausea.textContent).toContain("50 of 2767");
    expect(nausea.textContent).toContain("6.10 (5.60 to 6.60)");
    expect(nausea.textContent).toContain("7.20 (6.50 to 7.90)");
    expect(nausea.textContent).toContain("listed");
    expect(within(nausea).queryByRole("link")).toBeNull(); // listed: no finding
    expect(screen.getByTestId("tier-NAUSEA").textContent).toBe("in the label");
    const gord = screen.getByTestId("signal-GASTROOESOPHAGEAL REFLUX DISEASE");
    expect(within(gord).getByRole("link", { name: "SIG-0002" })).toBeTruthy();
    expect(screen.getByTestId("tier-GASTROOESOPHAGEAL REFLUX DISEASE").textContent).toBe("priority");
    expect(screen.getByTestId("triage").textContent).toContain("Only priority signals are findings");
    expect(screen.getByText(/oral xanomeline with trospium chloride/)).toBeTruthy();
    expect(screen.getByTestId("scan-20100").textContent).toContain("requested by operator:himanshu");
    const earlier = screen.getByTestId("scan-19985");
    expect(earlier.textContent).toContain("76 findings, 0 in the queue");
    expect(within(earlier).getByTestId("correction-note").textContent).toContain(
      "records requested_by as karan.mehta; in fact operator:himanshu",
    );
    expect(screen.getByTestId("signal-GASTROOESOPHAGEAL REFLUX DISEASE").textContent).toContain("not found");
    expect(screen.getByTestId("label-summary").textContent).toContain("1 of 2 signals are named in the label");
    const common = screen.getByRole("list", { name: "The label's most common adverse reactions" });
    expect(common.textContent).toContain("nausea: a signal (nausea)");
    expect(common.textContent).toContain("dyspepsia: not a signal");
    expect(screen.getByText(/There is no certainty that the reported event/)).toBeTruthy();
    expect(screen.getByText(/never an incidence and never evidence that the drug causes/)).toBeTruthy();
  });

  it("tells other roles plainly who sees it", async () => {
    server.use(http.get(`${BASE}/safety/signals`, () => HttpResponse.json({ detail: "forbidden" }, { status: 403 })));
    renderApp("/safety/signals", "asha.rao");
    expect(await screen.findByText(/shown to the medical monitor and the portfolio lead/)).toBeTruthy();
  });

  it("says when no scan has run", async () => {
    server.use(http.get(`${BASE}/safety/signals`, () => HttpResponse.json({ ...VIEW, status: "not_scanned", signals: [], not_signals: [] })));
    renderApp("/safety/signals", "rahul.iyer");
    expect((await screen.findByTestId("signals-none")).textContent).toContain("No scan of openFDA has run yet");
  });
});
