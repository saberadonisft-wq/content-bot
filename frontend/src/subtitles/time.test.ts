import { describe, expect, it } from "vitest";
import {
  SubtitleIntervalIndex,
  findActiveCue,
  findOverlapCueIds,
  formatTimecode,
  keyboardTargetMs,
  keyboardStepMs,
  parseTimecode,
  snapMsToFrame,
  sortCues,
} from "./time";
import type { SubtitleCueV2 } from "./types";
import type { FrameTiming } from "./types";

const cue = (id: string, start_ms: number, end_ms: number): SubtitleCueV2 => ({
  id,
  start_ms,
  end_ms,
  text: id,
  timing_source: "manual",
  timing_precision_ms: 1,
  needs_review: false,
  revision: 0,
});

describe("subtitle millisecond timebase", () => {
  it("round-trips one millisecond without float conversion", () => {
    expect(formatTimecode(1)).toBe("00:00:00.001");
    expect(parseTimecode("00:00:00.001")).toBe(1);
    expect(parseTimecode("62:03.456")).toBe(3_723_456);
    expect(parseTimecode("00:00:60.000")).toBeNull();
  });

  it("uses exact keyboard increments and frame snapping", () => {
    const base = { altKey: false, ctrlKey: false, metaKey: false, shiftKey: false };
    expect(keyboardStepMs(base, 30)).toBe(1);
    expect(keyboardStepMs({ ...base, shiftKey: true }, 30)).toBe(10);
    expect(keyboardStepMs({ ...base, ctrlKey: true }, 30)).toBe(100);
    expect(keyboardStepMs({ ...base, altKey: true }, 25)).toBe(40);
    expect(snapMsToFrame(41, 25)).toBe(40);
  });

  it("snaps and steps against real VFR presentation timestamps", () => {
    const timing: FrameTiming = {
      frame_rate_numerator: 30,
      frame_rate_denominator: 1,
      is_vfr: true,
      frame_pts_ms: [0, 33, 67, 117, 150],
    };
    const alt = { altKey: true, ctrlKey: false, metaKey: false, shiftKey: false };

    expect(snapMsToFrame(91, timing)).toBe(67);
    expect(snapMsToFrame(94, timing)).toBe(117);
    expect(keyboardTargetMs(67, 1, alt, timing)).toBe(117);
    expect(keyboardTargetMs(100, -1, alt, timing)).toBe(67);
  });

  it("keeps exact NTSC rational cadence instead of decimal FPS drift", () => {
    const ntsc: FrameTiming = {
      frame_rate_numerator: 30_000,
      frame_rate_denominator: 1001,
      is_vfr: false,
      frame_pts_ms: [],
    };
    expect(snapMsToFrame(1001, ntsc)).toBe(1001);
    expect(snapMsToFrame(10_010, ntsc)).toBe(10_010);
  });
});

describe("subtitle interval index", () => {
  const cues = sortCues([
    cue("later", 2000, 3000),
    cue("first", 0, 1000),
    cue("overlap-long", 500, 2500),
  ]);
  const index = new SubtitleIntervalIndex(cues, true);

  it("uses half-open intervals and leaves real gaps empty", () => {
    expect(findActiveCue([cue("first", 0, 1000)], 999)?.id).toBe("first");
    expect(findActiveCue([cue("first", 0, 1000)], 1000)).toBeNull();
    expect(findActiveCue([cue("first", 0, 1000)], 1500)).toBeNull();
  });

  it("finds an earlier long overlap when the latest-start cue ended", () => {
    expect(index.findActive(1500)?.id).toBe("overlap-long");
    expect(index.findActive(2600)?.id).toBe("later");
  });

  it("virtualizes by time range without dropping long overlaps", () => {
    expect(index.inRange(1200, 1800).map((item) => item.id)).toEqual([
      "overlap-long",
    ]);
  });
});

describe("findOverlapCueIds", () => {
  it("keeps detecting overlaps nested inside a long cue", () => {
    const ids = findOverlapCueIds([
      cue("long", 0, 10_000),
      cue("short", 1000, 2000),
      cue("nested", 3000, 4000),
    ]);

    expect([...ids]).toEqual(["long", "short", "nested"]);
  });
});
