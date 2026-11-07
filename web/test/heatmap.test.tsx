import { describe, expect, it } from "vitest";
import { thresholdBand, zBand } from "../src/views/Rbqm";

describe("heatmap bands", () => {
  it.each([
    [0, "normal"],
    [1.99, "normal"],
    [2, "warning"],
    [2.5, "warning"],
    [3, "warning"],
    [3.0001, "critical"],
    [-1.99, "normal"],
    [-2, "warning"],
    [-3, "warning"],
    [-5.46, "critical"],
  ])("z %d is %s", (z, band) => {
    expect(zBand(z)).toBe(band);
  });

  it("a missing z is not computed", () => {
    expect(zBand(null)).toBe("na");
    expect(zBand(undefined)).toBe("na");
  });

  it.each([
    [6.9, "normal"],
    [7, "normal"], // the agent flags above the threshold, not at it
    [7.1, "warning"],
    [10.5, "warning"],
    [10.6, "critical"],
    [14, "critical"],
  ])("entry lag %d days against 7 and 10.5 is %s", (days, band) => {
    expect(thresholdBand(days, 7, 10.5)).toBe(band);
  });

  it("query age uses its own thresholds, and a missing value or threshold is not computed", () => {
    expect(thresholdBand(21, 21, 31.5)).toBe("normal");
    expect(thresholdBand(40, 21, 31.5)).toBe("critical");
    expect(thresholdBand(null, 21, 31.5)).toBe("na");
    expect(thresholdBand(10, undefined, undefined)).toBe("na");
  });
});
