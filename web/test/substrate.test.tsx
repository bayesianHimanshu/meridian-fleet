import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { renderApp } from "./render";
import {
  BASE,
  IMAGE,
  MANIFEST,
  POLICY,
  POLICY_FILE,
  detail,
  server,
} from "./server";

const TEMPLATE_SHA = "7".repeat(64);
const COMMIT = "b34fbee742906494b5eb4f1f36987176bf8770a4";
const SOURCE = "563170a0b1c2d3e4f5061728394a5b6c7d8e9f01";

const placement: Schemas["SubstrateRun"] = {
  version: "v1.0.0",
  commit: COMMIT,
  atespace: "meridian",
  template: "rbqm-ccc1d3aa3a7b",
  template_sha256: TEMPLATE_SHA,
  template_file: "deploy/substrate/templates/rbqm.yaml",
  template_file_sha256: "6".repeat(64),
  image: `localhost:5001/meridian-agent@sha256:${"c".repeat(64)}`,
  worker_pool: "meridian/meridian",
  sandbox_class: "gvisor",
  actor: "mer-km-1-a8e5b26e",
  golden_tag: "aa7b3ef7-873c-4f20-8e0b-9e3957f78f3f",
  golden_snapshot: "f52e7bb5-6906-4f56-87df-c546b858d8b3",
  actor_uid: "17079247-2153-4678-ad9f-f9770e0c0627",
  worker_pod: "meridian-798dbc5455-v68vc",
  image_source_commit: SOURCE,
};

describe("Agent Substrate placement (phase 4a)", () => {
  it("shows the executor and the last run's placement on the fleet page", async () => {
    const agent: Schemas["FleetAgent"] = {
      agent_id: "rbqm.kri_monitor",
      version: "2.2.0",
      zone: "rbqm",
      role: "detector",
      enabled: true,
      policy: "rbqm",
      policy_file: "policies/rbqm.yaml",
      policy_file_sha256: POLICY_FILE,
      applied_policy_sha256: POLICY,
      last_run_at: "2026-11-10T09:00:00Z",
      last_outcome: "completed",
      last_trigger_seq: 1,
      runs_7d: 1,
      failed_7d: 0,
      last_failure_at: null,
      run_event_types: ["actor_woke", "actor_suspended"],
      executor: "substrate",
      substrate: placement,
    };
    server.use(
      http.get(`${BASE}/fleet`, () =>
        HttpResponse.json({ agents: [agent], containment: [] }),
      ),
    );
    renderApp("/fleet");
    const row = (await screen.findAllByText("rbqm.kri_monitor"))[0]?.closest(
      "tr",
    );
    const cells = within(row as HTMLElement);
    expect(cells.getByText("substrate")).toBeTruthy();
    expect(cells.getByText("rbqm-ccc1d3aa3a7b")).toBeTruthy();
    expect(cells.getByTitle(TEMPLATE_SHA)).toBeTruthy();
    expect(cells.getByText(placement.golden_snapshot)).toBeTruthy();
    expect(cells.getByText("gvisor")).toBeTruthy();
  });

  it("shows the placement in a finding's traceability", async () => {
    const d = detail();
    const run = d.run;
    if (!run?.suspended || !run.woke) throw new Error("fixture has a run");
    const traced: Schemas["FindingDetail"] = {
      ...d,
      run: {
        ...run,
        woke: {
          ...run.woke,
          executor: "substrate",
          substrate: { ...placement, actor_uid: null, worker_pod: null },
        },
        suspended: {
          ...run.suspended,
          executor: "substrate",
          image_digest: IMAGE,
          applied_policy_sha256: null,
          policy_file: null,
          policy_file_sha256: null,
          inputs_manifest_sha256: MANIFEST,
          substrate: placement,
        },
      },
    };
    server.use(
      http.get(`${BASE}/findings/RBQM-0003`, () => HttpResponse.json(traced)),
    );
    renderApp("/findings/RBQM-0003");
    await screen.findByRole("heading", { name: "Traceability" });
    const placed = screen.getByTestId("substrate-placement");
    const box = within(placed);
    expect(box.getByText("v1.0.0")).toBeTruthy();
    expect(box.getByTitle(COMMIT)).toBeTruthy();
    expect(box.getByTitle(TEMPLATE_SHA)).toBeTruthy();
    expect(box.getByTitle(SOURCE)).toBeTruthy();
    expect(box.getByText(placement.golden_snapshot)).toBeTruthy();
    expect(
      box.getByText(`${placement.actor} (${placement.actor_uid ?? ""})`),
    ).toBeTruthy();
    expect(screen.getByText("substrate")).toBeTruthy();
  });
});
