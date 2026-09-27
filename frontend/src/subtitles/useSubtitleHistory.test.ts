import { describe, expect, it } from "vitest";
import type { SubtitleCueV2 } from "./types";
import { createSubtitleDocument } from './studioConfig';
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
    let state = createSubtitleHistoryState(createSubtitleDocument([cue("a")]));
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
    expect(state.present.segments[0].text).toBe("abc");

    state = subtitleHistoryReducer(state, { type: "undo" });
    expect(state.present.segments[0].text).toBe("a");
    state = subtitleHistoryReducer(state, { type: "redo" });
    expect(state.present.segments[0].text).toBe("abc");
  });

  it("separates different timing operations", () => {
    let state = createSubtitleHistoryState(createSubtitleDocument([cue("a")]));
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

  it('undoes and redoes translated versions together with their source/model provenance', () => {
    const first = { ...createSubtitleDocument([cue('First translation')]), revision: 8,
      media_fingerprint: 'media-first',
      document_role: 'translation' as const, source_revision: 2, source_run_id: 'source-a',
      run_id: 'translation-a', translation_models: ['model-a'] };
    const second = { ...first, revision: 1, source_revision: 5, source_run_id: 'source-b',
      media_fingerprint: 'media-second',
      run_id: 'translation-b', translation_models: ['model-b'], segments: [cue('Second translation')] };
    let state = subtitleHistoryReducer(createSubtitleHistoryState(first), { type: 'apply', document: second });
    expect(state.present.revision).toBe(9);
    state = subtitleHistoryReducer(state, { type: 'undo' });
    expect(state.present).toEqual({ ...first, revision: 10 });
    state = subtitleHistoryReducer(state, { type: 'redo' });
    expect(state.present).toEqual({ ...second, revision: 11 });
    expect(first.revision).toBe(8);
    expect(second.revision).toBe(1);
  });

  it('treats applying metadata with identical cues as an undoable operation', () => {
    const first = createSubtitleDocument([cue('Same text')]);
    const second = { ...first, language: 'en', run_id: 'imported' };
    const applied = subtitleHistoryReducer(createSubtitleHistoryState(first), { type: 'apply', document: second });
    expect(applied.present.language).toBe('en');
    expect(subtitleHistoryReducer(applied, { type: 'undo' }).present.language).toBe('vi');
  });

  it('keeps provenance during grouped edits, but clears it when importing an unrelated document', () => {
    const translated = { ...createSubtitleDocument([cue('Translation')]), language: 'fr',
      media_fingerprint: 'media-source',
      source_revision: 4, source_run_id: 'source', translation_models: ['model'] };
    let state = createSubtitleHistoryState(translated);
    state = subtitleHistoryReducer(state, { type: 'commit', group: 'text:c1', at: 1, updater: [cue('Edited')] });
    expect(state.present).toMatchObject({ language: 'fr', source_revision: 4, source_run_id: 'source', translation_models: ['model'] });
    expect(state.present.media_fingerprint).toBe('media-source');
    state = subtitleHistoryReducer(state, { type: 'apply', document: createSubtitleDocument([cue('Imported')]) });
    expect(state.present.source_revision).toBeUndefined();
    expect(state.present.media_fingerprint).toBeUndefined();
    expect(state.present.translation_models).toBeUndefined();
    expect(subtitleHistoryReducer(state, { type: 'undo' }).present.source_revision).toBe(4);
  });

  it('does not advance revision for a no-op and cannot undo into another video after reset', () => {
    const initial = createSubtitleHistoryState(createSubtitleDocument([cue('Original')]));
    expect(subtitleHistoryReducer(initial, { type: 'undo' })).toBe(initial);
    expect(subtitleHistoryReducer(initial, { type: 'commit', updater: current => current as SubtitleCueV2[], group: null, at: 1 })).toBe(initial);
    const changed = subtitleHistoryReducer(initial, { type: 'apply', document: createSubtitleDocument([cue('Changed')]) });
    const reset = subtitleHistoryReducer(changed, { type: 'reset', document: createSubtitleDocument([]) });
    expect(subtitleHistoryReducer(reset, { type: 'undo' })).toBe(reset);
    expect(reset.present.segments).toEqual([]);
  });
});
