import { describe, expect, it } from 'vitest';
import type { VoiceClip } from './types';
import {
  autoFitVoiceClips,
  calculateFitRate,
  countVoiceOverlaps,
  resetVoiceOffsets,
  rippleShiftVoiceClips,
  smartResolveVoiceOverlaps,
} from './planner';

const makeClip = (id: string, start: number, end: number, duration: number, rate = 1.0, status: VoiceClip['status'] = 'ready'): VoiceClip => ({
  id,
  source_cue_ids: [id],
  source_text: 'Text',
  spoken_text: 'Text',
  start_ms: start,
  end_ms: end,
  offset_ms: 0,
  rate,
  gain: 1,
  asset_id: `asset-${id}`,
  generation_hash: 'hash',
  duration_ms: duration,
  status,
  error: null,
});

describe('calculateFitRate', () => {
  it('returns current rate if duration is 0 or window is invalid', () => {
    const clip = makeClip('1', 0, 1000, 0, 1.2);
    expect(calculateFitRate(clip)).toBe(1.2);
  });

  it('calculates needed rate to fit duration in cue window and clamps between 0.5 and 2.0', () => {
    // 2000ms duration in 1000ms window -> needed = 2.0x
    const clip1 = makeClip('1', 0, 1000, 2000, 1.0);
    expect(calculateFitRate(clip1)).toBe(2.0);

    // 1500ms duration in 1000ms window -> needed = 1.5x
    const clip2 = makeClip('2', 0, 1000, 1500, 1.0);
    expect(calculateFitRate(clip2)).toBe(1.5);

    // 4000ms duration in 1000ms window -> needed = 4.0x -> clamped to 2.0
    const clip3 = makeClip('3', 0, 1000, 4000, 1.0);
    expect(calculateFitRate(clip3)).toBe(2.0);

    // 300ms duration in 1000ms window -> needed = 0.3x -> clamped to 0.5
    const clip4 = makeClip('4', 0, 1000, 300, 1.0);
    expect(calculateFitRate(clip4)).toBe(0.5);
  });
});

describe('autoFitVoiceClips', () => {
  it('adjusts rate and clears overflow status for overflowing clips', () => {
    // Cue is 1000ms, speech duration is 1500ms, currently at 1.0x -> overflow
    const clip1 = makeClip('1', 0, 1000, 1500, 1.0, 'overflow');
    // Cue is 2000ms, speech duration is 1200ms, currently at 1.0x -> ready
    const clip2 = makeClip('2', 1500, 3500, 1200, 1.0, 'ready');

    const fitted = autoFitVoiceClips([clip1, clip2]);

    // Clip 1 should now have rate 1.5 and status 'ready'
    expect(fitted[0].rate).toBe(1.5);
    expect(fitted[0].status).toBe('ready');
    expect(fitted[0].duration_ms / fitted[0].rate).toBeLessThanOrEqual(1000 + 2);

    // Clip 2 should be untouched
    expect(fitted[1].rate).toBe(1.0);
    expect(fitted[1].status).toBe('ready');
  });
});

describe('rippleShiftVoiceClips', () => {
  it('shifts overlapping clips sequentially so each starts after previous finishes', () => {
    // Clip 1: 0 to 1000ms, duration 1500ms at 1.0x -> ends at 1500ms
    // Clip 2: 1000 to 2000ms, duration 1000ms at 1.0x -> starts at 1000ms, overlaps Clip 1!
    const clip1 = makeClip('1', 0, 1000, 1500, 1.0, 'overflow');
    const clip2 = makeClip('2', 1000, 2000, 1000, 1.0, 'ready');

    const shifted = rippleShiftVoiceClips([clip1, clip2], 60);

    expect(shifted[0].offset_ms).toBe(0);
    // Clip 1 ends at 1500ms. Clip 2 naturally starts at 1000ms.
    // To start at 1500 + 60 = 1560ms, offset should be 560ms!
    expect(shifted[1].offset_ms).toBe(560);
    expect(shifted[1].start_ms + shifted[1].offset_ms).toBe(1560);
  });
});

describe('countVoiceOverlaps and smartResolveVoiceOverlaps', () => {
  it('accurately counts overflows and overlaps, and smart resolve clears them', () => {
    // 3 consecutive overlapping clips
    const clip1 = makeClip('1', 0, 1000, 1400, 1.0, 'overflow');
    const clip2 = makeClip('2', 1000, 2000, 1400, 1.0, 'overflow');
    const clip3 = makeClip('3', 2000, 3000, 800, 1.0, 'ready');

    const beforeCounts = countVoiceOverlaps([clip1, clip2, clip3]);
    expect(beforeCounts.overflowCount).toBe(2);
    expect(beforeCounts.overlapCount).toBe(2);

    const resolved = smartResolveVoiceOverlaps([clip1, clip2, clip3]);
    const afterCounts = countVoiceOverlaps(resolved);
    expect(afterCounts.overflowCount).toBe(0);
    expect(afterCounts.overlapCount).toBe(0);
  });

  it('resetVoiceOffsets resets all offsets to 0', () => {
    const clip1 = { ...makeClip('1', 0, 1000, 500), offset_ms: 250 };
    const reset = resetVoiceOffsets([clip1]);
    expect(reset[0].offset_ms).toBe(0);
  });
});

