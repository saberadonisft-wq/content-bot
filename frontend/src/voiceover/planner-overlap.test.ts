import { expect, it } from 'vitest';
import fixtures from '../../../test-fixtures/voiceover-timing.json';
import type { VoiceClip } from './types';
import { voiceClipEnd } from './types';
import { autoFitVoiceClips, calculateFitRate, countVoiceOverlaps, resetVoiceOffsets, smartResolveVoiceOverlaps } from './planner';
import { automaticSync, legacySync, refreshTiming, windowIssues } from './timing';

const clip = (id: string, start = 0, end = 1000, duration = 1000): VoiceClip => ({
  id, source_cue_ids: [id], source_text: 'Lời đọc', spoken_text: 'Lời đọc', start_ms: start, end_ms: end,
  duration_ms: duration, offset_ms: 0, rate: 1, gain: 1, asset_id: id, generation_hash: 'hash',
  status: 'ready', error: null, sync: automaticSync(),
});

it('matches shared timing fixtures including positive and negative offsets', () => {
  for (const test of fixtures.cases) {
    const value = { ...clip(test.id), ...test };
    expect(voiceClipEnd(value)).toBeCloseTo(test.expected_end, 6);
    expect(windowIssues(value)).toEqual(test.issues);
    expect(refreshTiming([value])[0].sync?.state).not.toBe('aligned');
  }
});

it('counts non-adjacent nested overlaps and marks all involved clips', () => {
  const clips: VoiceClip[] = fixtures.nested.map((row: Partial<VoiceClip> & { id: string }) => ({ ...clip(row.id), ...row }));
  expect(countVoiceOverlaps(clips).overlapCount).toBe(fixtures.nested_overlap_intervals);
  expect(refreshTiming(clips).every(c => c.sync?.issues.includes('overlap'))).toBe(true);
});

it('fits a small overflow using remaining time without resetting offset', () => {
  const original = { ...clip('small', 0, 1000, 1000), offset_ms: 100 };
  const [fitted] = autoFitVoiceClips([original]);
  expect(fitted.rate).toBe(1.12);
  expect(fitted.offset_ms).toBe(100);
  expect(fitted.status).toBe('ready');
  expect(original.rate).toBe(1);
});

it('keeps an impossible fit unresolved and never pushes the following line', () => {
  const clips = [clip('first', 0, 1000, 1500), clip('next', 1000, 2000, 800)];
  const result = smartResolveVoiceOverlaps(clips);
  expect(result[0].rate).toBe(1.15);
  expect(result[0].status).toBe('overflow');
  expect(result[1].offset_ms).toBe(0);
  expect(result[1].start_ms).toBe(1000);
  expect(smartResolveVoiceOverlaps(result)).toEqual(result);
});

it('preserves legacy and manual settings and avoids stretching natural short lines', () => {
  for (const sync of [undefined, legacySync(), { ...automaticSync(), timing_origin: 'manual' as const, timing_locked: true }]) {
    const value = { ...clip('protected', 0, 1000, 1500), offset_ms: 157, sync };
    const [result] = autoFitVoiceClips([value]);
    expect([result.rate, result.offset_ms, result.spoken_text]).toEqual([value.rate, 157, value.spoken_text]);
  }
  expect(calculateFitRate(clip('short', 0, 1000, 300))).toBe(1);
  expect(autoFitVoiceClips([clip('short', 0, 1000, 300)])[0].rate).toBe(1);
  const fast = { ...clip('fast', 0, 1000, 3000), rate: 1.85 };
  expect(autoFitVoiceClips([fast])[0].rate).toBe(1.85);
});

it('does not count missing audio as actual overlap; explicit reset remains separate', () => {
  const missing = { ...clip('missing', 0, 10000, 0), asset_id: null, status: 'missing' as const };
  expect(countVoiceOverlaps([missing, clip('ready', 1000, 2000)])).toEqual({ overflowCount: 0, overlapCount: 0 });
  expect(resetVoiceOffsets([{ ...clip('manual'), offset_ms: 250 }])[0].offset_ms).toBe(0);
});
