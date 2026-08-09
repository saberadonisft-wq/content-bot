import { useEffect, useMemo, useSyncExternalStore } from "react";
import type { PlaybackClockStore } from "./playback-store";
import type { SubtitleIntervalIndex } from "./time";

const createActiveCueSource = (
  clock: PlaybackClockStore,
  index: SubtitleIntervalIndex,
) => {
  let activeCueId = index.findActive(clock.getSnapshot().currentMs)?.id ?? null;
  const listeners = new Set<() => void>();
  let unsubscribeClock: (() => void) | null = null;
  const update = (currentMs: number) => {
    const nextCueId = index.findActive(currentMs)?.id ?? null;
    if (nextCueId === activeCueId) return;
    activeCueId = nextCueId;
    listeners.forEach((listener) => listener());
  };
  return {
    getSnapshot: () => activeCueId,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      if (!unsubscribeClock) unsubscribeClock = clock.subscribeFrame(update);
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0 && unsubscribeClock) {
          unsubscribeClock();
          unsubscribeClock = null;
        }
      };
    },
    destroy: () => {
      if (unsubscribeClock) unsubscribeClock();
    },
  };
};

export const useActiveCueId = (
  clock: PlaybackClockStore,
  index: SubtitleIntervalIndex,
) => {
  const source = useMemo(() => createActiveCueSource(clock, index), [clock, index]);
  useEffect(() => source.destroy, [source]);
  return useSyncExternalStore(source.subscribe, source.getSnapshot, source.getSnapshot);
};
