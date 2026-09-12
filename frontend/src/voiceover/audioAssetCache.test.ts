import { expect, it, vi } from 'vitest';
import { AudioAssetCache } from './audioAssetCache';

it('caps simultaneous loads even while obsolete requests ignore cancellation', async () => {
  const requests: {id:string; signal:AbortSignal; resolve:(blob:Blob)=>void}[] = [];
  const cache = new AudioAssetCache({concurrency:2, onReady:vi.fn(), onError:vi.fn(),
    load:(id,signal)=>new Promise(resolve=>requests.push({id,signal,resolve}))});
  cache.setWindow(['a','b','c'],new Set());
  expect(requests.map(r=>r.id)).toEqual(['a','b']);
  cache.setWindow(['x','y'],new Set());
  expect(requests.every(r=>r.signal.aborted)).toBe(true);
  expect(cache.inflight).toBe(2);
  requests[0].resolve(new Blob(['obsolete']));
  await vi.waitFor(()=>expect(requests.map(r=>r.id)).toEqual(['a','b','x']));
  expect(cache.entryCount).toBe(0);
  cache.dispose();
  requests.slice(1).forEach(r=>r.resolve(new Blob(['late'])));
  await vi.waitFor(()=>expect(cache.inflight).toBe(0));
  expect(cache.byteSize).toBe(0);
});

it('enforces bytes with pinned playback entries and does not refetch evicted preloads forever', async () => {
  const load = vi.fn(async () => new Blob(['1234']));
  const cache = new AudioAssetCache({load,maxBytes:8,onReady:vi.fn(),onError:vi.fn(),concurrency:1});
  cache.setWindow(['a','b'],new Set(['a']));
  await vi.waitFor(()=>expect(cache.entryCount).toBe(2));
  const pinned = cache.get('a');
  cache.setWindow(['a','c','d','e'],new Set(['a']));
  await vi.waitFor(()=>expect(cache.inflight).toBe(0));
  expect(cache.byteSize).toBe(8);
  expect(cache.get('a')).toBe(pinned);
  const calls = load.mock.calls.length;
  cache.setWindow(['a','c','d','e'],new Set(['a']));
  expect(load).toHaveBeenCalledTimes(calls);
  cache.dispose();
  expect(cache.byteSize).toBe(0);
});

it('rejects oversized assets once per window and continues useful prefetch', async () => {
  const errors = vi.fn();
  const cache = new AudioAssetCache({load:async id=>new Blob([id==='large'?'123456789':'ok']),maxBytes:8,onReady:vi.fn(),onError:errors});
  cache.setWindow(['large','small'],new Set());
  await vi.waitFor(()=>expect(cache.inflight).toBe(0));
  expect(errors).toHaveBeenCalledOnce();
  expect(cache.get('large')).toBeNull();
  expect(cache.get('small')).not.toBeNull();
  cache.setWindow(['large','small'],new Set());
  expect(errors).toHaveBeenCalledOnce();
  cache.dispose();
});
