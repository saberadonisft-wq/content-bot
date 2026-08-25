import { describe, expect, it } from "vitest";
import {
  createInitialVideoClip,
  deletedVideoRanges,
  findVideoClipAt,
  keptVideoDurationMs,
  normalizeVideoClips,
  nearestKeptVideoTimeMs,
  splitVideoClip,
} from "./video-clips";

describe("video clips", () => {
  it("splits a source clip at the playhead", () => {
    const clips = splitVideoClip(createInitialVideoClip(10_000), "video-1", 4_250);
    expect(clips).toEqual([
      { id: "video-1", start_ms: 0, end_ms: 4_250 },
      { id: "video-1-4250", start_ms: 4_250, end_ms: 10_000 },
    ]);
    expect(keptVideoDurationMs(clips)).toBe(10_000);
  });

  it("does not create an unusably short clip", () => {
    const clips = createInitialVideoClip(10_000);
    expect(splitVideoClip(clips, "video-1", 20)).toEqual(clips);
  });

  it("normalizes saved clips and finds the clip under the playhead", () => {
    const clips = normalizeVideoClips([
      { id: "a", start_ms: 0, end_ms: 2_000 },
      { id: "b", start_ms: 4_000, end_ms: 20_000 },
    ], 10_000);
    expect(findVideoClipAt(clips, 4_500)?.id).toBe("b");
    expect(findVideoClipAt(clips, 3_000)).toBeNull();
    expect(clips[1].end_ms).toBe(10_000);
  });

  it("marks deleted ranges and prevents seeking into them", () => {
    const clips = [
      { id: "a", start_ms: 0, end_ms: 3_000 },
      { id: "b", start_ms: 6_000, end_ms: 9_000 },
    ];
    expect(deletedVideoRanges(clips, 10_000)).toEqual([
      { start_ms: 3_000, end_ms: 6_000 },
      { start_ms: 9_000, end_ms: 10_000 },
    ]);
    expect(nearestKeptVideoTimeMs(clips, 4_000)).toBe(2_999);
    expect(nearestKeptVideoTimeMs(clips, 5_500)).toBe(6_000);
    expect(nearestKeptVideoTimeMs(clips, 9_500)).toBe(8_999);
  });
});
