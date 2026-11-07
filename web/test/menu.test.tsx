import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderApp } from "./render";

describe("narrow-screen menu", () => {
  it("opens the same navigation as a panel, and Escape closes it with focus back on Menu", async () => {
    const user = userEvent.setup();
    renderApp("/review");
    const button = await screen.findByRole("button", { name: "Menu" });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    await user.click(button);
    const panel = screen.getByRole("dialog", { name: "Menu" });
    expect(button.getAttribute("aria-expanded")).toBe("true");
    const nav = within(panel).getByRole("navigation", { name: "Views" });
    expect(within(nav).getByRole("link", { name: /Review queue/ })).toBeTruthy();
    expect(within(nav).getByRole("group", { name: "Platform" })).toBeTruthy();
    expect(within(nav).getAllByText("arrives in a later step").length).toBeGreaterThan(0);
    expect(document.activeElement).toBe(within(panel).getByRole("button", { name: "Close" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Menu" })).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("closes with its close button and after choosing a view", async () => {
    const user = userEvent.setup();
    renderApp("/review");
    const button = await screen.findByRole("button", { name: "Menu" });
    await user.click(button);
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(button);
    await user.click(button);
    await user.click(within(screen.getByRole("dialog")).getByRole("link", { name: "Event ledger" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByRole("heading", { name: "Event ledger" })).toBeTruthy();
  });
});
