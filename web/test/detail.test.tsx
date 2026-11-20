import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { renderApp } from "./render";
import { BASE, FINDING_HASH, IMAGE, MANIFEST, POLICY, POLICY_FILE, detail, server } from "./server";

describe("finding detail", () => {
  it("shows the traceability fields, short with the full value on hover", async () => {
    renderApp("/findings/RBQM-0003");
    await screen.findByRole("heading", { name: "Traceability" });
    expect(screen.getByText("openshell")).toBeTruthy();
    for (const full of [IMAGE, POLICY, MANIFEST, FINDING_HASH]) {
      const shown = screen.getByTitle(full);
      expect(shown.textContent).not.toEqual(full);
      expect(shown.textContent).toContain("...");
    }
    expect(screen.getByTitle(IMAGE).textContent).toBe("sha256:9630f178...0e412b");
    expect(screen.getByRole("button", { name: "Copy full image digest" })).toBeTruthy();
    expect(screen.getByText("595")).toBeTruthy(); // ledger seq of the FindingRaised event
  });

  it("shows evidence displays with their role", async () => {
    renderApp("/findings/RBQM-0003");
    const value = await screen.findByText("18 observed (5.1 expected)");
    // The statistic key is a muted line under the value, not its own column.
    expect(value.closest("td")?.querySelector(".ev-key")?.textContent).toBe("deviations:observed");
    expect(screen.queryByRole("columnheader", { name: "Statistic" })).toBeNull();
    expect(screen.getByText("primary")).toBeTruthy();
    expect(screen.getByText("supporting")).toBeTruthy();
  });

  it("says a hidden or missing finding is not visible, without telling which", async () => {
    renderApp("/findings/DSMB-0001");
    expect(await screen.findByText("This finding does not exist, or your role cannot see it.")).toBeTruthy();
  });
});

