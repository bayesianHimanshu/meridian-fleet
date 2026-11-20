import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { AUDIT_CHECK_TEXT } from "../src/views/Quality";
import { renderApp } from "./render";
import { BASE, detail, finding, ledgerPage, server } from "./server";

const CHECKS = Object.keys(AUDIT_CHECK_TEXT);

function audit(over: Partial<Schemas["AuditView"]> = {}): Schemas["AuditView"] {
  return {
    agent_id: "quality.audit_trail_reviewer",
    agent_version: "1.0.0",
    last_review_seq: 4432,
    last_review_at: "2026-11-10T14:00:00Z",
    review_from_seq: 1,
    review_to_seq: 4430,
    review_completed: true,
    chain_linkage: "intact",
    chain_findings: 0,
    by_check: CHECKS.map((check) => ({
      check,
      count: check === "audit_rapid_decisions" ? 1 : 0,
    })),
    findings: [
      {
        finding_id: "QA-0150",
        check: "audit_rapid_decisions",
        severity: "medium",
        status: "open",
        title: "asha.rao recorded 12 decisions in 3.2 minutes",
        actor_under_review: "asha.rao",
      },
    ],
    ...over,
  };
}

describe("Quality and audit page", () => {
  it("has tabs, and the Audit tab shows the reviewed range, the chain result and findings by check", async () => {
    server.use(
      http.get(`${BASE}/quality/audit`, () => HttpResponse.json(audit())),
    );
    const user = userEvent.setup();
    renderApp("/quality?tab=audit", "nina.fernandes");
    const tabs = await screen.findByRole("tablist", {
      name: "Quality and audit",
    });
    expect(
      within(tabs)
        .getAllByRole("tab")
        .map((t) => t.textContent),
    ).toEqual(["Trial Master File", "Audit trail", "Observations"]);
    expect(
      within(tabs)
        .getByRole("tab", { name: "Audit trail" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    const range = await screen.findByTestId("review-range");
    expect(range.textContent).toContain("seq 1 to 4430");
    expect(screen.getByTestId("chain-result").textContent).toContain("Intact");
    const row = screen.getByRole("row", {
      name: /Many decisions in a few minutes/,
    });
    expect(within(row).getByRole("cell").textContent).toBe("1");
    expect(
      screen.getByRole("link", { name: "QA-0150" }).getAttribute("href"),
    ).toBe("/findings/QA-0150");
    expect(screen.getByText(/who may not\s+decide it/)).toBeTruthy();
    // Arrow keys move between tabs.
    within(tabs).getByRole("tab", { name: "Audit trail" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(
      within(tabs)
        .getByRole("tab", { name: "Observations" })
        .getAttribute("aria-selected"),
    ).toBe("true");
  });

  it("says when the chain is broken, when nothing was reviewed, and when the role may not see it", async () => {
    server.use(
      http.get(`${BASE}/quality/audit`, () =>
        HttpResponse.json(
          audit({ chain_linkage: "broken", chain_findings: 1 }),
        ),
      ),
    );
    const first = renderApp("/quality?tab=audit", "asha.rao");
    expect((await screen.findByTestId("chain-result")).textContent).toContain(
      "Broken",
    );
    first.unmount();
    server.use(
      http.get(`${BASE}/quality/audit`, () =>
        HttpResponse.json(
          audit({
            chain_linkage: "not_reviewed",
            review_from_seq: null,
            review_to_seq: null,
            findings: [],
          }),
        ),
      ),
    );
    const second = renderApp("/quality?tab=audit", "asha.rao");
    expect(
      await screen.findByText("No audit review has run yet."),
    ).toBeTruthy();
    second.unmount();
    server.use(
      http.get(`${BASE}/quality/audit`, () =>
        HttpResponse.json({ detail: "no" }, { status: 403 }),
      ),
    );
    renderApp("/quality?tab=audit", "karan.mehta");
    expect(
      await screen.findByText("Your role does not review the audit trail."),
    ).toBeTruthy();
  });

  it("lists observations on their tab", async () => {
    server.use(
      http.get(`${BASE}/quality/observations`, () =>
        HttpResponse.json([
          {
            finding_id: "OBS-0001",
            seq: 4440,
            recorded_at: "2026-11-10T15:00:00Z",
            raised_by: "nina.fernandes",
            severity: "medium",
            status: "open",
            title: "Dismissal reason too thin",
            refers_to_finding_id: "RBQM-0134",
            refers_to_seq: null,
          },
        ]),
      ),
    );
    renderApp("/quality?tab=observations", "karan.mehta");
    expect(await screen.findByRole("link", { name: "OBS-0001" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "RBQM-0134" })).toBeTruthy();
    expect(screen.getByText("nina.fernandes")).toBeTruthy();
  });
});

describe("Raise an observation", () => {
  function serveObservation() {
    const sent: unknown[] = [];
    server.use(
      http.post(`${BASE}/quality/observations`, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(
          {
            observation_id: "OB-0123456789ab",
            observation_seq: 4441,
            finding_id: "OBS-0002",
            finding_seq: 4442,
          },
          { status: 201 },
        );
      }),
    );
    return sent;
  }

  it("is offered to the QA auditor on a finding page; Enter submits only from the description", async () => {
    const sent = serveObservation();
    const user = userEvent.setup();
    renderApp("/findings/RBQM-0003", "nina.fernandes");
    await user.click(
      await screen.findByRole("button", { name: "Raise an observation" }),
    );
    const form = screen.getByRole("form", {
      name: "Raise an observation about finding RBQM-0003",
    });
    const title = within(form).getByLabelText("Title");
    await user.type(title, "Thin reason{Enter}");
    expect(sent).toEqual([]); // Enter in the title does nothing
    const description = within(form).getByLabelText(/Description/);
    await user.type(description, "Line one{Shift>}{Enter}{/Shift}line two");
    expect((description as HTMLTextAreaElement).value).toBe(
      "Line one\nline two",
    );
    expect(sent).toEqual([]);
    await user.type(description, "{Enter}");
    expect(await screen.findByTestId("observation-raised")).toBeTruthy();
    expect(sent).toEqual([
      {
        title: "Thin reason",
        description: "Line one\nline two",
        severity: "medium",
        finding_id: "RBQM-0003",
      },
    ]);
  });

  it("is offered on a ledger row, and to nobody but the QA auditor", async () => {
    const sent = serveObservation();
    const user = userEvent.setup();
    const qa = renderApp("/ledger", "nina.fernandes");
    const buttons = await screen.findAllByRole("button", {
      name: "Raise an observation",
    });
    expect(buttons.length).toBeGreaterThan(0);
    const first = buttons[0];
    if (!first) throw new Error("no button");
    await user.click(first);
    const form = screen.getByRole("form", {
      name: /Raise an observation about ledger seq/,
    });
    await user.type(
      within(form).getByLabelText("Title"),
      "Decision without reading",
    );
    await user.type(
      within(form).getByLabelText(/Description/),
      "Two decisions within a second.{Enter}",
    );
    expect(await screen.findByTestId("observation-raised")).toBeTruthy();
    // The first row shown in full (a redacted row offers no observation).
    const firstShown = ledgerPage.entries.find((e) => !("marker" in e));
    expect((sent[0] as { ledger_seq: number }).ledger_seq).toBe(
      firstShown?.seq,
    );
    qa.unmount();
    const ops = renderApp("/ledger", "asha.rao");
    await screen.findByRole("heading", { name: "Event ledger" });
    expect(
      screen.queryByRole("button", { name: "Raise an observation" }),
    ).toBeNull();
    ops.unmount();
    renderApp("/findings/RBQM-0003", "karan.mehta");
    await screen.findByRole("heading", { name: "Traceability" });
    expect(
      screen.queryByRole("button", { name: "Raise an observation" }),
    ).toBeNull();
  });

  it("explains why a person may not decide a finding about their own decisions", async () => {
    server.use(
      http.get(`${BASE}/findings/:id`, () =>
        HttpResponse.json(
          detail("QA-0150", {
            ...finding("QA-0150"),
            kind: "audit",
            agent_id: "quality.audit_trail_reviewer",
            actor_under_review: "asha.rao",
            may_decide: false,
            decide_blocked_reason:
              "QA-0150 is about the actions of asha.rao: a person may not decide a finding that names them as the actor under review",
          }),
        ),
      ),
    );
    renderApp("/findings/QA-0150", "asha.rao");
    const note = await screen.findByTestId("decide-blocked");
    expect(note.textContent).toContain("names them as the actor under review");
    expect(
      screen.queryByRole("form", { name: "Record a decision" }),
    ).toBeNull();
    expect(screen.getByTestId("under-review").textContent).toContain(
      "asha.rao",
    );
  });
});
