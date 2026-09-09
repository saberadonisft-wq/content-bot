import { describe, expect, it } from 'vitest';
import type { SubtitleCueV2 } from '../subtitles/types';
import { planVoiceClips, syncVoiceCues, voiceClipsInRange } from './planner';
import { DEFAULT_PROFILE, type VoiceDocument } from './types';

const cue = (id: string, start: number, end: number, text: string): SubtitleCueV2 => ({
  id, start_ms: start, end_ms: end, text, timing_source: 'manual', timing_precision_ms: 1,
  needs_review: false, revision: 1,
});
describe('voice planning', () => {
  it('selects whole overlapping clips using moved audio positions and playback rate', () => {
    const clips = planVoiceClips([cue('a', 0, 1000, 'Một.'), cue('b', 2000, 3000, 'Hai.')]);
    Object.assign(clips[0], { offset_ms: 4000, duration_ms: 2000, rate: 2 });
    expect(voiceClipsInRange(clips, 4500, 6000)).toEqual([clips[0].id]);
    expect(voiceClipsInRange(clips, 3000, 4000)).toEqual([]);
    expect(voiceClipsInRange(clips, 4000, 4000)).toEqual([]);
    expect(voiceClipsInRange(clips, NaN, 6000)).toEqual([]);
  });
  it('joins a sentence across adjacent cues but keeps overlaps separate', () => {
    const planned = planVoiceClips([cue('a', 0, 1000, 'Cô gái'), cue('b', 1100, 2000, 'mở cửa.'), cue('c', 1500, 2500, 'Ai đó nói.')]);
    expect(planned).toHaveLength(2);
    expect(planned[0].source_cue_ids).toEqual(['a', 'b']);
    expect(planned[0].spoken_text).toBe('Cô gái mở cửa.');
  });
  it('preserves pronunciation edits while following subtitle timing', () => {
    const clips = planVoiceClips([cue('a', 0, 1000, 'Alice tới.')]);
    clips[0].spoken_text = 'A lít tới.';
    const doc: VoiceDocument = { schema_version: 1, project_id: 'a'.repeat(20), video_fingerprint: 'v', revision: 1,
      clips, profile: DEFAULT_PROFILE, pronunciation: {}, mix: { enabled: true, muted: false, gain: 1, original_gain: 1, mode: 'mix' } };
    const next = syncVoiceCues(doc, [cue('a', 2000, 4000, 'Alice tới đây.')]);
    expect(next.clips[0].spoken_text).toBe('A lít tới.');
    expect(next.clips[0].start_ms).toBe(2000);
    expect(syncVoiceCues(next, []).clips[0].status).toBe('stale');
    const removed = syncVoiceCues(next, []);
    const restored = syncVoiceCues(removed, [cue('a', 2000, 4000, 'Alice tới đây.')]);
    expect(restored.clips[0].error).toBeNull();
    expect(restored.clips[0].spoken_text).toBe('A lít tới.');
    expect(syncVoiceCues(restored, [cue('a', 2000, 4000, 'Alice tới đây.')])).toBe(restored);
  });
});
