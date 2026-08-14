import { describe, expect, it } from "vitest";
import { previewSubtitleMetrics } from "./preview-scale";

describe("previewSubtitleMetrics", () => {
  it.each([180, 304, 720, 1080])(
    "keeps every metric proportional at a %dpx frame height",
    (frameHeight) => {
      const metrics = previewSubtitleMetrics(frameHeight, 14, 2, 1);
      const scale = frameHeight / 720;

      expect(metrics.scale).toBeCloseTo(scale);
      expect(metrics.fontSize).toBeCloseTo(14 * scale);
      expect(metrics.outlineWidth).toBeCloseTo(2 * scale);
      expect(metrics.shadowWidth).toBeCloseTo(scale);
      expect(metrics.paddingX).toBeCloseTo(12 * scale);
      expect(metrics.paddingY).toBeCloseTo(4 * scale);
    },
  );

  it("does not clamp a 14px design font to 10px in the small preview", () => {
    expect(previewSubtitleMetrics(304, 14, 2, 1).fontSize).toBeCloseTo(5.9111, 3);
  });
});
