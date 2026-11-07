import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, expect } from "vitest";
import { seen, server } from "./server";

const PUBLIC = ["/api/v1/people", "/api/v1/health"];

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
});
beforeEach(() => {
  seen.length = 0;
});
afterEach(() => {
  cleanup();
  // No request may ever leave without both identity headers (except the two public ones).
  const bare = seen.filter((s) => !PUBLIC.some((p) => s.path.startsWith(p)) && (!s.actor || !s.role));
  expect(bare).toEqual([]);
  server.resetHandlers();
  try {
    window.localStorage.clear();
  } catch {
    // ignore
  }
});
afterAll(() => {
  server.close();
});
