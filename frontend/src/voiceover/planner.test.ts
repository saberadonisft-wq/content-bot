import { describe, expect, it } from 'vitest';
import type { SubtitleCueV2 } from '../subtitles/types';
import { planVoiceClips, splitGroupedVoiceClips, syncVoiceCues, voiceClipsInRange } from './planner';
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
  it('keeps adjacent sentence fragments and overlapping cues at their own timestamps', () => {
    const planned = planVoiceClips([cue('a', 0, 1000, 'Cô gái'), cue('b', 1100, 2000, 'mở cửa.'), cue('c', 1500, 2500, 'Ai đó nói.')]);
    expect(planned.map(c => [c.source_cue_ids, c.spoken_text, c.start_ms, c.end_ms])).toEqual([
      [['a'], 'Cô gái', 0, 1000], [['b'], 'mở cửa.', 1100, 2000], [['c'], 'Ai đó nói.', 1500, 2500],
    ]);
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

describe('repairing grouped narration', () => {
  const cues = [cue('a', 0, 3100, 'Nửa đêm ba canh,'), cue('b', 3200, 6000, 'cô nương vì sao lại ở đây?'),
    cue('c', 8800, 12500, 'Nửa đêm ba canh, cô nương vì sao lại ở đây?')];
  function document(): VoiceDocument {
    const clips = planVoiceClips(cues);
    const text = `${cues[0].text} ${cues[1].text}`;
    const grouped = { ...clips[0], source_cue_ids: ['a', 'b'], source_text: text, spoken_text: text,
      end_ms: 6000, offset_ms: 100, asset_id: 'old-audio', generation_hash: 'old-hash', duration_ms: 2800,
      status: 'ready' as const, gain: 0.8 };
    return { schema_version: 1, project_id: 'a'.repeat(20), video_fingerprint: 'v', revision: 7,
      clips: [grouped, clips[2]], profile: DEFAULT_PROFILE, pronunciation: { cô: 'cô' },
      mix: { enabled: true, muted: false, gain: 1, original_gain: 1, mode: 'mix' } };
  }
  it('replaces grouped audio with separate cues while retaining unaffected clips and settings', () => {
    const original = document();
    const { document: repaired, skipped } = splitGroupedVoiceClips(original, cues);
    expect(skipped).toBe(0);
    expect(repaired.clips).toHaveLength(3);
    expect(repaired.clips[1]).toMatchObject({ source_cue_ids: ['b'], start_ms: 3200, end_ms: 6000,
      spoken_text: cues[1].text, offset_ms: 0, rate: 1.08, gain: 0.8, status: 'missing',
      asset_id: null, generation_hash: null, duration_ms: 0 });
    expect(repaired.clips[0].id).not.toBe(original.clips[0].id);
    expect(repaired.clips[2]).toBe(original.clips[1]);
    expect(repaired.mix).toBe(original.mix);
    expect(repaired.profile).toBe(original.profile);
    expect(repaired.pronunciation).toBe(original.pronunciation);
    expect(repaired.revision).toBe(original.revision);
    expect(original.clips[0].asset_id).toBe('old-audio');
    expect(splitGroupedVoiceClips(repaired, cues).document).toBe(repaired);
  });
  it('keeps customized narration intact for review', () => {
    const original = document();
    original.clips[0].spoken_text = 'Lời đọc được sửa riêng.';
    expect(splitGroupedVoiceClips(original, cues)).toEqual({ document: original, skipped: 1 });
  });
  it('does not discard narration with missing or empty source cues', () => {
    const original = document();
    expect(splitGroupedVoiceClips(original, cues.slice(1))).toEqual({ document: original, skipped: 1 });
    expect(splitGroupedVoiceClips(original, cues.map(c => c.id === 'b' ? { ...c, text: ' ' } : c)))
      .toEqual({ document: original, skipped: 1 });
  });
  it('does not create duplicate narration for cues already covered by another clip', () => {
    const original = document();
    original.clips.push(planVoiceClips(cues)[1]);
    expect(splitGroupedVoiceClips(original, cues)).toEqual({ document: original, skipped: 1 });
  });
});
