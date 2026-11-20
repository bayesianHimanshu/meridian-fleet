import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, expect } from "vitest";
import { seen, server } from "./server";

const PUBLIC = ["/api/v1/people", "/api/v1/health"];

// findBy* and waitFor wait up to 3 s (the default is 1 s): the heaviest pages (the RBQM
// heatmap) took 1.2 to 2.5 s to first render on a laptop under memory pressure, and the
// pre-commit hook failed on that timing alone. No assertion changes.
configure({ asyncUtilTimeout: 3000 });

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
