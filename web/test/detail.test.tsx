import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp } from "./render";
import { FINDING_HASH, IMAGE, MANIFEST, POLICY } from "./server";

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
