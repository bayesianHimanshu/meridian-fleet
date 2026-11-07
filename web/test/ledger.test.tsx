import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderApp } from "./render";
import { seen } from "./server";

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
