import { describe, expect, it } from "vitest";
import { buildIndex, coverage, DECLINE_BELOW, search, tokens, unseenIdf } from "../demo/search.mjs";

const passages = [
  { id: "t1", source: "transcript:scene-6", title: "Chapter 6", text: "The NOAEL is the toxicologist's call: two milligrams per kilogram.", start: 216, scene: 6 },
  { id: "d1", source: "docs/phase4b-summary.md#Measurements", title: "Phase 4b", text: "A full pass of 910 runs took 270 s on GKE." },
  { id: "d2", source: "README.md#Honest limits", title: "Limits", text: "Synthetic and public data only; development identity only." },
];

describe("the demo's retrieval", () => {
  it("tokenises like the index: lower case, no stop words, plurals folded", () => {
    expect(tokens("The Runs of GKE's dispatchers")).toEqual(["run", "gke", "dispatcher"]);
  });

  it("ranks the passage that holds the question's rare words first", () => {
    const r = search(buildIndex(passages), "How many runs in a full pass on GKE?");
    expect(r.declined).toBe(false);
    expect(r.results[0]?.source).toBe("docs/phase4b-summary.md#Measurements");
    expect(r.results[0]?.start).toBeNull();
  });

  it("gives a transcript passage its time and scene", () => {
    const r = search(buildIndex(passages), "What is the NOAEL?");
    expect(r.results[0]?.start).toBe(216);
    expect(r.chapters).toEqual([6]);
  });

  it("declines when nothing covers at least half of the question", () => {
    expect(DECLINE_BELOW).toBe(0.5);
    expect(search(buildIndex(passages), "weather forecast in Paris").declined).toBe(true);
  });

  it("counts a word that is in no passage towards a decline (run 2)", () => {
    const index = buildIndex(passages);
    const unseen = unseenIdf(index);
    expect(unseen).toBeGreaterThan(Math.max(...Object.values(index.idf)));
    // "noael" is known; "snowflake" is in no passage, so it can never be covered.
    const doc = index.docs[0];
    if (!doc) throw new Error("no passage");
    expect(coverage(index, doc, ["noael", "snowflake"])).toBeLessThan(DECLINE_BELOW);
    expect(search(index, "NOAEL in Snowflake").declined).toBe(true);
    expect(search(index, "What is the NOAEL?").declined).toBe(false);
  });
});
