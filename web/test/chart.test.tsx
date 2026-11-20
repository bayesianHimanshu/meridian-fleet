import { describe, expect, it } from "vitest";
import { MIN_LABEL_SPACING, PAD, labelBox, xTicks, yTicks } from "../src/views/Study";

// The 27 months of the CDISC pilot, 2012-07 to 2014-09.
const MONTHS = Array.from({ length: 27 }, (_, i) => {
  const m = 6 + i;
  return `${2012 + Math.floor(m / 12)}-${String((m % 12) + 1).padStart(2, "0")}`;
});

// Panel content width at a 390 px and a 1440 px viewport (gutters, nav, panel padding).
const WIDTHS = { "390 px": 390 - 2 * 16 - 2 * 16 - 2, "1440 px": 1440 - 216 - 2 * 32 - 2 * 16 - 2 };

describe("x ticks", () => {
  it.each(Object.entries(WIDTHS))("at %s: no overlap, first and last inside the plot", (_, width) => {
    const plot = width - PAD.left - PAD.right;
    const ticks = xTicks(MONTHS.length, plot);
    const boxes = ticks.map((t) => labelBox(PAD.left + t.x, t.anchor, MONTHS[t.index] ?? ""));
    expect(ticks[0]?.index).toBe(0);
    expect(ticks.at(-1)?.index).toBe(MONTHS.length - 1);
    for (const [left, right] of boxes) {
      expect(left).toBeGreaterThanOrEqual(0);
      expect(right).toBeLessThanOrEqual(width);
    }
    for (let i = 1; i < boxes.length; i++) {
      const prev = boxes[i - 1];
      const box = boxes[i];
      if (!prev || !box) throw new Error("missing box");
      expect(box[0]).toBeGreaterThan(prev[1]); // no overlap
      const a = ticks[i - 1];
      const b = ticks[i];
      if (!a || !b) throw new Error("missing tick");
      expect(b.x - a.x).toBeGreaterThanOrEqual(MIN_LABEL_SPACING); // one label per 80 px at most
    }
  });

  it("fits more labels at 1440 than at 390", () => {
    const narrow = xTicks(MONTHS.length, WIDTHS["390 px"] - PAD.left - PAD.right).length;
    const wide = xTicks(MONTHS.length, WIDTHS["1440 px"] - PAD.left - PAD.right).length;
    expect(wide).toBeGreaterThan(narrow);
    expect(narrow).toBeGreaterThanOrEqual(2);
  });
});

describe("y ticks", () => {
  it("uses nice steps capped near the maximum", () => {
    expect(yTicks(254)).toEqual([0, 50, 100, 150, 200, 250]);
    expect(yTicks(17)).toEqual([0, 5, 10, 15]);
    expect(yTicks(1)).toEqual([0, 0.2, 0.4, 0.6, 0.8, 1]);
    expect(yTicks(0)).toEqual([0]);
  });
});
