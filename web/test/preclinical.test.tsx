import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { firstLines, groupCandidates, noaelProblem } from "../src/views/Preclinical";
import { renderApp } from "./render";
import { BASE, finding, server } from "./server";

const STUDY = "MRD-101-TOX-01";

const study: Schemas["PreclinicalStudy"] = {
  study_id: STUDY,
  asset_id: "MRD-101",
  cut_id: "TOX-FINAL",
  loaded_seq: 19601,
  title: "13-Week Repeat Dose Toxicity Study on MRD-101 in Rats",
  species: "RAT",
  strain: "SPRAGUE-DAWLEY",
  route: "ORAL GAVAGE",
  duration: "P13W",
  dose_unit: "mg/kg",
  groups: [
    { setcd: "1", phase: "main", dose: 0, control: true, males: 10, females: 10 },
    { setcd: "2", phase: "main", dose: 2, control: false, males: 10, females: 10 },
    { setcd: "3", phase: "main", dose: 20, control: false, males: 10, females: 10 },
    { setcd: "4", phase: "main", dose: 200, control: false, males: 10, females: 10 },
  ],
};

function group(rank: number, dose: number, over: Partial<Schemas["EndpointGroupView"]> = {}): Schemas["EndpointGroupView"] {
  return { rank, dose, n: 10, mean: 40, sd: 5, percent_vs_control: rank ? 0 : null, p: rank ? 0.5 : null, affected: null, grades: [], flagged: false, ...over };
}

const endpoints: Schemas["PreclinicalEndpoints"] = {
  study_id: STUDY,
  cut_id: "TOX-FINAL",
  dose_unit: "mg/kg",
  tables: [
    {
      endpoint: "lb:AST", label: "AST", kind: "continuous", unit: "U/L", sex: "M", phase: "main",
      groups: [group(0, 0), group(1, 2), group(2, 20, { mean: 48, percent_vs_control: 20 }),
        group(3, 200, { mean: 56.4, percent_vs_control: 41, p: 0.0001, flagged: true })],
      trend_p: 3.4e-7, check: "dose_related_change", finding_id: "PRE-0007",
    },
    {
      endpoint: "lb:GLUC", label: "GLUC", kind: "continuous", unit: "mg/dL", sex: "M", phase: "main",
      groups: [group(0, 0), group(1, 2), group(2, 20), group(3, 200)],
      trend_p: 0.6, check: null, finding_id: null,
    },
    {
      endpoint: "mi:LIVER:HYPERTROPHY", label: "Liver: hypertrophy (microscopic)", kind: "incidence", unit: "", sex: "F", phase: "main",
      groups: [group(0, 0, { affected: 0, mean: null }), group(1, 2, { affected: 0, mean: null }), group(2, 20, { affected: 0, mean: null }),
        group(3, 200, { affected: 9, mean: null, grades: [{ grade: 2, animals: 9 }], flagged: true, p: 0.0001 })],
      trend_p: 2e-6, check: "dose_related_incidence", finding_id: "PRE-0040",
    },
  ],
  by_dose: [{ phase: "main", dose: 200, dose_related: ["PRE-0007", "PRE-0040"], without_trend: [] }],
};

function decision(over: Partial<Schemas["NoaelDecisionView"]> = {}): Schemas["NoaelDecisionView"] {
  return {
    seq: 19800, recorded_at: "2026-11-15T10:00:00Z", decision_id: "NOAEL-0123456789ab", study_id: STUDY,
    sex: "combined", established: true, noael_dose: 20, loael_dose: 200, dose_unit: "mg/kg",
    finding_ids: ["PRE-0007"], rationale: "Hepatocellular hypertrophy and AST rise at 200 only.",
    decided_by: "leela.nair", supersedes: null, superseded_by: null, current: true, ...over,
  };
}

const candidates: Schemas["NoaelCandidate"][] = [
  { finding_id: "PRE-0007", title: "AST higher with dose in males, main study", status: "open", sex: "M", main_doses: [200], recovery_doses: [] },
  { finding_id: "PRE-0040", title: "Liver: hypertrophy more frequent with dose in females", status: "open", sex: "F", main_doses: [200], recovery_doses: [200] },
  { finding_id: "PRE-0050", title: "WBC lower at 2, 20 and 200 mg/kg in males", status: "open", sex: "M", main_doses: [2, 20, 200], recovery_doses: [] },
  { finding_id: "PRE-0061", title: "Only at recovery: RBC lower in males", status: "open", sex: "M", main_doses: [], recovery_doses: [20, 200] },
  { finding_id: "PRE-0090", title: "Data quality: lb GGT: too few control values", status: "open", sex: null, main_doses: [], recovery_doses: [] },
];

