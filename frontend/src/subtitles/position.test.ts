import { describe, expect, it } from "vitest";
import { applyCuePosition } from "./position";
import { normalizeSavedDraft } from "./draft";
import { splitCueById } from "./model";
import type { SubtitleBurnOptions } from "../api";
import type { SubtitleCueV2 } from "./types";

const cues: SubtitleCueV2[] = [0, 1].map((index) => ({
  id: `cue-${index}`, start_ms: index * 2000, end_ms: (index + 1) * 2000,
  text: "Một dòng phụ đề", timing_source: "forced_alignment", timing_precision_ms: 1,
  needs_review: false, revision: 0,
}));

describe("subtitle position scope", () => {
  it("edits one cue, applies its position to all, then allows independent edits", () => {
    const first = applyCuePosition(cues, "cue-0", { x: 20, y: 30 }, false);
    expect(first[1]).toBe(cues[1]);
    expect(first[0].timing_source).toBe("forced_alignment");
    const all = applyCuePosition(first, "cue-0", first[0].layout!, true);
    expect(all.map((cue) => cue.layout)).toEqual([{ x: 20, y: 30 }, { x: 20, y: 30 }]);
    const separate = applyCuePosition(all, "cue-1", { x: 70, y: 80 }, false);
    expect(separate[0].layout).toEqual({ x: 20, y: 30 });
    expect(separate[1].layout).toEqual({ x: 70, y: 80 });
    expect(cues.every((cue) => !cue.layout)).toBe(true);
  });
  it("preserves positions through draft reload and cue splitting", () => {
    const edited = applyCuePosition(cues, "cue-0", { x: 12.5, y: 68 }, false);
    const draft = normalizeSavedDraft(JSON.parse(JSON.stringify({ version: 2, cues: edited })), {} as SubtitleBurnOptions)!;
    expect(draft.cues[0].layout).toEqual(edited[0].layout);
    const split = splitCueById(draft.cues, "cue-0", 1000);
    expect(split.slice(0, 2).map((cue) => cue.layout)).toEqual([edited[0].layout, edited[0].layout]);
    expect(draft.cues[1].layout).toBeUndefined();
  });
});
