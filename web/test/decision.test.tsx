import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { renderApp } from "./render";
import { BASE, seen, server } from "./server";

const posted = () => seen.filter((s) => s.method === "POST");

function respond(status: number, body: unknown) {
  server.use(
    http.post(`${BASE}/findings/:id/decisions`, async ({ request }) => {
      seen.push({
        method: "POST",
        path: new URL(request.url).pathname,
        actor: request.headers.get("X-Meridian-Actor"),
        role: request.headers.get("X-Meridian-Role"),
        body: await request.json(),
      });
      return HttpResponse.json(body as Record<string, unknown>, { status });
    }),
  );
}

const created: Schemas["DecisionResponse"] = {
  finding_id: "RBQM-0003",
  status: "accepted",
  decision: {
    seq: 789,
    hash: "3".repeat(64),
    prev_hash: "2".repeat(64),
    recorded_at: "2026-11-08T09:00:00Z",
    actor: "asha.rao",
    role: "clinical_ops_lead",
    action: "accepted",
    reason: "Confirmed with the site",
  },
};

describe("decision form", () => {
  it("posts the action and reason with the caller's headers", async () => {
    const user = userEvent.setup();
    respond(201, created);
    renderApp("/findings/RBQM-0003");
    await user.type(await screen.findByLabelText(/Reason/), "Confirmed with the site");
    await user.click(screen.getByRole("button", { name: /Record decision/ }));
    expect(await screen.findByText(/Recorded: accepted at ledger seq/)).toBeTruthy();
    expect(posted()).toEqual([
      {
        method: "POST",
        path: "/api/v1/findings/RBQM-0003/decisions",
        actor: "asha.rao",
        role: "clinical_ops_lead",
        body: { action: "accepted", reason: "Confirmed with the site" },
      },
    ]);
  });

  it.each([
    [409, { detail: "finding RBQM-0003 is already accepted" }, "finding RBQM-0003 is already accepted"],
    [403, { detail: "qa_auditor may review findings but may never make a decision" }, "qa_auditor may review findings but may never make a decision"],
    [422, { detail: "a dismissal needs a non-empty reason" }, "a dismissal needs a non-empty reason"],
    [422, { detail: [{ loc: ["body", "reason"], msg: "String should have at most 2000 characters", type: "string_too_long" }] }, "reason: String should have at most 2000 characters"],
  ])("shows the API's %i message as it came", async (status, body, text) => {
    const user = userEvent.setup();
    respond(status, body);
    renderApp("/findings/RBQM-0003");
    await user.click(await screen.findByRole("radio", { name: "Escalate" }));
    await user.click(screen.getByRole("button", { name: /Record decision/ }));
    expect((await screen.findByRole("alert")).textContent).toBe(text);
  });

  it("needs a reason to dismiss and sends nothing without one", async () => {
    const user = userEvent.setup();
    respond(201, created);
    renderApp("/findings/RBQM-0003");
    await user.click(await screen.findByRole("radio", { name: "Dismiss" }));
    await user.click(screen.getByRole("button", { name: /Record decision/ }));
    expect((await screen.findByRole("alert")).textContent).toBe("Give a reason to dismiss a finding.");
    expect(posted()).toEqual([]);
  });

  it("submits with Enter only from the reason field", async () => {
    const user = userEvent.setup();
    respond(201, created);
    renderApp("/findings/RBQM-0003");
    const radio = await screen.findByRole("radio", { name: "Accept" });
    radio.focus();
    await user.keyboard("{Enter}");
    expect(posted()).toEqual([]);
    await user.click(screen.getByLabelText(/Reason/));
    await user.keyboard("ok{Enter}");
    await screen.findByText(/Recorded: accepted/);
    expect(posted()).toHaveLength(1);
  });

  it("is hidden for the QA auditor, with a one-line explanation", async () => {
    renderApp("/findings/RBQM-0003", "nina.fernandes");
    expect((await screen.findByTestId("qa-note")).textContent).toBe(
      "QA auditors review findings and the record, but cannot make decisions.",
    );
    expect(screen.queryByRole("button", { name: /Record decision/ })).toBeNull();
    expect(screen.queryByRole("radio")).toBeNull();
  });
});