describe("finding detail, step 3a polish", () => {
  it("labels the policy file and the applied policy sha256 of the run", async () => {
    renderApp("/findings/RBQM-0003");
    await screen.findByRole("heading", { name: "Traceability" });
    expect(screen.getByText("policy file sha256")).toBeTruthy();
    expect(screen.getByText("applied policy sha256")).toBeTruthy();
    expect(screen.getByText("policies/rbqm.yaml")).toBeTruthy();
    expect(screen.getByTitle(POLICY_FILE)).toBeTruthy();
    expect(screen.getByTitle(POLICY)).toBeTruthy();
  });

  it("returns to where the reader came from, else to the review queue", async () => {
    const from = { path: "/safety", label: "Back to the Safety page" };
    const { unmount } = renderApp({ pathname: "/findings/RBQM-0003", state: { from } });
    const back = await screen.findByRole("link", { name: "Back to the Safety page" });
    expect(back.getAttribute("href")).toBe("/safety");
    unmount();
    renderApp("/findings/RBQM-0003");
    expect((await screen.findByRole("link", { name: "Back to the review queue" })).getAttribute("href")).toBe(
      "/review",
    );
  });

  it("ignores a carried origin that is not one of the known pages", async () => {
    renderApp({ pathname: "/findings/RBQM-0003", state: { from: { path: "https://example.org", label: "x" } } });
    expect(await screen.findByRole("link", { name: "Back to the review queue" })).toBeTruthy();
  });

  it("shows timestamps readably with the ISO value on hover, and the safety summary note", async () => {
    const iso = "2013-01-20T22:02:00+00:00";
    server.use(
      http.get(`${BASE}/findings/:id`, () => {
        const d = detail("SAF-0316", {
          agent_id: "safety.sae_reconciler",
          agent_version: "1.1.1",
          kind: "sae_reconciliation",
          title: "Safety case SC-1 reached the sponsor 126 hours (5.2 days) after awareness",
          summary: `The site became aware at ${iso} and the sponsor received the initial report at 2013-01-26T04:02:00Z.`,
        });
        const first = d.evidence[0];
        return HttpResponse.json({
          ...d,
          evidence: first ? [{ ...first, display: iso, selector: "CASEID=SC-1; VERSION=1; USUBJID=201-1001-001" }] : [],
        });
      }),
    );
    renderApp("/findings/SAF-0316", "karan.mehta");
    await screen.findByRole("heading", { name: /126 hours \(5\.2 days\)/ });
    const times = screen.getAllByTitle(iso);
    expect(times.map((t) => t.textContent)).toEqual(["20 Jan 2013, 22:02 UTC", "20 Jan 2013, 22:02 UTC"]);
    expect(screen.getByTitle("2013-01-26T04:02:00Z").textContent).toBe("26 Jan 2013, 04:02 UTC");
    expect(screen.queryByText(new RegExp(iso.replace(/[+]/g, "\\+")))).toBeNull();
    expect(screen.getByText("CASEID=SC-1; VERSION=1; USUBJID=201-1001-001")).toBeTruthy();
    expect(screen.getByTestId("safety-summary-note").textContent).toBe(
      "Safety findings are never sent to the cloud language model. They are summarised by a local model on this machine, and no local summary of this finding has been accepted yet.",
    );
    expect(screen.queryByText("No accepted narrative for this finding.")).toBeNull();
  });

  it("labels a local model summary and shows the same-event assessment, never a rejected answer", async () => {
    const digest = "89962fcc75239ac434cdebceb6b7e0669397f92eaef9c487774b718bc36a3e5f";
    server.use(
      http.get(`${BASE}/findings/:id`, () => {
        const d = detail("SAF-0500", {
          agent_id: "safety.sae_reconciler",
          agent_version: "1.1.1",
          kind: "sae_reconciliation",
          title: "Serious AE of 201-1001-001 has no case in the safety database",
        });
        const assessment = {
          seq: 900,
          recorded_at: "2026-11-09T12:00:00Z",
          agent_id: "safety.term_adjudicator",
          agent_version: "1.0.0",
          candidate: "unpaired_pair" as const,
          model: "granite4:3b",
          model_digest: digest,
          rejection_reasons: [],
          error: null,
          executor: "openshell",
          sandbox: "mer-lm-1a2b3c4d",
          answers: [],
        };
        return HttpResponse.json({
          ...d,
          narratives: [
            {
              seq: 899,
              recorded_at: "2026-11-09T11:59:00Z",
              narrator_version: "1.0.0",
              narrator_id: "safety.local_narrator",
              status: "accepted" as const,
              headline: "Site 1001: a serious AE has no safety case",
              explanation: "Subject 201-1001-001 has a serious AE with onset 2013-01-20 and no case.",
              model: "granite4:3b",
              model_digest: digest,
              rejection_reasons: [],
              executor: "openshell",
              sandbox: "mer-lm-1a2b3c4d",
            },
          ],
          assessments: [
            {
              ...assessment,
              finding_ids: ["SAF-0500", "SAF-0501"],
              status: "accepted" as const,
              verdict: "yes" as const,
              reason: "Both terms name a fainting episode.",
            },
            {
              ...assessment,
              seq: 901,
              finding_ids: ["SAF-0500", "SAF-0501"],
              status: "rejected" as const,
              verdict: null,
              reason: null,
              rejection_reasons: ["bad_json"],
            },
          ],
        });
      }),
    );
    renderApp("/findings/SAF-0500", "karan.mehta");
    await screen.findByRole("heading", { name: "Site 1001: a serious AE has no safety case" });
    expect(screen.getByTestId("narrator-source").textContent).toBe("local model granite4:3b, narrator 1.0.0");
    expect(screen.queryByTestId("safety-summary-note")).toBeNull();
    const shown = screen.getAllByTestId("assessment");
    expect(shown).toHaveLength(1);
    expect(shown[0]?.textContent).toContain("yes: probably the same event Both terms name a fainting episode.");
    expect(shown[0]?.textContent).toContain("Together with SAF-0501");
    expect(screen.getByRole("link", { name: "SAF-0501" })).toBeTruthy();
    expect(screen.getByTestId("assessment-not-shown").textContent).toBe("1 answer not shown: rejected (bad_json).");
    expect(screen.getByText(/the finding is unchanged/)).toBeTruthy();
    expect(screen.queryAllByTestId("assessment-answer")).toHaveLength(0);
  });

  it("shows both raw answers of an agreement assessment", async () => {
    const digest = "78fad5d182a7c33065e153a5f8ba210754207ba9d91973f57dffa7f487363753";
    const digest2 = "d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04";
    const textA = '{"same_event": "yes", "reason": "Both name high blood sugar."}';
    const textB = '{"same_event": "no", "reason": "One is high and one is low blood sugar."}';
    server.use(
      http.get(`${BASE}/findings/:id`, () => {
        const d = detail("SAF-0600", {
          agent_id: "safety.sae_reconciler",
          agent_version: "1.1.1",
          kind: "sae_reconciliation",
          title: "Preferred term differs between the clinical database and the safety case",
        });
        const answer = {
          status: "accepted" as const,
          reason: "r.",
          rejection_reasons: [],
          error: null,
          latency_ms: 900,
        };
        return HttpResponse.json({
          ...d,
          assessments: [
            {
              seq: 950,
              recorded_at: "2026-11-09T12:00:00Z",
              agent_id: "safety.term_adjudicator",
              agent_version: "1.1.0",
              candidate: "term_mismatch" as const,
              finding_ids: ["SAF-0600"],
              status: "accepted" as const,
              verdict: "unsure" as const,
              reason: "The models do not agree that the terms are one event or two (phi4-mini yes; qwen3.5 no).",
              model: "phi4-mini:3.8b-q4_K_M+qwen3.5:4b-q4_K_M",
              model_digest: null,
              rejection_reasons: [],
              error: null,
              executor: "openshell",
              sandbox: "mer-lm-1a2b3c4d",
              answers: [
                { ...answer, model: "phi4-mini:3.8b-q4_K_M", model_digest: digest, verdict: "yes" as const, text: textA },
                { ...answer, model: "qwen3.5:4b-q4_K_M", model_digest: digest2, verdict: "no" as const, text: textB },
              ],
            },
          ],
        });
      }),
    );
    renderApp("/findings/SAF-0600", "karan.mehta");
    const shown = await screen.findAllByTestId("assessment");
    expect(shown).toHaveLength(1);
    expect(shown[0]?.textContent).toContain("unsure");
    const answers = screen.getAllByTestId("assessment-answer");
    expect(answers).toHaveLength(2);
    expect(answers[0]?.textContent).toContain("phi4-mini:3.8b-q4_K_M");
    expect(answers[1]?.textContent).toContain("qwen3.5:4b-q4_K_M");
    expect(screen.getAllByTestId("raw-answer").map((e) => e.textContent)).toEqual([textA, textB]);
  });
});

