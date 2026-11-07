import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { ApiError, api, setIdentity, unwrap, type Schemas } from "../src/api/client";
import { renderApp } from "./render";
import { BASE, detail, seen, server } from "./server";

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
    await user.click(await screen.findByRole("radio", { name: "Accept" }));
    await user.type(screen.getByLabelText(/Reason/), "Confirmed with the site");
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

  it("a 409 from the API reaches the caller as the API's own message", async () => {
    respond(409, { detail: "finding RBQM-0003 is already accepted" });
    setIdentity({ actor: "asha.rao", role: "clinical_ops_lead" });
    const call = unwrap(
      api.POST("/api/v1/findings/{finding_id}/decisions", {
        params: { path: { finding_id: "RBQM-0003" } },
        body: { action: "escalated", reason: null },
      }),
    );
    await expect(call).rejects.toEqual(new ApiError(409, "finding RBQM-0003 is already accepted"));
    await expect(call).rejects.toMatchObject({ status: 409 });
    expect(posted()).toMatchObject([{ actor: "asha.rao", role: "clinical_ops_lead" }]);
  });

  it("replaces the form with one line once the finding is closed", async () => {
    server.use(
      http.get(`${BASE}/findings/:id`, () => {
        const closed = detail("RBQM-0003", { status: "dismissed", decision_count: 1 });
        closed.decisions = [
          {
            seq: 788,
            hash: "4".repeat(64),
            prev_hash: "3".repeat(64),
            recorded_at: "2026-11-07T18:20:39Z",
            actor: "asha.rao",
            role: "clinical_ops_lead",
            action: "dismissed",
            reason: "Known natural breach",
          },
        ];
        return HttpResponse.json(closed);
      }),
    );
    renderApp("/findings/RBQM-0003", "karan.mehta");
    const line = await screen.findByTestId("closed-line");
    expect(line.textContent).toBe(
      "Dismissed by Asha Rao (Clinical operations lead) on 2026-11-07 18:20 UTC: Known natural breach. Ledger seq 788.",
    );
    expect(screen.queryByRole("button", { name: /Record decision/ })).toBeNull();
    expect(screen.queryByRole("radio")).toBeNull();
  });

  it("selects nothing by default and keeps submit disabled until an action is chosen", async () => {
    const user = userEvent.setup();
    respond(201, created);
    renderApp("/findings/RBQM-0003");
    const radios = await screen.findAllByRole("radio");
    expect(radios.map((r) => (r as HTMLInputElement).checked)).toEqual([false, false, false]);
    const submit = screen.getByRole("button", { name: /Record decision/ });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    await user.type(screen.getByLabelText(/Reason/), "x{Enter}");
    expect(posted()).toEqual([]);
    await user.click(screen.getByRole("radio", { name: "Escalate" }));
    expect((submit as HTMLButtonElement).disabled).toBe(false);
  });

  it("notes that a finding from evaluation data does not change the study", async () => {
    server.use(
      http.get(`${BASE}/findings/:id`, () =>
        HttpResponse.json(detail("RBQM-0050", { scope: "evaluation", cut_id: "DC-RBQM-CORE" })),
      ),
    );
    renderApp("/findings/RBQM-0050");
    expect((await screen.findByTestId("evaluation-note")).textContent).toBe(
      "This finding comes from evaluation data, not the study's current data. A decision here is recorded in the ledger but does not change anything for the study.",
    );
    expect(screen.getByRole("button", { name: /Record decision/ })).toBeTruthy(); // the form stays
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
    await user.click(radio);
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
