import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { renderApp } from "./render";
import { BASE, ledgerPage, seen, server } from "./server";

describe("ledger", () => {
  it("renders redacted events as redacted, newest first", async () => {
    renderApp("/ledger", "karan.mehta");
    const row = await screen.findByTestId("redacted-row");
    expect(within(row).getByText("Redacted: a restricted event your role may not read.")).toBeTruthy();
    expect(within(row).getByText("9")).toBeTruthy();
    expect(screen.getByText("Data cut DC-BASE for MRD-201")).toBeTruthy();
    expect(seen.find((s) => s.path.startsWith("/api/v1/ledger"))?.path).toBe("/api/v1/ledger?order=desc&limit=50");
  });

  it("verifies the chain on request", async () => {
    const user = userEvent.setup();
    renderApp("/ledger");
    await user.click(await screen.findByRole("button", { name: "Verify the hash chain" }));
    expect(await screen.findByText("The chain verifies: 9 events checked, every hash recomputes.")).toBeTruthy();
  });
});


describe("ledger names", () => {
  it("names a person once, from people.toml, and links the finding", async () => {
    const decision: Schemas["Event"] = {
      seq: 10,
      event_id: "01a1b4f9-eca2-7056-afce-1ce6e01e9116",
      occurred_at: "2026-11-07T18:20:39Z",
      recorded_at: "2026-11-07T18:20:39Z",
      actor: "asha.rao",
      study_id: "MRD-201",
      visibility: "blinded",
      payload: {
        type: "human_decision",
        decision: { finding_id: "RBQM-0134", action: "dismissed", actor: "asha.rao", role: "clinical_ops_lead", reason: "x" },
      },
      prev_hash: "9".repeat(64),
      hash: "a".repeat(64),
    };
    server.use(
      http.get(`${BASE}/ledger`, () => HttpResponse.json({ ...ledgerPage, entries: [decision, ...ledgerPage.entries] })),
    );
    renderApp("/ledger");
    const link = await screen.findByRole("link", { name: "RBQM-0134" });
    expect(link.getAttribute("href")).toBe("/findings/RBQM-0134");
    const cell = link.closest("td");
    expect(cell?.textContent).toBe("RBQM-0134 dismissed by Asha Rao (Clinical operations lead)");
    expect(within(cell as HTMLElement).queryByText(/asha\.rao/)).toBeNull();
    // Agents keep their ids.
    expect(screen.getByText("by system.ingest")).toBeTruthy();
  });
});