function history(current: Schemas["NoaelDecisionView"][], may_record: boolean): Schemas["NoaelHistory"] {
  return {
    study_id: STUDY, dose_unit: "mg/kg", doses: [0, 2, 20, 200], current, history: current, may_record,
    rationale_min_chars: 200, candidates,
  };
}

const RATIONALE =
  "AST rise and hepatocellular hypertrophy at 200 mg/kg/day were considered adverse together.\n" +
  "Changes at 20 mg/kg/day lacked a dose response or a correlating lesion.\n" +
  "Third line of the rationale.\nA fourth line the summary leaves out, with enough words to pass the minimum.";

const DOSE_STEPS: Schemas["DoseStep"][] = [
  { statistic: "mrsd:noael_mg_per_kg", role: "primary", value: 2, source: "ledger:noael_decision", selector: "DECISION=NOAEL-cd4154d72a20; SEX=combined; SEQ=19943",
    display: "Step 1, NOAEL: 2 mg/kg, the current decision for both sexes. FDA 2005 section IV: the NOAEL is the starting point." },
  { statistic: "mrsd:hed_mg_per_kg", role: "primary", value: 0.3226, source: "config:agents.dose_translation.conversions", selector: "SPECIES=RAT",
    display: "Step 2, human equivalent dose: HED = NOAEL / 6.2 = 2 / 6.2 = 0.323 mg/kg. FDA 2005 section V.A, Table 1: Rat." },
  { statistic: "mrsd:mrsd_mg_per_kg", role: "primary", value: 0.03226, source: "config:agents.dose_translation", selector: "safety_factor",
    display: "Step 4, MRSD = HED / safety factor = 0.323 / 10 = 0.0323 mg/kg." },
  { statistic: "mrsd:starting_dose_mg", role: "primary", value: 1.935, source: "config:agents.dose_translation", selector: "human_weight_kg",
    display: "Step 5, starting dose for a 60 kg adult = 1.94 mg." },
  { statistic: "mrsd:limitation", role: "supporting", value: 0, source: "config:agents.dose_translation", selector: "LIMITATION=1",
    display: "Limitation: One species only." },
];

function doseView(over: Partial<Schemas["DoseTranslationView"]> = {}): Schemas["DoseTranslationView"] {
  return {
    study_id: STUDY, status: "proposed", why: "The current NOAEL converted by FDA's 2005 method.",
    current: {
      finding_id: "PRE-0240", seq: 19950, title: "Proposed first-in-human starting dose for MRD-101: 1.94 mg for a 60 kg adult (MRSD 0.0323 mg/kg)",
      recorded_title: "Proposed MRD-110 starting dose: 1.94 mg for a 60 kg adult (MRSD 0.0323 mg/kg)",
      summary: "", check: "dose_proposal", status: "accepted", basis_decision_id: "NOAEL-cd4154d72a20", steps: DOSE_STEPS,
      decided_by: "karan.mehta", decided_seq: 19960, decision_reason: "Agreed.",
    },
    superseded: ["PRE-0232"],
    ...over,
  };
}

function requirement(over: Partial<Schemas["RequirementView"]>): Schemas["RequirementView"] {
  return {
    id: "x", title: "x", source: "ICH M3(R2)", section: "V (5)", quote: "q", note: "", status: "met", required_weeks: null,
    candidates: 1, study_id: null, study_title: null, species: null, duration_weeks: null, glp: null, report_status: null,
    qa_audit_date: null, problems: [], finding_ids: [], ...over,
  };
}

