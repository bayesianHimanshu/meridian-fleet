// MSW handlers for the Fleet API, typed from the generated schema. Every request is
// recorded with its identity headers so tests can check what was sent.
import { http, HttpResponse, type JsonBodyType } from "msw";
import { setupServer } from "msw/node";
import type { Schemas } from "../src/api/client";

export const BASE = "http://127.0.0.1:5173/api/v1";

export interface Seen {
  method: string;
  path: string;
  actor: string | null;
  role: string | null;
  body: unknown;
}

export const seen: Seen[] = [];

export const people: Schemas["PersonView"][] = [
  { actor: "asha.rao", name: "Asha Rao", role: "clinical_ops_lead" },
  { actor: "karan.mehta", name: "Dr. Karan Mehta", role: "medical_monitor" },
  { actor: "nina.fernandes", name: "Nina Fernandes", role: "qa_auditor" },
  { actor: "rahul.iyer", name: "Rahul Iyer", role: "portfolio_lead" },
  { actor: "leela.nair", name: "Dr. Leela Nair", role: "toxicologist" },
  { actor: "omar.siddiqui", name: "Dr. Omar Siddiqui", role: "unblinded_statistician" },
];

export function finding(
  id: string,
  over: Partial<Schemas["FindingSummary"]> = {},
): Schemas["FindingSummary"] {
  return {
    finding_id: id,
    seq: 100,
    raised_at: "2026-11-07T17:31:50.000000Z",
    study_id: "MRD-201",
    cut_id: "DC-BASE",
    scope: "operational",
    check: "deviations",
    agent_id: "rbqm.kri_monitor",
    agent_version: "2.2.0",
    superseded: false,
    superseded_by: null,
    retired: false,
    retired_by_version: null,
    kind: "kri_breach",
    site_id: "1016",
    severity: "high",
    title: `Site 1016: finding ${id}`,
    summary: "Site 1016 has 18 protocol deviations; the rest of the study implies 5.1.",
    score: 4.2,
    visibility: "blinded",
    visible_to: ["clinical_ops_lead", "medical_monitor", "qa_auditor"],
    status: "open",
    decision_count: 0,
    latest_narrative: null,
    primary_evidence: ["18 observed (5.1 expected)", "Bonferroni-adjusted p = 1.6e-06 (threshold 0.01)"],
    ...over,
  };
}

export const queue: Schemas["ReviewQueue"] = {
  items: [
    finding("RBQM-0003", {
      seq: 300,
      severity: "high",
      agent_id: "rbqm.kri_monitor",
      site_id: "1016",
      latest_narrative: {
        narrator_version: "1.1.0",
        headline: "Site 1016: 18 protocol deviations against 5.1 expected",
        explanation: "Site 1016 recorded 18 observed (5.1 expected).",
        seq: 301,
        narrator_id: "rbqm.narrator",
        model: "claude-haiku-5-5",
      },
    }),
    finding("RBQM-0002", { seq: 200, severity: "medium", agent_id: "rbqm.csm", site_id: "1001", title: "Site 1001: vital signs vary unusually little" }),
    finding("RBQM-0001", { seq: 100, severity: "medium", agent_id: "rbqm.qtl_detector", site_id: null, status: "escalated", title: "Premature discontinuation breached its QTL" }),
  ],
};

export const IMAGE = "sha256:9630f178a65395a623144c87087130c063470ddcf2ba86da560f1fc2860e412b";
export const POLICY = "01637db6d926a6f556489e31d38a4a65404b90f7ebb7e149d7beee2a62b0805d";
export const POLICY_FILE = "e7eb24f3992ef09e55277d4765cf5ef62178672ce36567b13507f71c54540c82";
export const MANIFEST = "8834adff7f51ef74e4d7d32ac8d20980e82e223b397408a231ece226bb74eb73";
export const FINDING_HASH = "a2ff19868b00859f146d2ed336499fe7667f7ef7f4427293ad68f5dced741a8b";

