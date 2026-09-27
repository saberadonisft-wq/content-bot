import { describe, expect, it } from "vitest";
import {
  clampOcrRegion,
  DEFAULT_OCR_REGION,
  fitOcrContentBox,
  MIN_OCR_DIMENSION,
} from "./ocr-region";

describe("OCR region utilities", () => {
  it("provides sensible default coordinates targeting lower third", () => {
    expect(DEFAULT_OCR_REGION).toMatchObject({
      x: 10,
      y: 72,
      width: 80,
      height: 18,
    });
    expect(DEFAULT_OCR_REGION.x + DEFAULT_OCR_REGION.width).toBeLessThanOrEqual(100);
    expect(DEFAULT_OCR_REGION.y + DEFAULT_OCR_REGION.height).toBeLessThanOrEqual(100);
  });

  it("clamps coordinates within 0..100 boundary", () => {
    // Clamping over the right/bottom edge
    const overRight = clampOcrRegion({ x: 95, y: 90, width: 20, height: 20 });
    expect(overRight.width).toBe(20);
    expect(overRight.height).toBe(20);
    expect(overRight.x).toBe(80); // 100 - 20
    expect(overRight.y).toBe(80); // 100 - 20

    // Clamping negative coordinates
    const negative = clampOcrRegion({ x: -10, y: -5, width: 50, height: 20 });
    expect(negative.x).toBe(0);
    expect(negative.y).toBe(0);
    expect(negative.width).toBe(50);
    expect(negative.height).toBe(20);
  });

  it("enforces minimum dimensions", () => {
    const tooSmall = clampOcrRegion({ x: 10, y: 10, width: 0.5, height: 0.2 });
    expect(tooSmall.width).toBe(MIN_OCR_DIMENSION);
    expect(tooSmall.height).toBe(MIN_OCR_DIMENSION);
  });

  it("handles missing, non-finite, and NaN coordinates safely", () => {
    const fallback = clampOcrRegion({
      x: NaN,
      y: undefined,
      width: Infinity,
      height: -5,
    });
    expect(fallback.x).toBe(DEFAULT_OCR_REGION.x);
    expect(fallback.y).toBe(DEFAULT_OCR_REGION.y);
    expect(fallback.width).toBe(DEFAULT_OCR_REGION.width);
    expect(fallback.height).toBe(MIN_OCR_DIMENSION);
  });

  it("rounds values to 2 decimal places", () => {
    const rounded = clampOcrRegion({ x: 10.33333, y: 20.66666, width: 50.1234, height: 15.9876 });
    expect(rounded.x).toBe(10.33);
    expect(rounded.y).toBe(20.67);
    expect(rounded.width).toBe(50.12);
    expect(rounded.height).toBe(15.99);
  });

  it("maps OCR percentages to the actual image inside a letterboxed frame", () => {
    expect(fitOcrContentBox(1000, 1000, 1920, 1080)).toEqual({
      left: 0,
      top: 218.75,
      width: 1000,
      height: 562.5,
    });
    expect(fitOcrContentBox(1000, 500, 1080, 1920)).toEqual({
      left: 359.375,
      top: 0,
      width: 281.25,
      height: 500,
    });
  });
});
