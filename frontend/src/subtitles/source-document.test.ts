import { describe, expect, it } from 'vitest';
import { createSourceState, sourceDocumentReducer, sourceFromDocument } from './source-document';
import { normalizeSavedDraft } from './draft';
import type { SubtitleDocumentV2 } from './types';
import { DEFAULT_OPTIONS } from './studioConfig';

const source: SubtitleDocumentV2 = {
  schema_version: 2, language: 'zh', revision: 0, run_id: 'ocr-fixture',
  media_fingerprint: 'source-media',
  timing_source: 'ocr', timing_precision_ms: 200, timebase: 'milliseconds',
  segments: [{ id: 'c1', start_ms: 0, end_ms: 1000, text: 'wrong OCR', source_text: 'wrong OCR',
    source_language: 'zh', timing_source: 'ocr', timing_precision_ms: 200, needs_review: false, revision: 0 }],
};

describe('source document ownership', () => {
  it('edits the actual source consumed by translation without touching a translated document', () => {
    const translated = { ...source, language: 'vi', segments: [{ ...source.segments[0], text: 'Vietnamese' }] };
    const initial = createSourceState(sourceFromDocument(translated));
    const edited = sourceDocumentReducer(initial, { type: 'edit', id: 'c1', patch: { text: 'correct source' } });
    expect(edited.document?.segments[0]).toMatchObject({ text: 'correct source', source_text: 'correct source' });
    expect(edited.document?.media_fingerprint).toBe('source-media');
    expect(translated.segments[0].text).toBe('Vietnamese');
    const undone = sourceDocumentReducer(edited, { type: 'undo' });
    expect(undone.document?.segments[0].source_text).toBe('wrong OCR');
    expect(undone.document?.media_fingerprint).toBe('source-media');
    expect(undone.document!.revision).toBeGreaterThan(edited.document!.revision!);
    expect(sourceDocumentReducer(undone, { type: 'redo' }).document?.segments[0].source_text).toBe('correct source');
  });

  it('keeps locked source cues unchanged', () => {
    const locked = { ...source, segments: [{ ...source.segments[0], locked: true }] };
    const state = createSourceState(locked);
    expect(sourceDocumentReducer(state, { type: 'edit', id: 'c1', patch: { text: 'oops' } })).toBe(state);
  });

  it('persists source language, extraction settings and OCR provenance across reload', () => {
    const draft = normalizeSavedDraft({ version: 2, videoId: 'a'.repeat(16), cues: source.segments,
      sourceDocument: source, extractionSettings: { mode: 'ocr', region: { x: 10, y: 60, width: 80, height: 20 },
        ocrLanguage: 'zh', sampleFps: 4, asrLanguage: 'auto', asrModel: 'small', bilingual: true, model: '' },
    }, DEFAULT_OPTIONS);
    expect(draft?.sourceDocument?.language).toBe('zh');
    expect(draft?.sourceDocument?.media_fingerprint).toBe('source-media');
    expect(draft?.sourceDocument?.segments[0].timing_source).toBe('ocr');
    expect(draft?.extractionSettings?.region.y).toBe(60);
    expect(draft?.extractionSettings?.sampleFps).toBe(4);
  });
});