describe("finding detail, decisions that survive agent upgrades", () => {
  const prev = {
    finding_id: "RBQM-0001",
    agent_version: "2.2.0",
    action: "dismissed" as const,
    actor: "asha.rao",
    role: "clinical_ops_lead" as const,
    reason: "known site backlog",
    recorded_at: "2026-11-07T17:31:50.000000Z",
    seq: 42,
    primary_evidence_sha256: "a".repeat(64),
  };

  it("shows the earlier decision on a previously decided finding", async () => {
    server.use(
      http.get(`${BASE}/findings/:id`, () =>
        HttpResponse.json(detail("RBQM-0003", { previously_decided: prev })),
      ),
    );
    renderApp("/findings/RBQM-0003");
    const note = await screen.findByTestId("previously-decided");
    expect(note.textContent).toContain("asha.rao");
    expect(note.textContent).toContain("dismissed");
    expect(note.textContent).toContain("2.2.0");
    expect(note.textContent).toContain("ledger seq 42");
    expect(note.textContent).toContain("known site backlog");
    expect(note.textContent).toContain("not in the review queue");
    expect(screen.queryByTestId("evidence-changed")).toBeNull();
  });

  it("shows what changed since the earlier decision", async () => {
    server.use(
      http.get(`${BASE}/findings/:id`, () =>
        HttpResponse.json(
          detail("RBQM-0003", {
            evidence_changed_since: {
              decision: { ...prev, action: "accepted", reason: null },
              before: ["18 observed (5.1 expected)"],
              after: ["25 observed (5.1 expected)"],
              removed: ["18 observed (5.1 expected)"],
              added: ["25 observed (5.1 expected)"],
              primary_evidence_sha256: "b".repeat(64),
            },
          }),
        ),
      ),
    );
    renderApp("/findings/RBQM-0003");
    const note = await screen.findByTestId("evidence-changed");
    expect(note.textContent).toContain("accepted");
    expect(note.textContent).toContain("was: 18 observed (5.1 expected)");
    expect(note.textContent).toContain("now: 25 observed (5.1 expected)");
    expect(screen.queryByTestId("previously-decided")).toBeNull();
  });
});
