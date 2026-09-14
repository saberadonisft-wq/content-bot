import { expect, it } from 'vitest';
import { PlaybackIndex } from './playbackIndex';
import type { VoiceClip } from './types';

const clip = (id: string, start: number, duration = 800): VoiceClip => ({ id, start_ms:start, end_ms:start+duration,
  offset_ms:0, duration_ms:duration, rate:1, gain:1, asset_id:id, status:'ready' } as VoiceClip);

it('blocks the entire conflicting region instead of silently losing speech', () => {
  const index = new PlaybackIndex([clip('long', 0, 5000), {...clip('short', 1000, 1000), offset_ms:100, rate:2}, {...clip('stale',1200),status:'stale'}]);
  expect(index.active(1099)).toBeNull();
  expect(index.active(1100)).toBeNull();
  expect(index.active(1600)).toBeNull();
  expect(index.conflictAt(1600)).toMatchObject({ start: 0, end: 5000 });
  expect(index.conflictAt(5000)).toBeNull();
  expect(index.active(-1)).toBeNull();
  expect(index.active(10_000)).toBeNull();
});

it('selects a bounded local window for large timelines and distant seeks', () => {
  const index = new PlaybackIndex(Array.from({length:2000},(_,i)=>clip(String(i),i*1000)));
  const ids = index.windowAssets(1_500_200);
  expect(ids.slice(0,2)).toEqual(['1500','1501']);
  expect(ids.length).toBeLessThanOrEqual(24);
  expect(ids.every(id=>Number(id)>=1495 && Number(id)<=1520)).toBe(true);
  expect(index.windowAssets(200).slice(0,2)).toEqual(['0','1']);
});

it('uses maximum prior ends when overlapping durations are not monotonic', () => {
  const index = new PlaybackIndex([clip('long',0,100_000),clip('short',1000)]);
  expect(index.windowAssets(10_000)).toEqual(['long']);
});