const IND: Schemas["IndReadinessView"] = {
  asset_id: "MRD-101", status: "assessed", cut_id: "INV-12345678", loaded_seq: 19947, planned_submission_date: "2027-04-01",
  planned_clinical_duration_days: 14, multiple_dose: true, percent_met: 63.6, met: 7, applicable: 11,
  requirements: [
    requirement({ id: "repeat_dose_rodent", title: "Repeated-dose toxicity in a rodent", study_id: "MRD-101-TOX-01",
      duration_weeks: 13, required_weeks: 2, glp: "Y", report_status: "qa_audited" }),
    requirement({ id: "in_vitro_metabolism_protein_binding", title: "In vitro metabolism and plasma protein binding", status: "not_met",
      section: "III (3)", candidates: 0, problems: ["required_study_missing"], finding_ids: ["PRE-0244"] }),
  ],
};

const TRANSLATIONAL: Schemas["TranslationalView"] = {
  study_id: STUDY, status: "recommended", why: "How the first-in-human study should monitor the organs the NOAEL rests on.",
  current: {
    finding_id: "PRE-0237", seq: 19980, title: "Monitoring recommendation for MRD-101's first-in-human study: liver, heart",
    status: "open", decided_by: null, decided_seq: null,
    items: [
      { organ: "liver", label: "Liver", measure: "ALT, AST, ALP and total bilirubin", frequency: "Before dosing and after the last dose",
        review: "ALT or AST above 3 x ULN: repeat within 48 to 72 hours", stopping: "Consider stopping if ALT or AST > 8 x ULN",
        sources: [{ source: "FDA 2009 DILI", section: "IV.A.5", quote: "Discontinuation of treatment should be considered if: ALT or AST >8xULN" }],
        findings: ["PRE-0165", "PRE-0198"], finding_titles: ["AST", "Liver necrosis"] },
      { organ: "heart", label: "Heart (myocardium)", measure: "12-lead ECG with QTc; cardiac troponin (I or T)",
        frequency: "ECG at baseline and periodically on treatment", review: "QTc above 450 ms", stopping: "QTc > 500 ms",
        sources: [{ source: "ICH E14", section: "II.A.2 (2.1.2)", quote: "increases in QT/QTc to >500 ms" }],
        findings: ["PRE-0155"], finding_titles: ["Heart"] },
    ],
    unmapped: ["PRE-0153, unmapped (no mapping entry for dose_related_incidence:mi:ADRENAL GLAND:VACUOLIZATION:M:main): Adrenal"],
    margins: [
      { label: "NOAEL", hed_mg: 19.35, human_weight_kg: 60, display: "NOAEL 2 mg/kg as a human equivalent dose: 0.323 mg/kg, 19.4 mg", ratio: 10 },
      { label: "LOAEL", hed_mg: 193.5, human_weight_kg: 60, display: "LOAEL 20 mg/kg as a human equivalent dose: 3.23 mg/kg, 194 mg", ratio: 100 },
    ],
    starting_dose: "Planned starting dose 1.94 mg (PRE-0232, accepted by karan.mehta)",
    basis: "Why nonclinical findings shape Phase 1 monitoring: FDA 2007 grading scale, III",
  },
  superseded: [],
};

function preclinical(noael: Schemas["NoaelHistory"], dose = doseView(), ind = IND, translational = TRANSLATIONAL) {
  server.use(
    http.get(`${BASE}/preclinical/${STUDY}/translational`, () => HttpResponse.json(translational)),
    http.get(`${BASE}/preclinical/${STUDY}/dose-translation`, () => HttpResponse.json(dose)),
    http.get(`${BASE}/preclinical/assets/MRD-101/ind-readiness`, () => HttpResponse.json(ind)),
    http.get(`${BASE}/preclinical/studies`, () => HttpResponse.json([study])),
    http.get(`${BASE}/preclinical/${STUDY}/endpoints`, () => HttpResponse.json(endpoints)),
    http.get(`${BASE}/preclinical/${STUDY}/noael`, () => HttpResponse.json(noael)),
    http.get(`${BASE}/findings`, () =>
      HttpResponse.json({
        items: [
          finding("PRE-0007", { study_id: STUDY, agent_id: "preclinical.tox_study_monitor", title: "AST higher with dose in males, main study" }),
          finding("PRE-0040", { study_id: STUDY, agent_id: "preclinical.tox_study_monitor", title: "Liver: hypertrophy more frequent with dose in females" }),
        ],
        total: 2, limit: 500, offset: 0,
      }),
    ),
  );
}