export function detail(id = "RBQM-0003", over: Partial<Schemas["FindingSummary"]> = {}): Schemas["FindingDetail"] {
  return {
    finding: { ...finding(id, { seq: 595 }), ...over },
    dedup_key: `rbqm.kri_monitor@2.2.0|kri_breach|MRD-201|1016|DC-BASE|deviations`,
    hash: FINDING_HASH,
    evidence: [
      {
        source: "ops/deviations",
        selector: "SITEID=1016",
        statistic: "deviations:observed",
        value: 18,
        threshold: null,
        role: "primary",
        direction: "high",
        expected: 5.1,
        display: "18 observed (5.1 expected)",
      },
      {
        source: "ops/deviations",
        selector: "SITEID=1016",
        statistic: "deviations:phi",
        value: 1.23,
        threshold: null,
        role: "supporting",
        direction: "none",
        expected: null,
        display: "dispersion phi = 1.23",
      },
    ],
    narratives: [],
    assessments: [],
    decisions: [],
    run: {
      agent_id: "rbqm.kri_monitor",
      agent_version: "2.2.0",
      trigger_seq: 2,
      cut_id: "DC-BASE",
      woke: { seq: 593, hash: "1".repeat(64), recorded_at: "2026-11-07T17:31:48Z", executor: "openshell", image_digest: null, inputs_manifest_sha256: null },
      suspended: { seq: 597, hash: "2".repeat(64), recorded_at: "2026-11-07T17:31:51Z", executor: "openshell", image_digest: IMAGE, applied_policy_sha256: POLICY, policy_file: "policies/rbqm.yaml", policy_file_sha256: POLICY_FILE, inputs_manifest_sha256: MANIFEST },
    },
  };
}

export const ledgerPage: Schemas["LedgerPage"] = {
  entries: [
    { seq: 9, event_type: "finding_raised", recorded_at: "2026-11-07T18:00:00Z", hash: "9".repeat(64), prev_hash: "8".repeat(64), marker: "restricted" },
    {
      seq: 8,
      event_id: "01a1b4f9-eca2-7056-afce-1ce6e01e9115",
      occurred_at: "2026-11-07T17:00:00Z",
      recorded_at: "2026-11-07T17:00:00Z",
      actor: "system.ingest",
      study_id: "MRD-201",
      visibility: "blinded",
      payload: { type: "data_cut", cut_id: "DC-BASE", study_id: "MRD-201", path: "curated/MRD-201/DC-BASE" },
      prev_hash: "7".repeat(64),
      hash: "8".repeat(64),
    },
  ],
  next_since: null,
  next_before: null,
};

function record(request: Request, body: unknown = null): void {
  const url = new URL(request.url);
  seen.push({
    method: request.method,
    path: url.pathname + url.search,
    actor: request.headers.get("X-Meridian-Actor"),
    role: request.headers.get("X-Meridian-Role"),
    body,
  });
}

function json(data: JsonBodyType, status = 200): Response {
  return HttpResponse.json(data, { status });
}

export const handlers = [
  http.get(`${BASE}/people`, ({ request }) => {
    record(request);
    return json(people);
  }),
  http.get(`${BASE}/review-queue`, ({ request }) => {
    record(request);
    return json(queue);
  }),
  http.get(`${BASE}/findings/:id`, ({ request, params }) => {
    record(request);
    const id = String(params.id);
    return id.startsWith("RBQM") ? json(detail(id)) : json({ detail: "finding not found" }, 404);
  }),
  http.get(`${BASE}/ledger`, ({ request }) => {
    record(request);
    return json(ledgerPage);
  }),
  http.get(`${BASE}/ledger/verify`, ({ request }) => {
    record(request);
    const report: Schemas["VerifyReport"] = { ok: true, events_checked: 9, first_broken_seq: null, problem: null };
    return json(report);
  }),
];

export const server = setupServer(...handlers);
