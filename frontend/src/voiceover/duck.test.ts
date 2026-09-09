import { describe, expect, it } from 'vitest';
import { voiceDuckGain } from './duck';
import type { VoiceClip } from './types';

describe('timeline duck envelope', () => {
  const clip = { start_ms: 1000, offset_ms: 100, duration_ms: 1000, rate: 2, gain: 1, asset_id: 'audio' } as VoiceClip;
  it('uses audible position and rate, with attack and release', () => {
    for (const [ms, expected] of [[1099, 1], [1100, 1], [1110, 0.625], [1120, 0.25], [1600, 0.25], [1725, 0.625], [1850, 1]]) {
      expect(voiceDuckGain([clip], ms < 1100 ? 0 : 1, ms)).toBeCloseTo(expected, 5);
    }
  });
  it('does not duck a silent clip', () => {
    expect(voiceDuckGain([{ ...clip, gain: 0 }], 1, 1300)).toBe(1);
  });
  it('keeps transition durations in playback time when the video speeds up', () => {
    expect(voiceDuckGain([clip], 1, 1120, 2)).toBeCloseTo(.625);
    expect(voiceDuckGain([clip], 1, 1850, 2)).toBeCloseTo(.625);
    expect(voiceDuckGain([clip], 1, 2100, 2)).toBe(1);
  });
});