describe("Preclinical page", () => {
  it("is a live menu item and shows the design, the flagged endpoints and what flags each dose", async () => {
    preclinical(history([], true));
    renderApp("/preclinical", "leela.nair");
    expect(await screen.findByRole("heading", { name: "Preclinical" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Preclinical" })).toBeTruthy();
    const design = await screen.findByRole("table", { name: /Dose groups/ });
    expect(within(design).getAllByRole("row")).toHaveLength(5);
    const table = await screen.findByRole("table", { name: /group mean or incidence per dose/ });
    const rows = within(table).getAllByRole("row");
    expect(rows.map((r) => r.textContent).join("|")).toContain("AST");
    expect(rows.some((r) => r.textContent.includes("GLUC"))).toBe(false); // only flagged by default
    expect(within(table).getByText(/\(flagged\)/)).toBeTruthy();
    expect(within(table).getByRole("link", { name: "PRE-0007" })).toBeTruthy();
    const support = screen.getByLabelText("Findings per dose group");
    expect(within(support).getByText("200 mg/kg")).toBeTruthy();
    expect(within(support).getByText(/It is not a NOAEL/)).toBeTruthy();
  });

  it("switches sex and shows incidence with severity grades", async () => {
    preclinical(history([], false));
    const user = userEvent.setup();
    renderApp("/preclinical", "rahul.iyer");
    await screen.findByRole("table", { name: /group mean or incidence per dose/ });
    await user.selectOptions(screen.getByLabelText("Sex"), "F");
    const table = screen.getByRole("table", { name: /group mean or incidence per dose/ });
    expect(within(table).getByText("9/10")).toBeTruthy();
    expect(within(table).getByText("mild 9")).toBeTruthy();
  });

  it("shows 'not yet decided' and no form to a role that may not record", async () => {
    preclinical(history([], false));
    renderApp("/preclinical", "rahul.iyer");
    expect(await screen.findByTestId("noael-none")).toBeTruthy();
    expect(screen.queryByRole("form", { name: "Record a NOAEL decision" })).toBeNull();
  });

  it("groups the findings by sex and dose and records after a summary", async () => {
    preclinical(history([], true));
    let posted: unknown = null;
    let postedRole: string | null = null;
    server.use(
      http.post(`${BASE}/preclinical/${STUDY}/noael`, async ({ request }) => {
        posted = await request.json();
        postedRole = request.headers.get("X-Meridian-Role");
        return HttpResponse.json(decision(), { status: 201 });
      }),
    );
    const user = userEvent.setup();
    renderApp("/preclinical", "leela.nair");
    const form = await screen.findByRole("form", { name: "Record a NOAEL decision" });
    for (const name of [
      "Males, from 2 mg/kg",
      "Males, from 200 mg/kg",
      "Males, at recovery only",
      "Females, from 200 mg/kg",
      "No sex (data quality), no dose group flagged",
    ]) {
      expect(within(form).getByRole("group", { name })).toBeTruthy();
    }
    expect(within(form).getByRole("link", { name: "open PRE-0007" })).toBeTruthy();
    await user.selectOptions(within(form).getByLabelText("NOAEL"), "20");
    await user.selectOptions(within(form).getByLabelText("LOAEL"), "200");
    const males = within(form).getByRole("group", { name: "Males, from 200 mg/kg" });
    await user.click(within(males).getByRole("checkbox", { name: /PRE-0007/ }));
    const females = within(form).getByRole("group", { name: "Females, from 200 mg/kg" });
    await user.click(within(females).getByRole("checkbox", { name: /PRE-0040/ }));
    await user.type(within(form).getByLabelText(/Rationale/), "Too short.");
    await user.click(within(form).getByRole("button", { name: "Review the decision" }));
    expect((await within(form).findByRole("alert")).textContent).toContain("at least 200 characters");
    expect(posted).toBeNull();
    await user.clear(within(form).getByLabelText(/Rationale/));
    await user.type(within(form).getByLabelText(/Rationale/), RATIONALE);
    await user.click(within(form).getByRole("button", { name: "Review the decision" }));
    const summary = await screen.findByTestId("noael-summary");
    expect(summary.textContent).toContain("SexBoth sexes");
    expect(summary.textContent).toContain("NOAEL20 mg/kg");
    expect(summary.textContent).toContain("LOAEL200 mg/kg");
    expect(summary.textContent).toContain("Findings (2)");
    expect(summary.textContent).toContain("PRE-0007 AST higher with dose in males");
    expect(summary.textContent).toContain("Third line of the rationale.");
    expect(summary.textContent).not.toContain("A fourth line");
    expect(summary.textContent).toContain("This is the first both sexes decision");
    expect(posted).toBeNull(); // nothing is sent before the confirmation
    await user.click(within(summary).getByRole("button", { name: "Confirm and record" }));
    expect(await screen.findByTestId("noael-recorded")).toBeTruthy();
    expect(posted).toEqual({
      sex: "combined", established: true, noael_dose: 20, loael_dose: 200, finding_ids: ["PRE-0007", "PRE-0040"],
      rationale: RATIONALE.trim(), supersedes: null,
    });
    expect(postedRole).toBe("toxicologist");
  });

  it("keeps a sex's decision to that sex's findings and asks for the LOAEL when not established", async () => {
    preclinical(history([], true));
    const user = userEvent.setup();
    renderApp("/preclinical", "leela.nair");
    const form = await screen.findByRole("form", { name: "Record a NOAEL decision" });
    await user.click(within(form).getByRole("checkbox", { name: /PRE-0040/ }));
    await user.selectOptions(within(form).getByLabelText("Sex"), "M");
    expect(within(form).queryByRole("checkbox", { name: /PRE-0040/ })).toBeNull();
    expect(within(form).getByText(/0 chosen; males only/)).toBeTruthy();
    await user.click(within(form).getByRole("checkbox", { name: /NOAEL not established/ }));
    await user.click(within(form).getByRole("checkbox", { name: /PRE-0050/ }));
    await user.type(within(form).getByLabelText(/Rationale/), RATIONALE);
    await user.click(within(form).getByRole("button", { name: "Review the decision" }));
    expect((await within(form).findByRole("alert")).textContent).toContain("needs the LOAEL");
    await user.selectOptions(within(form).getByLabelText("LOAEL"), "20");
    await user.click(within(form).getByRole("button", { name: "Review the decision" }));
    expect((await within(form).findByRole("alert")).textContent).toContain("the LOAEL is 2");
    await user.selectOptions(within(form).getByLabelText("LOAEL"), "2");
    await user.click(within(form).getByRole("button", { name: "Review the decision" }));
    const summary = await screen.findByTestId("noael-summary");
    expect(summary.textContent).toContain("NOAELnot established");
    await user.click(within(summary).getByRole("button", { name: "Back to edit" }));
    expect(screen.queryByTestId("noael-summary")).toBeNull();
    expect(within(form).getByRole("button", { name: "Review the decision" })).toBeTruthy();
  });

  it("says in the summary which decision a revision replaces, and when it was recorded", async () => {
    preclinical(history([decision()], true));
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/preclinical/${STUDY}/noael`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json(decision({ decision_id: "NOAEL-ba9876543210", supersedes: "NOAEL-0123456789ab" }), {
          status: 201,
        });
      }),
    );
    const user = userEvent.setup();
    renderApp("/preclinical", "leela.nair");
    expect(await screen.findByTestId("noael-current")).toBeTruthy();
    expect(screen.getByText(/NOAEL 20 mg\/kg, LOAEL 200 mg\/kg/)).toBeTruthy();
    const form = screen.getByRole("form", { name: "Record a NOAEL decision" });
    expect(within(form).getByText(/This revises/)).toBeTruthy();
    await user.selectOptions(within(form).getByLabelText("LOAEL"), "200");
    await user.click(within(form).getByRole("checkbox", { name: /PRE-0007/ }));
    await user.type(within(form).getByLabelText(/Rationale/), RATIONALE);
    await user.click(within(form).getByRole("button", { name: "Review the decision" }));
    const replaces = await screen.findByTestId("noael-replaces");
    expect(replaces.textContent).toMatch(/This replaces decision NOAEL-0123456789ab recorded at \S/);
    await user.click(screen.getByRole("button", { name: "Confirm and record" }));
    expect(await screen.findByTestId("noael-recorded")).toBeTruthy();
    expect(posted).toMatchObject({ supersedes: "NOAEL-0123456789ab" });
    expect(screen.queryByRole("button", { name: "Raise an observation" })).toBeNull();
  });

  it("links each finding a decision rests on", async () => {
    preclinical(history([decision({ finding_ids: ["PRE-0007", "PRE-0040"] })], false));
    renderApp("/preclinical", "rahul.iyer");
    const current = await screen.findByTestId("noael-current");
    expect(within(current).getByRole("link", { name: "PRE-0007" }).getAttribute("href")).toBe("/findings/PRE-0007");
    expect(within(current).getByRole("link", { name: "PRE-0040" })).toBeTruthy();
  });

  it("offers the QA auditor an observation on the current decision", async () => {
    preclinical(history([decision()], false));
    renderApp("/preclinical", "nina.fernandes");
    const current = await screen.findByTestId("noael-current");
    expect(within(current).getByRole("button", { name: "Raise an observation" })).toBeTruthy();
  });

  it("tells a clinical role plainly that it does not see preclinical studies", async () => {
    server.use(
      http.get(`${BASE}/preclinical/studies`, () => HttpResponse.json({ detail: "forbidden" }, { status: 403 })),
    );
    renderApp("/preclinical", "asha.rao");
    expect(await screen.findByText(/Your role does not see preclinical studies/)).toBeTruthy();
  });
});

describe("Portfolio NOAEL field", () => {
  function portfolio(live: Schemas["AssetLive"]): Schemas["Portfolio"] {
    return {
      assets: [{
        asset_id: "MRD-101",
        static: { title: "MRD-101", stage: "preclinical", indication: "Fibrosis", intervention: "Oral ALK5 inhibitor",
          milestone: "IND submission", milestone_due: "Q2 2027", progress_percent: 72, probability_of_success: null,
          status: { level: "warning", text: "Dog study in-life" } },
        live,
      }],
    };
  }

  it("shows the current NOAEL as a live field", async () => {
    server.use(http.get(`${BASE}/portfolio`, () => HttpResponse.json(portfolio({
      open_findings: 3, noael_status: "decided",
      noael: [{ decision_id: "NOAEL-0123456789ab", sex: "combined", established: true, noael_dose: 20, dose_unit: "mg/kg",
        recorded_at: "2026-11-15T10:00:00Z", decided_by: "leela.nair" }],
    }))));
    renderApp("/portfolio", "rahul.iyer");
    const field = await screen.findByTestId("noael-field");
    expect(field.textContent).toContain("NOAEL: 20 mg/kg (both sexes)");
  });

  it("shows MRD-101's planned first-in-human starting dose with the chain behind it", async () => {
    server.use(http.get(`${BASE}/portfolio`, () => HttpResponse.json({
      assets: [{
        asset_id: "MRD-101",
        static: { title: "Oral ALK5 inhibitor", stage: "preclinical", indication: "Fibrosis", intervention: null,
          milestone: "IND submission", milestone_due: "Q2 2027", progress_percent: 72, probability_of_success: null,
          status: { level: "warning", text: "Dog study in-life" } },
        live: {
          open_findings: 0,
          starting_dose: {
            status: "planned", dose_mg: 1.9355, mrsd_mg_per_kg: 0.032258, human_weight_kg: 60,
            why: "Accepted by karan.mehta (ledger seq 19960).",
            chain: [
              { step: "study", label: "MRD-101-TOX-01, SEND cut TOX-FINAL", ref: STUDY, who: "system.ingest", seq: 19601 },
              { step: "noael", label: "NOAEL 2 mg/kg (combined), NOAEL-cd4154d72a20", ref: "NOAEL-cd4154d72a20", who: "leela.nair", seq: 19943 },
              { step: "proposal", label: "Proposed first-in-human starting dose for MRD-101", ref: "PRE-0240", who: "preclinical.dose_translation", seq: 19950 },
              { step: "decision", label: "accepted by karan.mehta", ref: "PRE-0240", who: "karan.mehta", seq: 19960 },
            ],
          },
        },
      }],
    })));
    renderApp("/portfolio", "karan.mehta");
    const field = await screen.findByTestId("dose-field");
    expect(field.textContent).toContain("Planned first-in-human starting dose: 1.94 mg for a 60 kg adult (MRSD 0.0323 mg/kg)");
    const chain = within(field).getByRole("list", { name: "From the study to the starting dose" });
    const links = within(chain).getAllByRole("listitem").map((li) => li.textContent);
    expect(links).toEqual([
      "Study: MRD-101-TOX-01, SEND cut TOX-FINAL (system.ingest), ledger seq 19601",
      "NOAEL: NOAEL 2 mg/kg (combined), NOAEL-cd4154d72a20 (leela.nair), ledger seq 19943",
      "Proposal: Proposed first-in-human starting dose for MRD-101 (preclinical.dose_translation), ledger seq 19950",
      "Decision: accepted by karan.mehta (karan.mehta), ledger seq 19960",
    ]);
    expect(screen.queryByText(/open findings to review/)).toBeNull();
  });

  it("shows MRD-101's first-in-human monitoring status", async () => {
    server.use(http.get(`${BASE}/portfolio`, () => HttpResponse.json(portfolio({
      open_findings: 4, noael_status: "decided", noael: [], ind_status: "assessed", ind_readiness_percent: 63.6,
      starting_dose: { status: "planned", dose_mg: 1.9355, mrsd_mg_per_kg: 0.032258, human_weight_kg: 60, why: "Accepted.", chain: [] },
      monitoring: { status: "accepted", finding_id: "PRE-0237", organs: ["Liver", "Heart (myocardium)"], unmapped: 0,
        decided_by: "karan.mehta", decided_seq: 19990 },
    }))));
    renderApp("/portfolio", "rahul.iyer");
    const field = await screen.findByTestId("monitoring-field");
    expect(field.textContent).toContain("First-in-human monitoring: accepted (Liver, Heart (myocardium)) by karan.mehta, ledger seq 19990");
    expect(screen.getByTestId("dose-field").textContent).toContain("Planned first-in-human starting dose: 1.94 mg");
  });

  it("shows MRD-101's IND readiness and why there is no starting dose yet", async () => {
    server.use(http.get(`${BASE}/portfolio`, () => HttpResponse.json(portfolio({
      open_findings: 4, noael_status: "not_yet_decided", noael: [], ind_status: "assessed", ind_readiness_percent: 63.6,
      starting_dose: { status: "no_decision", why: "No NOAEL decision is recorded for MRD-101-TOX-01.", chain: [] },
    }))));
    renderApp("/portfolio", "rahul.iyer");
    expect((await screen.findByTestId("ind-field")).textContent).toContain("IND readiness: 63.6% of first-in-human requirements met");
    const dose = screen.getByTestId("dose-field");
    expect(dose.textContent).toContain("No first-in-human starting dose yet");
    expect(dose.textContent).toContain("No NOAEL decision is recorded");
  });

  it("says not yet decided, or not shown to the role", async () => {
    server.use(http.get(`${BASE}/portfolio`, () => HttpResponse.json(portfolio({ open_findings: 0, noael_status: "not_shown", noael: [] }))));
    renderApp("/portfolio", "asha.rao");
    expect((await screen.findByTestId("noael-field")).textContent).toContain("not shown to your role");
  });
});

describe("Dose translation and IND readiness", () => {
  it("shows the working step by step with its sources, and the decision on the proposal", async () => {
    preclinical(history([decision()], false));
    renderApp("/preclinical", "rahul.iyer");
    const current = await screen.findByTestId("dose-current");
    const steps = within(current).getByRole("list", { name: "The working, step by step" });
    const items = within(steps).getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toHaveLength(4);
    expect(items[0]).toContain("NOAEL:");
    expect(items[1]).toContain("HED = NOAEL / 6.2 = 2 / 6.2 = 0.323 mg/kg");
    expect(items[1]).toContain("source config:agents.dose_translation.conversions");
    expect(items[3]).toContain("1.94 mg");
    expect(within(current).getByText(/One species only/)).toBeTruthy();
    expect(within(current).getByRole("link", { name: "PRE-0240" })).toBeTruthy();
    expect(within(current).getByText(/by karan.mehta/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "PRE-0232" })).toBeTruthy(); // superseded
  });

  it("says why there is no proposal", async () => {
    preclinical(history([], false), doseView({ status: "no_decision", why: "No NOAEL decision is recorded for this study, so preclinical.dose_translation records nothing.", current: null, superseded: [] }));
    renderApp("/preclinical", "rahul.iyer");
    expect((await screen.findByTestId("dose-why")).textContent).toContain("records nothing");
    expect(screen.queryByTestId("dose-current")).toBeNull();
  });

  it("lists every requirement with its status, its study and links to the gaps", async () => {
    preclinical(history([], false));
    renderApp("/preclinical", "rahul.iyer");
    expect((await screen.findByTestId("ind-summary")).textContent).toContain("63.6% of applicable requirements met (7 of 11)");
    const met = screen.getByTestId("req-repeat_dose_rodent");
    expect(met.textContent).toContain("met");
    expect(met.textContent).toContain("MRD-101-TOX-01");
    expect(met.textContent).toContain("13 weeks (at least 2.0), GLP Y, report qa audited");
    const gap = screen.getByTestId("req-in_vitro_metabolism_protein_binding");
    expect(gap.textContent).toContain("not met");
    expect(gap.textContent).toContain("no study");
    expect(within(gap).getByRole("link", { name: "PRE-0244" })).toBeTruthy();
    expect(gap.textContent).toContain("ICH M3(R2) III (3)");
  });
});

describe("Translational panel", () => {
  it("shows monitoring per organ with its sources, the unmapped findings and the margins", async () => {
    preclinical(history([], false));
    renderApp("/preclinical", "rahul.iyer");
    const current = await screen.findByTestId("translational-current");
    const liver = within(current).getByRole("article", { name: "Monitoring: Liver" });
    expect(liver.textContent).toContain("MeasureALT, AST, ALP and total bilirubin");
    expect(liver.textContent).toContain("StoppingConsider stopping if ALT or AST > 8 x ULN");
    expect(within(liver).getByRole("link", { name: "PRE-0198" })).toBeTruthy();
    expect(within(liver).getByText(/FDA 2009 DILI, IV.A.5:/)).toBeTruthy();
    expect(within(current).getByRole("article", { name: "Monitoring: Heart (myocardium)" }).textContent).toContain("QTc");
    expect(within(current).getByLabelText("Unmapped findings").textContent).toContain("PRE-0153, unmapped");
    expect(within(current).getByTestId("margin-NOAEL").textContent).toContain("10.0 x");
    expect(within(current).getByTestId("margin-LOAEL").textContent).toContain("100 x");
    expect(current.textContent).toContain("accepted by karan.mehta");
  });

  it("says why there is no recommendation", async () => {
    preclinical(history([], false), doseView(), IND, {
      study_id: STUDY, status: "no_decision", why: "No NOAEL decision is recorded, so preclinical.translational_bridge has nothing to map.",
      current: null, superseded: [],
    });
    renderApp("/preclinical", "rahul.iyer");
    expect((await screen.findByTestId("translational-why")).textContent).toContain("nothing to map");
    expect(screen.queryByTestId("translational-current")).toBeNull();
  });
});

describe("NOAEL form rules", () => {
  const first = candidates[0];
  if (first === undefined) throw new Error("no candidate");
  const base = {
    sex: "combined" as const, established: true, noael: 20, loael: 200, treated: [2, 20, 200],
    chosen: [first], rationale: "x".repeat(200), minChars: 200,
  };

  it("are the API's rules", () => {
    expect(noaelProblem(base)).toBeNull();
    expect(noaelProblem({ ...base, rationale: "x".repeat(199) })).toContain("at least 200");
    expect(noaelProblem({ ...base, loael: 20 })).toContain("above the NOAEL");
    expect(noaelProblem({ ...base, chosen: [] })).toContain("at least one finding");
    expect(noaelProblem({ ...base, established: false, noael: null, loael: null })).toContain("needs the LOAEL");
    expect(noaelProblem({ ...base, established: false, noael: null, loael: 20 })).toContain("the LOAEL is 2");
    expect(noaelProblem({ ...base, sex: "F" })).toContain("PRE-0007 is not a finding about females");
  });

  it("group by sex, then by the lowest flagged dose", () => {
    expect(groupCandidates(candidates, "M", "mg/kg").map((g) => [g.label, g.items.map((c) => c.finding_id)])).toEqual([
      ["Males, from 2 mg/kg", ["PRE-0050"]],
      ["Males, from 200 mg/kg", ["PRE-0007"]],
      ["Males, at recovery only", ["PRE-0061"]],
    ]);
    expect(groupCandidates(candidates, "combined", "mg/kg").map((g) => g.label)).toContain(
      "No sex (data quality), no dose group flagged",
    );
  });

  it("show the first lines of the rationale", () => {
    expect(firstLines("one\n\ntwo\nthree\nfour")).toBe("one two three");
    expect(firstLines("y".repeat(400))).toBe(`${"y".repeat(300)}...`);
  });
});
