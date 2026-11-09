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
      "Safety findings are not sent to the cloud language model. A summary from a local model arrives in a later step.",
    );
    expect(screen.queryByText("No accepted narrative for this finding.")).toBeNull();
  });
});
