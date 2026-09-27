import { describe, expect, it } from "vitest";
import type { SubtitleBurnOptions } from "../api";
import { normalizeSavedDraft } from "./draft";

const options = {
  font_name: "Arimo",
  font_size: 38,
  animation: "none",
  trim_end: null,
} as SubtitleBurnOptions;

describe("subtitle draft migration", () => {
  it('preserves the source revision and actual translation models across reload', () => {
    const documentMeta = { revision: 12, run_id: 'translation-run', document_role: 'translation',
      media_fingerprint: 'source-media',
      source_revision: 3, source_run_id: 'source-run', translation_models: ['gemini-3.7-flash'] };
    const draft = normalizeSavedDraft({ documentMeta, translatedSourceRevision: 3,
      cues: [{ id: 'c1', start_ms: 0, end_ms: 1000, text: 'Xin chào', source_text: 'Hello' }] }, options);
    expect(draft?.documentMeta).toEqual(documentMeta);
    expect(draft?.translatedSourceRevision).toBe(3);
  });
  it('preserves independent speech bounds and their provenance across restart', () => {
    const evidence = { method: 'asr_observed', audio_identity: 'a'.repeat(64), transcript_sha256: 'b'.repeat(64),
      start_ms: 800, end_ms: 2100, algorithm: 'pilot', transcript_complete: true };
    const draft = normalizeSavedDraft({ cues: [{ id: 'a', start_ms: 1000, end_ms: 2000, text: 'Xin chào',
      speech_start_ms: 800, speech_end_ms: 2100, speech_evidence: evidence,
      words: [{ id: 'w', text: 'Hello', start_ms: 800, end_ms: 1200, alignment_method: 'asr_observed' }],
    }] }, options);
    expect(draft?.cues[0].speech_evidence).toEqual(evidence);
    expect(draft?.cues[0].words?.[0]).toMatchObject({ start_ms: 800, alignment_method: 'asr_observed' });
    const broken = normalizeSavedDraft({ cues: [{ ...draft!.cues[0], speech_end_ms: null }] }, options);
    expect(broken?.cues[0].speech_start_ms).toBeNull();
    expect(broken?.cues[0].speech_evidence).toBeNull();
    expect(broken?.cues[0].words).toEqual([]);
  });
  it("keeps source transcript, locks, timing and revision across restart", () => {
    const draft = normalizeSavedDraft({ documentMeta: { revision: 4, run_id: "run1" }, cues: [{
      id: "c1", start_ms: 1000, end_ms: 2000, text: "Xin chào", source_text: "你好", source_language: "zh",
      origin_chunk_id: "chunk1", origin_model: "gemini-3.6-flash", locked: true, content_source: "audio",
      speech_start_ms: 1100, speech_end_ms: 1900,
      words: [{ id: "word1", text: "你好", start_ms: 1100, end_ms: 1900 }],
    }] }, options);
    expect(draft?.documentMeta).toEqual({ revision: 4, run_id: "run1" });
    expect(draft?.cues[0]).toMatchObject({ source_text: "你好", source_language: "zh", locked: true, origin_chunk_id: "chunk1", speech_start_ms: 1100 });
    expect(draft?.cues[0].words).toHaveLength(1);
  });
  it("migrates legacy seconds while dropping corrupt cues and duplicate IDs", () => {
    const draft = normalizeSavedDraft(
      {
        subtitles: [
          { id: "same", start_seconds: 1.001, end_seconds: 2.501, text: "Một" },
          { id: "same", start_seconds: 3, end_seconds: 4, text: "Hai" },
          { id: "broken", start_ms: 10, end_ms: 9, text: "Hỏng" },
        ],
        selectedCueId: "broken",
        options: { font_size: Number.POSITIVE_INFINITY, animation: "fade" },
        overlayId: "a".repeat(64),
        overlayName: "logo.png",
        overlayLayout: { x: -20, y: 47.25, width: 120 },
        subtitleMasks: [{
          id: "mask-one",
          shape: "ellipse",
          effect: "pixelate",
          x: 85,
          y: 96,
          width: 30,
          height: 20,
          strength: 18,
          opacity: 2,
          feather: -1,
          cornerRadius: 90,
          color: "#aabbcc",
        }],
      },
      options,
    );

    expect(draft?.cues).toHaveLength(2);
    expect(draft?.cues[0]).toMatchObject({ start_ms: 1001, end_ms: 2501 });
    expect(draft?.cues[1].id).toBe("same-2");
    expect(draft?.selectedCueId).toBeNull();
    expect(draft?.options.font_size).toBe(38);
    expect(draft?.options.animation).toBe("fade");
    expect(draft?.overlayId).toBe("a".repeat(64));
    expect(draft?.overlayName).toBe("logo.png");
    expect(draft?.overlayLayout).toEqual({ x: 0, y: 47.3, width: 90 });
    expect(draft?.subtitleMasks).toEqual([expect.objectContaining({
      id: "mask-one",
      shape: "ellipse",
      effect: "pixelate",
      x: 70,
      y: 80,
      width: 30,
      height: 20,
      opacity: 1,
      feather: 0,
      cornerRadius: 50,
      color: "#AABBCC",
    })]);
  });

  it("rejects values that are not draft objects", () => {
    expect(normalizeSavedDraft(null, options)).toBeNull();
    expect(normalizeSavedDraft({ version: 2 }, options)).toBeNull();
  });

  it("drops an unsafe overlay id while keeping the normalized layout", () => {
    const draft = normalizeSavedDraft(
      {
        cues: [{ id: "one", start_ms: 0, end_ms: 1000, text: "Một" }],
        overlayId: "../../logo.png",
        overlayName: "  logo.webp  ",
        overlayLayout: { x: 14, y: 12, width: 22 },
      },
      options,
    );

    expect(draft?.overlayId).toBeNull();
    expect(draft?.overlayName).toBe("logo.webp");
    expect(draft?.subtitleMasks).toEqual([]);
  });
});
