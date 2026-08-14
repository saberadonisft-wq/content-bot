import { describe, expect, it } from "vitest";
import { createSubtitleMask, normalizeSubtitleMask, normalizeSubtitleMasks } from "./masks";

describe("subtitle masks", () => {
  it("uses a percentage-based blur rectangle by default", () => {
    expect(createSubtitleMask()).toMatchObject({
      shape: "rectangle",
      effect: "blur",
      x: 20,
      y: 72,
      width: 60,
      height: 12,
    });
  });

  it("keeps regions inside the frame and makes bands full width", () => {
    expect(normalizeSubtitleMask({
      id: "band-1",
      shape: "band",
      x: 42,
      y: 99,
      width: 4,
      height: 14,
    })).toMatchObject({ x: 0, y: 86, width: 100, height: 14 });
  });

  it("drops invalid items and enforces the mask limit", () => {
    const masks = Array.from({ length: 12 }, (_, index) => ({
      id: `mask-${index}`,
      x: 1,
      y: 2,
      width: 30,
      height: 10,
    }));
    expect(normalizeSubtitleMasks([null, ...masks])).toHaveLength(8);
  });
});
