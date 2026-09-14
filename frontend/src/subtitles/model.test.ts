import { describe, expect, it } from "vitest";
import {
  cueToLegacySubtitle,
  mergeCueWithNext,
  splitCueById,
  updateCueById,
} from "./model";
import type { SubtitleCueV2 } from "./types";

const source: SubtitleCueV2 = {
  id: "cue-1",
  start_ms: 1,
  end_ms: 1234,
  text: "Một mili-giây",
  timing_source: "gemini_estimate",
  timing_precision_ms: 1000,
  needs_review: false,
  revision: 0,
};

describe("subtitle model migration boundary", () => {
  it('invalidates speech evidence when a cue is manually changed or split', () => {
    const cue: SubtitleCueV2 = { ...source, speech_start_ms: 0, speech_end_ms: 1300,
      speech_evidence: { method: 'asr_observed', audio_identity: 'a'.repeat(64), transcript_sha256: 'b'.repeat(64),
        start_ms: 0, end_ms: 1300, algorithm: 'pilot', transcript_complete: true } };
    expect(updateCueById([cue], cue.id, { start_ms: 100 })[0].speech_evidence).toBeNull();
    expect(splitCueById([cue], cue.id, 600).every(c => c.speech_evidence === null)).toBe(true);
  });
  it("keeps canonical integer ms and derives legacy seconds only for render", () => {
    const legacy = cueToLegacySubtitle(source);
    expect(legacy.start_time).toBe("00:00:00,001");
    expect(legacy.end_time).toBe("00:00:01,234");
    expect(legacy.start_seconds).toBe(0.001);
  });

  it("marks manual edits and increments revision", () => {
    const [updated] = updateCueById([source], source.id, { start_ms: 2 });
    expect(updated.start_ms).toBe(2);
    expect(updated.timing_source).toBe("manual");
    expect(updated.timing_precision_ms).toBe(1);
    expect(updated.revision).toBe(1);
  });

  it("splits at an exact millisecond without creating a gap", () => {
    const [first, second] = splitCueById(
      [{ ...source, start_ms: 1000, end_ms: 3000, text: "Xin chào mọi người" }],
      source.id,
      2123,
    );
    expect(first.end_ms).toBe(2123);
    expect(second.start_ms).toBe(2123);
    expect(`${first.text} ${second.text}`).toBe("Xin chào mọi người");
    expect(second.id).not.toBe(first.id);
    expect(first.timing_source).toBe("manual");
  });

  it("merges the next cue while preserving the outer timing", () => {
    const merged = mergeCueWithNext(
      [
        { ...source, start_ms: 100, end_ms: 500, text: "Xin chào" },
        { ...source, id: "cue-2", start_ms: 700, end_ms: 1234, text: "mọi người" },
      ],
      source.id,
    );
    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      id: source.id,
      start_ms: 100,
      end_ms: 1234,
      text: "Xin chào mọi người",
      timing_source: "manual",
    });
  });
});
