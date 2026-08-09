import { describe, expect, it } from "vitest";
import {
  DEFAULT_OVERLAY_LAYOUT,
  fitOverlayLayout,
  normalizeOverlayLayout,
} from "./overlay";

describe("overlay layout", () => {
  it("normalizes invalid persisted values and keeps a usable size", () => {
    expect(normalizeOverlayLayout({ x: -5, y: 130, width: 200 })).toEqual({
      x: 0,
      y: 100,
      width: 90,
    });
    expect(normalizeOverlayLayout({ x: Number.NaN })).toEqual(DEFAULT_OVERLAY_LAYOUT);
  });

  it("keeps a landscape image fully inside the video frame", () => {
    expect(
      fitOverlayLayout(
        { x: 3, y: 2, width: 40 },
        { frameWidth: 960, frameHeight: 540, imageWidth: 1600, imageHeight: 900 },
      ),
    ).toEqual({ x: 20, y: 20, width: 40 });
  });

  it("limits tall images by height while preserving their aspect ratio", () => {
    expect(
      fitOverlayLayout(
        { x: 50, y: 50, width: 90 },
        { frameWidth: 1600, frameHeight: 900, imageWidth: 500, imageHeight: 1000 },
      ),
    ).toEqual({ x: 50, y: 50, width: 25.3 });
  });
});
