import { expect, it } from 'vitest';
import fixture from '../../../test-fixtures/voiceover-alignment.json';
import { automaticSync, refreshTiming, windowIssues } from './timing';
import { calculateFitRate, sourceSignature, syncVoiceCues } from './planner';
import { clipSignature, DEFAULT_PROFILE, voiceClipEnd, voiceClipStart, type VoiceClip, type VoiceDocument } from './types';
import type { SubtitleCueV2 } from '../subtitles/types';

const makeClip = (): VoiceClip => ({ ...fixture.clip, status: 'ready',
  sync: { ...automaticSync(), alignment: { ...fixture.alignment } } });
const cue = { ...fixture.cue, timing_source: 'manual', timing_precision_ms: 1, needs_review: false } as SubtitleCueV2;

it('shares the server binding and uses measured speech independently of subtitle display', () => {
  const clip = makeClip();
  expect(clipSignature(clip)).toBe(fixture.alignment.clip_signature);
  expect(sourceSignature(cue)).toBe(fixture.alignment.source_signature);
  expect(voiceClipStart(clip)).toBe(fixture.file_start);
  expect(voiceClipEnd(clip)).toBe(fixture.file_end);
  expect(windowIssues(clip)).toEqual([]);
  expect(refreshTiming([clip])[0].sync?.state).toBe('aligned');
  expect(calculateFitRate(clip)).toBe(1);
});

it('invalidates measured evidence on timing, wording, rate or asset edits', () => {
  for (const change of [{ offset_ms: -100 }, { rate: 1.1 }, { spoken_text: 'Khác' }, { asset_id: 'd'.repeat(64) }]) {
    const updated = refreshTiming([{ ...makeClip(), ...change }])[0];
    expect(updated.sync?.alignment).toBeNull();
    expect(updated.sync?.issues).toContain('source_unverified');
  }
});

it('invalidates source changes even when translated text and display times stay the same', () => {
  const doc: VoiceDocument = { schema_version: 2, revision: 1, project_id: 'a'.repeat(20), video_fingerprint: 'video',
    clips: [makeClip()], profile: DEFAULT_PROFILE, pronunciation: {},
    mix: { enabled: true, muted: false, gain: 1, original_gain: 1, mode: 'mix' } };
  expect(syncVoiceCues(doc, [cue])).toBe(doc);
  const changed = syncVoiceCues(doc, [{ ...cue, source_text: 'Different source' }]);
  expect(changed.clips[0].sync?.alignment).toBeNull();
  expect(changed.clips[0].offset_ms).toBe(-175);
  expect(changed.clips[0].spoken_text).toBe(doc.clips[0].spoken_text);
});
