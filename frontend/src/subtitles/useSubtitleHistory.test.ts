import { describe, expect, it } from "vitest";
import type { SubtitleCueV2 } from "./types";
import {
  createSubtitleHistoryState,
  subtitleHistoryReducer,
} from "./useSubtitleHistory";

const cue = (text: string): SubtitleCueV2 => ({
  id: "cue-1",
  start_ms: 0,
  end_ms: 1000,
  text,
  timing_source: "manual",
  timing_precision_ms: 1,
  needs_review: false,
  revision: 0,
});

describe("subtitle history", () => {
  it("groups rapid text edits into one undo step", () => {
    let state = createSubtitleHistoryState([cue("a")]);
    state = subtitleHistoryReducer(state, {
      type: "commit",
      updater: [cue("ab")],
      group: "text:cue-1",
      at: 100,
    });
    state = subtitleHistoryReducer(state, {
      type: "commit",
      updater: [cue("abc")],
      group: "text:cue-1",
      at: 200,
    });
    expect(state.past).toHaveLength(1);
    expect(state.present[0].text).toBe("abc");

    state = subtitleHistoryReducer(state, { type: "undo" });
    expect(state.present[0].text).toBe("a");
    state = subtitleHistoryReducer(state, { type: "redo" });
    expect(state.present[0].text).toBe("abc");
  });

  it("separates different timing operations", () => {
    let state = createSubtitleHistoryState([cue("a")]);
    state = subtitleHistoryReducer(state, {
      type: "commit",
      updater: [cue("b")],
      group: "text:cue-1",
      at: 100,
    });
    state = subtitleHistoryReducer(state, {
      type: "commit",
      updater: [{ ...cue("b"), start_ms: 1 }],
      group: "timing:cue-1",
      at: 120,
    });
    expect(state.past).toHaveLength(2);
  });
});
