import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { renderApp } from "./render";
import { BASE, POLICY, POLICY_FILE, seen, server } from "./server";

const SAFETY_FILE = "8a23ec440fc55757080f8d5c4b14c0571cfbae940b1f35eac8b6e439297c407c";

function agent(over: Partial<Schemas["FleetAgent"]>): Schemas["FleetAgent"] {
  return {
    agent_id: "rbqm.qtl_detector",
    version: "2.1.0",
    zone: "rbqm",
    role: "detector",
    enabled: true,
    policy: "rbqm",
    policy_file: "policies/rbqm.yaml",
    policy_file_sha256: POLICY_FILE,
    applied_policy_sha256: POLICY,
    last_run_at: "2026-11-08T09:00:00Z",
    last_outcome: "completed",
    last_trigger_seq: 1,
    runs_7d: 3,
    failed_7d: 0,
    last_failure_at: null,
    run_event_types: ["actor_woke", "actor_suspended"],
    ...over,
  };
}

const fleet: Schemas["Fleet"] = {
  agents: [
    agent({}),
    agent({
      agent_id: "safety.sae_reconciler",
      version: "1.1.1",
      zone: "safety",
      policy: "safety.sae_reconciler",
      policy_file: "policies/safety.sae_reconciler.yaml",
      policy_file_sha256: SAFETY_FILE,
      runs_7d: 40,
      failed_7d: 31,
      last_failure_at: "2026-11-08T18:20:00Z",
    }),
  ],
  containment: [],
};

describe("agent fleet", () => {
  it("labels both policy hashes and counts failed runs, linking to the agent's run events", async () => {
    server.use(http.get(`${BASE}/fleet`, () => HttpResponse.json(fleet)));
    renderApp("/fleet");
    const row = (await screen.findAllByText("safety.sae_reconciler"))[0]?.closest("tr");
    expect(row).toBeTruthy();
    const cells = within(row as HTMLElement);
    expect(cells.getByText("policy file sha256")).toBeTruthy();
    expect(cells.getByText("applied policy sha256")).toBeTruthy();
    expect(cells.getByTitle(SAFETY_FILE)).toBeTruthy();
    expect(cells.getByTitle(POLICY)).toBeTruthy(); // the same applied policy as rbqm
    expect(cells.getByText("40")).toBeTruthy();
    expect(cells.getByText("31")).toBeTruthy();
    expect(cells.getByText("2026-11-08 18:20 UTC")).toBeTruthy();
    const link = cells.getByRole("link", { name: "Run events in the ledger" });
    expect(link.getAttribute("href")).toBe(
      "/ledger?agent=safety.sae_reconciler&type=actor_woke&type=actor_suspended",
    );
  });

  it("opens the ledger filtered to that agent's run events", async () => {
    renderApp("/ledger?agent=safety.sae_reconciler&type=actor_woke&type=actor_suspended");
    expect((await screen.findByTestId("agent-filter")).textContent).toContain("safety.sae_reconciler");
    const call = seen.find((s) => s.path.startsWith("/api/v1/ledger?"));
    expect(call?.path).toBe(
      "/api/v1/ledger?order=desc&limit=50&type=actor_woke&type=actor_suspended&agent=safety.sae_reconciler",
    );
  });
});
