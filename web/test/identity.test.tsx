import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { MissingIdentityError, api, setIdentity } from "../src/api/client";
import { renderApp } from "./render";
import { seen } from "./server";

describe("development identity", () => {
  it("says it is not real sign-in, and the pill follows the role", async () => {
    const user = userEvent.setup();
    renderApp("/review");
    expect(await screen.findByText(/Development identity: not real sign-in/)).toBeTruthy();
    expect(screen.getByText("Blinded view")).toBeTruthy();
    await user.selectOptions(screen.getByLabelText("Acting as"), "omar.siddiqui");
    expect(await screen.findByText("Unblinded view")).toBeTruthy();
  });

  it("switching person changes the headers and refetches", async () => {
    const user = userEvent.setup();
    renderApp("/review");
    await screen.findByRole("list", { name: "Findings to review" });
    const before = seen.filter((s) => s.path.startsWith("/api/v1/review-queue")).length;
    await user.selectOptions(screen.getByLabelText("Acting as"), "karan.mehta");
    await waitFor(() => {
      const queue = seen.filter((s) => s.path.startsWith("/api/v1/review-queue"));
      expect(queue.length).toBeGreaterThan(before);
      expect(queue.at(-1)).toMatchObject({ actor: "karan.mehta", role: "medical_monitor" });
    });
    expect(window.localStorage.getItem("meridian.command-center.actor")).toBe("karan.mehta");
    const asha = seen.filter((s) => s.actor === "asha.rao").length;
    expect(asha).toBeGreaterThan(0);
    expect(seen.slice(-1)[0]?.actor).toBe("karan.mehta");
  });

  it("never sends a request without identity headers", async () => {
    setIdentity(null);
    await expect(api.GET("/api/v1/findings")).rejects.toBeInstanceOf(MissingIdentityError);
    expect(seen).toEqual([]); // nothing reached the network
  });

  it("works when localStorage is unavailable", async () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("blocked");
      },
    });
    try {
      const { render } = await import("@testing-library/react");
      const { MemoryRouter } = await import("react-router");
      const { App } = await import("../src/App");
      const { Providers, makeQueryClient } = await import("../src/root");
      render(
        <Providers client={makeQueryClient()}>
          <MemoryRouter initialEntries={["/review"]}>
            <App />
          </MemoryRouter>
        </Providers>,
      );
      expect(await screen.findByText("Blinded view")).toBeTruthy(); // the first person
    } finally {
      if (original) Object.defineProperty(window, "localStorage", original);
    }
  });
});
